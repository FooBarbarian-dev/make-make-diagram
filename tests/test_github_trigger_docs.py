"""Trigger docs for GitHub Actions reports: provider dispatch, wording,
cross-provider scenario skipping — the GitHub side of test_trigger_docs."""

from pathlib import Path

from pipeview.parsers.github_parser import parse_github
from pipeview.render.trigger_docs import INDEX_NAME, generate_trigger_docs
from pipeview.scenarios import Scenario, to_github_whatif_config

FIXTURES = Path(__file__).parent / "fixtures" / "github"

PROVENANCE = {"project": "demo", "ref": "main", "commit": "", "version": "0"}


def _scenario(**kwargs) -> Scenario:
    base = dict(id="s", event="push_branch", title="S", intro="", config={})
    base.update(kwargs)
    return Scenario(**base)


def _docs(fixture: str, scenarios, skipped=()):
    report = parse_github(str(FIXTURES / fixture)).to_dict()
    return generate_trigger_docs(report, scenarios, list(skipped),
                                 PROVENANCE, "pipeview …")


class TestGithubDocs:
    def test_push_doc_renders_runs_and_workflow_column(self):
        files = _docs("whatif_features", [_scenario(
            id="push-main", title="Push to main",
            config={"branch": "main", "changed_files": "all"})])
        doc = files["push-main.md"]
        assert "| Job | Workflow | Verdict | Why |" in doc
        assert "`build` | pipeline.yml | runs" in doc
        assert "*depends*" in doc            # vars.CANARY_ENABLED unknown
        assert "Jobs not in this run" in doc

    def test_needs_edges_resolve_through_workflow_namespace(self):
        files = _docs("whatif_features", [_scenario(
            id="push-main", config={"branch": "main",
                                    "changed_files": "all"})])
        assert "j_pipeline_yml__build --> j_pipeline_yml__test" \
            in files["push-main.md"]

    def test_workflow_run_cascade_noted(self):
        files = _docs("whatif_features", [_scenario(
            id="push-main", config={"branch": "main",
                                    "changed_files": "all"})])
        assert "`publish.yml` starts (`workflow_run`" in files["push-main.md"]

    def test_reusable_call_is_a_boundary(self):
        files = _docs("reusable", [_scenario(
            id="push-main", config={"branch": "main"})])
        assert "reusable workflow `deploy.yml`" in files["push-main.md"]

    def test_gitlab_only_scenario_skipped_with_note(self):
        files = _docs("minimal", [
            _scenario(id="push-main",
                      config={"branch": "main", "changed_files": "all"}),
            _scenario(id="mr-x", event="mr", config={"branch": "b"}),
        ])
        assert "mr-x.md" not in files
        index = files[INDEX_NAME]
        assert "event `mr` applies to GitLab CI configurations" in index

    def test_dispatch_scenario_with_inputs(self):
        files = _docs("whatif_features", [_scenario(
            id="dispatch", event="workflow_dispatch",
            config={"workflow": "pipeline.yml",
                    "inputs": {"environment": "production"}})])
        doc = files["dispatch.md"]
        assert "manual dispatch of `pipeline.yml`" in doc
        assert "`dispatch_deploy` | pipeline.yml | runs" in doc

    def test_duplicate_wording_uses_workflow_runs(self):
        files = _docs("whatif_dup", [_scenario(
            id="push-pr",
            config={"branch": "feature/x", "open_pr": {"target": "main"}})])
        doc = files["push-pr.md"]
        assert "in more than one of these workflow runs" in doc

    def test_gitlab_report_still_skips_github_events(self):
        from pipeview.parsers.gitlab_parser import parse_gitlab
        gl = parse_gitlab(str(Path(__file__).parent / "fixtures" / "gitlab"
                              / "minimal" / ".gitlab-ci.yml")).to_dict()
        files = generate_trigger_docs(gl, [
            _scenario(id="rel", event="release", config={"tag": "v1"}),
            _scenario(id="push", config={"branch": "main"}),
        ], [], PROVENANCE, "cmd")
        assert "rel.md" not in files
        assert "push.md" in files
        assert "event `release` applies to GitHub Actions" in files[INDEX_NAME]


class TestGithubScenarioConfig:
    def test_open_pr_maps_to_camel_case(self):
        s = _scenario(config={"branch": "b",
                              "open_pr": {"target": "main", "draft": True,
                                          "action": "labeled"}})
        cfg = to_github_whatif_config(s)
        assert cfg["openPR"] is True
        assert cfg["target"] == "main"
        assert cfg["draft"] is True
        assert cfg["prAction"] == "labeled"

    def test_open_mr_is_honored_as_open_pr(self):
        # a shared scenario file can say open_mr and mean "open PR" on GitHub
        s = _scenario(config={"branch": "b", "open_mr": {"target": "dev"}})
        cfg = to_github_whatif_config(s)
        assert cfg["openPR"] is True
        assert cfg["target"] == "dev"

    def test_dispatch_and_release_keys(self):
        s = _scenario(event="workflow_dispatch",
                      config={"workflow": "ci.yml",
                              "inputs": {"suite": "quick"}})
        cfg = to_github_whatif_config(s)
        assert cfg["dispatchWorkflow"] == "ci.yml"
        assert cfg["inputs"] == {"suite": "quick"}
        s2 = _scenario(event="release",
                       config={"tag": "v2", "release_action": "created"})
        cfg2 = to_github_whatif_config(s2)
        assert cfg2["releaseAction"] == "created"
        assert cfg2["tag"] == "v2"


class TestLoaderNewEvents:
    def test_new_events_load(self, tmp_path):
        from pipeview.scenarios import load_scenarios
        f = tmp_path / "s.yaml"
        f.write_text(
            "version: 1\n"
            "scenarios:\n"
            "  - id: a\n    event: pr\n    branch: b\n    target: main\n"
            "    pr_action: labeled\n"
            "  - id: b\n    event: workflow_dispatch\n    workflow: ci.yml\n"
            "    inputs: {x: '1'}\n"
            "  - id: c\n    event: release\n    tag: v1\n"
            "  - id: d\n    event: push_branch\n    branch: main\n"
            "    open_pr: {target: dev}\n")
        scenarios, diags = load_scenarios(str(f))
        assert [s.id for s in scenarios] == ["a", "b", "c", "d"]
        assert not [d for d in diags if d.severity == "error"]

    def test_open_pr_bad_key_fails_scenario(self, tmp_path):
        from pipeview.scenarios import load_scenarios
        f = tmp_path / "s.yaml"
        f.write_text(
            "version: 1\n"
            "scenarios:\n"
            "  - id: a\n    event: push_branch\n"
            "    open_pr: {nope: 1}\n")
        scenarios, diags = load_scenarios(str(f))
        assert scenarios == []
        assert any("open_pr" in d.message for d in diags)

    def test_github_predefined_shadow_warns(self, tmp_path):
        from pipeview.scenarios import load_scenarios
        f = tmp_path / "s.yaml"
        f.write_text(
            "version: 1\n"
            "scenarios:\n"
            "  - id: a\n    event: push_branch\n"
            "    variables: {GITHUB_REF: refs/heads/x}\n")
        _, diags = load_scenarios(str(f))
        assert any("GitHub predefined" in d.message for d in diags)
