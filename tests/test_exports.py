import json
import os
import tempfile
from pathlib import Path

import pytest

from pipeview.model import SCHEMA_VERSION
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.make_parser import parse_makefile
from pipeview.render.exports import export_dot, export_json, export_mermaid, export_svg

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"


@pytest.fixture
def make_report():
    return parse_makefile(str(MAKE_FIXTURES / "minimal" / "Makefile"))


@pytest.fixture
def gitlab_report():
    return parse_gitlab(str(GITLAB_FIXTURES / "minimal" / ".gitlab-ci.yml"))


@pytest.fixture
def tmpdir():
    with tempfile.TemporaryDirectory() as d:
        yield d


class TestJsonExport:
    def test_json_valid(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "model.json")
        export_json(make_report, path)
        with open(path) as f:
            data = json.load(f)
        assert data["schema_version"] == SCHEMA_VERSION
        assert len(data["nodes"]) > 0

    def test_json_round_trips(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "model.json")
        export_json(make_report, path)
        from pipeview.model import Report
        with open(path) as f:
            restored = Report.from_dict(json.load(f))
        assert len(restored.nodes) == len(make_report.nodes)


class TestDotExport:
    def test_dot_structure(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "graph.dot")
        export_dot(make_report, path)
        with open(path) as f:
            content = f.read()
        assert content.startswith("digraph pipeline")
        assert "}" in content
        assert "all" in content

    def test_dot_edge_styles(self, tmpdir):
        report = parse_makefile(str(MAKE_FIXTURES / "order_only" / "Makefile"))
        path = os.path.join(tmpdir, "graph.dot")
        export_dot(report, path)
        with open(path) as f:
            content = f.read()
        assert "dashed" in content

    def test_dot_ghost_nodes(self, tmpdir):
        # minimal's main.c/utils.c are referenced but never defined as
        # targets, so they stay ghosts and must render dashed. (The broken
        # fixture no longer has ghosts: its `foo` is defined by a later rule,
        # which now correctly upgrades the node from ghost to target.)
        report = parse_makefile(str(MAKE_FIXTURES / "minimal" / "Makefile"))
        assert any(n.kind == "ghost" for n in report.nodes)
        path = os.path.join(tmpdir, "graph.dot")
        export_dot(report, path)
        with open(path) as f:
            content = f.read()
        assert "dashed" in content


class TestMermaidExport:
    def test_mermaid_structure(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "graph.mmd")
        export_mermaid(make_report, path)
        with open(path) as f:
            content = f.read()
        assert content.startswith("flowchart LR")
        assert "all" in content

    def test_mermaid_gitlab(self, gitlab_report, tmpdir):
        path = os.path.join(tmpdir, "graph.mmd")
        export_mermaid(gitlab_report, path)
        with open(path) as f:
            content = f.read()
        assert "build_job" in content

    def test_mermaid_ids_and_labels_are_parseable(self, tmpdir):
        # Make graphs carry ids like `$(OBJS)` and `%.o`; bare, either one
        # is a mermaid parse error. Ids must flatten to [0-9A-Za-z_] and
        # every label must be quoted.
        import re
        repo_root = Path(__file__).parent.parent
        report = parse_makefile(str(repo_root / "examples" / "make-project"
                                    / "Makefile"))
        assert any("$(" in n.id or "%" in n.id for n in report.nodes)
        path = os.path.join(tmpdir, "graph.mmd")
        export_mermaid(report, path)
        with open(path) as f:
            lines = f.read().splitlines()
        node_re = re.compile(
            r'^  (\w+)(\[|\{\{|\(\[)"[^"]*"(\]|\}\}|\]\))$')
        node_lines = [ln for ln in lines[1:]
                      if not ln.startswith(("  style", "  subgraph"))
                      and "-->" not in ln and ".->" not in ln and ln.strip()]
        assert node_lines
        for ln in node_lines:
            assert node_re.match(ln), f"unparseable mermaid node line: {ln!r}"


class TestSvgExport:
    def test_svg_valid(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "graph.svg")
        export_svg(make_report, path)
        with open(path) as f:
            content = f.read()
        assert '<svg xmlns="http://www.w3.org/2000/svg"' in content
        assert "</svg>" in content

    def test_svg_contains_nodes(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "graph.svg")
        export_svg(make_report, path)
        with open(path) as f:
            content = f.read()
        assert "all" in content

    def test_svg_gitlab(self, gitlab_report, tmpdir):
        path = os.path.join(tmpdir, "graph.svg")
        export_svg(gitlab_report, path)
        with open(path) as f:
            content = f.read()
        assert "</svg>" in content
