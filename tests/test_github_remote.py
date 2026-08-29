"""Tests for the pipeview.github remote-fetch feature.

Everything runs against FakeGitHub — no test ever touches a network.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from pipeview.github import auth as auth_mod
from pipeview.github import cli as github_cli
from pipeview.github.api import GitHubNotFound, api_root
from pipeview.github.config import GitHubConfig
from pipeview.github.fetch import extract_job_uses, fetch_config, uses_key
from pipeview.github.report import generate_report

HOST = "https://github.com"


# ---------------------------------------------------------------------------
# Fake client
# ---------------------------------------------------------------------------

class FakeGitHub:
    """Implements the GitHubClient surface the feature consumes."""

    def __init__(self):
        self.base_url = HOST
        self.repos: dict[str, dict] = {}
        # (full_name, ref) -> {file_path: content}
        self.trees: dict[tuple[str, str], dict[str, str]] = {}
        self.calls: list[tuple] = []

    # -- helpers to build scenarios
    def add_repo(self, full_name, default_branch="main", **attrs):
        self.repos[full_name] = {
            "id": len(self.repos) + 1,
            "full_name": full_name,
            "path_with_namespace": full_name,
            "name": full_name.rsplit("/", 1)[-1],
            "default_branch": default_branch,
            "html_url": f"{HOST}/{full_name}",
            "web_url": f"{HOST}/{full_name}",
            "last_activity_at": "2026-08-20T00:00:00Z",
            **attrs,
        }
        return self.repos[full_name]

    def add_file(self, full_name, ref, file_path, content):
        self.trees.setdefault((full_name, ref), {})[
            file_path.lstrip("/")] = content

    # -- client surface
    def current_user(self):
        return {"login": "tester", "name": "Test User"}

    def get_repo(self, full_name):
        self.calls.append(("get_repo", full_name))
        repo = self.repos.get(full_name)
        if repo is None:
            raise GitHubNotFound(f"404 repo {full_name}", 404)
        return repo

    get_project = get_repo

    def list_dir(self, full_name, dir_path, ref):
        self.calls.append(("list_dir", full_name, dir_path, ref))
        tree = self.trees.get((full_name, ref), {})
        prefix = dir_path.strip("/") + "/"
        out = []
        for path in sorted(tree):
            if path.startswith(prefix) and "/" not in path[len(prefix):]:
                out.append({"name": path[len(prefix):], "path": path,
                            "type": "file"})
        if not out:
            raise GitHubNotFound(f"404 {full_name}@{ref}:{dir_path}", 404)
        return out

    def get_raw_file(self, full_name, file_path, ref):
        self.calls.append(("get_raw_file", full_name, file_path, ref))
        tree = self.trees.get((full_name, ref))
        if tree is None or file_path.lstrip("/") not in tree:
            raise GitHubNotFound(f"404 {full_name}@{ref}:{file_path}", 404)
        return tree[file_path.lstrip("/")]

    def list_repos(self, search=None, page=1, per_page=50):
        items = [p for p in self.repos.values()
                 if not search or search in p["full_name"]]
        return items, None

    list_projects = list_repos

    def list_branches(self, full_name, search=None, page=1, per_page=50):
        return [{"name": "main"}, {"name": "dev"}], None

    def list_tags(self, full_name, page=1, per_page=50):
        return [], None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_CI_YML = """\
name: CI
on: [push]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: make build
  deploy:
    needs: build
    uses: octo-org/shared/.github/workflows/deploy.yml@v2
    with:
      environment: prod
"""

_SHARED_DEPLOY = """\
name: Deploy
on:
  workflow_call:
    inputs:
      environment: {type: string, required: true}
jobs:
  push_release:
    runs-on: ubuntu-latest
    steps:
      - run: ./deploy.sh
  verify:
    uses: ./.github/workflows/verify.yml
"""

_SHARED_VERIFY = """\
name: Verify
on: [workflow_call]
jobs:
  smoke:
    runs-on: ubuntu-latest
    steps:
      - run: ./smoke.sh
"""


@pytest.fixture
def fake():
    gh = FakeGitHub()
    gh.add_repo("octo-org/app")
    gh.add_file("octo-org/app", "main", ".github/workflows/ci.yml", _CI_YML)
    gh.add_repo("octo-org/shared")
    gh.add_file("octo-org/shared", "v2",
                ".github/workflows/deploy.yml", _SHARED_DEPLOY)
    gh.add_file("octo-org/shared", "v2",
                ".github/workflows/verify.yml", _SHARED_VERIFY)
    return gh


# ---------------------------------------------------------------------------
# API basics
# ---------------------------------------------------------------------------

class TestApiRoot:
    def test_github_com_uses_api_subdomain(self):
        assert api_root("https://github.com") == "https://api.github.com"

    def test_enterprise_uses_api_v3(self):
        assert api_root("https://ghe.corp.example") \
            == "https://ghe.corp.example/api/v3"


class TestExtractJobUses:
    def test_extracts_remote_and_local(self):
        assert extract_job_uses(_SHARED_DEPLOY) \
            == ["./.github/workflows/verify.yml"]
        assert extract_job_uses(_CI_YML) \
            == ["octo-org/shared/.github/workflows/deploy.yml@v2"]

    def test_yaml_errors_degrade_to_empty(self):
        assert extract_job_uses("on: [push\njobs:") == []


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

class TestFetch:
    def test_workflows_and_reusables_fetched(self, fake):
        result = fetch_config(fake, fake.repos["octo-org/app"], "main")
        rels = {f.rel_path for f in result.files}
        assert ".github/workflows/ci.yml" in rels
        assert ("_external/octo-org-shared@v2/.github/workflows/deploy.yml"
                in rels)
        # the nested LOCAL call inside octo-org/shared resolves in THAT repo
        assert ("_external/octo-org-shared@v2/.github/workflows/verify.yml"
                in rels)
        assert result.external_map[
            uses_key("octo-org/shared/.github/workflows/deploy.yml@v2")] \
            == "_external/octo-org-shared@v2/.github/workflows/deploy.yml"
        assert result.external_map[
            uses_key("octo-org/shared/.github/workflows/verify.yml@v2")] \
            == "_external/octo-org-shared@v2/.github/workflows/verify.yml"

    def test_missing_reusable_notes_not_fails(self, fake):
        fake.trees[("octo-org/shared", "v2")].pop(
            ".github/workflows/deploy.yml")
        result = fetch_config(fake, fake.repos["octo-org/app"], "main")
        assert any("Cannot fetch reusable workflow" in m
                   for _, m in result.notes)

    def test_no_workflows_raises(self, fake):
        fake.add_repo("octo-org/empty")
        from pipeview.github.api import GitHubError
        with pytest.raises(GitHubError):
            fetch_config(fake, fake.repos["octo-org/empty"], "main")


# ---------------------------------------------------------------------------
# Report generation (end to end, offline after the fake fetch)
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_end_to_end(self, fake, tmp_path):
        report, written = generate_report(
            fake, "octo-org/app", outdir=str(tmp_path))
        assert report.format == "github_actions"
        html = [p for p in written if p.endswith(".report.html")]
        assert html and os.path.isfile(html[0])
        assert os.path.basename(html[0]) == "octo-org-app@main.report.html"

        # the remote reusable workflow's jobs are IN the report
        ids = {n.id for n in report.nodes if n.kind == "job"}
        assert "ci.yml::build" in ids
        assert any(i.endswith("::push_release") for i in ids)
        # …including the nested local call inside the other repo
        assert any(i.endswith("::smoke") for i in ids)

        remote = report.annotations["github_remote"]
        assert remote["project"] == "octo-org/app"
        assert remote["ref"] == "main"
        assert {e["project"] for e in remote["include_projects"]} \
            == {"octo-org/shared"}

    def test_caller_links_to_materialized_workflow(self, fake, tmp_path):
        report, _ = generate_report(fake, "octo-org/app",
                                    outdir=str(tmp_path))
        deploy = report.node_by_id("ci.yml::deploy")
        assert deploy.annotations["uses_info"]["kind"] == "remote"
        invokes = [(e.src, e.dst) for e in report.edges
                   if e.kind == "invokes" and e.src == "ci.yml::deploy"]
        assert invokes and not invokes[0][1].startswith("downstream:")

    def test_trigger_info_for_rollup(self, fake, tmp_path):
        report, _ = generate_report(fake, "octo-org/app",
                                    outdir=str(tmp_path))
        info = report.node_by_id("ci.yml::deploy").annotations["trigger_info"]
        assert info["mode"] == "multi_project"
        assert info["project"] == "octo-org/shared"
        assert info["ref"] == "v2"


# ---------------------------------------------------------------------------
# Rollup across GitHub repositories
# ---------------------------------------------------------------------------

class TestRollup:
    def test_reusable_call_resolves_across_tracked_repos(self, fake, tmp_path):
        from pipeview.gitlab.rollup import RollupSource, build_rollup
        app, _ = generate_report(fake, "octo-org/app", outdir=str(tmp_path))
        fake.add_file("octo-org/shared", "main",
                      ".github/workflows/deploy.yml", _SHARED_DEPLOY)
        fake.add_file("octo-org/shared", "main",
                      ".github/workflows/verify.yml", _SHARED_VERIFY)
        shared, _ = generate_report(fake, "octo-org/shared",
                                    outdir=str(tmp_path))
        rollup = build_rollup(HOST, [
            RollupSource("octo-org/app", app, "a.html"),
            RollupSource("octo-org/shared@v2", shared, "b.html"),
        ])
        trigger_links = [link for link in rollup["links"]
                         if link["kind"] == "trigger"]
        assert trigger_links
        link = trigger_links[0]
        assert link["dst"]["project"] is not None
        assert rollup["projects"][link["dst"]["project"]]["project"] \
            == "octo-org/shared"


# ---------------------------------------------------------------------------
# Config and auth
# ---------------------------------------------------------------------------

class TestConfig:
    def test_separate_file_from_gitlab(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        from pipeview.github.config import config_path
        assert config_path().endswith(os.path.join("pipeview", "github.json"))

    def test_track_roundtrip_0600(self, tmp_path):
        cfg = GitHubConfig(str(tmp_path / "github.json"))
        assert cfg.track(HOST, "octo-org/app") is True
        assert cfg.track(HOST, "octo-org/app", "dev") is True
        cfg.save()
        mode = stat.S_IMODE(os.stat(cfg.path).st_mode)
        assert mode == 0o600
        again = GitHubConfig(cfg.path)
        assert again.tracked(HOST) == ["octo-org/app", "octo-org/app@dev"]


class TestAuth:
    def test_resolution_order(self, tmp_path):
        cfg = GitHubConfig(str(tmp_path / "github.json"))
        cfg.set_token(HOST, "stored-token")
        token, source = auth_mod.resolve_token(HOST, "flag-token", cfg, {})
        assert (token, source) == ("flag-token", "--token flag")
        token, source = auth_mod.resolve_token(
            HOST, None, cfg, {"GITHUB_TOKEN": "env-token"})
        assert (token, source) == ("env-token", "$GITHUB_TOKEN")
        token, source = auth_mod.resolve_token(HOST, None, cfg, {})
        assert token == "stored-token"

    def test_gh_token_env(self, tmp_path):
        cfg = GitHubConfig(str(tmp_path / "github.json"))
        token, source = auth_mod.resolve_token(
            HOST, None, cfg, {"GH_TOKEN": "gh-cli"})
        assert (token, source) == ("gh-cli", "$GH_TOKEN")

    def test_token_creation_url(self):
        url = auth_mod.token_creation_url("github.com")
        assert url.startswith("https://github.com/settings/tokens/new?")
        assert "description=pipeview" in url


# ---------------------------------------------------------------------------
# CLI (client swapped for the fake)
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch, fake):
    monkeypatch.setenv("PIPEVIEW_GITHUB_CONFIG",
                       str(tmp_path / "github.json"))
    monkeypatch.setattr(github_cli, "_make_client",
                        lambda args, config, host: fake)
    return tmp_path


class TestCli:
    def test_repos_listing(self, cli_env, capsys):
        assert github_cli.main(["repos"]) == 0
        out = capsys.readouterr().out
        assert "octo-org/app" in out and "octo-org/shared" in out

    def test_track_sync_rollup(self, cli_env, capsys):
        out_dir = str(cli_env / "out")
        assert github_cli.main(["track", "octo-org/app"]) == 0
        assert github_cli.main(["track", "octo-org/shared@v2"]) == 0
        assert github_cli.main(["tracked"]) == 0
        # shared@v2 has no default-branch tree entry conflict; sync both
        code = github_cli.main(["sync", "-o", out_dir])
        out = capsys.readouterr().out
        assert "octo-org/app: " in out
        assert "rollup: " in out
        assert os.path.isfile(os.path.join(out_dir, "rollup.report.html"))
        rollup = json.loads(
            open(os.path.join(out_dir, "rollup.json")).read())
        assert {p["project"] for p in rollup["projects"]} \
            == {"octo-org/app", "octo-org/shared"}
        assert code in (0, 1)

    def test_report_command(self, cli_env, capsys):
        out_dir = str(cli_env / "out")
        code = github_cli.main(["report", "octo-org/app", "-o", out_dir])
        assert code in (0, 1)
        out = capsys.readouterr().out
        assert "octo-org-app@main.report.html" in out

    def test_untrack_all_refs(self, cli_env, capsys):
        github_cli.main(["track", "octo-org/app@v1"])
        github_cli.main(["track", "octo-org/app@v2"])
        assert github_cli.main(["untrack", "octo-org/app"]) == 0
        assert "2 ref entries" in capsys.readouterr().out

    def test_default_host_is_github_com(self, cli_env):
        cfg = GitHubConfig(os.environ["PIPEVIEW_GITHUB_CONFIG"])

        class A:
            host = None
        assert github_cli._resolve_host(A(), cfg) == "https://github.com"
