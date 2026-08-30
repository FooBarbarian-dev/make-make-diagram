"""GitHub what-if compiler: expression grammar, filter patterns, lints."""

from pipeview.parsers.github_whatif import (
    collect_ctx_paths,
    match_pattern_list,
    parse_condition,
    pattern_to_regex,
    uses_status_function,
)


class TestExpressionParser:
    def test_simple_comparison(self):
        ast, notes, err = parse_condition("github.ref == 'refs/heads/main'")
        assert err is None and notes == []
        assert ast == {"op": "cmp", "cmp": "==",
                       "left": {"t": "ctx", "path": "github.ref"},
                       "right": {"t": "lit", "value": "refs/heads/main"}}

    def test_boolean_yaml_value(self):
        ast, _, err = parse_condition(True)
        assert err is None
        assert ast == {"t": "lit", "value": True}

    def test_context_paths_lowercased(self):
        ast, _, err = parse_condition("GitHub.Event_Name == 'push'")
        assert err is None
        assert ast["left"]["path"] == "github.event_name"

    def test_wrapped_template_unwraps(self):
        ast, notes, err = parse_condition("${{ github.ref_type == 'tag' }}")
        assert err is None and notes == []
        assert ast["op"] == "cmp"

    def test_template_mixed_with_text_is_opaque_with_lint(self):
        ast, notes, err = parse_condition("${{ false }} || true")
        assert ast["op"] == "opaque"
        assert any("truthy" in n for n in notes)

    def test_unknown_function_is_invalid(self):
        ast, _, err = parse_condition("exists('x')")
        assert ast["op"] == "invalid"
        assert "unknown function" in err

    def test_double_quotes_invalid(self):
        ast, _, err = parse_condition('github.ref == "main"')
        assert ast["op"] == "invalid"

    def test_precedence_and_over_or(self):
        ast, _, err = parse_condition("a.x == 1 || b.y == 2 && c.z == 3")
        assert err is None
        assert ast["op"] == "or"
        assert ast["args"][1]["op"] == "and"

    def test_not_binds_tighter_than_cmp_chain(self):
        ast, _, err = parse_condition("!github.event.pull_request.draft")
        assert err is None
        assert ast["op"] == "not"

    def test_call_with_nested_args(self):
        ast, _, err = parse_condition(
            "contains(fromJSON('[\"a\"]'), github.ref_name)")
        assert err is None
        assert ast == {"op": "call", "fn": "contains", "args": [
            {"op": "call", "fn": "fromjson",
             "args": [{"t": "lit", "value": '["a"]'}]},
            {"t": "ctx", "path": "github.ref_name"},
        ]}

    def test_index_access_folds_literals(self):
        ast, _, err = parse_condition(
            "github.event.commits[0].message == 'x'")
        assert err is None
        assert ast["left"]["path"] == "github.event.commits.0.message"

    def test_dynamic_index_marks_term(self):
        ast, _, err = parse_condition(
            "github.event.commits[github.run_number].id == 'x'")
        assert err is None
        assert ast["left"].get("dynamic") is True

    def test_single_quote_escape(self):
        ast, _, err = parse_condition("github.ref_name == 'it''s'")
        assert err is None
        assert ast["right"]["value"] == "it's"

    def test_collect_ctx_paths(self):
        ast, _, _ = parse_condition(
            "github.ref == 'x' && (vars.A != '' || secrets.B)")
        assert collect_ctx_paths(ast) == ["github.ref", "vars.a",
                                          "secrets.b"]

    def test_uses_status_function(self):
        ast, _, _ = parse_condition("always() && vars.X == '1'")
        assert uses_status_function(ast)
        ast2, _, _ = parse_condition("vars.X == '1'")
        assert not uses_status_function(ast2)


class TestFilterPatterns:
    def test_star_does_not_cross_slash(self):
        rx = pattern_to_regex("feature/*")
        assert rx.match("feature/a")
        assert not rx.match("feature/a/b")

    def test_double_star_crosses_slash(self):
        rx = pattern_to_regex("feature/**")
        assert rx.match("feature/a/b")

    def test_plus_and_question_quantify_preceding(self):
        rx = pattern_to_regex("v[0-9]+.[0-9]+")
        assert rx.match("v12.3")
        assert not rx.match("v.3")
        rx2 = pattern_to_regex("va?")
        assert rx2.match("v") and rx2.match("va")

    def test_escaped_special(self):
        rx = pattern_to_regex("release\\+hotfix")
        assert rx.match("release+hotfix")

    def test_ordered_negation_last_match_wins(self):
        pats = ["releases/**", "!releases/**-alpha"]
        assert match_pattern_list("releases/1.0", pats) is True
        assert match_pattern_list("releases/1.0-alpha", pats) is False

    def test_reinclude_after_negation(self):
        pats = ["releases/**", "!releases/**-alpha",
                "releases/keep-alpha"]
        assert match_pattern_list("releases/keep-alpha", pats) is True

    def test_only_negatives_never_match(self):
        assert match_pattern_list("main", ["!dev"]) is False
