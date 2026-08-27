"""Shared expectation checkers for the what-if vector suite.

tests/whatif_vectors.json is the parity contract between the two
interpreters of the compiled what-if program — templates/whatif.js (run
under node by test_whatif_evaluator.py) and
pipeview/parsers/gitlab_whatif_eval.py (run natively by
test_whatif_eval_py.py). Both test files funnel their results through
these checkers, so an expectation can never mean two different things.
"""

import json


def candidate_by_id(got: dict, cand_id: str) -> dict:
    for c in got["candidates"]:
        if c["id"] == cand_id:
            return c
    raise AssertionError(
        f"candidate {cand_id!r} missing; have {[c['id'] for c in got['candidates']]}"
    )


def check_candidate(name: str, cand: dict, expect: dict, failures: list[str]) -> None:
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
    for job_id, inst_map in expect.get("matrixStates", {}).items():
        job = cand["jobs"].get(job_id)
        got_map = {m["name"]: m["state"] for m in (job or {}).get("matrix", [])}
        for iname, istate in inst_map.items():
            if got_map.get(iname) != istate:
                failures.append(
                    f"{name}/{cand['id']}/{job_id}: instance {iname!r} expected "
                    f"{istate!r}, got {got_map.get(iname)!r} (all: {got_map})"
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
        check_candidate(f"{name}/{cand['id']}", child, child_expect, failures)


def check_event_expectations(name: str, got: dict, expect: dict,
                             failures: list[str]) -> None:
    """The whole per-vector expectation: candidates, duplicates, fatal."""
    for cand_id, cand_expect in expect.get("candidates", {}).items():
        try:
            cand = candidate_by_id(got, cand_id)
        except AssertionError as e:
            failures.append(f"{name}: {e}")
            continue
        check_candidate(name, cand, cand_expect, failures)
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
