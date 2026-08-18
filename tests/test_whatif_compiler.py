"""The what-if compiler: expression parsing, program compilation, exists
baking, artifact extraction, and lint — the Python half of the simulator."""

from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_whatif import glob_to_regex, parse_expression

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

    def test_parse_error_degrades_to_opaque(self):
        ast, _, err = parse_expression('CI_COMMIT_BRANCH === "oops"')
        assert err is not None
        assert ast["op"] == "opaque"
        assert "CI_COMMIT_BRANCH" in ast["src"]

    def test_unterminated_string_is_opaque(self):
        ast, _, err = parse_expression('$A == "unclosed')
        assert err is not None
        assert ast["op"] == "opaque"


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
    ])
    def test_patterns(self, pattern, path, matches):
        rx = glob_to_regex(pattern)
        assert rx is not None
        assert bool(rx.match(path)) is matches


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

    def test_unparseable_if_becomes_opaque_with_diagnostic(self, features):
        rules = whatif_of(features, "weird_rules")["program"]["rules"]
        assert rules[0]["if"]["op"] == "opaque"
        assert any("weird_rules" in d.message and "unknown" in d.message
                   for d in features.diagnostics if d.severity == "warning")

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
        assert whatif_of(features, "trigger_child")["trigger"] == \
            {"children": ["ci/child.yml"]}

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

    def test_make_reports_have_no_whatif(self):
        from pipeview.parsers.make_parser import parse_makefile
        make_fixtures = Path(__file__).parent / "fixtures" / "make"
        r = parse_makefile(str(make_fixtures / "minimal" / "Makefile"))
        assert "whatif" not in r.annotations
