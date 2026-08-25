"""Tests for the pipeview.gitlab remote-fetch feature.

Everything runs against FakeGitLab — no test ever touches a network.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from pipeview.gitlab import auth as auth_mod
from pipeview.gitlab import cli as gitlab_cli
from pipeview.gitlab.api import GitLabError, GitLabForbidden, GitLabNotFound
from pipeview.gitlab.config import GitLabConfig
from pipeview.gitlab.fetch import (
    _wildcard_regex,
    fetch_config,
    include_keys,
    materialize,
)
from pipeview.gitlab.report import generate_report
from pipeview.gitlab.tui import order_projects, order_refs, truncate, visible_window

HOST = "https://gitlab.example.com"


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------

class FakeGitLab:
    """Implements the GitLabClient surface the feature consumes."""

    def __init__(self):
        self.base_url = HOST
        self.projects: dict[str, dict] = {}
        # (project_path, ref) -> {file_path: content}
        self.trees: dict[tuple[str, str], dict[str, str]] = {}
        self.lint_responses: dict[str, dict] = {}      # project_path -> response
        self.lint_error: Exception | None = None
        self.templates: dict[str, str] = {}
        self.remote_files: dict[str, str] = {}
        self.tags: dict[str, list[str]] = {}
        self.calls: list[tuple] = []

    # -- helpers to build scenarios
    def add_project(self, path, default_branch="main", **attrs):
        self.projects[path] = {
            "id": len(self.projects) + 1,
            "path_with_namespace": path,
            "name": path.rsplit("/", 1)[-1],
            "default_branch": default_branch,
            "web_url": f"{HOST}/{path}",
            "last_activity_at": "2026-08-20T00:00:00Z",
            **attrs,
        }
        return self.projects[path]

    def add_file(self, path, ref, file_path, content):
        self.trees.setdefault((path, ref), {})[file_path.lstrip("/")] = content

    # -- client surface
    def current_user(self):
        return {"username": "tester", "name": "Test User"}

    def get_project(self, path_or_id):
        self.calls.append(("get_project", str(path_or_id)))
        proj = self.projects.get(str(path_or_id))
        if proj is None:
            for p in self.projects.values():
                if str(p["id"]) == str(path_or_id):
                    return p
            raise GitLabNotFound(f"404 project {path_or_id}", 404)
        return proj

    def ci_lint(self, path_or_id, ref=None, include_jobs=False):
        self.calls.append(("ci_lint", str(path_or_id), ref))
        if self.lint_error is not None:
            raise self.lint_error
        resp = self.lint_responses.get(str(path_or_id))
        if resp is None:
            raise GitLabNotFound("404 lint", 404)
        return resp

    def get_raw_file(self, path_or_id, file_path, ref):
        self.calls.append(("get_raw_file", str(path_or_id), file_path, ref))
        tree = self.trees.get((str(path_or_id), ref))
        if tree is None or file_path.lstrip("/") not in tree:
            raise GitLabNotFound(f"404 {path_or_id}@{ref}:{file_path}", 404)
        return tree[file_path.lstrip("/")]

    def iter_tree(self, path_or_id, ref, max_pages=20):
        tree = self.trees.get((str(path_or_id), ref), {})
        for path in sorted(tree):
            yield {"path": path, "type": "blob"}

    def get_ci_template(self, name):
        if name not in self.templates:
            raise GitLabNotFound(f"404 template {name}", 404)
        return {"name": name, "content": self.templates[name]}

    def get_url_raw(self, url):
        if url not in self.remote_files:
            raise GitLabError(f"cannot reach {url}")
        return self.remote_files[url]

    def list_projects(self, search=None, page=1, per_page=50, membership=True):
        items = [p for p in self.projects.values()
                 if not search or search in p["path_with_namespace"]]
        return items, None

    def list_branches(self, path_or_id, search=None, page=1, per_page=50):
        return [{"name": "main"}, {"name": "dev"}], None

    def list_tags(self, path_or_id, page=1, per_page=50):
        return [{"name": t} for t in self.tags.get(str(path_or_id), [])], None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MERGED_YAML = """\
stages: [build, test]
## Compile everything
build_job:
  stage: build
  script: [make build]
## Run the tests
test_job:
  stage: test
  needs: [build_job]
  script: [make test]
"""


@pytest.fixture
def lint_gl():
    gl = FakeGitLab()
    gl.add_project("group/app")
    gl.lint_responses["group/app"] = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "merged_yaml": MERGED_YAML,
        "includes": [
            {"type": "file", "location": "ci/build.yml",
             "extra": {"project": "group/lib", "ref": "main"}},
            {"type": "template", "location": "Jobs/Deploy.gitlab-ci.yml",
             "extra": {}},
        ],
    }
    return gl


@pytest.fixture
def files_gl():
    gl = FakeGitLab()
    gl.add_project("group/app")
    gl.add_project("group/lib", default_branch="stable")
    gl.lint_error = GitLabForbidden("403 lint", 403)
    gl.add_file("group/app", "main", ".gitlab-ci.yml", """\
stages: [build, test, deploy]
include:
  - local: ci/local.yml
  - project: group/lib
    file: /ci/shared.yml
  - template: Security/SAST.gitlab-ci.yml
## Build it
build_job:
  stage: build
  script: [make]
""")
    gl.add_file("group/app", "main", "ci/local.yml", """\
## Local include job
local_job:
  stage: test
  script: [make check]
""")
    # group/lib has no explicit ref in the include -> its default branch
    # ("stable"); its file pulls a nested include:local from ITS OWN repo.
    gl.add_file("group/lib", "stable", "ci/shared.yml", """\
include:
  - local: ci/nested.yml
## Shared job from group/lib
shared_job:
  stage: test
  script: [make shared]
""")
    gl.add_file("group/lib", "stable", "ci/nested.yml", """\
## Nested cross-repo job
nested_job:
  stage: deploy
  script: [make nested]
""")
    gl.templates["Security/SAST"] = """\
## Static analysis
sast:
  stage: test
  script: [run-sast]
"""
    return gl


# ---------------------------------------------------------------------------
# Lint strategy
# ---------------------------------------------------------------------------

class TestLintStrategy:
    def test_end_to_end(self, lint_gl, tmpdir):
        report, written = generate_report(
            lint_gl, "group/app", outdir=str(tmpdir))
        names = {n.name for n in report.nodes}
        assert {"build_job", "test_job"} <= names
        remote = report.annotations["gitlab_remote"]
        assert remote["strategy"] == "lint"
        assert remote["project"] == "group/app"
        assert remote["ref"] == "main"
        assert remote["lint_valid"] is True
        html = [p for p in written if p.endswith(".html")]
        assert html and os.path.isfile(html[0])
        assert os.path.isfile(os.path.join(
            str(tmpdir), "fetched", "group-app@main", ".gitlab-ci.yml"))

    def test_provenance_in_file_map(self, lint_gl, tmpdir):
        report, _ = generate_report(lint_gl, "group/app", outdir=str(tmpdir))
        paths = {f.path for f in report.files}
        assert "[file:group/lib@main] ci/build.yml" in paths
        assert "[template] Jobs/Deploy.gitlab-ci.yml" in paths

    def test_lint_verdict_becomes_diagnostics(self, lint_gl, tmpdir):
        report, _ = generate_report(lint_gl, "group/app", outdir=str(tmpdir))
        messages = [d.message for d in report.diagnostics]
        assert any("CI Lint: configuration is valid" in m for m in messages)
        assert report.max_severity() == "info"

    def test_gitlab_errors_surface(self, lint_gl, tmpdir):
        # Invalid config, but GitLab still returned a merged view.
        gl = lint_gl
        gl.lint_responses["group/app"]["valid"] = False
        gl.lint_responses["group/app"]["errors"] = [
            "jobs:deploy config contains unknown keys: scriptt"]
        report, _ = generate_report(gl, "group/app", outdir=str(tmpdir))
        errors = [d for d in report.diagnostics if d.severity == "error"]
        assert any("unknown keys: scriptt" in d.message for d in errors)
        assert any("INVALID" in d.message for d in errors)

    def test_forced_lint_raises_when_forbidden(self, files_gl):
        project = files_gl.get_project("group/app")
        with pytest.raises(GitLabError):
            fetch_config(files_gl, project, "main", strategy="lint")


# ---------------------------------------------------------------------------
# Files strategy (cross-repo traversal)
# ---------------------------------------------------------------------------

class TestFilesStrategy:
    def test_falls_back_when_lint_forbidden(self, files_gl):
        project = files_gl.get_project("group/app")
        result = fetch_config(files_gl, project, "main", strategy="auto")
        assert result.strategy == "files"
        assert any("falling back" in msg for _, msg in result.notes)

    def test_cross_repo_jobs_are_real_not_ghosts(self, files_gl, tmpdir):
        report, _ = generate_report(files_gl, "group/app", outdir=str(tmpdir))
        by_name = {n.name: n for n in report.nodes}
        for job in ("build_job", "local_job", "shared_job", "nested_job", "sast"):
            assert job in by_name, f"{job} missing"
            assert by_name[job].kind == "job", f"{job} is {by_name[job].kind}"
        # No unresolved-include ghosts remain.
        assert not [f for f in report.files if f.status == "unresolved"]

    def test_external_files_materialized(self, files_gl, tmpdir):
        report, _ = generate_report(files_gl, "group/app", outdir=str(tmpdir))
        workdir = os.path.join(str(tmpdir), "fetched", "group-app@main")
        assert os.path.isfile(os.path.join(workdir, ".gitlab-ci.yml"))
        assert os.path.isfile(os.path.join(workdir, "ci", "local.yml"))
        # include:project ref defaulted to group/lib's default branch (stable)
        ext = os.path.join(workdir, "_external", "group-lib@stable")
        assert os.path.isfile(os.path.join(ext, "ci", "shared.yml"))
        # nested include:local inside group/lib resolved against group/lib
        assert os.path.isfile(os.path.join(ext, "ci", "nested.yml"))
        tpl = os.path.join(workdir, "_external", "templates",
                           "Security", "SAST.gitlab-ci.yml")
        assert os.path.isfile(tpl)

    def test_include_edges_present(self, files_gl, tmpdir):
        report, _ = generate_report(files_gl, "group/app", outdir=str(tmpdir))
        inc_edges = {(e.src, e.dst) for e in report.edges if e.kind == "includes"}
        assert (".gitlab-ci.yml", os.path.join("ci", "local.yml")) in inc_edges
        assert any(dst.startswith("_external") and "shared" in dst
                   for _, dst in inc_edges)

    def test_wildcard_local_include(self):
        gl = FakeGitLab()
        gl.add_project("group/app")
        gl.lint_error = GitLabNotFound("404", 404)
        gl.add_file("group/app", "main", ".gitlab-ci.yml",
                    "include:\n  - local: ci/*.yml\njob:\n  script: [x]\n")
        gl.add_file("group/app", "main", "ci/a.yml", "a_job:\n  script: [a]\n")
        gl.add_file("group/app", "main", "ci/b.yml", "b_job:\n  script: [b]\n")
        gl.add_file("group/app", "main", "ci/sub/c.yml", "c_job:\n  script: [c]\n")
        project = gl.get_project("group/app")
        result = fetch_config(gl, project, "main")
        rels = {f.rel_path for f in result.files}
        assert "ci/a.yml" in rels and "ci/b.yml" in rels
        assert "ci/sub/c.yml" not in rels   # * does not cross directories

    def test_custom_ci_config_path(self, tmpdir):
        gl = FakeGitLab()
        gl.add_project("group/app", ci_config_path="ci/main.yml")
        gl.lint_error = GitLabNotFound("404", 404)
        gl.add_file("group/app", "main", "ci/main.yml", """\
include: [{local: ci/extra.yml}]
root_job:
  script: [x]
""")
        gl.add_file("group/app", "main", "ci/extra.yml",
                    "extra_job:\n  script: [y]\n")
        report, _ = generate_report(gl, "group/app", outdir=str(tmpdir))
        names = {n.name for n in report.nodes}
        assert {"root_job", "extra_job"} <= names

    def test_ci_config_path_in_other_project(self, tmpdir):
        gl = FakeGitLab()
        gl.add_project("group/app",
                       ci_config_path=".wrench-ci.yml@infra/pipelines")
        gl.add_project("infra/pipelines", default_branch="master")
        gl.lint_error = GitLabNotFound("404", 404)
        gl.add_file("infra/pipelines", "master", ".wrench-ci.yml",
                    "central_job:\n  script: [run]\n")
        report, _ = generate_report(gl, "group/app", outdir=str(tmpdir))
        assert "central_job" in {n.name for n in report.nodes}

    def test_missing_include_stays_ghost_with_note(self, tmpdir):
        gl = FakeGitLab()
        gl.add_project("group/app")
        gl.lint_error = GitLabNotFound("404", 404)
        gl.add_file("group/app", "main", ".gitlab-ci.yml", """\
include:
  - project: group/gone
    file: /ci/x.yml
job:
  script: [x]
""")
        report, _ = generate_report(gl, "group/app", outdir=str(tmpdir))
        assert any(f.status == "unresolved" for f in report.files)
        assert any("Cannot look up project group/gone" in d.message
                   for d in report.diagnostics)

    def test_self_include_terminates(self, tmpdir):
        gl = FakeGitLab()
        gl.add_project("group/app")
        gl.lint_error = GitLabNotFound("404", 404)
        gl.add_file("group/app", "main", ".gitlab-ci.yml", """\
include:
  - project: group/app
    file: /.gitlab-ci.yml
    ref: main
job:
  script: [x]
""")
        report, _ = generate_report(gl, "group/app", outdir=str(tmpdir))
        assert "job" in {n.name for n in report.nodes}

    def test_root_missing_raises(self):
        gl = FakeGitLab()
        gl.add_project("group/empty")
        gl.lint_error = GitLabNotFound("404", 404)
        project = gl.get_project("group/empty")
        with pytest.raises(GitLabError):
            fetch_config(gl, project, "main")


# ---------------------------------------------------------------------------
# fetch internals
# ---------------------------------------------------------------------------

class TestFetchHelpers:
    def test_include_keys_project_multi_file(self):
        keys = include_keys({"project": "g/p", "ref": "v1",
                             "file": ["/a.yml", "b.yml"]})
        assert keys == ["project:g/p@v1:a.yml", "project:g/p@v1:b.yml"]

    def test_include_keys_no_ref(self):
        assert include_keys({"project": "g/p", "file": "a.yml"}) == \
            ["project:g/p@:a.yml"]

    def test_include_keys_other_kinds(self):
        assert include_keys({"template": "T.yml"}) == ["template:T.yml"]
        assert include_keys({"remote": "https://x/y.yml"}) == \
            ["remote:https://x/y.yml"]
        assert include_keys({"component": "h/g/c@1"}) == ["component:h/g/c@1"]
        assert include_keys({"local": "x.yml"}) == []

    def test_wildcard_star_stays_in_directory(self):
        rx = _wildcard_regex("ci/*.yml")
        assert rx.match("ci/a.yml")
        assert not rx.match("ci/sub/a.yml")

    def test_wildcard_doublestar_crosses(self):
        rx = _wildcard_regex("ci/**.yml")
        assert rx.match("ci/a.yml")
        assert rx.match("ci/sub/deep/a.yml")

    def test_materialize_resolver_roundtrip(self, files_gl, tmpdir):
        project = files_gl.get_project("group/app")
        result = fetch_config(files_gl, project, "main")
        workdir = os.path.join(str(tmpdir), "wd")
        root_abs, resolver, roots = materialize(result, workdir)
        assert os.path.isfile(root_abs)
        assert resolver is not None
        paths = resolver({"project": "group/lib", "file": "/ci/shared.yml"})
        assert paths and os.path.isfile(paths[0])
        assert resolver({"project": "group/unknown", "file": "/x.yml"}) is None
        assert any(r.endswith("group-lib@stable") for r in roots)


# ---------------------------------------------------------------------------
# auth + config
# ---------------------------------------------------------------------------

class TestAuth:
    def test_resolution_order(self, tmpdir):
        cfg = GitLabConfig(str(tmpdir.join("gitlab.json")))
        cfg.set_token(HOST, "stored-token")

        token, source = auth_mod.resolve_token(HOST, "flag-token", cfg, {})
        assert (token, source) == ("flag-token", "--token flag")

        token, source = auth_mod.resolve_token(
            HOST, None, cfg, {"PIPEVIEW_GITLAB_TOKEN": "env-a",
                              "GITLAB_TOKEN": "env-b"})
        assert (token, source) == ("env-a", "$PIPEVIEW_GITLAB_TOKEN")

        token, source = auth_mod.resolve_token(
            HOST, None, cfg, {"GITLAB_TOKEN": "env-b"})
        assert (token, source) == ("env-b", "$GITLAB_TOKEN")

        token, _ = auth_mod.resolve_token(HOST, None, cfg, {})
        assert token == "stored-token"

        empty = GitLabConfig(str(tmpdir.join("none.json")))
        token, source = auth_mod.resolve_token(HOST, None, empty, {})
        assert token is None and source == "not found"

    def test_token_creation_url(self):
        url = auth_mod.token_creation_url("gitlab.example.com")
        assert url.startswith(
            "https://gitlab.example.com/-/user_settings/personal_access_tokens?")
        assert "name=pipeview" in url and "scopes=read_api" in url


class TestConfig:
    def test_saved_with_0600(self, tmpdir):
        path = str(tmpdir.join("sub", "gitlab.json"))
        cfg = GitLabConfig(path)
        cfg.set_token(HOST, "secret")
        cfg.save()
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        reread = GitLabConfig(path)
        assert reread.stored_token(HOST) == "secret"

    def test_track_untrack_roundtrip(self, tmpdir):
        cfg = GitLabConfig(str(tmpdir.join("gitlab.json")))
        assert cfg.track(HOST, "g/a") is True
        assert cfg.track(HOST, "g/a") is False
        cfg.track(HOST, "g/b")
        assert cfg.tracked(HOST) == ["g/a", "g/b"]
        assert cfg.untrack(HOST, "g/a") is True
        assert cfg.untrack(HOST, "g/a") is False
        assert cfg.tracked(HOST) == ["g/b"]

    def test_track_with_ref(self, tmpdir):
        cfg = GitLabConfig(str(tmpdir.join("gitlab.json")))
        assert cfg.track(HOST, "g/a", "dev") is True
        assert cfg.track(HOST, "g/a", "dev") is False
        cfg.track(HOST, "g/a")           # same project, default branch
        cfg.track(HOST, "g/a", "v1.0")   # same project, a tag
        assert cfg.tracked(HOST) == ["g/a", "g/a@dev", "g/a@v1.0"]
        # exact-entry membership vs any-ref membership
        assert cfg.is_tracked(HOST, "g/a", "dev")
        assert not cfg.is_tracked(HOST, "g/a", "other")
        assert cfg.is_tracked_any(HOST, "g/a")
        assert not cfg.is_tracked_any(HOST, "g/b")
        # exact untrack leaves the other entries alone
        assert cfg.untrack(HOST, "g/a", "dev") is True
        assert cfg.tracked(HOST) == ["g/a", "g/a@v1.0"]
        # untrack_all sweeps the rest
        assert cfg.untrack_all(HOST, "g/a") == 2
        assert cfg.tracked(HOST) == []

    def test_entry_parse_roundtrip(self):
        assert GitLabConfig.parse_entry("g/a") == ("g/a", None)
        assert GitLabConfig.parse_entry("g/a@dev") == ("g/a", "dev")
        # refs may contain '/' and even '@'; the FIRST '@' splits
        assert GitLabConfig.parse_entry("g/a@feature/x@y") == ("g/a", "feature/x@y")
        assert GitLabConfig.make_entry("g/a", None) == "g/a"
        assert GitLabConfig.make_entry("g/a", "feature/x") == "g/a@feature/x"
        entry = GitLabConfig.make_entry("g/a", "feature/x@y")
        assert GitLabConfig.parse_entry(entry) == ("g/a", "feature/x@y")

    def test_normalize_host(self):
        assert GitLabConfig.normalize_host("gitlab.example.com/") == \
            "https://gitlab.example.com"
        assert GitLabConfig.normalize_host("http://gl.local") == "http://gl.local"

    def test_corrupt_file_ignored(self, tmpdir):
        path = tmpdir.join("gitlab.json")
        path.write("{not json")
        cfg = GitLabConfig(str(path))
        assert cfg.tracked(HOST) == []


# ---------------------------------------------------------------------------
# TUI pure helpers
# ---------------------------------------------------------------------------

class TestTuiHelpers:
    def test_visible_window_small_list(self):
        assert visible_window(0, 3, 10) == (0, 3)

    def test_visible_window_scrolls(self):
        start, end = visible_window(50, 100, 10)
        assert start <= 50 < end
        assert end - start == 10
        assert visible_window(99, 100, 10) == (90, 100)
        assert visible_window(0, 100, 10) == (0, 10)

    def test_order_refs(self):
        refs = order_refs("main", ["dev", "main", "feat"], ["v1", "v2"])
        assert refs[0] == ("default", "main")
        assert ("branch", "main") not in refs
        assert refs[-2:] == [("tag", "v1"), ("tag", "v2")]

    def test_order_projects_tracked_first(self):
        projects = [{"path_with_namespace": p} for p in ("a/a", "b/b", "c/c")]
        ordered = order_projects(projects, ["c/c"])
        assert ordered[0]["path_with_namespace"] == "c/c"
        assert [p["path_with_namespace"] for p in ordered[1:]] == ["a/a", "b/b"]

    def test_order_projects_ref_entries_count_as_tracked(self):
        projects = [{"path_with_namespace": p} for p in ("a/a", "b/b", "c/c")]
        ordered = order_projects(projects, ["b/b@dev", "c/c@v1.0"])
        assert [p["path_with_namespace"] for p in ordered] == \
            ["b/b", "c/c", "a/a"]

    def test_truncate(self):
        assert truncate("hello", 10) == "hello"
        assert truncate("hello world", 8) == "hello w…"
        assert truncate("x", 0) == ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestGitLabCli:
    def _env(self, monkeypatch, tmpdir):
        cfg_path = str(tmpdir.join("cfg.json"))
        monkeypatch.setenv("PIPEVIEW_GITLAB_CONFIG", cfg_path)
        for var in ("PIPEVIEW_GITLAB_HOST", "GITLAB_HOST", "CI_SERVER_URL",
                    "PIPEVIEW_GITLAB_TOKEN", "GITLAB_TOKEN",
                    "GITLAB_PRIVATE_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        return cfg_path

    def test_help_exits_zero(self):
        with pytest.raises(SystemExit) as exc:
            gitlab_cli.main(["--help"])
        assert exc.value.code == 0

    def test_dispatch_from_main_cli(self, monkeypatch, tmpdir, capsys):
        self._env(monkeypatch, tmpdir)
        from pipeview.cli import main as pipeview_main
        rc = pipeview_main(["gitlab", "tracked", "--host", HOST])
        assert rc == 0
        assert "No tracked projects" in capsys.readouterr().out

    def test_track_untrack_tracked(self, monkeypatch, tmpdir, capsys):
        self._env(monkeypatch, tmpdir)
        assert gitlab_cli.main(["track", "group/app", "--host", HOST]) == 0
        assert gitlab_cli.main(["tracked", "--host", HOST]) == 0
        assert "group/app" in capsys.readouterr().out
        assert gitlab_cli.main(["untrack", "group/app", "--host", HOST]) == 0

    def test_track_specific_branch(self, monkeypatch, tmpdir, capsys):
        self._env(monkeypatch, tmpdir)
        # inline @ref and --ref both work
        assert gitlab_cli.main(["track", "group/app@dev", "--host", HOST]) == 0
        assert gitlab_cli.main(
            ["track", "group/app", "--ref", "v1.0", "--host", HOST]) == 0
        assert gitlab_cli.main(["tracked", "--host", HOST]) == 0
        out = capsys.readouterr().out
        assert "group/app@dev" in out and "group/app@v1.0" in out
        # exact untrack removes only that entry
        assert gitlab_cli.main(["untrack", "group/app@dev", "--host", HOST]) == 0
        capsys.readouterr()
        assert gitlab_cli.main(["tracked", "--host", HOST]) == 0
        out = capsys.readouterr().out
        assert "group/app@dev" not in out and "group/app@v1.0" in out

    def test_bare_untrack_sweeps_ref_entries(self, monkeypatch, tmpdir, capsys):
        self._env(monkeypatch, tmpdir)
        gitlab_cli.main(["track", "group/app@dev", "--host", HOST])
        gitlab_cli.main(["track", "group/app@v1.0", "--host", HOST])
        capsys.readouterr()
        assert gitlab_cli.main(["untrack", "group/app", "--host", HOST]) == 0
        assert "2 ref entries" in capsys.readouterr().out
        assert gitlab_cli.main(["tracked", "--host", HOST]) == 0
        assert "No tracked projects" in capsys.readouterr().out

    def test_no_host_is_actionable(self, monkeypatch, tmpdir, capsys):
        self._env(monkeypatch, tmpdir)
        rc = gitlab_cli.main(["projects"])
        assert rc == 2
        assert "--host" in capsys.readouterr().err

    def test_report_end_to_end(self, monkeypatch, tmpdir, capsys, lint_gl):
        self._env(monkeypatch, tmpdir)
        monkeypatch.setattr(gitlab_cli, "_make_client",
                            lambda args, config, host: lint_gl)
        out = str(tmpdir.join("out"))
        rc = gitlab_cli.main(["report", "group/app", "--host", HOST, "-o", out])
        assert rc == 0
        assert os.path.isfile(os.path.join(out, "group-app@main.report.html"))
        model = os.path.join(out, "group-app@main.model.json")
        with open(model) as f:
            data = json.load(f)
        assert data["annotations"]["gitlab_remote"]["strategy"] == "lint"

    def test_projects_listing(self, monkeypatch, tmpdir, capsys, lint_gl):
        self._env(monkeypatch, tmpdir)
        monkeypatch.setattr(gitlab_cli, "_make_client",
                            lambda args, config, host: lint_gl)
        rc = gitlab_cli.main(["projects", "--host", HOST])
        assert rc == 0
        assert "group/app" in capsys.readouterr().out

    def test_sync_tracked_projects(self, monkeypatch, tmpdir, capsys, lint_gl):
        self._env(monkeypatch, tmpdir)
        monkeypatch.setattr(gitlab_cli, "_make_client",
                            lambda args, config, host: lint_gl)
        out = str(tmpdir.join("out"))
        assert gitlab_cli.main(["track", "group/app", "--host", HOST]) == 0
        rc = gitlab_cli.main(["sync", "--host", HOST, "-o", out])
        assert rc == 0
        assert os.path.isfile(os.path.join(out, "group-app@main.report.html"))

    def test_sync_uses_each_entrys_ref(self, monkeypatch, tmpdir, capsys, lint_gl):
        self._env(monkeypatch, tmpdir)
        monkeypatch.setattr(gitlab_cli, "_make_client",
                            lambda args, config, host: lint_gl)
        out = str(tmpdir.join("out"))
        gitlab_cli.main(["track", "group/app@dev", "--host", HOST])
        gitlab_cli.main(["track", "group/app", "--host", HOST])
        capsys.readouterr()
        rc = gitlab_cli.main(["sync", "--host", HOST, "-o", out])
        assert rc == 0
        # bare entry -> default branch; pinned entry -> its own ref
        assert os.path.isfile(os.path.join(out, "group-app@main.report.html"))
        assert os.path.isfile(os.path.join(out, "group-app@dev.report.html"))
        assert ("ci_lint", "group/app", "dev") in lint_gl.calls

    def test_report_inline_ref(self, monkeypatch, tmpdir, capsys, lint_gl):
        self._env(monkeypatch, tmpdir)
        monkeypatch.setattr(gitlab_cli, "_make_client",
                            lambda args, config, host: lint_gl)
        out = str(tmpdir.join("out"))
        rc = gitlab_cli.main(["report", "group/app@dev", "--host", HOST,
                              "-o", out])
        assert rc == 0
        assert os.path.isfile(os.path.join(out, "group-app@dev.report.html"))
