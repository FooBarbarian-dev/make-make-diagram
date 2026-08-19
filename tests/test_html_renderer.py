import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from pipeview.model import Report
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.make_parser import parse_makefile
from pipeview.render.html import render_html

MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"
EXAMPLES = Path(__file__).parent.parent / "examples"


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


class TestHtmlGeneration:
    def test_generates_file(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(make_report, path)
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 1000

    def test_contains_model_json(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(make_report, path)
        with open(path) as f:
            content = f.read()
        assert '"schema_version"' in content
        assert '"nodes"' in content

    def test_whatif_evaluator_inlined(self, gitlab_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(gitlab_report, path)
        with open(path) as f:
            content = f.read()
        assert "PipeviewWhatIf" in content
        assert "/*WHATIF_PLACEHOLDER*/" not in content
        assert '"whatif"' in content   # gitlab reports carry the program

    def test_json_round_trips_from_html(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(make_report, path)
        with open(path) as f:
            content = f.read()
        m = re.search(r"const REPORT = (.+?);\s*\n", content, re.DOTALL)
        assert m is not None
        data = json.loads(m.group(1))
        restored = Report.from_dict(data)
        assert len(restored.nodes) == len(make_report.nodes)

    def test_contains_dagre(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(make_report, path)
        with open(path) as f:
            content = f.read()
        assert "dagre" in content.lower()

    def test_gitlab_report(self, gitlab_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(gitlab_report, path)
        assert os.path.isfile(path)
        with open(path) as f:
            content = f.read()
        assert "build_job" in content


class TestNoNetworkResources:
    """MANDATORY: Generated HTML must never reference external resources."""

    _URL_PATTERN = re.compile(
        r'(?:src|href|url\()\s*[=\(]?\s*["\']?(https?://[^"\'>\s\)]+)',
        re.IGNORECASE,
    )

    _FETCH_PATTERN = re.compile(
        r'(?:fetch|XMLHttpRequest|\.open)\s*\(\s*["\']https?://',
        re.IGNORECASE,
    )

    _RESOURCE_URL_PATTERN = re.compile(
        r'(src|href|action|data|poster|srcset)\s*=\s*["\']https?://[^"\']+["\']',
        re.IGNORECASE,
    )

    def test_no_external_resources_make(self, make_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(make_report, path)
        self._assert_no_network_refs(path)

    def test_no_external_resources_gitlab(self, gitlab_report, tmpdir):
        path = os.path.join(tmpdir, "report.html")
        render_html(gitlab_report, path)
        self._assert_no_network_refs(path)

    def test_no_external_resources_empty_report(self, tmpdir):
        report = Report(root="test", format="makefile")
        path = os.path.join(tmpdir, "report.html")
        render_html(report, path)
        self._assert_no_network_refs(path)

    def test_no_external_resources_make_example(self, tmpdir):
        report = parse_makefile(str(EXAMPLES / "make-project" / "Makefile"))
        path = os.path.join(tmpdir, "report.html")
        render_html(report, path)
        self._assert_no_network_refs(path)

    def test_no_external_resources_gitlab_example(self, tmpdir):
        report = parse_gitlab(str(EXAMPLES / "gitlab-project" / ".gitlab-ci.yml"))
        path = os.path.join(tmpdir, "report.html")
        render_html(report, path)
        self._assert_no_network_refs(path)

    def test_no_external_resources_torture_example(self, tmpdir):
        report = parse_makefile(str(EXAMPLES / "torture-project" / "Makefile"))
        path = os.path.join(tmpdir, "report.html")
        render_html(report, path)
        self._assert_no_network_refs(path)

    def _assert_no_network_refs(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        violations = []

        for m in self._RESOURCE_URL_PATTERN.finditer(content):
            violations.append(f"Resource attribute with URL: {m.group()[:100]}")

        for m in self._FETCH_PATTERN.finditer(content):
            violations.append(f"Network fetch call: {m.group()[:100]}")

        for line_no, line in enumerate(content.splitlines(), 1):
            if re.search(r'<link[^>]+href\s*=\s*["\']https?://', line, re.IGNORECASE):
                violations.append(f"Line {line_no}: External stylesheet: {line.strip()[:100]}")
            if re.search(r'<script[^>]+src\s*=\s*["\']https?://', line, re.IGNORECASE):
                violations.append(f"Line {line_no}: External script: {line.strip()[:100]}")
            if re.search(r'<img[^>]+src\s*=\s*["\']https?://', line, re.IGNORECASE):
                violations.append(f"Line {line_no}: External image: {line.strip()[:100]}")
            if re.search(r'@import\s+url\(["\']?https?://', line, re.IGNORECASE):
                violations.append(f"Line {line_no}: CSS @import: {line.strip()[:100]}")
            if re.search(r'@font-face[^}]*src:\s*url\(["\']?https?://', line, re.IGNORECASE):
                violations.append(f"Line {line_no}: Remote font: {line.strip()[:100]}")

        assert violations == [], (
            "Generated HTML contains external resource references "
            "(offline constraint violated):\n" + "\n".join(violations)
        )


class TestTortureExample:
    """The overflow torture fixture must parse and carry its long tokens
    through to the rendered report (the visual overflow check itself runs
    in a browser; see docs/ux-audit.md)."""

    def test_torture_report_renders_long_tokens(self, tmpdir):
        report = parse_makefile(str(EXAMPLES / "torture-project" / "Makefile"))
        long_values = [
            e.raw_value for var in report.variables for e in var.events
            if len(e.raw_value) >= 200
        ]
        assert long_values, "expected a 200-char variable value in the fixture"
        long_recipes = [
            line for n in report.nodes for line in n.recipe if len(line) >= 300
        ]
        assert long_recipes, "expected a 300-char one-line recipe in the fixture"

        path = os.path.join(tmpdir, "report.html")
        render_html(report, path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert long_values[0] in content
        assert "torture-build" in content
