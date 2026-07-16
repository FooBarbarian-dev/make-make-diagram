import os
import json
import pytest
from pathlib import Path

from pipeview.parsers.make_parser import parse_makefile

FIXTURES = Path(__file__).parent / "fixtures" / "make"


class TestMinimal:
    def test_parses_targets(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        names = {n.name for n in r.nodes}
        assert "all" in names
        assert "clean" in names
        assert "main.o" in names
        assert "utils.o" in names

    def test_prerequisite_edges(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        edges = [(e.src, e.dst, e.kind) for e in r.edges]
        assert ("all", "main.o", "prerequisite") in edges
        assert ("all", "utils.o", "prerequisite") in edges

    def test_phony_flags(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        all_node = r.node_by_id("all")
        assert all_node is not None
        assert "phony" in all_node.flags
        clean_node = r.node_by_id("clean")
        assert clean_node is not None
        assert "phony" in clean_node.flags

    def test_default_goal(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        assert r.default_goal == "all"
        all_node = r.node_by_id("all")
        assert "default_goal" in all_node.flags

    def test_recipes_captured(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        all_node = r.node_by_id("all")
        assert len(all_node.recipe) == 1
        assert "gcc -o app" in all_node.recipe[0]

    def test_docstrings(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        all_node = r.node_by_id("all")
        assert all_node.doc == "Build everything"
        clean_node = r.node_by_id("clean")
        assert clean_node.doc == "Remove build artifacts"

    def test_source_locations(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        all_node = r.node_by_id("all")
        assert all_node.source is not None
        assert all_node.source.line == 2

    def test_source_files(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        paths = [f.path for f in r.files]
        assert any("Makefile" in p for p in paths)


class TestVariables:
    def test_all_operators(self):
        r = parse_makefile(str(FIXTURES / "variables" / "Makefile"))
        var_ops = {v.name: [e.operator for e in v.events] for v in r.variables}
        assert var_ops["CC"] == [":="]
        assert var_ops["CFLAGS"] == ["="]
        assert var_ops["LDFLAGS"] == ["?="]
        assert var_ops["SRCS"] == ["+="]
        assert var_ops["RESULT"] == ["!="]

    def test_target_specific_var(self):
        r = parse_makefile(str(FIXTURES / "variables" / "Makefile"))
        bar_var = next((v for v in r.variables if v.name == "BAR"), None)
        assert bar_var is not None
        assert any(e.scope == "foo" for e in bar_var.events)

    def test_define_block(self):
        r = parse_makefile(str(FIXTURES / "variables" / "Makefile"))
        ml = next((v for v in r.variables if v.name == "MULTI_LINE"), None)
        assert ml is not None
        assert "line1" in ml.events[0].raw_value
        assert "line2" in ml.events[0].raw_value

    def test_variable_usage_tracking(self):
        r = parse_makefile(str(FIXTURES / "variables" / "Makefile"))
        cc = next(v for v in r.variables if v.name == "CC")
        assert len(cc.used_by) > 0


class TestPatternRules:
    def test_pattern_rule_kind(self):
        r = parse_makefile(str(FIXTURES / "pattern_rules" / "Makefile"))
        pat_nodes = [n for n in r.nodes if n.kind == "pattern_rule"]
        pat_names = {n.name for n in pat_nodes}
        assert "%.o" in pat_names
        assert "%.d" in pat_names

    def test_pattern_rule_deps(self):
        r = parse_makefile(str(FIXTURES / "pattern_rules" / "Makefile"))
        edges = [(e.src, e.dst, e.kind) for e in r.edges]
        assert ("%.o", "%.c", "prerequisite") in edges


class TestOrderOnly:
    def test_order_only_edges(self):
        r = parse_makefile(str(FIXTURES / "order_only" / "Makefile"))
        oo_edges = [e for e in r.edges if e.kind == "order_only"]
        assert len(oo_edges) >= 2
        dsts = {e.dst for e in oo_edges}
        assert "builddir" in dsts

    def test_normal_and_order_only_separated(self):
        r = parse_makefile(str(FIXTURES / "order_only" / "Makefile"))
        all_edges = [e for e in r.edges if e.src == "all"]
        kinds = {e.kind for e in all_edges}
        assert "prerequisite" in kinds
        assert "order_only" in kinds


class TestConditionals:
    def test_conditional_annotations(self):
        r = parse_makefile(str(FIXTURES / "conditionals" / "Makefile"))
        cflags = next((v for v in r.variables if v.name == "CFLAGS"), None)
        assert cflags is not None
        assert len(cflags.events) >= 2

    def test_conditional_diagnostics(self):
        r = parse_makefile(str(FIXTURES / "conditionals" / "Makefile"))
        cond_diags = [
            d for d in r.diagnostics
            if "condition" in d.message.lower()
        ]
        assert len(cond_diags) > 0


class TestDocstrings:
    def test_above_target_doc(self):
        r = parse_makefile(str(FIXTURES / "docstrings" / "Makefile"))
        build = r.node_by_id("build")
        assert build is not None
        assert build.doc == "Build the project"
        test = r.node_by_id("test")
        assert test is not None
        assert test.doc == "Run unit tests"

    def test_inline_doc(self):
        r = parse_makefile(str(FIXTURES / "docstrings" / "Makefile"))
        deploy = r.node_by_id("deploy")
        assert deploy is not None
        assert deploy.doc == "Deploy to production"


class TestIncludeChain:
    def test_includes_resolved(self):
        r = parse_makefile(str(FIXTURES / "include_chain" / "Makefile"))
        paths = [f.path for f in r.files]
        assert any("config.mk" in p for p in paths)
        assert any("vars.mk" in p for p in paths)

    def test_missing_optional_include(self):
        r = parse_makefile(str(FIXTURES / "include_chain" / "Makefile"))
        diags = [d for d in r.diagnostics if "missing.mk" in d.message.lower()]
        assert len(diags) >= 1
        assert diags[0].severity == "info"

    def test_include_edges(self):
        r = parse_makefile(str(FIXTURES / "include_chain" / "Makefile"))
        inc_edges = [e for e in r.edges if e.kind == "includes"]
        assert len(inc_edges) >= 2

    def test_variables_from_includes(self):
        r = parse_makefile(str(FIXTURES / "include_chain" / "Makefile"))
        var_names = {v.name for v in r.variables}
        assert "CC" in var_names
        assert "VERSION" in var_names
        assert "LIBDIR" in var_names


class TestRecursiveTree:
    def test_recursive_invocation(self):
        r = parse_makefile(str(FIXTURES / "recursive_tree" / "Makefile"))
        inv_edges = [e for e in r.edges if e.kind == "invokes"]
        assert len(inv_edges) >= 2

    def test_sub_targets_namespaced(self):
        r = parse_makefile(str(FIXTURES / "recursive_tree" / "Makefile"))
        ids = {n.id for n in r.nodes}
        assert any("sub:" in nid or "sub" in nid for nid in ids)

    def test_sub_targets_present(self):
        r = parse_makefile(str(FIXTURES / "recursive_tree" / "Makefile"))
        names = {n.name for n in r.nodes}
        assert "sub_target" in names or any("sub_target" in n for n in names)


class TestBroken:
    def test_broken_produces_report(self):
        r = parse_makefile(str(FIXTURES / "broken" / "Makefile"))
        assert r is not None
        assert len(r.nodes) > 0

    def test_broken_has_diagnostics(self):
        r = parse_makefile(str(FIXTURES / "broken" / "Makefile"))
        assert len(r.diagnostics) > 0
        sevs = {d.severity for d in r.diagnostics}
        assert "warning" in sevs

    def test_valid_parts_still_parsed(self):
        r = parse_makefile(str(FIXTURES / "broken" / "Makefile"))
        assert r.node_by_id("all") is not None
        assert r.node_by_id("foo") is not None


class TestJsonSnapshot:
    def test_serialization_round_trip(self):
        r = parse_makefile(str(FIXTURES / "minimal" / "Makefile"))
        js = r.to_json(indent=2)
        restored = r.from_json(js)
        assert restored.to_json(indent=2) == js
