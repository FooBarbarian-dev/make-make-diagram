"""Tests for --upstream: resolving cross-repo includes via the git remote.

Fetching runs against FakeGitLab; git detection runs against throwaway
local repositories — no test ever touches a network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from pipeview.cli import main
from pipeview.gitlab import upstream as upstream_mod
from pipeview.gitlab.fetch import fetch_local_externals, materialize
from pipeview.gitlab.upstream import (
    Upstream,
    UpstreamError,
    detect_upstream,
    parse_remote_url,
    resolve_upstream_includes,
)
from pipeview.parsers.gitlab_parser import parse_gitlab
from tests.test_gitlab_remote import FakeGitLab

HAVE_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAVE_GIT, reason="git not installed")


# ---------------------------------------------------------------------------
# Remote URL parsing (pure)
# ---------------------------------------------------------------------------

class TestParseRemoteUrl:
    @pytest.mark.parametrize("url,host,path", [
        ("git@gitlab.example.com:group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("git@gitlab.example.com:group/sub/app.git",
         "https://gitlab.example.com", "group/sub/app"),
        ("git@gitlab.example.com:/group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("ssh://git@gitlab.example.com/group/app.git",
         "https://gitlab.example.com", "group/app"),
        # ssh port is not the API port — dropped
        ("ssh://git@gitlab.example.com:2222/group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("git://gitlab.example.com/group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("https://gitlab.example.com/group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("https://gitlab.example.com/group/app/",
         "https://gitlab.example.com", "group/app"),
        # https port IS the API port — kept
        ("https://gitlab.example.com:8443/group/app",
         "https://gitlab.example.com:8443", "group/app"),
        ("https://oauth2:tok@gitlab.example.com/group/app.git",
         "https://gitlab.example.com", "group/app"),
        ("http://gitlab.local/group/app",
         "http://gitlab.local", "group/app"),
    ])
    def test_accepted(self, url, host, path):
        assert parse_remote_url(url) == (host, path)

    @pytest.mark.parametrize("url", [
        "",
        "/srv/git/repo.git",                    # local path
        "../relative/repo",                     # local path
        "file:///srv/git/repo.git",             # unsupported scheme
        "https://gitlab.example.com/app",       # no namespace
        "git@gitlab.example.com:app.git",       # no namespace
        "https://gitlab.example.com",           # no path at all
    ])
    def test_rejected(self, url):
        assert parse_remote_url(url) is None


# ---------------------------------------------------------------------------
# Upstream detection (runs git against throwaway repos)
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "checkout"
    d.mkdir()
    _git(d, "init", "-q", "-b", "main")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "T")
    (d / "README").write_text("x\n")
    _git(d, "add", "README")
    _git(d, "commit", "-q", "-m", "init")
    return d


@needs_git
class TestDetectUpstream:
    URL = "git@gitlab.example.com:group/app.git"

    def test_origin_default(self, repo):
        _git(repo, "remote", "add", "origin", self.URL)
        up = detect_upstream(str(repo))
        assert up.remote_name == "origin"
        assert up.host == "https://gitlab.example.com"
        assert up.project_path == "group/app"
        assert up.url == self.URL
        assert os.path.samefile(up.toplevel, str(repo))
        assert up.branch == "main"

    def test_detect_from_subdirectory(self, repo):
        _git(repo, "remote", "add", "origin", self.URL)
        sub = repo / "ci"
        sub.mkdir()
        up = detect_upstream(str(sub))
        assert os.path.samefile(up.toplevel, str(repo))

    def test_tracking_remote_preferred_over_origin(self, repo):
        _git(repo, "remote", "add", "origin", self.URL)
        _git(repo, "remote", "add", "fork",
             "git@gitlab.example.com:me/app.git")
        sha = subprocess.run(
            ["git", "-C", repo, "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True).stdout.strip()
        _git(repo, "update-ref", "refs/remotes/fork/main", sha)
        _git(repo, "branch", "--set-upstream-to=fork/main")
        up = detect_upstream(str(repo))
        assert up.remote_name == "fork"
        assert up.project_path == "me/app"

    def test_explicit_remote_wins(self, repo):
        _git(repo, "remote", "add", "origin", self.URL)
        _git(repo, "remote", "add", "mirror",
             "git@gitlab.example.com:mirror/app.git")
        up = detect_upstream(str(repo), remote="mirror")
        assert up.project_path == "mirror/app"

    def test_sole_remote_used(self, repo):
        _git(repo, "remote", "add", "upstream", self.URL)
        assert detect_upstream(str(repo)).remote_name == "upstream"

    def test_several_remotes_without_tracking_error(self, repo):
        _git(repo, "remote", "add", "alpha", self.URL)
        _git(repo, "remote", "add", "beta", self.URL)
        with pytest.raises(UpstreamError, match="--upstream-remote"):
            detect_upstream(str(repo))

    def test_no_remotes(self, repo):
        with pytest.raises(UpstreamError, match="no git remotes"):
            detect_upstream(str(repo))

    def test_not_a_repository(self, tmp_path):
        with pytest.raises(UpstreamError, match="not inside a git repository"):
            detect_upstream(str(tmp_path))

    def test_unknown_explicit_remote(self, repo):
        _git(repo, "remote", "add", "origin", self.URL)
        with pytest.raises(UpstreamError, match="origin"):
            detect_upstream(str(repo), remote="nope")

    def test_unparseable_url(self, repo):
        _git(repo, "remote", "add", "origin", "/srv/git/app.git")
        with pytest.raises(UpstreamError, match="cannot infer"):
            detect_upstream(str(repo))


# ---------------------------------------------------------------------------
# Local-seed fetch: local truth, remote externals
# ---------------------------------------------------------------------------

ROOT_YML = """\
stages: [build, test, deploy]
include:
  - local: ci/jobs.yml
  - project: group/lib
    ref: stable
    file: /templates/deploy.yml
  - template: Bash.gitlab-ci.yml
  - remote: https://example.com/x.yml
## Build it
build_job:
  stage: build
  script: [make]
"""

JOBS_YML = """\
include:
  - project: group/other
    file: ci/x.yml
## Local test job (uncommitted edit)
test_job:
  stage: test
  script: [make test]
"""


def _write_tree(base, files):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


@pytest.fixture
def local_tree(tmp_path):
    tree = tmp_path / "tree"
    _write_tree(tree, {".gitlab-ci.yml": ROOT_YML, "ci/jobs.yml": JOBS_YML})
    return tree


@pytest.fixture
def upstream_gl():
    gl = FakeGitLab()
    gl.add_project("group/app")
    gl.add_project("group/lib", default_branch="stable")
    gl.add_project("group/other")   # default branch main
    gl.add_file("group/lib", "stable", "templates/deploy.yml", """\
include:
  - local: templates/nested.yml
## Deploy job from group/lib
deploy_lib:
  stage: deploy
  script: [deploy]
""")
    gl.add_file("group/lib", "stable", "templates/nested.yml", """\
nested_lib:
  stage: deploy
  script: [nested]
""")
    gl.add_file("group/other", "main", "ci/x.yml",
                "other_job:\n  stage: test\n  script: [x]\n")
    gl.templates["Bash"] = "bash_job:\n  stage: test\n  script: [bash]\n"
    gl.remote_files["https://example.com/x.yml"] = \
        "remote_job:\n  stage: test\n  script: [r]\n"
    return gl


class TestFetchLocalExternals:
    def _fetch(self, gl, tree):
        project = gl.get_project("group/app")
        return fetch_local_externals(
            gl, project, "main",
            repo_root=str(tree), root_file=str(tree / ".gitlab-ci.yml"))

    def test_nothing_local_is_materialized(self, upstream_gl, local_tree):
        result = self._fetch(upstream_gl, local_tree)
        assert result.strategy == "upstream"
        assert result.files
        assert all(f.rel_path.startswith("_external/") for f in result.files)
        # ...and no fetch of the local repo's own files happened
        assert not any(c[0] == "get_raw_file" and c[1] == "group/app"
                       for c in upstream_gl.calls)

    def test_externals_discovered_through_local_includes(
            self, upstream_gl, local_tree):
        # group/other is only reachable through ci/jobs.yml (a local file)
        result = self._fetch(upstream_gl, local_tree)
        assert "project:group/other@:ci/x.yml" in result.external_map
        # nested include:local inside group/lib fetched from group/lib
        rels = {f.rel_path for f in result.files}
        assert "_external/group-lib@stable/templates/nested.yml" in rels

    def test_end_to_end_ghosts_become_real(self, upstream_gl, local_tree,
                                           tmp_path):
        result = self._fetch(upstream_gl, local_tree)
        _, resolver, roots = materialize(result, str(tmp_path / "wd"))
        report = parse_gitlab(
            str(local_tree / ".gitlab-ci.yml"), repo_root=str(local_tree),
            external_resolver=resolver, local_roots=roots)
        by_name = {n.name: n for n in report.nodes}
        for job in ("build_job", "test_job", "deploy_lib", "nested_lib",
                    "other_job", "bash_job", "remote_job"):
            assert job in by_name, f"{job} missing"
            assert by_name[job].kind == "job"
        assert not [f for f in report.files if f.status == "unresolved"]

    def test_without_resolver_same_tree_ghosts(self, local_tree):
        # The control: offline, the very same tree leaves ghosts.
        report = parse_gitlab(str(local_tree / ".gitlab-ci.yml"),
                              repo_root=str(local_tree))
        unresolved = [f for f in report.files if f.status == "unresolved"]
        assert unresolved

    def test_wildcard_local_walked(self, upstream_gl, tmp_path):
        tree = tmp_path / "wild"
        _write_tree(tree, {
            ".gitlab-ci.yml": "include:\n  - local: ci/*.yml\n"
                              "job:\n  script: [x]\n",
            "ci/a.yml": "include:\n  - project: group/other\n"
                        "    file: ci/x.yml\n",
            "ci/b.yml": "b_job:\n  script: [b]\n",
            "ci/sub/c.yml": "include:\n  - remote: https://example.com/x.yml\n",
        })
        result = fetch_local_externals(
            upstream_gl, upstream_gl.get_project("group/app"), "main",
            repo_root=str(tree), root_file=str(tree / ".gitlab-ci.yml"))
        # ci/a.yml matched -> its external include fetched
        assert "project:group/other@:ci/x.yml" in result.external_map
        # ci/sub/c.yml NOT matched (* does not cross directories)
        assert "remote:https://example.com/x.yml" not in result.external_map

    def test_local_include_cycle_terminates(self, upstream_gl, tmp_path):
        tree = tmp_path / "cycle"
        _write_tree(tree, {
            ".gitlab-ci.yml": "include: [{local: a.yml}]\n",
            "a.yml": "include: [{local: b.yml}]\n",
            "b.yml": "include: [{local: a.yml}]\n",
        })
        result = fetch_local_externals(
            upstream_gl, upstream_gl.get_project("group/app"), "main",
            repo_root=str(tree), root_file=str(tree / ".gitlab-ci.yml"))
        assert result.files == []

    def test_unsafe_local_include_skipped(self, upstream_gl, tmp_path):
        tree = tmp_path / "unsafe"
        _write_tree(tree, {
            ".gitlab-ci.yml": "include: [{local: ../../etc/passwd}]\n",
        })
        result = fetch_local_externals(
            upstream_gl, upstream_gl.get_project("group/app"), "main",
            repo_root=str(tree), root_file=str(tree / ".gitlab-ci.yml"))
        assert any("Unsafe include path" in msg for _, msg in result.notes)


# ---------------------------------------------------------------------------
# Orchestration + CLI
# ---------------------------------------------------------------------------

def _fake_upstream(tree):
    return Upstream(host="https://gitlab.example.com",
                    project_path="group/app", remote_name="origin",
                    url="git@gitlab.example.com:group/app.git",
                    toplevel=str(tree), branch="main")


@pytest.fixture
def patched_env(monkeypatch, tmp_path):
    """No real user config, no ambient tokens."""
    monkeypatch.setenv("PIPEVIEW_GITLAB_CONFIG", str(tmp_path / "no-config.json"))
    for var in ("PIPEVIEW_GITLAB_TOKEN", "GITLAB_TOKEN", "GITLAB_PRIVATE_TOKEN"):
        monkeypatch.delenv(var, raising=False)


class TestResolveUpstreamIncludes:
    def test_detection_failure_degrades(self, tmp_path, patched_env):
        tree = tmp_path / "plain"          # not a git repository
        _write_tree(tree, {".gitlab-ci.yml": ROOT_YML})
        res = resolve_upstream_includes(str(tree / ".gitlab-ci.yml"),
                                        str(tmp_path / "out"))
        assert res.resolver is None
        assert res.annotation is None
        assert any(d.severity == "warning" and "--upstream" in d.message
                   for d in res.diagnostics)

    def test_no_token_keeps_annotation_and_warns(
            self, monkeypatch, local_tree, tmp_path, patched_env):
        monkeypatch.setattr(upstream_mod, "detect_upstream",
                            lambda d, r=None: _fake_upstream(local_tree))
        monkeypatch.setattr(upstream_mod, "GitLabClient",
                            lambda *a, **k: pytest.fail("client built without token"))
        res = resolve_upstream_includes(str(local_tree / ".gitlab-ci.yml"),
                                        str(tmp_path / "out"))
        assert res.resolver is None
        assert res.annotation["host"] == "https://gitlab.example.com"
        assert any("no API token" in d.message for d in res.diagnostics)

    def test_resolves_with_token(self, monkeypatch, upstream_gl, local_tree,
                                 tmp_path, patched_env):
        monkeypatch.setenv("PIPEVIEW_GITLAB_TOKEN", "tok")
        monkeypatch.setattr(upstream_mod, "detect_upstream",
                            lambda d, r=None: _fake_upstream(local_tree))
        monkeypatch.setattr(upstream_mod, "GitLabClient",
                            lambda *a, **k: upstream_gl)
        res = resolve_upstream_includes(str(local_tree / ".gitlab-ci.yml"),
                                        str(tmp_path / "out"))
        assert res.resolver is not None
        assert res.repo_root == str(local_tree)
        assert any("resolved via upstream" in d.message
                   for d in res.diagnostics)
        assert os.path.isfile(os.path.join(
            str(tmp_path / "out"), "fetched", "group-app@upstream",
            "_external", "group-lib@stable", "templates", "deploy.yml"))


class TestCliUpstream:
    def test_report_with_upstream(self, monkeypatch, upstream_gl, local_tree,
                                  tmp_path, patched_env, capsys):
        monkeypatch.setenv("PIPEVIEW_GITLAB_TOKEN", "tok")
        monkeypatch.setattr(upstream_mod, "detect_upstream",
                            lambda d, r=None: _fake_upstream(local_tree))
        monkeypatch.setattr(upstream_mod, "GitLabClient",
                            lambda *a, **k: upstream_gl)
        out = tmp_path / "out"
        code = main([str(local_tree), "-o", str(out), "--upstream"])
        assert code == 0
        with open(out / "gitlab-ci.model.json", encoding="utf-8") as f:
            model = json.load(f)
        ann = model["annotations"]["gitlab_upstream"]
        assert ann["project"] == "group/app"
        assert ann["remote"] == "origin"
        names = {n["name"] for n in model["nodes"]}
        assert {"deploy_lib", "other_job", "remote_job"} <= names

    def test_no_token_warns_and_ghosts(self, monkeypatch, local_tree,
                                       tmp_path, patched_env, capsys):
        monkeypatch.setattr(upstream_mod, "detect_upstream",
                            lambda d, r=None: _fake_upstream(local_tree))
        monkeypatch.setattr(upstream_mod, "GitLabClient",
                            lambda *a, **k: pytest.fail("network attempted"))
        out = tmp_path / "out"
        code = main([str(local_tree), "-o", str(out), "--upstream"])
        assert code == 1   # the warning floors the exit code
        assert "no API token" in capsys.readouterr().err
        with open(out / "gitlab-ci.model.json", encoding="utf-8") as f:
            model = json.load(f)
        assert model["annotations"]["gitlab_upstream"]["host"]
        assert any(f["status"] == "unresolved" for f in model["files"])

    def test_without_flag_no_upstream_machinery(self, monkeypatch, local_tree,
                                                tmp_path, patched_env):
        monkeypatch.setattr(
            upstream_mod, "detect_upstream",
            lambda d, r=None: pytest.fail("detect_upstream called"))
        monkeypatch.setattr(
            upstream_mod, "GitLabClient",
            lambda *a, **k: pytest.fail("client constructed"))
        code = main([str(local_tree), "-o", str(tmp_path / "out")])
        assert code in (0, 1)
        with open(tmp_path / "out" / "gitlab-ci.model.json",
                  encoding="utf-8") as f:
            model = json.load(f)
        assert "gitlab_upstream" not in model["annotations"]
