"""Full-output lockstep proof: the Python evaluator and the shipped JS
evaluator must produce deep-equal JSON for the same (report, config), over
every gitlab fixture and example project and a config matrix covering all
seven scenarios. The vector suite pins agreed expectations; this test pins
the two interpreters to EACH OTHER, so a semantics change in one without
the other fails loudly whatever the vectors happen to cover.

Skips (with a notice) when node is not on PATH.
"""

import difflib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif_eval import evaluate_event

TESTS = Path(__file__).parent
REPO = TESTS.parent
FIXTURES = TESTS / "fixtures" / "gitlab"
EXAMPLES = [REPO / "examples" / "gitlab-project",
            REPO / "examples" / "gitlab-whatif-project"]
WHATIF_JS = REPO / "pipeview" / "render" / "templates" / "whatif.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — JS/Python parity sweep skipped"
)

CONFIGS = [
    {"scenario": "push_branch", "branch": "main"},
    {"scenario": "push_branch", "branch": "feature/x", "openMR": True,
     "target": "main", "draft": True},
    {"scenario": "push_branch", "branch": "topic", "newBranch": True},
    {"scenario": "push_branch", "branch": "main",
     "changedFiles": ["src/app.py", "docs/readme.md"]},
    {"scenario": "push_branch", "branch": "main", "changedFiles": "all",
     "overrides": {"DEPLOY": "1"}},
    {"scenario": "push_tag", "tag": "v2.0.0", "tagProtected": True},
    {"scenario": "push_tag", "tag": "nightly-build"},
    {"scenario": "mr", "branch": "feature/x", "target": "main",
     "mrFlavor": "merged_result", "mrLabels": "urgent,backend"},
    {"scenario": "mr", "branch": "feature/x", "target": "dev", "draft": True},
    {"scenario": "schedule"},
    {"scenario": "schedule", "refKind": "tag", "tag": "v1.0.0"},
    {"scenario": "web", "branch": "dev"},
    {"scenario": "api"},
    {"scenario": "trigger", "branch": "main"},
]

# One inline runner instead of a checked-in .js file: it has no logic of its
# own — parse input, call evaluateEvent, print JSON.
_RUNNER = """
'use strict';
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const W = require(input.whatifPath);
const out = [];
for (const c of input.cases) {
  out.push({ name: c.name, got: W.evaluateEvent(c.report, c.config) });
}
process.stdout.write(JSON.stringify(out));
"""


def _cases() -> list[dict]:
    cases = []
    roots = sorted(p for p in FIXTURES.iterdir()
                   if (p / ".gitlab-ci.yml").is_file())
    roots += EXAMPLES
    for root in roots:
        report = parse_gitlab(str(root / ".gitlab-ci.yml")).to_dict()
        if not (report.get("annotations") or {}).get("whatif"):
            continue
        for i, config in enumerate(CONFIGS):
            cases.append({"name": f"{root.name}#{i}", "report": report,
                          "config": config})
    return cases


def test_python_and_js_evaluators_agree_exactly():
    cases = _cases()
    assert len(cases) > 100  # the sweep is meant to be broad

    with tempfile.TemporaryDirectory() as td:
        runner = Path(td) / "runner.js"
        runner.write_text(_RUNNER, encoding="utf-8")
        payload = Path(td) / "input.json"
        payload.write_text(
            json.dumps({"whatifPath": str(WHATIF_JS), "cases": cases}),
            encoding="utf-8")
        proc = subprocess.run([node, str(runner), str(payload)],
                              capture_output=True, text=True, timeout=300)
        assert proc.returncode == 0, f"parity runner failed:\n{proc.stderr}"
        js_results = {r["name"]: r["got"] for r in json.loads(proc.stdout)}

    failures = []
    for case in cases:
        py = evaluate_event(case["report"], case["config"])
        # round-trip through JSON so both sides are plain data
        py_n = json.loads(json.dumps(py, sort_keys=True))
        js_n = json.loads(json.dumps(js_results[case["name"]], sort_keys=True))
        if py_n != js_n:
            a = json.dumps(js_n, sort_keys=True, indent=1).splitlines()
            b = json.dumps(py_n, sort_keys=True, indent=1).splitlines()
            diff = "\n".join(list(difflib.unified_diff(
                a, b, "js", "py", lineterm=""))[:30])
            failures.append(f"{case['name']}:\n{diff}")
        if len(failures) >= 3:
            break
    assert not failures, "\n\n".join(failures)
