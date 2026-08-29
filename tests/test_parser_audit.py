"""Regression tests born from the parser conformance audit
(docs/agents/parser-audit.md). Each class pins a failure that existed before the
audit-fix pass; the two `Reported*` classes reproduce the field reports that
motivated the audit verbatim.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.make_parser import parse_makefile

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"

# The taxonomy contract: these phrases may only appear for input real make
# would itself reject.
_UNPARSEABLE_PHRASES = ("Unparseable", "Not valid Make syntax")


def _unparseable(report):
    return [
        d for d in report.diagnostics
        if any(p in d.message for p in _UNPARSEABLE_PHRASES)
    ]


class TestReportedExportRegression:
    """Field report: `export PROMPT_CONFIG_UPDATES ?= true` and
    `export HTTPS_MODE = false` warned 'Unparseable line'. The acceptance
    criterion: both land as Variable events with the exported mark and zero
    diagnostics."""

    @pytest.fixture()
    def report(self):
        return parse_makefile(str(MAKE_FIXTURES / "export_directives" / "Makefile"))

    def test_zero_diagnostics_for_export_lines(self, report):
        assert _unparseable(report) == []

    def test_reported_variables_present_with_operators(self, report):
        by_name = {v.name: v for v in report.variables}
        prompt = by_name["PROMPT_CONFIG_UPDATES"]
        https = by_name["HTTPS_MODE"]
        assert [e.operator for e in prompt.events] == ["?="]
        assert prompt.events[0].raw_value == "true"
        assert [e.operator for e in https.events] == ["="]
        assert https.events[0].raw_value == "false"

    def test_exported_mark(self, report):
        by_name = {v.name: v for v in report.variables}
        assert by_name["PROMPT_CONFIG_UPDATES"].exported
        assert by_name["HTTPS_MODE"].exported
        assert by_name["PROMPT_CONFIG_UPDATES"].events[0].annotations.get("export")

    def test_export_simple_assignment_is_not_a_rule(self, report):
        # `export SIMPLE := ...` used to silently fabricate a rule named
        # 'export' with ghost prerequisites '=' and the value.
        names = {n.name for n in report.nodes}
        assert "export" not in names
        assert "=" not in names
        by_name = {v.name: v for v in report.variables}
        assert by_name["SIMPLE"].events[0].operator == ":="
        assert by_name["SIMPLE"].exported

    def test_default_goal_not_corrupted(self, report):
        assert report.default_goal == "all"

    def test_override_and_stacked_directives(self, report):
        by_name = {v.name: v for v in report.variables}
        assert by_name["OPT"].events[0].annotations.get("override")
        stacked = by_name["STACKED"]
        assert stacked.exported
        assert stacked.events[0].annotations.get("override")
        assert stacked.events[0].operator == ":="

    def test_bare_export_unexport_undefine(self, report):
        by_name = {v.name: v for v in report.variables}
        ops = [e.operator for e in by_name["FOO"].events]
        assert ops == [":=", "export", "unexport"]
        assert not by_name["FOO"].exported  # unexport was last
        assert by_name["GONE"].events[0].operator == "undefine"


class TestReportedReferenceRegression:
    """Field report: one `!reference [MR Docker Build & Push, script]` whose
    target lives in an unfetchable `include: project:` destroyed the parse of
    the entire file, with the error attributed to line 1. The contract: one
    external reference degrades one value — never the job, never the file."""

    @pytest.fixture()
    def report(self):
        return parse_gitlab(str(GITLAB_FIXTURES / "reference_external" / ".gitlab-ci.yml"))

    def test_file_status_ok(self, report):
        root = next(f for f in report.files if f.path == ".gitlab-ci.yml")
        assert root.status == "ok"

    def test_no_error_severity(self, report):
        assert all(d.severity != "error" for d in report.diagnostics)

    def test_every_other_job_intact(self, report):
        jobs = {n.name for n in report.nodes if n.kind == "job"}
        assert {"build_local", "deploy_job", "uses_local_reference"} <= jobs
        build = report.node_by_id("build_local")
        assert build.recipe == ["[script] echo build"]

    def test_referencing_job_parses_completely_with_placeholder(self, report):
        deploy = report.node_by_id("deploy_job")
        assert deploy is not None
        assert deploy.recipe[0] == "[script] echo before"
        assert deploy.recipe[-1] == "[script] echo after"
        placeholder = deploy.recipe[1]
        assert "MR Docker Build & Push" in placeholder
        assert "defined externally" in placeholder

    def test_ghost_node_carries_target_name(self, report):
        ghost = report.node_by_id("MR Docker Build & Push")
        assert ghost is not None
        assert ghost.kind == "ghost"

    def test_diagnostic_names_reference_and_include(self, report):
        diags = [d for d in report.diagnostics
                 if "MR Docker Build & Push" in d.message]
        assert diags, "expected a diagnostic naming the unresolved reference"
        assert any("devops/pipeline-repo" in d.message for d in diags), \
            "diagnostic should point at the likely unresolved include"
        assert all(d.source is not None and d.source.line > 1 for d in diags), \
            "diagnostic must carry the real line, not line 1"

    def test_local_reference_splices(self, report):
        job = report.node_by_id("uses_local_reference")
        assert job.recipe == [
            "[script] echo shared-one",
            "[script] echo shared-two",
            "[script] echo own-step",
        ]


class TestMakeAcceptsContract:
    """Permanent enforcement of the diagnostic taxonomy: `make -pqn` accepts
    the composition fixture (directive+assignment, URL/PATH values, semicolon
    recipe, nested functions, canned recipe, CRLF endings), so pipeview must
    emit zero unparseable-class diagnostics on it."""

    @pytest.fixture()
    def report(self):
        return parse_makefile(str(MAKE_FIXTURES / "contract" / "Makefile"))

    @pytest.mark.skipif(shutil.which("make") is None, reason="make not on PATH")
    def test_real_make_accepts_fixture(self):
        proc = subprocess.run(
            ["make", "-pqn", "-f", "Makefile"],
            cwd=str(MAKE_FIXTURES / "contract"),
            capture_output=True, text=True, timeout=30,
        )
        assert "missing separator" not in proc.stderr
        assert "***" not in proc.stderr, proc.stderr

    def test_zero_unparseable_diagnostics(self, report):
        assert _unparseable(report) == []

    def test_colon_values_are_variables_not_rules(self, report):
        by_name = {v.name: v for v in report.variables}
        assert by_name["URL"].events[0].raw_value == "https://example.com/path?x=1"
        assert by_name["LDPATH"].events[0].raw_value == "/usr/lib:/usr/local/lib:/lib"
        assert "URL := https" not in {n.name for n in report.nodes}

    def test_empty_and_comment_values(self, report):
        by_name = {v.name: v for v in report.variables}
        assert by_name["EMPTY"].events[0].raw_value == ""
        assert by_name["EMPTY2"].events[0].raw_value == ""
        assert by_name["HASH_VALUE"].events[0].raw_value == "literal#hash"

    def test_semicolon_recipe(self, report):
        quick = report.node_by_id("quick")
        assert quick is not None
        assert quick.kind == "target"
        assert any("built via semicolon recipe" in r for r in quick.recipe)
        edges = {(e.src, e.dst) for e in report.edges if e.kind == "prerequisite"}
        assert ("quick", "deps") in edges

    def test_shell_dollars_not_make_variables(self, report):
        names = {v.name for v in report.variables}
        assert "HOME" not in names
        assert "pwd" not in names

    def test_canned_recipe_links_to_definition(self, report):
        run_tests = next(v for v in report.variables if v.name == "run-tests")
        assert "test" in run_tests.used_by
        assert "echo" in run_tests.events[0].raw_value

    def test_empty_recipe_annotation(self, report):
        node = report.node_by_id("empty-recipe")
        assert node is not None
        assert node.annotations.get("empty_recipe")


class TestGhostUpgrade:
    def test_prerequisite_first_target_becomes_real(self, tmp_path):
        # `all: build test` at the top of nearly every Makefile: the targets
        # defined below must not stay dashed "unresolved reference" ghosts.
        mk = tmp_path / "Makefile"
        mk.write_text("all: build\n\nbuild:\n\techo built\n")
        r = parse_makefile(str(mk))
        build = r.node_by_id("build")
        assert build.kind == "target"
        assert build.source is not None
        assert build.recipe == ["echo built"]


class TestMakeRulesAdvanced:
    @pytest.fixture()
    def report(self):
        return parse_makefile(str(MAKE_FIXTURES / "rules_advanced" / "Makefile"))

    def test_static_pattern_rule(self, report):
        a_o = report.node_by_id("a.o")
        assert a_o is not None and a_o.kind == "target"
        assert a_o.annotations.get("static_pattern") == "%.o: %.c"
        edges = {(e.src, e.dst) for e in report.edges if e.kind == "prerequisite"}
        assert ("a.o", "a.c") in edges
        assert ("b.o", "b.c") in edges

    def test_grouped_targets(self, report):
        lib = report.node_by_id("lib.a")
        sym = report.node_by_id("out.sym")
        assert lib.annotations.get("grouped")
        assert sym.annotations.get("grouped")

    def test_double_colon_appends_recipes(self, report):
        docs = report.node_by_id("docs")
        assert docs.annotations.get("double_colon")
        assert docs.recipe == ["cat intro.md", "cat api.md"]

    def test_recipe_override_warns_like_make(self, report):
        warns = [d for d in report.diagnostics
                 if "Overriding recipe" in d.message and "'same'" in d.message]
        assert len(warns) == 1
        same = report.node_by_id("same")
        assert same.recipe == ["echo two"]  # last recipe wins, like make


class TestMakeCycles:
    def test_include_cycle_terminates_with_diagnostic(self):
        r = parse_makefile(str(MAKE_FIXTURES / "cycles" / "Makefile"))
        cyc = [d for d in r.diagnostics if "Include cycle" in d.message]
        assert cyc, "include cycle must be named"
        # both files still parsed once
        names = {v.name for v in r.variables}
        assert {"A", "B"} <= names

    def test_recursion_loop_terminates(self):
        r = parse_makefile(str(MAKE_FIXTURES / "cycles" / "Makefile"))
        assert r is not None  # termination IS the assertion

    def test_unclosed_define_diagnosed(self, tmp_path):
        mk = tmp_path / "Makefile"
        mk.write_text("all:\n\techo hi\ndefine NEVER\nbody\n")
        r = parse_makefile(str(mk))
        assert any("never closed" in d.message for d in r.diagnostics)

    def test_stray_endif_diagnosed(self, tmp_path):
        mk = tmp_path / "Makefile"
        mk.write_text("endif\nall:\n\techo hi\n")
        r = parse_makefile(str(mk))
        assert any("'endif' without matching" in d.message for d in r.diagnostics)


class TestMakeVariableSemantics:
    def test_builtin_default_origin(self, tmp_path):
        mk = tmp_path / "Makefile"
        mk.write_text("all:\n\t$(CC) -o app main.c && $(RM) tmp\n")
        r = parse_makefile(str(mk))
        by_name = {v.name: v for v in r.variables}
        assert by_name["CC"].origin == "built-in default"
        assert by_name["RM"].origin == "built-in default"

    def test_automatic_variables_excluded(self, tmp_path):
        mk = tmp_path / "Makefile"
        mk.write_text("%.o: %.c\n\tcc -c $< -o $@ $(@D)\n")
        r = parse_makefile(str(mk))
        names = {v.name for v in r.variables}
        assert not names & {"@", "<", "@D"}

    def test_target_specific_propagation_annotation(self, tmp_path):
        mk = tmp_path / "Makefile"
        mk.write_text("foo: PATH := /usr/bin:/bin\nfoo:\n\techo $(PATH)\n")
        r = parse_makefile(str(mk))
        path = next(v for v in r.variables if v.name == "PATH")
        evt = path.events[0]
        assert evt.scope == "foo"
        assert evt.raw_value == "/usr/bin:/bin"
        assert evt.annotations.get("propagates_to_prereqs")

    def test_wildcard_include_expands(self, tmp_path):
        (tmp_path / "one.mk").write_text("FROM_ONE := 1\n")
        (tmp_path / "two.mk").write_text("FROM_TWO := 2\n")
        mk = tmp_path / "Makefile"
        mk.write_text("include $(wildcard *.mk)\nall:\n\techo hi\n")
        r = parse_makefile(str(mk))
        names = {v.name for v in r.variables}
        assert {"FROM_ONE", "FROM_TWO"} <= names

    def test_variable_include_path_resolves(self, tmp_path):
        (tmp_path / "cfg").mkdir()
        (tmp_path / "cfg" / "x.mk").write_text("FROM_CFG := 1\n")
        mk = tmp_path / "Makefile"
        mk.write_text("CONFIG_DIR := cfg\ninclude $(CONFIG_DIR)/x.mk\nall:\n\techo hi\n")
        r = parse_makefile(str(mk))
        assert "FROM_CFG" in {v.name for v in r.variables}

    def test_resolvable_make_C_variable(self, tmp_path):
        sub = tmp_path / "engine"
        sub.mkdir()
        (sub / "Makefile").write_text("inner:\n\techo inner\n")
        mk = tmp_path / "Makefile"
        mk.write_text("SUBDIR := engine\nall:\n\t$(MAKE) -C $(SUBDIR)\n")
        r = parse_makefile(str(mk))
        assert any(e.kind == "invokes" for e in r.edges)
        assert any("inner" in n.name for n in r.nodes)


class TestGitlabExtendsReplacement:
    @pytest.fixture()
    def report(self):
        return parse_gitlab(str(GITLAB_FIXTURES / "extends_replacement" / ".gitlab-ci.yml"))

    def test_child_script_replaces_parent(self, report):
        child = report.node_by_id("replacer")
        script_lines = [r for r in child.recipe if r.startswith("[script]")]
        assert script_lines == ["[script] echo child-only"]

    def test_replacement_is_annotated(self, report):
        child = report.node_by_id("replacer")
        overrides = child.annotations.get("overrides", {})
        assert "script" in overrides
        assert ".base" in overrides["script"]

    def test_parent_before_script_still_inherited(self, report):
        child = report.node_by_id("replacer")
        assert "[before_script] echo parent-before" in child.recipe

    def test_variables_merge_not_replace(self, report):
        scopes = {
            v.name: [e.scope for e in v.events] for v in report.variables
        }
        assert "replacer" in scopes.get("X", [])  # inherited hash-merges
        assert "replacer" in scopes.get("Y", [])

    def test_heir_without_script_inherits_everything(self, report):
        heir = report.node_by_id("pure_heir")
        assert heir.recipe[-2:] == ["[script] echo parent-a", "[script] echo parent-b"]
        assert heir.annotations.get("image") == "alpine"

    def test_multiple_parents_later_wins(self, report):
        child = report.node_by_id("multi_child")
        assert child.annotations.get("image") == "img-second"


class TestGitlabCoercion:
    @pytest.fixture()
    def report(self):
        return parse_gitlab(str(GITLAB_FIXTURES / "coercion" / ".gitlab-ci.yml"))

    def test_values_are_the_strings_gitlab_passes(self, report):
        vals = {v.name: v.events[0].raw_value for v in report.variables if v.events}
        assert vals["DEBUG"] == "yes"
        assert vals["MODE"] == "off"
        assert vals["MASK"] == "0777"
        assert vals["TZ"] == "12:30"
        assert vals["VER"] == "1.10"
        assert vals["QUOTED"] == "yes"

    def test_ambiguous_values_diagnosed(self, report):
        msgs = " ".join(d.message for d in report.diagnostics)
        assert "DEBUG" in msgs and "TZ" in msgs
        # quoted value is unambiguous — no diagnostic
        assert "QUOTED" not in msgs

    def test_hash_form_variable(self, report):
        rich = next(v for v in report.variables if v.name == "RICH")
        assert rich.events[0].raw_value == "default-val"
        assert rich.events[0].annotations.get("description") == "A documented variable"

    def test_duplicate_keys_diagnosed(self, report):
        dups = [d for d in report.diagnostics if "Duplicate key" in d.message]
        assert any("job_one" in d.message for d in dups)

    def test_string_form_script_captured(self, report):
        job = report.node_by_id("string_script")
        assert job.recipe and "line one" in job.recipe[0]

    def test_nested_script_flattened(self, report):
        job = report.node_by_id("nested_script")
        assert job.recipe == [
            "[script] echo a", "[script] echo b", "[script] echo c",
        ]


class TestGitlabCycles:
    @pytest.fixture()
    def report(self):
        return parse_gitlab(str(GITLAB_FIXTURES / "cycles" / ".gitlab-ci.yml"))

    def test_extends_cycle_diagnosed(self, report):
        assert any("extends cycle" in d.message for d in report.diagnostics)

    def test_reference_cycle_diagnosed(self, report):
        user = report.node_by_id("cyclic_ref_user")
        assert user is not None  # termination
        assert any("cycle" in d.message and "reference" in d.message.lower()
                   for d in report.diagnostics)

    def test_include_cycle_diagnosed(self, report):
        assert any("Include cycle" in d.message for d in report.diagnostics)
        # both files still parsed once
        jobs = {n.name for n in report.nodes if n.kind == "job"}
        assert {"a_job", "b_job"} <= jobs


class TestGitlabPipelineSemantics:
    @pytest.fixture()
    def report(self):
        return parse_gitlab(str(GITLAB_FIXTURES / "pipeline_semantics" / ".gitlab-ci.yml"))

    def test_workflow_rules_banner(self, report):
        rules = report.annotations.get("workflow_rules")
        assert rules and "main" in rules[0]

    def test_needs_empty_starts_immediately(self, report):
        racer = report.node_by_id("racer")
        assert "starts_immediately" in racer.flags
        assert not any(
            e.src == "racer" and e.kind == "stage_order" for e in report.edges
        )

    def test_needs_details_and_dependencies_separate(self, report):
        waiter = report.node_by_id("waiter")
        assert waiter.annotations.get("needs_details") == ["builder (optional, no artifacts)"]
        assert waiter.annotations.get("artifact_dependencies") == ["builder"]
        assert any(e.src == "waiter" and e.dst == "builder" and e.kind == "needs"
                   for e in report.edges)

    def test_stageless_job_defaults_to_test(self, report):
        assert report.node_by_id("stageless").annotations.get("stage") == "test"

    def test_undeclared_stage_is_an_error(self, report):
        errs = [d for d in report.diagnostics
                if d.severity == "error" and "nonexistent" in d.message]
        assert len(errs) == 1

    def test_matrix_expansion(self, report):
        fan = report.node_by_id("fan_out")
        assert "parallel" in fan.flags
        matrix = fan.annotations.get("matrix")
        assert matrix and matrix["count"] == 4  # aws,gcp × us,eu
        axes = {v.name for v in report.variables if any(
            e.annotations.get("matrix_axis") for e in v.events)}
        assert {"PROVIDER", "REGION"} <= axes

    def test_pages_is_a_job(self, report):
        pages = report.node_by_id("pages")
        assert pages is not None and pages.kind == "job"


class TestGitlabTriggerChild:
    def test_child_pipeline_parsed_not_ghosted(self):
        r = parse_gitlab(str(GITLAB_FIXTURES / "trigger_child" / ".gitlab-ci.yml"))
        child = r.node_by_id("ci/child.yml::child_job")
        assert child is not None and child.kind == "job"
        assert child.annotations.get("child_pipeline") == "ci/child.yml"
        assert child.recipe == ["[script] echo child work"]
        trigger = r.node_by_id("run_child")
        assert trigger.annotations.get("trigger_strategy") == "depend"
        assert any(e.src == "run_child" and e.kind == "invokes" for e in r.edges)
        paths = {f.path for f in r.files}
        assert "ci/child.yml" in paths


class TestGitlabRobustness:
    def test_bool_and_int_job_keys_do_not_crash(self, tmp_path):
        f = tmp_path / ".gitlab-ci.yml"
        f.write_text('true:\n  script: [echo weird]\n123:\n  script: [echo n]\n')
        r = parse_gitlab(str(f))
        names = {n.name for n in r.nodes if n.kind == "job"}
        assert {"true", "123"} <= names

    def test_empty_root_file(self, tmp_path):
        f = tmp_path / ".gitlab-ci.yml"
        f.write_text("# only comments\n")
        r = parse_gitlab(str(f))
        assert any("empty" in d.message for d in r.diagnostics)

    def test_multi_document_salvages_first(self, tmp_path):
        f = tmp_path / ".gitlab-ci.yml"
        f.write_text("---\njob_a:\n  script: [echo a]\n---\njob_b:\n  script: [echo b]\n")
        r = parse_gitlab(str(f))
        assert r.node_by_id("job_a") is not None
        assert any("multiple YAML documents" in d.message for d in r.diagnostics)

    def test_predefined_variables_labeled(self, tmp_path):
        f = tmp_path / ".gitlab-ci.yml"
        f.write_text("job:\n  script: [echo $CI_COMMIT_SHA $MY_SECRET]\n")
        r = parse_gitlab(str(f))
        by_name = {v.name: v for v in r.variables}
        assert by_name["CI_COMMIT_SHA"].origin == "predefined"
        assert by_name["MY_SECRET"].origin is None

    def test_wildcard_include(self, tmp_path):
        ci = tmp_path / "ci"
        ci.mkdir()
        (ci / "w1.yml").write_text("w1_job:\n  script: [echo 1]\n")
        (ci / "w2.yml").write_text("w2_job:\n  script: [echo 2]\n")
        f = tmp_path / ".gitlab-ci.yml"
        f.write_text("include:\n  - local: 'ci/*.yml'\nroot_job:\n  script: [echo r]\n")
        r = parse_gitlab(str(f))
        jobs = {n.name for n in r.nodes if n.kind == "job"}
        assert {"w1_job", "w2_job", "root_job"} <= jobs


class TestExamplesSeeded:
    """The demo reports permanently exercise the reported quirk classes."""

    def test_make_example_has_exported_variable(self):
        examples = Path(__file__).parent.parent / "examples"
        r = parse_makefile(str(examples / "make-project" / "Makefile"))
        profile = next(v for v in r.variables if v.name == "BUILD_PROFILE")
        assert profile.exported
        assert profile.events[0].operator == "?="
        assert _unparseable(r) == []

    def test_gitlab_example_has_extends_replacement(self):
        examples = Path(__file__).parent.parent / "examples"
        r = parse_gitlab(str(examples / "gitlab-project" / ".gitlab-ci.yml"))
        smoke = r.node_by_id("smoke_tests")
        overrides = smoke.annotations.get("overrides", {})
        assert "script" in overrides and ".checks" in overrides["script"]
        scripts = [x for x in smoke.recipe if x.startswith("[script]")]
        assert scripts == ["[script] pytest tests/smoke -q --no-header"]
