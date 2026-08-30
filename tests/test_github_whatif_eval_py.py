"""Runs the PYTHON GitHub what-if evaluator (parsers/github_whatif_eval.py)
against the same vector suite the shipped JS evaluator answers to —
natively, no node required. tests/github_whatif_vectors.json is the parity
contract: a vector added here is exercised in whatif_github.js by
test_github_whatif_evaluator.py too.
"""

import json
from pathlib import Path

import pytest

from pipeview.parsers.github_parser import parse_github
from pipeview.parsers.github_whatif import WHATIF_VERSION, parse_condition
from pipeview.parsers.github_whatif_eval import (
    UNKNOWN,
    WhatifVersionError,
    eval_condition,
    evaluate_event,
)
from tests.whatif_checks import check_event_expectations

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "github"
VECTORS = TESTS / "github_whatif_vectors.json"


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS.read_text())


def _ctx(v: dict) -> dict:
    ctx = dict(v.get("ctx") or {})
    ctx.setdefault("contexts", {})
    return ctx


def test_expression_vectors(vectors):
    failures = []
    for v in vectors["expr"]:
        ast = v.get("ast")
        if ast is None:
            ast, _, _ = parse_condition(v["src"])
        got = eval_condition(ast, _ctx(v), [])
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
            report = parse_github(str(FIXTURES / fixture))
            report_cache[fixture] = report.to_dict()
        got = evaluate_event(report_cache[fixture], s["config"])
        assert got is not None, f"{s['name']}: no what-if program in fixture"
        check_event_expectations(s["name"], got, s["expect"], failures)
    assert not failures, "\n" + "\n".join(failures)


def test_results_are_json_serializable(vectors):
    """The renderer and the vector harness both treat results as plain data —
    the UNKNOWN sentinel may never leak out of the evaluator."""
    report = parse_github(str(FIXTURES / "whatif_features")).to_dict()
    for config in ({"scenario": "push_branch", "branch": "main"},
                   {"scenario": "workflow_dispatch"},
                   {"scenario": "release"}):
        got = evaluate_event(report, config)
        blob = json.dumps(got)   # raises on the sentinel
        assert "unknown\": {}" not in blob
        assert UNKNOWN is not None  # the sentinel exists and stays internal


def test_version_guard():
    report = parse_github(str(FIXTURES / "minimal")).to_dict()
    report["annotations"]["whatif"]["version"] = WHATIF_VERSION + 1
    with pytest.raises(WhatifVersionError):
        evaluate_event(report, {"scenario": "push_branch", "branch": "main"})


def test_gitlab_report_returns_none():
    """The GitHub evaluator refuses non-GitHub programs — the provider
    discriminator keeps the two data-shaped seams apart."""
    gitlab = TESTS / "fixtures" / "gitlab" / "minimal" / ".gitlab-ci.yml"
    from pipeview.parsers.gitlab_parser import parse_gitlab
    report = parse_gitlab(str(gitlab)).to_dict()
    assert evaluate_event(report, {"scenario": "push_branch"}) is None


def test_github_report_refused_by_gitlab_evaluator():
    """...and the GitLab evaluator refuses GitHub programs."""
    from pipeview.parsers.gitlab_whatif_eval import (
        evaluate_event as gl_evaluate,
    )
    report = parse_github(str(FIXTURES / "minimal")).to_dict()
    assert gl_evaluate(report, {"scenario": "push_branch"}) is None
