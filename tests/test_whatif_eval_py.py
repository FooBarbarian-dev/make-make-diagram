"""Runs the PYTHON what-if evaluator (parsers/gitlab_whatif_eval.py) against
the same vector suite the shipped JS evaluator answers to — natively, no
node required. tests/whatif_vectors.json is the parity contract: a vector
added here is exercised in whatif.js by test_whatif_evaluator.py too.
"""

import json
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif import WHATIF_VERSION, parse_expression
from pipeview.parsers.gitlab_whatif_eval import (
    WhatifVersionError,
    eval_expr,
    evaluate_event,
)
from tests.whatif_checks import check_event_expectations

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "gitlab"
VECTORS = TESTS / "whatif_vectors.json"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text())


def test_expression_vectors(vectors):
    failures = []
    for v in vectors["expr"]:
        ast = v.get("ast")
        if ast is None:
            ast, _, _ = parse_expression(v["src"])
        got = eval_expr(ast, v["env"], [], v.get("controlled"))
        got = "unknown" if got is None else got
        if got != v["expect"]:
            failures.append(f"{v['name']}: expected {v['expect']!r}, got {got!r}")
    assert not failures, "\n".join(failures)


def test_scenario_vectors(vectors):
    report_cache: dict[str, dict] = {}
    failures: list[str] = []
    for s in vectors["scenarios"]:
        fixture = s["fixture"]
        if fixture not in report_cache:
            report = parse_gitlab(str(FIXTURES / fixture / ".gitlab-ci.yml"))
            report_cache[fixture] = report.to_dict()
        got = evaluate_event(report_cache[fixture], s["config"])
        assert got is not None, f"{s['name']}: no what-if program in fixture"
        check_event_expectations(s["name"], got, s["expect"], failures)
    assert not failures, "\n" + "\n".join(failures)


def test_results_are_json_serializable(vectors):
    """The renderer and the vector harness both treat results as plain data —
    no sentinel may leak out of the evaluator."""
    report = parse_gitlab(str(FIXTURES / "whatif_features" / ".gitlab-ci.yml"))
    got = evaluate_event(report.to_dict(), {"scenario": "push_branch", "branch": "main"})
    json.dumps(got)


def test_version_guard():
    report = {"annotations": {"whatif": {"version": WHATIF_VERSION + 1}},
              "nodes": []}
    with pytest.raises(WhatifVersionError):
        evaluate_event(report, {"scenario": "push_branch"})


def test_no_whatif_annotation_returns_none():
    assert evaluate_event({"annotations": {}, "nodes": []},
                          {"scenario": "push_branch"}) is None
