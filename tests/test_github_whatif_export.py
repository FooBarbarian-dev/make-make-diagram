"""The GitHub What-If tab's "Export scenario" YAML, round-tripped
semantically: the node-generated export must load through
pipeview.scenarios and map back (to_github_whatif_config) to a config that
EVALUATES identically to the original — the GitHub twin of
test_whatif_export.py.

Skips (with a notice) when node is not on PATH.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.github_parser import parse_github
from pipeview.parsers.github_whatif_eval import evaluate_event
from pipeview.scenarios import load_scenarios, to_github_whatif_config

TESTS = Path(__file__).parent
REPO = TESTS.parent
WHATIF_JS = REPO / "pipeview" / "render" / "templates" / "whatif_github.js"
RUNNER = TESTS / "run_whatif_export.js"
EXAMPLE = REPO / "examples" / "github-project"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — scenario export tests skipped"
)

# One config per tab affordance, defaulted knobs included the way
# wiConfigGH() emits them (every key present).
CONFIGS = {
    "push-main": {
        "scenario": "push_branch", "branch": "main", "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "newBranch": False,
        "target": "main", "draft": False, "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "push-with-open-pr": {
        "scenario": "push_branch", "branch": "feature/x", "openPR": True,
        "target": "main", "draft": True, "prAction": "labeled",
        "tag": "v1.0.0", "refKind": "branch", "newBranch": False,
        "commitMessage": "", "changedFiles": None, "overrides": {},
    },
    "new-branch-changed-list": {
        "scenario": "push_branch", "branch": "topic", "newBranch": True,
        "changedFiles": ["src/**", "weird name.txt"], "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "target": "main",
        "draft": False, "commitMessage": "", "overrides": {},
    },
    "tag-push": {
        "scenario": "push_tag", "tag": "v2.0.0", "branch": "main",
        "refKind": "tag", "openPR": False, "newBranch": False,
        "target": "main", "draft": False, "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "pr-full": {
        "scenario": "pr", "branch": "feature/x", "target": "dev",
        "draft": True, "prAction": "labeled", "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "newBranch": False,
        "commitMessage": "", "changedFiles": "all", "overrides": {},
    },
    "schedule": {
        "scenario": "schedule", "branch": "main", "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "newBranch": False,
        "target": "main", "draft": False, "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "dispatch-with-inputs": {
        "scenario": "workflow_dispatch", "dispatchWorkflow": "release.yml",
        "inputs": {"dry_run": "false"}, "branch": "main", "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "newBranch": False,
        "target": "main", "draft": False, "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "release-created": {
        "scenario": "release", "releaseAction": "created", "tag": "v3.0.0",
        "branch": "main", "refKind": "tag", "openPR": False,
        "newBranch": False, "target": "main", "draft": False,
        "commitMessage": "", "changedFiles": None, "overrides": {},
    },
    "all-changed-and-variables": {
        "scenario": "push_branch", "branch": "main", "changedFiles": "all",
        "overrides": {"vars.CANARY": "1", "EMPTY": ""}, "tag": "v1.0.0",
        "refKind": "branch", "openPR": False, "newBranch": False,
        "target": "main", "draft": False, "commitMessage": "",
    },
}


@pytest.fixture(scope="module")
def exports() -> dict[str, str]:
    cases = [{"name": name, "config": config}
             for name, config in CONFIGS.items()]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"whatifPath": str(WHATIF_JS), "cases": cases}, f)
        input_path = f.name
    proc = subprocess.run([node, str(RUNNER), input_path],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"export runner failed:\n{proc.stderr}"
    return {r["name"]: r["yaml"] for r in json.loads(proc.stdout)}


@pytest.fixture(scope="module")
def report() -> dict:
    return parse_github(str(EXAMPLE)).to_dict()


def _load_export(tmp_path, yaml_text: str):
    p = tmp_path / "exported.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    scenarios, diags = load_scenarios(str(p))
    assert not [d for d in diags if d.severity == "error"], \
        (yaml_text, [d.message for d in diags])
    assert len(scenarios) == 1, yaml_text
    return scenarios[0]


def test_exports_load_cleanly(exports, tmp_path):
    for name, yaml_text in exports.items():
        scenario = _load_export(tmp_path, yaml_text)
        assert re.fullmatch(r"[a-z0-9-]+", scenario.id), (name, scenario.id)


def test_round_trip_evaluates_identically(exports, report, tmp_path):
    failures = []
    for name, config in CONFIGS.items():
        scenario = _load_export(tmp_path, exports[name])
        original = json.loads(json.dumps(
            evaluate_event(report, config), sort_keys=True))
        rebuilt = json.loads(json.dumps(
            evaluate_event(report, to_github_whatif_config(scenario)),
            sort_keys=True))
        if original != rebuilt:
            failures.append(name)
    assert not failures, \
        f"exported scenarios evaluate differently after round trip: {failures}"


def test_export_omits_defaulted_noise(exports):
    text = exports["push-main"]
    for absent in ("tag:", "draft:", "changed_files:", "commit_message:",
                   "variables:", "open_pr:", "new_branch:", "ref_kind:",
                   "workflow:", "inputs:", "release_action:"):
        assert absent not in text, (absent, text)
    assert "event: push_branch" in text
    assert "branch: main" in text


def test_export_is_a_complete_file(exports):
    for name, text in exports.items():
        assert text.startswith("#"), name
        assert "version: 1" in text, name
        assert "scenarios:" in text, name
