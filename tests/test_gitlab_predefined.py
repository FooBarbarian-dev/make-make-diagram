"""The predefined-variable documentation catalog: schema, honesty, coverage.

The catalog (pipeview/parsers/gitlab_predefined.py) is static data with three
contracts: entries are well-formed and offline-safe; every predefined name the
shipped templates mention (the What-If simulator's env matrix and the report
UI) has an entry, so simulator and docs cannot drift; and GitLab reports —
only GitLab reports — carry the catalog in their annotations.
"""

import re
from pathlib import Path

import pytest

from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_predefined import PREDEFINED_VAR_DOCS
from pipeview.parsers.make_parser import parse_makefile

TEMPLATES = Path(__file__).parent.parent / "pipeview" / "render" / "templates"
GITLAB_FIXTURES = Path(__file__).parent / "fixtures" / "gitlab"
MAKE_FIXTURES = Path(__file__).parent / "fixtures" / "make"

_REQUIRED_FIELDS = {"summary", "example", "set_when"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"unset_when", "note"}

# Segment-based on purpose: a trailing underscore (prefix strings like
# "CI_MERGE_REQUEST_" in whatif.js) kills the word boundary, so pure
# prefixes are never extracted as names.
_NAME_IN_TEMPLATE_RE = re.compile(r"\b(?:CI|GITLAB)_[A-Z]+(?:_[A-Z]+)*\b")


class TestCatalogSchema:
    def test_names_look_predefined(self):
        for name in PREDEFINED_VAR_DOCS:
            assert name == "CI" or re.fullmatch(r"(CI|GITLAB)_[A-Z0-9_]+", name), name

    def test_entries_are_well_formed(self):
        for name, entry in PREDEFINED_VAR_DOCS.items():
            assert _REQUIRED_FIELDS <= set(entry), f"{name}: missing required fields"
            assert set(entry) <= _ALLOWED_FIELDS, f"{name}: unknown fields"
            for field, value in entry.items():
                assert isinstance(value, str) and value.strip(), f"{name}.{field}"
            # the UI renders summaries in tooltips — keep them one-sentence-ish
            assert len(entry["summary"]) <= 220, f"{name}: summary too long"

    def test_offline_safe(self):
        # Doc text must not smuggle links into offline reports; URL-shaped
        # examples stay scheme-less.
        for name, entry in PREDEFINED_VAR_DOCS.items():
            for field, value in entry.items():
                assert "http://" not in value and "https://" not in value, (
                    f"{name}.{field} contains a URL scheme"
                )


class TestTemplateCoverage:
    """Every predefined name the shipped templates mention has an entry —
    when the simulator grows a new variable, the catalog must too."""

    @pytest.mark.parametrize("template", ["whatif.js", "report.html"])
    def test_template_names_are_documented(self, template):
        source = (TEMPLATES / template).read_text(encoding="utf-8")
        found = sorted(set(_NAME_IN_TEMPLATE_RE.findall(source)))
        assert found, f"expected predefined names in {template}"
        missing = [n for n in found if n not in PREDEFINED_VAR_DOCS]
        assert missing == [], f"{template} mentions undocumented variables: {missing}"


class TestReportEmbedding:
    def test_gitlab_report_carries_the_catalog(self):
        report = parse_gitlab(str(GITLAB_FIXTURES / "minimal" / ".gitlab-ci.yml"))
        assert report.annotations["predefined_var_docs"] == PREDEFINED_VAR_DOCS

    def test_make_report_does_not(self):
        report = parse_makefile(str(MAKE_FIXTURES / "minimal" / "Makefile"))
        assert "predefined_var_docs" not in report.annotations
