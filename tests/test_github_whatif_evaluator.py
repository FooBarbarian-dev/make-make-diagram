"""Runs the SHIPPED JS GitHub evaluator (templates/whatif_github.js) under
node against tests/github_whatif_vectors.json — the same vectors the Python
twin answers natively in test_github_whatif_eval_py.py. Expression ASTs are
compiled Python-side (parse_condition), exactly as report generation does.

Skips (with a notice) when node is not on PATH.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.github_parser import parse_github
from pipeview.parsers.github_whatif import parse_condition
from tests.whatif_checks import check_event_expectations

TESTS = Path(__file__).parent
REPO = TESTS.parent
FIXTURES = TESTS / "fixtures" / "github"
VECTORS = TESTS / "github_whatif_vectors.json"
RUNNER = TESTS / "run_github_whatif_vectors.js"
WHATIF_JS = REPO / "pipeview" / "render" / "templates" / "whatif_github.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — JS evaluator tests skipped"
)


def _run_vectors():
    vectors = json.loads(VECTORS.read_text())
    expr = []
    for v in vectors["expr"]:
        ast = v.get("ast")
        if ast is None:
            ast, _, _ = parse_condition(v["src"])
        expr.append({"name": v["name"], "ast": ast, "ctx": v.get("ctx")})
    scenarios = []
    report_cache: dict[str, dict] = {}
    for s in vectors["scenarios"]:
        fixture = s["fixture"]
        if fixture not in report_cache:
            report_cache[fixture] = parse_github(
                str(FIXTURES / fixture)).to_dict()
        scenarios.append({"name": s["name"],
                          "report": report_cache[fixture],
                          "config": s["config"]})
    with tempfile.NamedTemporaryFile("w", suffix=".json",
                                     delete=False) as f:
        json.dump({"whatifPath": str(WHATIF_JS), "expr": expr,
                   "scenarios": scenarios}, f)
        payload = f.name
    proc = subprocess.run([node, str(RUNNER), payload],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"vector runner failed:\n{proc.stderr}"
    return vectors, json.loads(proc.stdout)


def test_js_evaluator_answers_the_vectors():
    vectors, results = _run_vectors()
    failures = []
    got_expr = {r["name"]: r["got"] for r in results["expr"]}
    for v in vectors["expr"]:
        got = got_expr.get(v["name"])
        if got != v["expect"]:
            failures.append(
                f"expr {v['name']}: expected {v['expect']!r}, got {got!r}")
    got_scen = {r["name"]: r["got"] for r in results["scenarios"]}
    for s in vectors["scenarios"]:
        got = got_scen.get(s["name"])
        assert got is not None, f"{s['name']}: JS returned no result"
        check_event_expectations(s["name"], got, s["expect"], failures)
    assert not failures, "\n".join(failures)
