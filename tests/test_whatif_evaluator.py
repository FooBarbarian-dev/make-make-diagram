"""Runs the SHIPPED JS evaluator (templates/whatif.js) against the vector
suite under node. The vectors' expectations are hand-written from the GitLab
docs; the Python side only compiles expressions/fixtures — evaluation
semantics are exercised in the exact artifact users get.

Skips (with a notice) when node is not on PATH.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif import parse_expression

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "gitlab"
WHATIF_JS = TESTS.parent / "pipeview" / "render" / "templates" / "whatif.js"
RUNNER = TESTS / "run_whatif_vectors.js"
VECTORS = TESTS / "whatif_vectors.json"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — JS evaluator vectors skipped"
)


def _run_vectors() -> dict:
    vectors = json.loads(VECTORS.read_text())

    expr_in = []
    for v in vectors["expr"]:
        ast = v.get("ast")
        if ast is None:
            ast, _, _ = parse_expression(v["src"])
        expr_in.append({"name": v["name"], "ast": ast, "env": v["env"]})

    scenarios_in = []
    report_cache: dict[str, dict] = {}
    for s in vectors["scenarios"]:
        fixture = s["fixture"]
        if fixture not in report_cache:
            report = parse_gitlab(str(FIXTURES / fixture / ".gitlab-ci.yml"))
            report_cache[fixture] = report.to_dict()
        scenarios_in.append({
            "name": s["name"],
            "report": report_cache[fixture],
            "config": s["config"],
        })

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"whatifPath": str(WHATIF_JS), "expr": expr_in,
                   "scenarios": scenarios_in}, f)
        input_path = f.name

    proc = subprocess.run(
        [node, str(RUNNER), input_path],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"vector runner failed:\n{proc.stderr}"
    return {
        "vectors": vectors,
        "results": json.loads(proc.stdout),
    }


@pytest.fixture(scope="module")
def run():
    return _run_vectors()


def test_expression_vectors(run):
    expected = {v["name"]: v["expect"] for v in run["vectors"]["expr"]}
    failures = []
    for result in run["results"]["expr"]:
        want = expected[result["name"]]
        if result["got"] != want:
            failures.append(f"{result['name']}: expected {want!r}, got {result['got']!r}")
    assert not failures, "\n".join(failures)


def _candidate_by_id(got: dict, cand_id: str) -> dict:
    for c in got["candidates"]:
        if c["id"] == cand_id:
            return c
    raise AssertionError(
        f"candidate {cand_id!r} missing; have {[c['id'] for c in got['candidates']]}"
    )


def _check_candidate(name: str, cand: dict, expect: dict, failures: list[str]) -> None:
    if "created" in expect:
        want = expect["created"]
        got = "unknown" if cand["created"] is None else cand["created"]
        if got != want:
            failures.append(
                f"{name}/{cand['id']}: created expected {want!r}, got {got!r} "
                f"({cand.get('reason')})"
            )
    if "reasonContains" in expect:
        if expect["reasonContains"] not in (cand.get("reason") or ""):
            failures.append(
                f"{name}/{cand['id']}: reason {cand.get('reason')!r} does not "
                f"contain {expect['reasonContains']!r}"
            )
    for k, want_val in expect.get("workflowVariablesContain", {}).items():
        got_val = cand.get("workflowVariables", {}).get(k)
        if got_val != want_val:
            failures.append(
                f"{name}/{cand['id']}: workflowVariables[{k}] expected "
                f"{want_val!r}, got {got_val!r}"
            )
    if "childrenCount" in expect:
        n = len(cand.get("children", []))
        if n != expect["childrenCount"]:
            failures.append(
                f"{name}/{cand['id']}: expected {expect['childrenCount']} "
                f"children, got {n}"
            )
    for job_id, details in expect.get("jobsDetail", {}).items():
        job = cand["jobs"].get(job_id)
        if job is None:
            failures.append(f"{name}/{cand['id']}: job {job_id} not evaluated")
            continue
        for key, want_val in details.items():
            if job.get(key) != want_val:
                failures.append(
                    f"{name}/{cand['id']}/{job_id}: {key} expected {want_val!r}, "
                    f"got {job.get(key)!r}"
                )
    for job_id, want_state in expect.get("jobs", {}).items():
        job = cand["jobs"].get(job_id)
        if job is None:
            failures.append(f"{name}/{cand['id']}: job {job_id} not evaluated")
            continue
        if job["state"] != want_state:
            failures.append(
                f"{name}/{cand['id']}/{job_id}: expected {want_state!r}, "
                f"got {job['state']!r} (trace: {job.get('trace')})"
            )
    if "artifactErrorsAtLeast" in expect:
        n = len(cand["artifacts"]["errors"])
        if n < expect["artifactErrorsAtLeast"]:
            failures.append(
                f"{name}/{cand['id']}: expected >= "
                f"{expect['artifactErrorsAtLeast']} artifact errors, got {n}"
            )
    for needle in expect.get("errorsContain", []):
        blob = json.dumps(cand["artifacts"]["errors"])
        if needle not in blob:
            failures.append(f"{name}/{cand['id']}: no artifact error contains {needle!r}")
    for needle in expect.get("notesContain", []):
        blob = json.dumps(cand["artifacts"]["notes"])
        if needle not in blob:
            failures.append(f"{name}/{cand['id']}: no artifact note contains {needle!r}")
    for child_rel, child_expect in expect.get("children", {}).items():
        child = next((c for c in cand["children"] if c["childOf"] == child_rel), None)
        if child is None:
            failures.append(f"{name}/{cand['id']}: child pipeline {child_rel} not spawned")
            continue
        _check_candidate(f"{name}/{cand['id']}", child, child_expect, failures)


def test_scenario_vectors(run):
    expected = {v["name"]: v["expect"] for v in run["vectors"]["scenarios"]}
    failures: list[str] = []
    for result in run["results"]["scenarios"]:
        name = result["name"]
        got = result["got"]
        expect = expected[name]
        for cand_id, cand_expect in expect.get("candidates", {}).items():
            try:
                cand = _candidate_by_id(got, cand_id)
            except AssertionError as e:
                failures.append(f"{name}: {e}")
                continue
            _check_candidate(name, cand, cand_expect, failures)
        if "duplicates" in expect:
            got_dups = sorted(d["job"] for d in got["duplicates"])
            if got_dups != sorted(expect["duplicates"]):
                failures.append(
                    f"{name}: duplicates expected {expect['duplicates']}, got {got_dups}"
                )
        if "fatalAtLeast" in expect:
            n = len(got.get("fatal", []))
            if n < expect["fatalAtLeast"]:
                failures.append(
                    f"{name}: expected >= {expect['fatalAtLeast']} fatal entries, "
                    f"got {n}: {got.get('fatal')}"
                )
        for needle in expect.get("fatalContain", []):
            if needle not in json.dumps(got.get("fatal", [])):
                failures.append(f"{name}: no fatal entry contains {needle!r}")
    assert not failures, "\n" + "\n".join(failures)
