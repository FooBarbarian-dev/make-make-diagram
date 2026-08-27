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
from tests.whatif_checks import check_event_expectations

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
        expr_in.append({"name": v["name"], "ast": ast, "env": v["env"],
                        "controlled": v.get("controlled")})

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


def test_scenario_vectors(run):
    # expectation checking is shared with the Python evaluator's suite —
    # see tests/whatif_checks.py
    expected = {v["name"]: v["expect"] for v in run["vectors"]["scenarios"]}
    failures: list[str] = []
    for result in run["results"]["scenarios"]:
        check_event_expectations(result["name"], result["got"],
                                 expected[result["name"]], failures)
    assert not failures, "\n" + "\n".join(failures)
