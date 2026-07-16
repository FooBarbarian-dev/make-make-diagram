import json
import os
import re
import tempfile
from pathlib import Path

import pytest

from pipeview.parsers.make_parser import parse_makefile
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.render.html import render_html
from pipeview.model import Report

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
