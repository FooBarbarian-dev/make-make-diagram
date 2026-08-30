"""GitHub Actions parser: model shape, edges, diagnostics, honesty rules."""

from pathlib import Path

from pipeview.parsers.github_parser import parse_github
from pipeview.parsers.github_predefined import (
    CONTEXT_FIELD_DOCS,
    PREDEFINED_VAR_DOCS,
)

FIXTURES = Path(__file__).parent / "fixtures" / "github"


class TestMinimal:
    def test_format(self):
        r = parse_github(str(FIXTURES / "minimal"))
        assert r.format == "github_actions"

    def test_jobs_parsed_and_namespaced(self):
        r = parse_github(str(FIXTURES / "minimal"))
        ids = {n.id for n in r.nodes if n.kind == "job"}
        assert ids == {"ci.yml::build", "ci.yml::test", "ci.yml::deploy"}
        build = r.node_by_id("ci.yml::build")
        assert build.name == "build"

    def test_workflow_grouping_annotation(self):
        r = parse_github(str(FIXTURES / "minimal"))
        for n in r.nodes:
            if n.kind == "job":
                assert n.annotations["child_pipeline"] == "ci.yml"

    def test_needs_edges(self):
        r = parse_github(str(FIXTURES / "minimal"))
        needs = {(e.src, e.dst) for e in r.edges if e.kind == "needs"}
        assert ("ci.yml::test", "ci.yml::build") in needs
        assert ("ci.yml::deploy", "ci.yml::build") in needs
        assert ("ci.yml::deploy", "ci.yml::test") in needs

    def test_no_stage_nodes(self):
        # GitHub Actions has no stages — the needs DAG is the ordering.
        r = parse_github(str(FIXTURES / "minimal"))
        assert not [n for n in r.nodes if n.kind == "stage"]

    def test_docstrings(self):
        r = parse_github(str(FIXTURES / "minimal"))
        assert r.node_by_id("ci.yml::build").doc == "Build the application"
        assert r.node_by_id("ci.yml::test").doc == "Run the test suite"

    def test_recipes(self):
        r = parse_github(str(FIXTURES / "minimal"))
        build = r.node_by_id("ci.yml::build")
        assert "[uses] actions/checkout@v4" in build.recipe
        assert any("make build" in line for line in build.recipe)
        assert any(line.startswith("[step] Compile") for line in build.recipe)

    def test_runner_annotations(self):
        r = parse_github(str(FIXTURES / "minimal"))
        assert r.node_by_id("ci.yml::build").annotations["tags"] == \
            ["ubuntu-latest"]
        assert r.node_by_id("ci.yml::deploy").annotations["tags"] == \
            ["self-hosted", "linux"]

    def test_environment_and_gates(self):
        r = parse_github(str(FIXTURES / "minimal"))
        deploy = r.node_by_id("ci.yml::deploy")
        assert deploy.annotations["environment"] == "production"
        # approval gates live in repo settings — noted, never guessed
        assert "manual approval" in deploy.annotations["environment_note"]
        assert "allow_failure" in deploy.flags
        assert deploy.annotations["timeout"] == "30 minutes"

    def test_env_variables_with_scopes(self):
        r = parse_github(str(FIXTURES / "minimal"))
        by_name = {v.name: v for v in r.variables}
        app = by_name["APP_NAME"]
        assert app.events[0].operator == "workflow"
        assert app.events[0].scope == "global"
        target = by_name["BUILD_TARGET"]
        assert target.events[0].operator == "job"
        assert target.events[0].scope == "ci.yml::build"

    def test_used_by(self):
        r = parse_github(str(FIXTURES / "minimal"))
        by_name = {v.name: v for v in r.variables}
        assert "ci.yml::build" in by_name["APP_NAME"].used_by

    def test_files(self):
        r = parse_github(str(FIXTURES / "minimal"))
        assert [(f.path, f.kind, f.status) for f in r.files] == \
            [("ci.yml", "github_yaml", "ok")]

    def test_clean_fixture_has_no_diagnostics(self):
        r = parse_github(str(FIXTURES / "minimal"))
        assert r.diagnostics == []

    def test_predefined_docs_embedded(self):
        r = parse_github(str(FIXTURES / "minimal"))
        docs = r.annotations["predefined_var_docs"]
        for name in PREDEFINED_VAR_DOCS:
            assert name in docs
        for name in CONTEXT_FIELD_DOCS:
            assert name in docs

    def test_single_file_root(self):
        r = parse_github(
            str(FIXTURES / "minimal" / ".github" / "workflows" / "ci.yml"))
        assert {n.id for n in r.nodes if n.kind == "job"} == \
            {"ci.yml::build", "ci.yml::test", "ci.yml::deploy"}


class TestOnTriggers:
    def test_push_filters_normalized(self):
        r = parse_github(str(FIXTURES / "minimal"))
        on = r.annotations["whatif"]["workflows"]["ci.yml"]["on"]
        assert on["push"]["branches"] == ["main", "releases/**"]
        assert on["push"]["paths"] == ["src/**", "!src/docs/**"]
        assert on["pull_request"] == {}

    def test_workflow_summary_annotation(self):
        r = parse_github(str(FIXTURES / "minimal"))
        summaries = {w["file"]: w for w in r.annotations["workflows"]}
        assert summaries["ci.yml"]["name"] == "CI"
        assert not summaries["ci.yml"]["reusable"]
        assert any(t.startswith("push") for t in summaries["ci.yml"]["triggers"])

    def test_schedule_and_dispatch(self):
        r = parse_github(str(FIXTURES / "whatif_dup"))
        on = r.annotations["whatif"]["workflows"]["nightly.yml"]["on"]
        assert on["schedule"]["crons"] == ["0 4 * * *"]
        assert on["workflow_dispatch"]["inputs"]["suite"]["default"] == "full"
        assert on["workflow_dispatch"]["inputs"]["suite"]["options"] == \
            ["full", "quick"]

    def test_dispatch_inputs_become_variables(self):
        r = parse_github(str(FIXTURES / "whatif_dup"))
        by_name = {v.name: v for v in r.variables}
        suite = by_name["inputs.suite"]
        assert suite.events[0].operator == "input"
        assert suite.events[0].raw_value == "full"


class TestReusableWorkflows:
    def test_local_reusable_parsed_and_linked(self):
        r = parse_github(str(FIXTURES / "reusable"))
        assert r.node_by_id("deploy.yml::push_release") is not None
        invokes = {(e.src, e.dst) for e in r.edges if e.kind == "invokes"}
        assert ("ci.yml::deploy", "deploy.yml") in invokes

    def test_reusable_marked(self):
        r = parse_github(str(FIXTURES / "reusable"))
        wf = r.annotations["whatif"]["workflows"]["deploy.yml"]
        assert wf["reusable"] is True
        job = r.node_by_id("deploy.yml::push_release")
        assert job.annotations["reusable_workflow"] is True

    def test_local_uses_info(self):
        r = parse_github(str(FIXTURES / "reusable"))
        deploy = r.node_by_id("ci.yml::deploy")
        info = deploy.annotations["uses_info"]
        assert info["kind"] == "local"
        assert info["workflow"] == "deploy.yml"
        assert info["inputs"] == ["environment"]
        assert info["secrets"] == "inherit"

    def test_remote_uses_ghosts_with_typed_record(self):
        r = parse_github(str(FIXTURES / "reusable"))
        notify = r.node_by_id("ci.yml::notify")
        info = notify.annotations["uses_info"]
        assert info["kind"] == "remote"
        assert info["project"] == "octo-org/shared"
        assert info["ref"] == "v2"
        trig = notify.annotations["trigger_info"]
        assert trig["mode"] == "multi_project"
        assert trig["project"] == "octo-org/shared"
        ghost = r.node_by_id(
            "downstream:octo-org/shared/.github/workflows/notify.yml@v2")
        assert ghost is not None and ghost.kind == "ghost"

    def test_call_inputs_become_variables(self):
        r = parse_github(str(FIXTURES / "reusable"))
        by_name = {v.name: v for v in r.variables}
        assert "inputs.environment" in by_name
        evt = by_name["inputs.environment"].events[0]
        assert evt.annotations["input_of"] == "workflow_call"
        assert evt.annotations["required"] is True


class TestMatrix:
    def test_matrix_expansion(self):
        r = parse_github(str(FIXTURES / "matrix"))
        test = r.node_by_id("ci.yml::test")
        assert "parallel" in test.flags
        mat = test.annotations["matrix"]
        # 2×2 minus one exclude = 3 combos; include only augments
        assert mat["count"] == 3
        assert set(mat["variables"]["os"]) == {"ubuntu-latest", "macos-latest"}
        assert "coverage" in mat["variables"]

    def test_matrix_axes_are_variables(self):
        r = parse_github(str(FIXTURES / "matrix"))
        by_name = {v.name: v for v in r.variables}
        assert by_name["os"].events[0].operator == "matrix"

    def test_whatif_combos_named(self):
        r = parse_github(str(FIXTURES / "matrix"))
        prog = r.node_by_id("ci.yml::test").annotations["whatif"]
        names = [c["name"] for c in prog["parallel"]["combos"]]
        assert "test (ubuntu-latest, 18)" in names
        assert len(names) == 3


class TestInvalidWorkflow:
    def test_conflicting_filters(self):
        r = parse_github(str(FIXTURES / "invalid"))
        msgs = [d.message for d in r.diagnostics if d.severity == "error"]
        assert any("both branches and branches-ignore" in m for m in msgs)

    def test_unknown_needs_is_error_and_ghost(self):
        r = parse_github(str(FIXTURES / "invalid"))
        ghost = r.node_by_id("broken.yml::missing_job")
        assert ghost is not None and ghost.kind == "ghost"
        msgs = [d.message for d in r.diagnostics if d.severity == "error"]
        assert any("needs 'missing_job'" in m for m in msgs)

    def test_job_without_steps(self):
        r = parse_github(str(FIXTURES / "invalid"))
        msgs = [d.message for d in r.diagnostics if d.severity == "error"]
        assert any("neither steps: nor uses:" in m for m in msgs)

    def test_double_quoted_string_rejected(self):
        r = parse_github(str(FIXTURES / "invalid"))
        msgs = [d.message for d in r.diagnostics if d.severity == "error"]
        assert any("double-quoted strings" in m for m in msgs)

    def test_circular_needs(self):
        r = parse_github(str(FIXTURES / "invalid"))
        msgs = [d.message for d in r.diagnostics if d.severity == "error"]
        assert any("circular needs dependency" in m for m in msgs)

    def test_workflow_marked_invalid_in_program(self):
        r = parse_github(str(FIXTURES / "invalid"))
        wf = r.annotations["whatif"]["workflows"]["broken.yml"]
        assert wf["invalid"]


class TestLints:
    def test_push_pull_request_duplicate_lint(self):
        r = parse_github(str(FIXTURES / "whatif_dup"))
        lint = r.annotations["whatif"]["lint"]
        assert any("starts it twice" in e["message"] for e in lint)

    def test_branch_filtered_push_not_flagged(self):
        r = parse_github(str(FIXTURES / "minimal"))
        lint = r.annotations["whatif"]["lint"]
        assert not any("starts it twice" in e["message"] for e in lint)

    def test_github_ref_bare_branch_lint(self):
        r = parse_github(str(FIXTURES / "whatif_dup"))
        lint = r.annotations["whatif"]["lint"]
        assert any("never matches" in e["message"] for e in lint)


class TestMissingInputs:
    def test_empty_directory(self, tmp_path):
        r = parse_github(str(tmp_path))
        assert r.max_severity() == "error"

    def test_yaml_error_degrades_file(self, tmp_path):
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "bad.yml").write_text("on: [push\njobs:")
        (wf / "good.yml").write_text(
            "on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n"
            "    steps:\n      - run: true\n")
        r = parse_github(str(tmp_path))
        statuses = {f.path: f.status for f in r.files}
        assert statuses["bad.yml"] == "error"
        assert statuses["good.yml"] == "ok"
        assert r.node_by_id("good.yml::a") is not None
