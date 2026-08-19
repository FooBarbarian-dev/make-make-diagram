"""The what-if compiler: expression parsing, program compilation, exists
baking, artifact extraction, and lint — the Python half of the simulator."""

from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif import (
    _eval_exists,
    glob_to_regex,
    parse_expression,
)

FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"


def whatif_of(report, job_id):
    node = report.node_by_id(job_id)
    assert node is not None, f"missing node {job_id}"
    assert "whatif" in node.annotations, f"{job_id} has no whatif program"
    return node.annotations["whatif"]


class TestExpressionParser:
    def test_equality_with_string(self):
        ast, notes, err = parse_expression('$CI_COMMIT_BRANCH == "main"')
        assert err is None
        assert ast == {
            "op": "cmp", "cmp": "==",
            "left": {"t": "var", "name": "CI_COMMIT_BRANCH"},
            "right": {"t": "str", "value": "main"},
        }

    def test_single_quotes(self):
        ast, _, err = parse_expression("$X == 'y'")
        assert err is None
        assert ast["right"] == {"t": "str", "value": "y"}

    def test_null_comparison(self):
        ast, _, err = parse_expression("$V != null")
        assert err is None
        assert ast["cmp"] == "!="
        assert ast["right"] == {"t": "null"}

    def test_bare_variable_is_truthy_test(self):
        ast, _, err = parse_expression("$CI_COMMIT_TAG")
        assert err is None
        assert ast == {"op": "truthy", "term": {"t": "var", "name": "CI_COMMIT_TAG"}}

    def test_braced_variable(self):
        ast, _, err = parse_expression("${FOO}")
        assert err is None
        assert ast["term"]["name"] == "FOO"

    def test_regex_with_flags(self):
        ast, _, err = parse_expression("$REF =~ /^feature-.*/i")
        assert err is None
        assert ast["cmp"] == "=~"
        assert ast["right"] == {"t": "re", "source": "^feature-.*", "flags": "i"}

    def test_regex_with_escaped_slash(self):
        ast, _, err = parse_expression(r"$P =~ /a\/b/")
        assert err is None
        assert ast["right"]["source"] == r"a\/b"

    def test_variable_on_both_sides(self):
        ast, _, err = parse_expression("$A == $B")
        assert err is None
        assert ast["right"] == {"t": "var", "name": "B"}

    def test_and_binds_tighter_than_or(self):
        ast, _, err = parse_expression("$A || $B && $C")
        assert err is None
        assert ast["op"] == "or"
        assert ast["args"][1]["op"] == "and"

    def test_parentheses_override_precedence(self):
        ast, _, err = parse_expression("($A || $B) && $C")
        assert err is None
        assert ast["op"] == "and"
        assert ast["args"][0]["op"] == "or"

    def test_negation(self):
        ast, _, err = parse_expression('!($A == "x")')
        assert err is None
        assert ast["op"] == "not"
        assert ast["arg"]["cmp"] == "=="

    def test_non_re2_pattern_gets_note(self):
        _, notes, err = parse_expression("$V =~ /(?=look)ahead/")
        assert err is None
        assert notes and "RE2" in notes[0]

    def test_token_junk_is_invalid_not_opaque(self):
        # GitLab itself rejects these ("invalid expression syntax")
        ast, _, err = parse_expression('CI_COMMIT_BRANCH === "oops"')
        assert err is not None
        assert ast["op"] == "invalid"
        assert "CI_COMMIT_BRANCH" in ast["src"]
        for src in ("$X == true", "", "   "):
            ast, _, err = parse_expression(src)
            assert ast["op"] == "invalid", src

    def test_unterminated_string_is_invalid(self):
        ast, _, err = parse_expression('$A == "unclosed')
        assert err is not None
        assert ast["op"] == "invalid"

    def test_structural_failure_stays_opaque(self):
        ast, _, err = parse_expression('($A == "x"')   # tokens fine, structure not
        assert err is not None
        assert ast["op"] == "opaque"

    def test_chained_comparisons_parse_left_associative(self):
        ast, _, err = parse_expression('$A == "a" == "b"')
        assert err is None
        assert ast["cmp"] == "=="
        assert ast["left"]["cmp"] == "=="
        assert ast["right"] == {"t": "str", "value": "b"}


class TestGlobTranslation:
    @pytest.mark.parametrize("pattern,path,matches", [
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),          # PATHNAME: * stops at /
        ("docs/**/*", "docs/a/b.md", True),
        ("**/*.json", "a.json", True),              # **/ matches zero dirs
        ("**/*.json", "x/y/a.json", True),
        ("src/*.{rb,py}", "src/app.py", True),
        ("src/*.{rb,py}", "src/app.js", False),
        ("?.txt", "a.txt", True),
        ("?.txt", "ab.txt", False),
        (".hidden", ".hidden", True),               # DOTMATCH
        ("{docs/**,spec/**}", "docs/a/b.md", True),  # wildcards inside braces
        ("{docs/**,spec/**}", "src/a.py", False),
        (r"a\*b", "a*b", True),                     # backslash escapes literal
        (r"a\*b", "aXb", False),
    ])
    def test_patterns(self, pattern, path, matches):
        rx = glob_to_regex(pattern)
        assert rx is not None
        assert bool(rx.match(path)) is matches


class TestExistsEvaluation:
    def test_match_short_circuits_past_bad_patterns(self):
        assert _eval_exists(["Dockerfile", "[z-a]["], ["Dockerfile"], {}, False) is True

    def test_bad_pattern_without_match_is_unknown(self):
        assert _eval_exists(["[z-a]["], ["Dockerfile"], {}, False) is None

    def test_truncated_listing_never_bakes_false(self):
        assert _eval_exists(["missing.txt"], ["a.txt"], {}, True) is None
        assert _eval_exists(["a.txt"], ["a.txt"], {}, True) is True

    def test_definite_false_on_complete_listing(self):
        assert _eval_exists(["missing.txt"], ["a.txt"], {}, False) is False


@pytest.fixture(scope="module")
def dup():
    return parse_gitlab(str(FIXTURES / "whatif_dup" / ".gitlab-ci.yml"))


@pytest.fixture(scope="module")
def features():
    return parse_gitlab(str(FIXTURES / "whatif_features" / ".gitlab-ci.yml"))


class TestCompiledPrograms:
    def test_report_annotation_shape(self, dup):
        w = dup.annotations["whatif"]
        assert w["version"] == 1
        assert w["default_branch"] == "main"
        assert w["protected_refs"] == ["main", "dev"]
        assert w["workflow"] is None
        assert w["stages"][0] == ".pre" and w["stages"][-1] == ".post"

    def test_no_rules_job_gets_implicit_only_default(self, dup):
        program = whatif_of(dup, "build_all")["program"]
        assert program["kind"] == "legacy"
        assert program["implicit_default"] is True
        assert program["only"]["refs"] == ["branches", "tags"]

    def test_rules_job_compiled(self, dup):
        program = whatif_of(dup, "test_mr")["program"]
        assert program["kind"] == "rules"
        rule = program["rules"][0]
        assert rule["if"]["cmp"] == "=="
        assert rule["if"]["left"]["name"] == "CI_PIPELINE_SOURCE"
        assert rule["if"]["right"]["value"] == "merge_request_event"

    def test_final_bare_when_lints(self, dup):
        w = dup.annotations["whatif"]
        assert any(e["job"] == "lint_everything" for e in w["lint"])
        assert any("duplicate pipelines" in d.message for d in dup.diagnostics
                   if d.related_node == "lint_everything")

    def test_workflow_compiled(self, features):
        wf = features.annotations["whatif"]["workflow"]
        assert wf["name"] == "feature pipeline"
        assert wf["rules"][0]["when"] == "never"
        assert wf["rules"][0]["if"]["term"]["name"] == "CI_COMMIT_TAG"
        assert wf["rules"][1]["variables"] == {"FROM_WORKFLOW": "yes"}

    def test_exists_is_baked_at_generation_time(self, features):
        rules = whatif_of(features, "docker_build")["program"]["rules"]
        assert rules[0]["exists"]["result"] is True    # Dockerfile exists
        assert rules[1]["exists"]["result"] is False   # docker-compose.yml doesn't

    def test_changes_and_rule_variables(self, features):
        rules = whatif_of(features, "docs_check")["program"]["rules"]
        assert rules[0]["changes"]["paths"] == ["docs/**/*"]
        assert rules[0]["variables"] == {"DOCS_MODE": "strict"}
        assert rules[0]["allow_failure"] is True
        assert rules[1]["when"] == "delayed"
        assert rules[1]["start_in"] == "10 minutes"

    def test_legacy_only_except(self, features):
        program = whatif_of(features, "legacy_deploy")["program"]
        assert program["kind"] == "legacy"
        assert program["implicit_default"] is False
        assert program["only"]["refs"] == ["main", "/^release-.*/", "schedules"]
        assert program["only"]["variables"][0]["cmp"] == "=="
        assert program["except"]["refs"] == ["tags"]

    def test_gitlab_rejected_if_is_invalid_and_fatal(self, features):
        # 'CI_COMMIT_BRANCH === ...' is token junk GitLab itself rejects
        rules = whatif_of(features, "weird_rules")["program"]["rules"]
        assert rules[0]["if"]["op"] == "invalid"
        fatal = features.annotations["whatif"]["fatal"]
        assert any("weird_rules" in f["where"] for f in fatal)
        assert any("invalid expression syntax" in d.message
                   for d in features.diagnostics if d.severity == "error")

    def test_artifacts_and_dotenv(self, features):
        w = whatif_of(features, "build_meta")
        assert w["artifacts"]["paths"] == ["dist/"]
        assert w["artifacts"]["dotenv"] == ["build.env"]

    def test_needs_structure(self, features):
        needs = whatif_of(features, "integration")["needs"]
        assert needs[0] == {"job": "build_meta", "optional": False, "artifacts": True}
        assert needs[1] == {"job": "docker_build", "optional": True, "artifacts": False}

    def test_dependencies(self, features):
        assert whatif_of(features, "package")["dependencies"] == ["build_meta"]

    def test_trigger_child(self, features):
        trig = whatif_of(features, "trigger_child")["trigger"]
        assert trig["children"] == ["ci/child.yml"]
        assert trig["unresolved"] == []
        assert trig["forward"] == {"yaml_variables": True, "pipeline_variables": False}

    def test_child_jobs_marked(self, features):
        w = whatif_of(features, "ci/child.yml::child_mr_only")
        assert w["child_of"] == "ci/child.yml"
        assert w["name"] == "child_mr_only"

    def test_global_variables_captured(self, features):
        assert features.annotations["whatif"]["globals"]["DEPLOY_ENV"] == "staging"

    def test_templates_get_no_program(self):
        r = parse_gitlab(str(FIXTURES / "templates" / ".gitlab-ci.yml"))
        for node in r.nodes:
            if node.kind == "job" and "template" in node.flags:
                assert "whatif" not in node.annotations

    def test_child_workflow_captured(self, features):
        cw = features.annotations["whatif"]["child_workflows"]
        assert "ci/child.yml" in cw
        rule = cw["ci/child.yml"]["rules"][0]
        assert rule["if"]["right"]["value"] == "parent_pipeline"

    def test_child_globals_do_not_leak_into_parent(self, features):
        w = features.annotations["whatif"]
        assert "CHILD_ONLY" not in w["globals"]
        assert w["child_globals"]["ci/child.yml"]["CHILD_ONLY"] == "yes"

    def test_child_workflow_from_child_include(self):
        r = parse_gitlab(str(FIXTURES / "whatif_childwf" / ".gitlab-ci.yml"))
        w = r.annotations["whatif"]
        cw = w["child_workflows"]
        assert "ci/mronly.yml" in cw
        rule = cw["ci/mronly.yml"]["rules"][0]
        assert rule["if"]["right"]["value"] == "merge_request_event"
        # entry-file globals beat the child's include's globals
        assert w["child_globals"]["ci/mronly.yml"]["SHARED"] == "from-entry"

    def test_workflow_from_includes_merges_like_gitlab(self):
        r = parse_gitlab(str(FIXTURES / "whatif_wfinclude" / ".gitlab-ci.yml"))
        wf = r.annotations["whatif"]["workflow"]
        assert wf["name"] == "named pipeline"          # root name survives
        # last include wins for rules
        assert wf["rules"][0]["if"]["term"]["name"] == "CI_COMMIT_TAG"

    def test_unresolved_trigger_children_recorded(self):
        r = parse_gitlab(str(FIXTURES / "whatif_childwf" / ".gitlab-ci.yml"))
        trig = whatif_of(r, "trigger_remote")["trigger"]
        assert trig["children"] == []
        assert trig["unresolved"] and "other/proj" in trig["unresolved"][0]
        assert trig["forward"] == {"yaml_variables": True, "pipeline_variables": False}

    def test_trigger_forward_compiled(self):
        r = parse_gitlab(str(FIXTURES / "whatif_forward" / ".gitlab-ci.yml"))
        assert whatif_of(r, "trig_nofwd")["trigger"]["forward"]["yaml_variables"] is False
        assert whatif_of(r, "trig_default")["trigger"]["forward"]["yaml_variables"] is True

    def test_inherit_variables_compiled(self):
        r = parse_gitlab(str(FIXTURES / "whatif_nested" / ".gitlab-ci.yml"))
        assert whatif_of(r, "inherit_off")["inherit_variables"] is False
        assert "inherit_variables" not in whatif_of(r, "inherit_on")

    def test_matrix_combos_expand_in_axis_order(self):
        r = parse_gitlab(str(FIXTURES / "whatif_matrix" / ".gitlab-ci.yml"))
        par = whatif_of(r, "build")["parallel"]
        assert par["kind"] == "matrix"
        assert [c["name"] for c in par["combos"]] == \
            ["build: [x86, linux]", "build: [arm64, linux]"]
        assert par["combos"][0]["vars"] == {"ARCH": "x86", "OS": "linux"}
        # instance-name needs resolve to the parent definition, no ghost
        assert not any("test_x86" in d.message and "not defined" in d.message
                       for d in r.diagnostics)
        edges = [(e.src, e.dst) for e in r.edges if e.kind == "needs"]
        assert ("test_x86", "build") in edges

    def test_include_gate_compiled(self):
        r = parse_gitlab(str(FIXTURES / "whatif_incgate" / ".gitlab-ci.yml"))
        gate = whatif_of(r, "manual_cleanup")["include_gate"]
        assert gate[0]["if"]["right"]["value"] == "web"
        assert "include_gate" not in whatif_of(r, "always_job")

    def test_nested_rules_flatten_in_order(self):
        r = parse_gitlab(str(FIXTURES / "whatif_nested" / ".gitlab-ci.yml"))
        rules = whatif_of(r, "job_a")["program"]["rules"]
        assert [x["raw_if"] for x in rules] == \
            ["$MAIN_BUMPED", "$OTHER_FLAG", "$CI_COMMIT_BRANCH"]
        assert rules[0]["when"] == "never"
        # the display summary flattens too — no raw python repr strings
        display = r.node_by_id("job_a").annotations["rules"]
        assert len(display) == 3
        assert not any("[{" in s for s in display)

    def test_rules_needs_override_captured(self):
        _, _, err = parse_expression("$X")
        assert err is None  # sanity

        from pipeview.parsers.gitlab_whatif import _compile_needs
        assert _compile_needs(["a", {"job": "b", "optional": True}]) == [
            {"job": "a", "optional": False, "artifacts": True},
            {"job": "b", "optional": True, "artifacts": True},
        ]

    def test_braced_var_form_warns(self):
        _, notes, err = parse_expression('${FOO} == "x"')
        assert err is None
        assert any("${VAR}" in n for n in notes)

    def test_make_reports_have_no_whatif(self):
        from pipeview.parsers.make_parser import parse_makefile
        make_fixtures = Path(__file__).parent / "fixtures" / "make"
        r = parse_makefile(str(make_fixtures / "minimal" / "Makefile"))
        assert "whatif" not in r.annotations
