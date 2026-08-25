"""Tests for the bundled GitLab template snapshot (pipeview.gitlab_templates)
and its offline use by the parser and CLI.

The snapshot is real GitLab content (lib/gitlab/ci/templates at a pinned
release), so assertions stick to facts stable across GitLab versions: the
files exist, Jobs/Build defines a job named "build", Security/SAST includes
Jobs/SAST.
"""

from __future__ import annotations

import os

from pipeview import gitlab_templates
from pipeview.cli import main as cli_main
from pipeview.parsers.gitlab_parser import parse_gitlab


class TestBundledStore:
    def test_snapshot_is_present_and_plausible(self):
        names = gitlab_templates.template_names()
        assert len(names) > 100
        assert "Jobs/Build.gitlab-ci.yml" in names
        assert "Security/SAST.gitlab-ci.yml" in names
        assert os.path.isfile(os.path.join(gitlab_templates.bundled_root(),
                                           "LICENSE"))

    def test_meta_records_provenance(self):
        meta = gitlab_templates.bundled_meta()
        assert meta.get("gitlab_version")
        assert meta.get("ref")
        assert meta.get("source", "").startswith("https://")
        assert meta.get("template_count") == len(gitlab_templates.template_names())
        assert meta["gitlab_version"] in gitlab_templates.bundled_version()

    def test_template_path_lookup(self):
        path = gitlab_templates.template_path("Jobs/Build.gitlab-ci.yml")
        assert path and os.path.isfile(path)
        # Tolerated spellings: missing suffix, leading slash.
        assert gitlab_templates.template_path("Jobs/Build") == path
        assert gitlab_templates.template_path("/Jobs/Build.gitlab-ci.yml") == path
        assert gitlab_templates.template_path("No/Such.gitlab-ci.yml") is None

    def test_template_path_never_escapes_the_snapshot(self):
        assert gitlab_templates.template_path("../gitlab_templates.py") is None
        assert gitlab_templates.template_path("../../etc/passwd") is None
        assert gitlab_templates.template_path("/etc/passwd") is None
        assert gitlab_templates.template_path("") is None
        assert gitlab_templates.template_path(".") is None


class TestOfflineParsing:
    def _write_root(self, tmpdir, *includes: str) -> str:
        inc = "".join(f"  - template: {name}\n" for name in includes)
        path = tmpdir.join(".gitlab-ci.yml")
        path.write(f"include:\n{inc}own_job:\n  script: [echo hi]\n")
        return str(path)

    def test_template_include_resolves_offline(self, tmpdir):
        root = self._write_root(tmpdir, "Jobs/Build.gitlab-ci.yml")
        report = parse_gitlab(root)
        by_name = {n.name: n for n in report.nodes if n.kind == "job"}
        assert "own_job" in by_name
        assert "build" in by_name              # defined by the bundled template
        assert by_name["build"].source.file == "[template] Jobs/Build.gitlab-ci.yml"
        assert "[template] Jobs/Build.gitlab-ci.yml" in \
            {f.path for f in report.files if f.status == "ok"}
        assert not [f for f in report.files if f.status == "unresolved"]
        assert any(d.severity == "info" and "bundled" in d.message
                   for d in report.diagnostics)

    def test_template_chain_recurses(self, tmpdir):
        # Security/SAST.gitlab-ci.yml is a stub including Jobs/SAST.gitlab-ci.yml.
        root = self._write_root(tmpdir, "Security/SAST.gitlab-ci.yml")
        report = parse_gitlab(root)
        paths = {f.path for f in report.files}
        assert "[template] Security/SAST.gitlab-ci.yml" in paths
        assert "[template] Jobs/SAST.gitlab-ci.yml" in paths
        edges = {(e.src, e.dst) for e in report.edges if e.kind == "includes"}
        assert ("[template] Security/SAST.gitlab-ci.yml",
                "[template] Jobs/SAST.gitlab-ci.yml") in edges

    def test_opt_out_restores_ghosting(self, tmpdir):
        root = self._write_root(tmpdir, "Jobs/Build.gitlab-ci.yml")
        report = parse_gitlab(root, bundled_templates=False)
        assert "[template:Jobs/Build.gitlab-ci.yml]" in \
            {f.path for f in report.files if f.status == "unresolved"}
        assert any("never fetches remote content" in d.message
                   for d in report.diagnostics)

    def test_unknown_template_diagnostic_names_the_snapshot(self, tmpdir):
        root = self._write_root(tmpdir, "No/Such.gitlab-ci.yml")
        report = parse_gitlab(root)
        assert any(d.severity == "warning"
                   and "not in pipeview's bundled" in d.message
                   for d in report.diagnostics)


class TestCliFlag:
    def test_no_bundled_templates_flag(self, tmpdir, capsys):
        ci = tmpdir.join(".gitlab-ci.yml")
        ci.write("include:\n  - template: Jobs/Build.gitlab-ci.yml\n"
                 "own_job:\n  script: [x]\n")
        out_on = tmpdir.join("on")
        out_off = tmpdir.join("off")
        assert cli_main([str(ci), "-o", str(out_on), "--format", "json"]) == 0
        # Opting out leaves the include unresolved -> warning -> exit 1.
        assert cli_main([str(ci), "-o", str(out_off), "--format", "json",
                         "--no-bundled-templates"]) == 1
        with open(os.path.join(str(out_on), "gitlab-ci.model.json")) as f:
            on_data = f.read()
        with open(os.path.join(str(out_off), "gitlab-ci.model.json")) as f:
            off_data = f.read()
        assert "[template] Jobs/Build.gitlab-ci.yml" in on_data
        assert "[template:Jobs/Build.gitlab-ci.yml]" in off_data
