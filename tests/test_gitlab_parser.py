import pytest
from pathlib import Path

from pipeview.parsers.gitlab_parser import parse_gitlab

FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"


class TestMinimal:
    def test_stages_parsed(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        stage_nodes = [n for n in r.nodes if n.kind == "stage"]
        stage_names = {n.name for n in stage_nodes}
        assert stage_names == {"build", "test", "deploy"}

    def test_jobs_parsed(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        job_nodes = [n for n in r.nodes if n.kind == "job"]
        job_names = {n.name for n in job_nodes}
        assert "build_job" in job_names
        assert "test_job" in job_names
        assert "deploy_job" in job_names

    def test_needs_edges(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        needs_edges = [(e.src, e.dst) for e in r.edges if e.kind == "needs"]
        assert ("test_job", "build_job") in needs_edges
        assert ("deploy_job", "test_job") in needs_edges

    def test_manual_flag(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        deploy = r.node_by_id("deploy_job")
        assert deploy is not None
        assert "manual" in deploy.flags
        assert "allow_failure" in deploy.flags

    def test_global_variables(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        var_names = {v.name for v in r.variables}
        assert "APP_NAME" in var_names
        assert "VERSION" in var_names

    def test_image_annotation(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert build.annotations.get("image") == "gcc:latest"

    def test_tags_annotation(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert build.annotations.get("tags") == ["docker"]

    def test_recipes_captured(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert any("make build" in line for line in build.recipe)

    def test_docstrings(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert build.doc == "Build the application"
        test = r.node_by_id("test_job")
        assert test.doc == "Run tests"

    def test_stage_order_edges(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        stage_edges = [e for e in r.edges if e.kind == "stage_order"]
        assert len(stage_edges) > 0

    def test_source_files(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        paths = [f.path for f in r.files]
        assert any(".gitlab-ci.yml" in p for p in paths)


class TestExtendsChain:
    def test_extends_edges(self):
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        ext_edges = [(e.src, e.dst) for e in r.edges if e.kind == "extends"]
        assert ("build_job", ".build_base") in ext_edges
        assert (".build_base", ".base") in ext_edges

    def test_template_flag(self):
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        base = r.node_by_id(".base")
        assert base is not None
        assert "template" in base.flags

    def test_undefined_parent_ghost(self):
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        ghost = r.node_by_id(".undefined_parent")
        assert ghost is not None
        assert ghost.kind == "ghost"
        ext_edges = [(e.src, e.dst) for e in r.edges if e.kind == "extends"]
        assert ("test_job", ".undefined_parent") in ext_edges

    def test_undefined_parent_diagnostic(self):
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        warns = [d for d in r.diagnostics if "undefined" in d.message.lower()]
        assert len(warns) >= 1

    def test_inherited_image(self):
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert build.annotations.get("image") == "alpine:latest"

    def test_inherited_variables(self):
        # Inheritance provenance rides on the event's annotations now; the
        # operator stays "job" because that is what the flattened config says.
        r = parse_gitlab(str(FIXTURES / "extends_chain" / ".gitlab-ci.yml"))
        env_var = next((v for v in r.variables if v.name == "ENV"), None)
        assert env_var is not None
        inherited = [e for e in env_var.events if e.annotations.get("inherited_from")]
        assert len(inherited) >= 1


class TestIncludeChain:
    def test_local_include_resolved(self):
        r = parse_gitlab(str(FIXTURES / "include_chain" / ".gitlab-ci.yml"))
        paths = [f.path for f in r.files]
        assert any("build.yml" in p for p in paths)

    def test_project_include_unresolved(self):
        r = parse_gitlab(str(FIXTURES / "include_chain" / ".gitlab-ci.yml"))
        unresolved = [f for f in r.files if f.status == "unresolved"]
        assert len(unresolved) >= 1
        warns = [d for d in r.diagnostics if "project" in d.message.lower()]
        assert len(warns) >= 1

    def test_include_edges(self):
        r = parse_gitlab(str(FIXTURES / "include_chain" / ".gitlab-ci.yml"))
        inc_edges = [e for e in r.edges if e.kind == "includes"]
        assert len(inc_edges) >= 1

    def test_jobs_from_include(self):
        r = parse_gitlab(str(FIXTURES / "include_chain" / ".gitlab-ci.yml"))
        names = {n.name for n in r.nodes if n.kind == "job"}
        assert "compile_job" in names


class TestNeedsDag:
    def test_needs_edges_complete(self):
        r = parse_gitlab(str(FIXTURES / "needs_dag" / ".gitlab-ci.yml"))
        needs = [(e.src, e.dst) for e in r.edges if e.kind == "needs"]
        assert ("test_a", "build_a") in needs
        assert ("test_b", "build_a") in needs
        assert ("test_b", "build_b") in needs
        assert ("deploy", "test_a") in needs
        assert ("deploy", "test_b") in needs

    def test_jobs_with_needs_no_stage_order(self):
        r = parse_gitlab(str(FIXTURES / "needs_dag" / ".gitlab-ci.yml"))
        stage_order_srcs = {e.src for e in r.edges if e.kind == "stage_order"}
        needs_srcs = {e.src for e in r.edges if e.kind == "needs"}
        assert not (stage_order_srcs & needs_srcs)


class TestTemplates:
    def test_template_flag_set(self):
        r = parse_gitlab(str(FIXTURES / "templates" / ".gitlab-ci.yml"))
        templates = [n for n in r.nodes if "template" in n.flags]
        template_names = {n.name for n in templates}
        assert ".build_template" in template_names
        assert ".test_template" in template_names

    def test_non_templates_not_flagged(self):
        r = parse_gitlab(str(FIXTURES / "templates" / ".gitlab-ci.yml"))
        build_app = r.node_by_id("build_app")
        assert "template" not in build_app.flags


class TestRulesManual:
    def test_rules_annotation(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert "rules" in build.annotations
        assert len(build.annotations["rules"]) >= 1

    def test_manual_from_rules(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        build = r.node_by_id("build_job")
        assert "manual" in build.flags

    def test_manual_from_when(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        deploy = r.node_by_id("deploy_staging")
        assert "manual" in deploy.flags

    def test_allow_failure(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        deploy = r.node_by_id("deploy_staging")
        assert "allow_failure" in deploy.flags

    def test_only_except_annotations(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        staging = r.node_by_id("deploy_staging")
        assert "only" in staging.annotations
        prod = r.node_by_id("deploy_prod")
        assert "except" in prod.annotations

    def test_trigger_invokes_edge(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        inv_edges = [e for e in r.edges if e.kind == "invokes"]
        assert len(inv_edges) >= 1
        dsts = {e.dst for e in inv_edges}
        assert any("downstream" in d for d in dsts)

    def test_job_variables(self):
        r = parse_gitlab(str(FIXTURES / "rules_manual" / ".gitlab-ci.yml"))
        target_var = next((v for v in r.variables if v.name == "TARGET"), None)
        assert target_var is not None
        assert any(e.scope == "deploy_staging" for e in target_var.events)


class TestYamlError:
    def test_error_produces_report(self):
        r = parse_gitlab(str(FIXTURES / "yaml_error" / ".gitlab-ci.yml"))
        assert r is not None

    def test_error_diagnostic(self):
        r = parse_gitlab(str(FIXTURES / "yaml_error" / ".gitlab-ci.yml"))
        errors = [d for d in r.diagnostics if d.severity == "error"]
        assert len(errors) >= 1
        assert any("yaml" in d.message.lower() or "parse" in d.message.lower() for d in errors)

    def test_error_file_status(self):
        r = parse_gitlab(str(FIXTURES / "yaml_error" / ".gitlab-ci.yml"))
        error_files = [f for f in r.files if f.status == "error"]
        assert len(error_files) >= 1


class TestJsonSnapshot:
    def test_serialization_round_trip(self):
        r = parse_gitlab(str(FIXTURES / "minimal" / ".gitlab-ci.yml"))
        js = r.to_json(indent=2)
        restored = r.from_json(js)
        assert restored.to_json(indent=2) == js
