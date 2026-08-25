"""Pins the What-If plain-text listing (textSummary) and the scenario delta
(diffEvents/textDiff) by running the SHIPPED templates/whatif.js under node,
exactly like the evaluator vector suite.

Skips (with a notice) when node is not on PATH.
"""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab

TESTS = Path(__file__).parent
FIXTURES = TESTS / "fixtures" / "gitlab"
WHATIF_JS = TESTS.parent / "pipeview" / "render" / "templates" / "whatif.js"
RUNNER = TESTS / "run_whatif_textdiff.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(
    node is None, reason="node is not installed — JS text/diff cases skipped"
)

PUSH_WITH_MR = {
    "scenario": "push_branch", "branch": "feature/widget",
    "openMR": True, "target": "main",
}
PUSH_NO_MR = {"scenario": "push_branch", "branch": "feature/widget"}
TAG_PUSH = {"scenario": "push_tag", "tag": "v2.0.0"}

CASES = [
    {"name": "dup_listing", "fixture": "whatif_dup", "configA": PUSH_WITH_MR},
    {"name": "dup_tag_listing", "fixture": "whatif_dup", "configA": TAG_PUSH},
    {"name": "dup_branch_vs_tag", "fixture": "whatif_dup",
     "configA": PUSH_WITH_MR, "configB": TAG_PUSH},
    {"name": "dup_mr_closed", "fixture": "whatif_dup",
     "configA": PUSH_WITH_MR, "configB": PUSH_NO_MR},
    {"name": "invalid_listing", "fixture": "whatif_invalid",
     "configA": {"scenario": "push_branch", "branch": "main"}},
    {"name": "child_listing", "fixture": "whatif_forward",
     "configA": {"scenario": "push_branch", "branch": "main"}},
    {"name": "matrix_listing", "fixture": "whatif_matrix",
     "configA": {"scenario": "push_branch", "branch": "main"}},
]


def _run_cases() -> dict:
    report_cache: dict[str, dict] = {}
    cases_in = []
    for c in CASES:
        fixture = c["fixture"]
        if fixture not in report_cache:
            report = parse_gitlab(str(FIXTURES / fixture / ".gitlab-ci.yml"))
            report_cache[fixture] = report.to_dict()
        entry = {"name": c["name"], "report": report_cache[fixture],
                 "configA": c["configA"]}
        if "configB" in c:
            entry["configB"] = c["configB"]
        cases_in.append(entry)

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"whatifPath": str(WHATIF_JS), "cases": cases_in}, f)
        input_path = f.name

    proc = subprocess.run(
        [node, str(RUNNER), input_path],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, f"textdiff runner failed:\n{proc.stderr}"
    results = json.loads(proc.stdout)
    return {c["name"]: c for c in results["cases"]}


@pytest.fixture(scope="module")
def run():
    return _run_cases()


class TestTextSummary:
    def test_header_describes_the_event(self, run):
        summary = run["dup_listing"]["summaryA"]
        assert summary.splitlines()[0] == (
            "what-if: Push to a branch — branch feature/widget — open MR → main"
        )

    def test_lists_only_jobs_that_would_run_per_pipeline(self, run):
        summary = run["dup_listing"]["summaryA"]
        assert "Branch pipeline (push on feature/widget)" in summary
        assert "Merge request pipeline (merge_request_event on feature/widget → main)" in summary
        branch_part = summary.split("Merge request pipeline")[0]
        mr_part = summary.split("Merge request pipeline")[1]
        # test_mr is MR-only; build_all is branch/tag-only (implicit only:)
        assert "test_mr" not in branch_part
        assert "build_all" in branch_part
        assert "build_all" not in mr_part
        assert "test_mr" in mr_part

    def test_job_lines_carry_stage_and_state(self, run):
        summary = run["dup_listing"]["summaryA"]
        line = next(ln for ln in summary.splitlines() if "build_all" in ln)
        assert "[build]" in line
        assert "runs" in line

    def test_duplicates_footer(self, run):
        assert "duplicate jobs (may run in more than one pipeline" in (
            run["dup_listing"]["summaryA"]
        )
        assert "lint_everything" in run["dup_listing"]["summaryA"].splitlines()[-1]

    def test_tag_event_has_no_mr_section(self, run):
        summary = run["dup_tag_listing"]["summaryA"]
        assert "Tag pipeline (push on v2.0.0)" in summary
        assert "Merge request pipeline" not in summary
        assert "test_mr" not in summary

    def test_invalid_config_says_so_instead_of_listing(self, run):
        summary = run["invalid_listing"]["summaryA"]
        assert "invalid configuration — GitLab refuses to create any pipeline" in summary
        assert "[build]" not in summary   # no job listing follows

    def test_child_pipelines_indent_under_their_parent(self, run):
        summary = run["child_listing"]["summaryA"]
        assert "child pipeline:" in summary
        # children list their own jobs, indented deeper than the parent's
        parent_line = next(
            ln for ln in summary.splitlines() if ln.startswith("Branch pipeline")
        )
        child_line = next(
            ln for ln in summary.splitlines() if "child pipeline:" in ln
        )
        assert len(child_line) - len(child_line.lstrip()) > (
            len(parent_line) - len(parent_line.lstrip())
        )

    def test_matrix_jobs_show_instance_counts(self, run):
        summary = run["matrix_listing"]["summaryA"]
        line = next(ln for ln in summary.splitlines() if "build" in ln and "[build]" in ln)
        assert "×2" in line


class TestDiffEvents:
    def test_branch_vs_tag_counts(self, run):
        case = run["dup_branch_vs_tag"]
        assert case["counts"] == {"added": 0, "removed": 1, "changed": 1, "same": 1}

    def test_branch_vs_tag_job_verdicts(self, run):
        deltas = run["dup_branch_vs_tag"]["deltas"]
        assert deltas["test_mr"] == "removed"        # MR pipeline gone on a tag
        assert deltas["build_all"] == "same"         # implicit only: covers tags
        # same state, but 2 pipelines -> 1: surfaced as changed
        assert deltas["lint_everything"] == "changed"

    def test_branch_vs_tag_pairs(self, run):
        pairs = {p["key"]: p for p in run["dup_branch_vs_tag"]["pairs"]}
        # non-MR top-level candidates match each other: branch vs tag
        assert pairs["main"]["a"] == "Branch pipeline"
        assert pairs["main"]["b"] == "Tag pipeline"
        assert pairs["main"]["deltas"] == {
            "build_all": "same", "lint_everything": "same",
        }
        # the MR pipeline only exists on the baseline side
        assert pairs["mr"]["b"] is None
        assert pairs["mr"]["deltas"] == {
            "test_mr": "removed", "lint_everything": "removed",
        }

    def test_closing_the_mr_dedups_the_catchall(self, run):
        case = run["dup_mr_closed"]
        deltas = case["deltas"]
        assert deltas["test_mr"] == "removed"
        assert deltas["lint_everything"] == "changed"   # ran in 2 pipelines, now 1
        assert deltas["build_all"] == "same"


class TestTextDiff:
    def test_symbols_and_labels(self, run):
        text = run["dup_branch_vs_tag"]["textDiff"]
        lines = text.splitlines()
        assert lines[0] == "what-if delta"
        assert lines[1].endswith("Push to a branch — branch feature/widget — open MR → main")
        assert lines[2].endswith("Push a new tag — tag v2.0.0")
        assert "0 added, 1 removed, 1 changed, 1 unchanged" in lines[3]
        assert any(ln.startswith("- test_mr") and "[Merge request pipeline]" in ln
                   for ln in lines)
        assert any(ln.startswith("~ lint_everything") and "2 pipelines → 1 pipeline" in ln
                   for ln in lines)
        assert any(ln.startswith("= build_all") for ln in lines)
