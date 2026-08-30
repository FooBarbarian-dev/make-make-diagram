"""The GitHub predefined-variable documentation catalog: schema, honesty.

The catalog (pipeview/parsers/github_predefined.py) is static data with the
same contracts as its GitLab twin: entries are well-formed and offline-safe,
and GitHub reports — only GitHub reports — carry the catalog in their
annotations (pinned in test_github_parser.py once the parser lands).
"""

import re

from pipeview.parsers.github_predefined import (
    CONTEXT_FIELD_DOCS,
    PREDEFINED_VAR_DOCS,
)

_REQUIRED_FIELDS = {"summary", "example", "set_when"}
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"unset_when", "note"}


class TestCatalogSchema:
    def test_env_names_look_predefined(self):
        for name in PREDEFINED_VAR_DOCS:
            assert name == "CI" or re.fullmatch(
                r"(GITHUB|RUNNER)_[A-Z0-9_]+", name
            ), name

    def test_context_names_look_like_context_paths(self):
        for name in CONTEXT_FIELD_DOCS:
            assert re.fullmatch(r"github(\.[a-z_]+)+", name), name

    def test_entries_are_well_formed(self):
        for catalog in (PREDEFINED_VAR_DOCS, CONTEXT_FIELD_DOCS):
            for name, entry in catalog.items():
                assert _REQUIRED_FIELDS <= set(entry), f"{name}: missing required fields"
                assert set(entry) <= _ALLOWED_FIELDS, f"{name}: unknown fields"
                for field, value in entry.items():
                    assert isinstance(value, str) and value.strip(), f"{name}.{field}"
                # the UI renders summaries in tooltips — keep them one-sentence-ish
                assert len(entry["summary"]) <= 220, f"{name}: summary too long"

    def test_offline_safe(self):
        # Doc text must not smuggle links into offline reports; URL-shaped
        # examples stay scheme-less.
        for catalog in (PREDEFINED_VAR_DOCS, CONTEXT_FIELD_DOCS):
            for name, entry in catalog.items():
                for field, value in entry.items():
                    assert "http://" not in value and "https://" not in value, (
                        f"{name}.{field} contains a URL scheme"
                    )

    def test_env_and_context_twins_stay_consistent(self):
        # Where an env var mirrors a context field, the documented example
        # must agree — the two render side by side in tooltips.
        twins = {
            "GITHUB_EVENT_NAME": "github.event_name",
            "GITHUB_REF_TYPE": "github.ref_type",
            "GITHUB_BASE_REF": "github.base_ref",
            "GITHUB_HEAD_REF": "github.head_ref",
            "GITHUB_SHA": "github.sha",
        }
        for env_name, ctx_name in twins.items():
            assert PREDEFINED_VAR_DOCS[env_name]["example"] == \
                CONTEXT_FIELD_DOCS[ctx_name]["example"], (env_name, ctx_name)
