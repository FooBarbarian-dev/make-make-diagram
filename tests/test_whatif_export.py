"""The What-If tab's "Export scenario" YAML, round-tripped semantically:
the node-generated export must load through pipeview.scenarios and map
back to a config that EVALUATES identically to the original — key-by-key
equality would drown in the tab's defaulted knobs; evaluation equality is
what lossless means.

Skips (with a notice) when node is not on PATH.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif_eval import evaluate_event
from pipeview.scenarios import load_scenarios, to_whatif_config

TESTS = Path(__file__).parent
REPO = TESTS.parent
WHATIF_JS = REPO / "pipeview" / "render" / "templates" / "whatif.js"
RUNNER = TESTS / "run_whatif_export.js"
EXAMPLE = REPO / "examples" / "gitlab-whatif-project" / ".gitlab-ci.yml"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — scenario export tests skipped"
)

# One config per tab affordance, defaulted knobs included the way
# wiConfig() emits them (every key present).
CONFIGS = {
    "push-main": {
        "scenario": "push_branch", "branch": "main", "tag": "v1.0.0",
        "refKind": "branch", "tagProtected": False, "openMR": False,
        "newBranch": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "", "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "push-with-open-mr": {
        "scenario": "push_branch", "branch": "feature/x", "openMR": True,
        "target": "main", "draft": True, "mrFlavor": "detached",
        "mrLabels": "", "commitMessage": "", "changedFiles": None,
        "refKind": "branch", "tagProtected": False, "newBranch": False,
        "tag": "v1.0.0", "overrides": {},
    },
    "new-branch-changed-list": {
        "scenario": "push_branch", "branch": "topic", "newBranch": True,
        "changedFiles": ["src/**/*.py", "weird name.txt"],
        "refKind": "branch", "openMR": False, "tag": "v1.0.0",
        "tagProtected": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "", "commitMessage": "",
        "overrides": {},
    },
    "protected-tag": {
        "scenario": "push_tag", "tag": "v2.0.0", "tagProtected": True,
        "branch": "main", "refKind": "tag", "openMR": False,
        "newBranch": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "", "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "mr-full": {
        "scenario": "mr", "branch": "feature/x", "target": "dev",
        "draft": True, "mrFlavor": "merged_result",
        "mrLabels": "urgent,backend", "openMR": True, "newBranch": False,
        "tag": "v1.0.0", "refKind": "branch", "tagProtected": False,
        "commitMessage": "", "changedFiles": None, "overrides": {},
    },
    "schedule-with-open-mr": {
        "scenario": "schedule", "branch": "main", "openMR": True,
        "refKind": "branch", "tag": "v1.0.0", "tagProtected": False,
        "newBranch": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "", "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "api-on-tag": {
        "scenario": "api", "refKind": "tag", "tag": "v1.0.0",
        "branch": "main", "openMR": False, "newBranch": False,
        "tagProtected": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "", "commitMessage": "",
        "changedFiles": None, "overrides": {},
    },
    "all-changed-and-variables": {
        "scenario": "push_branch", "branch": "main", "changedFiles": "all",
        "overrides": {"DEPLOY": "1", "EMPTY": ""},
        "refKind": "branch", "openMR": False, "newBranch": False,
        "tag": "v1.0.0", "tagProtected": False, "target": "",
        "draft": False, "mrFlavor": "detached", "mrLabels": "",
        "commitMessage": "",
    },
    "skip-ci-commit-message": {
        "scenario": "push_branch", "branch": "main",
        "commitMessage": "chore: bump\n\n[skip ci]",
        "changedFiles": [], "overrides": {}, "refKind": "branch",
        "openMR": False, "newBranch": False, "tag": "v1.0.0",
        "tagProtected": False, "target": "", "draft": False,
        "mrFlavor": "detached", "mrLabels": "",
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
    return parse_gitlab(str(EXAMPLE)).to_dict()


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
            evaluate_event(report, to_whatif_config(scenario)), sort_keys=True))
        if original != rebuilt:
            failures.append(name)
    assert not failures, \
        f"exported scenarios evaluate differently after round trip: {failures}"


def test_export_omits_defaulted_noise(exports):
    text = exports["push-main"]
    for absent in ("tag:", "target:", "draft:", "mr_flavor:", "mr_labels:",
                   "changed_files:", "commit_message:", "variables:",
                   "open_mr:", "new_branch:", "ref_kind:"):
        assert absent not in text, (absent, text)
    assert "event: push_branch" in text
    assert "branch: main" in text


def test_export_is_a_complete_file(exports):
    for name, text in exports.items():
        assert text.startswith("#"), name
        assert "version: 1" in text, name
        assert "scenarios:" in text, name
