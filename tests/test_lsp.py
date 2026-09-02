"""Tests for `pipeview lsp` — the stdio language server.

The server class is driven in-process (its `send` callback collects
server->client messages); framing is tested against byte streams.
No test touches a network.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeview import browser as browser_mod
from pipeview import lsp as lsp_mod
from pipeview.lsp import (
    CMD_OPEN_REPORT,
    CMD_OPEN_REPORT_OFFLINE,
    LspServer,
    cache_home,
    default_outdir,
    find_root,
    index_to_utf16,
    path_to_uri,
    read_message,
    uri_to_path,
    utf16_to_index,
    write_message,
)


@pytest.fixture(autouse=True)
def _not_wsl(monkeypatch):
    monkeypatch.setattr(browser_mod, "is_wsl", lambda: False)


GITLAB_OK = """\
stages: [build]
build_job:
  stage: build
  script: [make]
"""

GITLAB_BROKEN_INCLUDE = """\
stages: [build]
include:
  - local: ci/missing.yml
build_job:
  stage: build
  script: [make]
"""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Client:
    """Drives an LspServer in-process."""

    def __init__(self, options: dict | None = None):
        self.out: list[dict] = []
        self.server = LspServer(self.out.append)
        self._id = 0
        self.request("initialize",
                     {"initializationOptions": options or {}})
        self.notify("initialized", {})

    def request(self, method, params):
        self._id += 1
        resp = self.server.dispatch(
            {"jsonrpc": "2.0", "id": self._id, "method": method,
             "params": params})
        assert resp is not None and "error" not in resp, resp
        return resp["result"]

    def notify(self, method, params):
        assert self.server.dispatch(
            {"jsonrpc": "2.0", "method": method, "params": params}) is None

    def open(self, path, text=None):
        if text is None:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        uri = path_to_uri(str(path))
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "yaml", "version": 1, "text": text}})
        return uri

    def diagnostics_for(self, uri):
        published = [m["params"] for m in self.out
                     if m.get("method") == "textDocument/publishDiagnostics"
                     and m["params"]["uri"] == uri]
        return published[-1]["diagnostics"] if published else None

    def messages(self):
        return [m["params"] for m in self.out
                if m.get("method") == "window/showMessage"]


def write_tree(base, files):
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# ---------------------------------------------------------------------------
# Framing + plumbing
# ---------------------------------------------------------------------------

class TestFraming:
    def test_round_trip(self):
        buf = io.BytesIO()
        write_message(buf, {"jsonrpc": "2.0", "id": 1, "method": "x",
                            "params": {"täst": "välue"}})
        buf.seek(0)
        assert read_message(buf) == {"jsonrpc": "2.0", "id": 1, "method": "x",
                                     "params": {"täst": "välue"}}

    def test_eof_returns_none(self):
        assert read_message(io.BytesIO(b"")) is None

    def test_truncated_body_returns_none(self):
        assert read_message(io.BytesIO(b"Content-Length: 99\r\n\r\n{}")) is None

    def test_malformed_body_skipped_not_eof(self):
        # The frame was consumed whole, so the stream stays in sync: the
        # bad body must not end the serve loop like an EOF would.
        buf = io.BytesIO()
        buf.write(b"Content-Length: 8\r\n\r\nnot-json")
        write_message(buf, {"jsonrpc": "2.0", "method": "after", "params": {}})
        buf.seek(0)
        assert read_message(buf) == {}
        assert read_message(buf)["method"] == "after"

    def test_uri_path_round_trip(self, tmp_path):
        p = str(tmp_path / "a dir" / "x.yml")
        assert uri_to_path(path_to_uri(p)) == p

    def test_utf16_offset_round_trip(self):
        line = 'x: "🚀 $CI_COMMIT_SHA"'
        idx = line.index("CI_COMMIT_SHA")
        col = index_to_utf16(line, idx)
        assert col == idx + 1          # the emoji is 2 UTF-16 units
        assert utf16_to_index(line, col) == idx


class TestFindRoot:
    def test_roots_are_themselves(self, tmp_path):
        write_tree(tmp_path, {"Makefile": "all:\n", ".gitlab-ci.yml": "a:\n"})
        assert find_root(str(tmp_path / "Makefile")) == \
            (str(tmp_path / "Makefile"), "makefile")
        assert find_root(str(tmp_path / ".gitlab-ci.yml")) == \
            (str(tmp_path / ".gitlab-ci.yml"), "gitlab_yaml")

    def test_included_yaml_walks_up(self, tmp_path):
        write_tree(tmp_path, {".gitlab-ci.yml": "a:\n", "ci/x.yml": "b:\n"})
        assert find_root(str(tmp_path / "ci" / "x.yml")) == \
            (str(tmp_path / ".gitlab-ci.yml"), "gitlab_yaml")

    def test_unrelated_yaml_is_none(self, tmp_path):
        write_tree(tmp_path, {"conf/app.yml": "a: 1\n"})
        assert find_root(str(tmp_path / "conf" / "app.yml")) is None

    def test_mk_finds_makefile(self, tmp_path):
        write_tree(tmp_path, {"Makefile": "all:\n", "mk/vars.mk": "X=1\n"})
        assert find_root(str(tmp_path / "mk" / "vars.mk")) == \
            (str(tmp_path / "Makefile"), "makefile")

    def test_workflow_file_finds_workflows_dir(self, tmp_path):
        write_tree(tmp_path, {".github/workflows/ci.yml": "on: push\n"})
        assert find_root(str(tmp_path / ".github" / "workflows" / "ci.yml")) \
            == (str(tmp_path / ".github" / "workflows"), "github_workflows")

    def test_workflow_beats_gitlab_walk_up(self, tmp_path):
        # A repo can carry both CI systems: workflow files belong to the
        # GitHub root even when .gitlab-ci.yml exists above them.
        write_tree(tmp_path, {".gitlab-ci.yml": "a:\n",
                              ".github/workflows/ci.yml": "on: push\n"})
        _, kind = find_root(str(tmp_path / ".github" / "workflows" / "ci.yml"))
        assert kind == "github_workflows"


# ---------------------------------------------------------------------------
# Lifecycle + diagnostics
# ---------------------------------------------------------------------------

class TestLifecycle:
    def test_initialize_capabilities(self):
        client = Client()
        caps = client.request("initialize", {})["capabilities"]
        assert caps["hoverProvider"] is True
        assert set(caps["executeCommandProvider"]["commands"]) == \
            {CMD_OPEN_REPORT, CMD_OPEN_REPORT_OFFLINE}

    def test_unknown_request_gets_error(self):
        client = Client()
        resp = client.server.dispatch(
            {"jsonrpc": "2.0", "id": 99, "method": "nope/nope", "params": {}})
        assert resp["error"]["code"] == -32601

    def test_unknown_notification_ignored(self):
        client = Client()
        assert client.server.dispatch(
            {"jsonrpc": "2.0", "method": "nope/nope", "params": {}}) is None

    def test_exit_after_shutdown_is_clean(self):
        client = Client()
        client.request("shutdown", {})
        client.notify("exit", {})
        assert client.server.exited
        assert client.server.exit_code == 0

    def test_exit_without_shutdown_is_error(self):
        # LSP: exit without a prior shutdown request must exit with 1.
        client = Client()
        client.notify("exit", {})
        assert client.server.exited
        assert client.server.exit_code == 1


class TestDiagnostics:
    def test_broken_include_published_then_cleared(self, tmp_path):
        root = tmp_path / ".gitlab-ci.yml"
        root.write_text(GITLAB_BROKEN_INCLUDE)
        client = Client()
        uri = client.open(root)
        diags = client.diagnostics_for(uri)
        assert diags and any("missing.yml" in d["message"] for d in diags)
        assert all(d["source"] == "pipeview" for d in diags)

        root.write_text(GITLAB_OK)
        client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})
        assert client.diagnostics_for(uri) == []

    def test_diagnostic_lands_on_its_file(self, tmp_path):
        write_tree(tmp_path, {
            ".gitlab-ci.yml": "include:\n  - local: ci/jobs.yml\n"
                              "build:\n  script: [x]\n",
            # a job with an unknown needs target -> diagnostic in this file
            "ci/jobs.yml": "test_job:\n  script: [t]\n  needs: [ghost_job]\n",
        })
        client = Client()
        client.open(tmp_path / ".gitlab-ci.yml")
        sub_uri = path_to_uri(str(tmp_path / "ci" / "jobs.yml"))
        diags = client.diagnostics_for(sub_uri)
        assert diags and any("ghost_job" in d["message"] for d in diags)

    def test_unrelated_yaml_stays_silent(self, tmp_path):
        write_tree(tmp_path, {"conf/app.yml": "a: 1\n"})
        client = Client()
        uri = client.open(tmp_path / "conf" / "app.yml")
        assert client.diagnostics_for(uri) is None

    def test_makefile_analysis_never_enriches(self, tmp_path, monkeypatch):
        # enrichment would execute make; the LSP must never call it
        import pipeview.parsers.enrich as enrich_mod
        monkeypatch.setattr(
            enrich_mod, "enrich_make_report",
            lambda *a, **k: pytest.fail("enrichment invoked from the LSP"))
        write_tree(tmp_path, {"Makefile": "all: dep\n\techo hi\n"})
        client = Client()
        client.open(tmp_path / "Makefile")


# ---------------------------------------------------------------------------
# Hover, links, code actions
# ---------------------------------------------------------------------------

class TestHover:
    def _client_with_doc(self, tmp_path, line):
        write_tree(tmp_path, {".gitlab-ci.yml": line + "\n"})
        client = Client()
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        return client, uri

    def test_predefined_variable_docs(self, tmp_path):
        client, uri = self._client_with_doc(
            tmp_path, 'job:\n  script: [echo "$CI_COMMIT_SHA"]')
        hover = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": 20},
        })
        assert "CI_COMMIT_SHA" in hover["contents"]["value"]
        assert "predefined GitLab CI variable" in hover["contents"]["value"]

    def test_unknown_word_no_hover(self, tmp_path):
        client, uri = self._client_with_doc(tmp_path, "job:\n  script: [make]")
        hover = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": 12},
        })
        assert hover is None

    def test_hover_position_is_utf16(self, tmp_path):
        # An astral-plane char before the variable shifts UTF-16 columns
        # by one relative to Python indices.
        line = '  script: [echo "🚀 $CI_COMMIT_SHA"]'
        client, uri = self._client_with_doc(tmp_path, "job:\n" + line)
        idx = line.index("CI_COMMIT_SHA")
        hover = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": index_to_utf16(line, idx)},
        })
        assert "CI_COMMIT_SHA" in hover["contents"]["value"]
        assert hover["range"]["start"]["character"] == idx + 1


class TestDocumentLinks:
    def test_local_include_becomes_link(self, tmp_path):
        write_tree(tmp_path, {
            ".gitlab-ci.yml": "include:\n  - local: ci/build.yml\n",
            "ci/build.yml": "b:\n  script: [x]\n",
        })
        client = Client()
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        links = client.request("textDocument/documentLink",
                               {"textDocument": {"uri": uri}})
        assert len(links) == 1
        assert links[0]["target"].endswith("ci/build.yml")
        assert links[0]["range"]["start"]["line"] == 1

    def test_missing_target_no_link(self, tmp_path):
        write_tree(tmp_path, {".gitlab-ci.yml":
                              "include:\n  - local: ci/nope.yml\n"})
        client = Client()
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        assert client.request("textDocument/documentLink",
                              {"textDocument": {"uri": uri}}) == []

    def test_commented_out_include_no_link(self, tmp_path):
        write_tree(tmp_path, {
            ".gitlab-ci.yml": "include:\n  # - local: ci/build.yml\n",
            "ci/build.yml": "b:\n  script: [x]\n",
        })
        client = Client()
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        assert client.request("textDocument/documentLink",
                              {"textDocument": {"uri": uri}}) == []


class TestCodeActions:
    def test_gitlab_root_offers_both_actions(self, tmp_path):
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        client = Client()
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        actions = client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 0}},
            "context": {"diagnostics": []},
        })
        commands = [a["command"]["command"] for a in actions]
        assert commands == [CMD_OPEN_REPORT, CMD_OPEN_REPORT_OFFLINE]

    def test_upstream_disabled_hides_offline_variant(self, tmp_path):
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        client = Client({"upstream": False})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        actions = client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 0}},
            "context": {"diagnostics": []},
        })
        assert [a["command"]["command"] for a in actions] == [CMD_OPEN_REPORT]

    def test_makefile_offers_report_only(self, tmp_path):
        write_tree(tmp_path, {"Makefile": "all:\n\techo hi\n"})
        client = Client()
        uri = client.open(tmp_path / "Makefile")
        actions = client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 0}},
            "context": {"diagnostics": []},
        })
        assert [a["command"]["command"] for a in actions] == [CMD_OPEN_REPORT]


# ---------------------------------------------------------------------------
# GitHub Actions integration
# ---------------------------------------------------------------------------

WORKFLOW_OK = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: make}]
"""

WORKFLOW_BAD_NEEDS = """\
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: make}]
  test:
    needs: [ghost]
    runs-on: ubuntu-latest
    steps: [{run: make test}]
"""


class TestGitHubIntegration:
    def test_diagnostics_land_on_workflow_file(self, tmp_path):
        write_tree(tmp_path, {".github/workflows/ci.yml": WORKFLOW_BAD_NEEDS})
        client = Client()
        uri = client.open(tmp_path / ".github" / "workflows" / "ci.yml")
        diags = client.diagnostics_for(uri)
        assert diags and any("ghost" in d["message"] for d in diags)

    def test_hover_uses_github_catalog(self, tmp_path):
        write_tree(tmp_path, {".github/workflows/ci.yml":
                              'on: push\n# echo "$GITHUB_REPOSITORY"\n'})
        client = Client()
        uri = client.open(tmp_path / ".github" / "workflows" / "ci.yml")
        hover = client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 1, "character": 12},
        })
        assert "GITHUB_REPOSITORY" in hover["contents"]["value"]
        assert "GitHub Actions" in hover["contents"]["value"]
        # ...and the GitLab-only variable is unknown here
        assert client.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 1},
        }) is None

    def test_local_uses_become_links(self, tmp_path):
        write_tree(tmp_path, {
            ".github/workflows/ci.yml":
                "on: push\njobs:\n  reuse:\n"
                "    uses: ./.github/workflows/reuse.yml\n"
                "  act:\n    steps:\n      - uses: ./tools/lint\n",
            ".github/workflows/reuse.yml": "on: workflow_call\n",
            "tools/lint/action.yml": "runs: {using: composite}\n",
        })
        client = Client()
        uri = client.open(tmp_path / ".github" / "workflows" / "ci.yml")
        links = client.request("textDocument/documentLink",
                               {"textDocument": {"uri": uri}})
        targets = sorted(link["target"] for link in links)
        assert len(targets) == 2
        assert targets[0].endswith("reuse.yml")
        assert targets[1].endswith("tools/lint/action.yml")

    def test_code_action_single_no_upstream_variant(self, tmp_path):
        write_tree(tmp_path, {".github/workflows/ci.yml": WORKFLOW_OK})
        client = Client()
        uri = client.open(tmp_path / ".github" / "workflows" / "ci.yml")
        actions = client.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0, "character": 0}},
            "context": {"diagnostics": []},
        })
        assert [a["command"]["command"] for a in actions] == [CMD_OPEN_REPORT]

    def test_report_generated_and_opened(self, tmp_path, monkeypatch):
        write_tree(tmp_path, {".github/workflows/ci.yml": WORKFLOW_OK})
        opened = []
        monkeypatch.setattr(browser_mod.webbrowser, "open",
                            lambda url: opened.append(url) or True)
        outdir = str(tmp_path / "out")
        client = Client({"outputDir": outdir})
        uri = client.open(tmp_path / ".github" / "workflows" / "ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT, "arguments": [uri]})
        html = os.path.join(outdir, "github-actions.report.html")
        assert os.path.isfile(html)
        assert opened == [path_to_uri(html)]
        # a github root never fetches, whatever the upstream setting says
        server = client.server
        argv = server.report_argv(
            str(tmp_path / ".github" / "workflows"), "github_workflows",
            upstream=True)
        assert "--upstream" not in argv


# ---------------------------------------------------------------------------
# Report commands
# ---------------------------------------------------------------------------

class TestExecuteCommand:
    def test_offline_report_generated_and_opened(self, tmp_path, monkeypatch):
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        opened = []
        monkeypatch.setattr(browser_mod.webbrowser, "open",
                            lambda url: opened.append(url) or True)
        outdir = str(tmp_path / "out")
        client = Client({"outputDir": outdir})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT_OFFLINE, "arguments": [uri]})
        html = os.path.join(outdir, "gitlab-ci.report.html")
        assert os.path.isfile(html)
        assert opened == [path_to_uri(html)]
        assert any("report opened" in m["message"] for m in client.messages())

    def test_open_report_argv_carries_upstream(self, tmp_path):
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        server = LspServer(lambda m: None)
        server.options = {"upstream": True, "upstreamRemote": "fork",
                          "outputDir": str(tmp_path / "o")}
        root = str(tmp_path / ".gitlab-ci.yml")
        argv = server.report_argv(root, "gitlab_yaml", upstream=True)
        assert "--upstream" in argv
        assert argv[argv.index("--upstream-remote") + 1] == "fork"
        # the offline command and Makefile roots never fetch
        assert "--upstream" not in server.report_argv(
            root, "gitlab_yaml", upstream=False)
        assert "--upstream" not in server.report_argv(
            root, "makefile", upstream=True)

    def test_default_outdir_outside_repo(self, tmp_path):
        root = str(tmp_path / ".gitlab-ci.yml")
        out = default_outdir(root)
        assert not out.startswith(str(tmp_path))
        assert "pipeview" in out

    def test_argparse_rejection_does_not_kill_server(self, tmp_path,
                                                     monkeypatch):
        # An outputDir starting with '-' makes argparse SystemExit inside
        # the in-process CLI call; the server must survive and report.
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        monkeypatch.setattr(browser_mod.webbrowser, "open",
                            lambda url: pytest.fail("opened despite failure"))
        client = Client({"outputDir": "-reports"})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT_OFFLINE, "arguments": [uri]})
        assert any(m["type"] == 1 and "failed" in m["message"]
                   for m in client.messages())
        assert not client.server.exited

    def test_failure_reports_error_message(self, tmp_path, monkeypatch):
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        monkeypatch.setattr(lsp_mod, "_cli_main", lambda argv: 2)
        monkeypatch.setattr(browser_mod.webbrowser, "open",
                            lambda url: pytest.fail("opened despite failure"))
        client = Client({"outputDir": str(tmp_path / "empty")})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT, "arguments": [uri]})
        assert any(m["type"] == 1 and "failed" in m["message"]
                   for m in client.messages())

    def test_stdout_stays_clean_during_generation(self, tmp_path, monkeypatch,
                                                  capsys):
        # cli.main prints to stdout — the LSP protocol channel. The server
        # must capture it.
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        monkeypatch.setattr(browser_mod.webbrowser, "open", lambda url: None)
        client = Client({"outputDir": str(tmp_path / "out")})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT_OFFLINE, "arguments": [uri]})
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _lsp_session(*messages: dict) -> bytes:
    out = io.BytesIO()
    for m in messages:
        write_message(out, m)
    return out.getvalue()


class TestEntryPoints:
    """`pipeview lsp` is one spelling; editors that take a bare
    executable and no arguments (Zed's lsp.<name>.binary.path runs
    exactly that — the extension is bypassed) need `pipeview-lsp` or
    `python -m pipeview.lsp` to reach the same server."""

    def test_console_script_declared(self):
        from importlib import import_module

        import tomllib

        pyproject = tomllib.loads(
            (Path(__file__).parent.parent / "pyproject.toml").read_text())
        target = pyproject["project"]["scripts"]["pipeview-lsp"]
        module, func = target.split(":")
        assert getattr(import_module(module), func) is lsp_mod.main

    def test_module_runs_as_script(self):
        # `python -m pipeview.lsp -v`: a full initialize/shutdown/exit
        # handshake over real pipes, exit 0, logging on stderr, nothing
        # but framed JSON on stdout.
        proc = subprocess.run(
            [sys.executable, "-m", "pipeview.lsp", "-v"],
            input=_lsp_session(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"initializationOptions": {"announce": False}}},
                {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
                {"jsonrpc": "2.0", "method": "exit"},
            ),
            capture_output=True, timeout=60,
            cwd=str(Path(__file__).parent.parent),
        )
        assert proc.returncode == 0, proc.stderr.decode()
        assert b"serving on stdio" in proc.stderr
        first = read_message(io.BytesIO(proc.stdout))
        assert first["id"] == 1
        assert first["result"]["serverInfo"]["name"] == "pipeview"

    def test_cli_subcommand_reaches_the_same_server(self):
        proc = subprocess.run(
            [sys.executable, "-m", "pipeview", "lsp"],
            input=_lsp_session(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
                {"jsonrpc": "2.0", "method": "exit"},
            ),
            capture_output=True, timeout=60,
            cwd=str(Path(__file__).parent.parent),
        )
        assert proc.returncode == 0, proc.stderr.decode()
        assert read_message(io.BytesIO(proc.stdout))["id"] == 1


# ---------------------------------------------------------------------------
# Discoverability
# ---------------------------------------------------------------------------

class TestAnnouncement:
    def test_attach_is_announced_once(self):
        # Zed cannot host palette commands for the extension, so the
        # server itself says what it offers, once, when the client is up.
        client = Client()
        infos = [m for m in client.messages() if m["type"] == 3]
        assert [m["message"] for m in infos] == [lsp_mod.ANNOUNCEMENT]
        assert "ctrl-." in lsp_mod.ANNOUNCEMENT
        # nothing else chatters before a document is opened
        assert len(client.messages()) == 1

    def test_announcement_can_be_silenced(self):
        client = Client({"announce": False})
        assert client.messages() == []


# ---------------------------------------------------------------------------
# Platform paths
# ---------------------------------------------------------------------------

class TestWindowsPaths:
    def test_drive_letter_uri(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        assert uri_to_path("file:///c%3A/proj/Makefile") == \
            "c:\\proj\\Makefile"
        assert uri_to_path("file:///C:/proj/.gitlab-ci.yml") == \
            "C:\\proj\\.gitlab-ci.yml"

    def test_unc_authority_is_kept(self, monkeypatch):
        # Zed sends \\\\server\\share\\... worktrees as file://server/share/...
        monkeypatch.setattr(os, "name", "nt")
        assert uri_to_path("file://fileserver/proj/Makefile") == \
            "\\\\fileserver\\proj\\Makefile"
        assert uri_to_path("file://wsl.localhost/Ubuntu/home/u/p/Makefile") \
            == "\\\\wsl.localhost\\Ubuntu\\home\\u\\p\\Makefile"
        # localhost is not a UNC host
        assert uri_to_path("file://localhost/C:/proj/Makefile") == \
            "C:\\proj\\Makefile"

    def test_posix_uri_unchanged(self, tmp_path):
        p = tmp_path / "Makefile"
        assert uri_to_path(path_to_uri(str(p))) == str(p)

    def test_cache_home_windows(self, monkeypatch):
        monkeypatch.setattr(os, "name", "nt")
        monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\u\\AppData\\Local")
        monkeypatch.setenv("XDG_CACHE_HOME", "/ignored")
        assert cache_home() == "C:\\Users\\u\\AppData\\Local"

    def test_cache_home_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert cache_home() == str(tmp_path)
        monkeypatch.delenv("XDG_CACHE_HOME")
        assert cache_home().endswith(".cache")


class TestBrowserlessHost:
    def test_no_browser_is_reported_not_claimed(self, tmp_path, monkeypatch):
        # WSL without wslu, a headless box over SSH: the report exists but
        # nothing could open it — say so instead of "report opened".
        write_tree(tmp_path, {".gitlab-ci.yml": GITLAB_OK})
        monkeypatch.setattr(browser_mod.webbrowser, "open", lambda url: False)
        outdir = str(tmp_path / "out")
        client = Client({"outputDir": outdir})
        uri = client.open(tmp_path / ".gitlab-ci.yml")
        client.request("workspace/executeCommand", {
            "command": CMD_OPEN_REPORT_OFFLINE, "arguments": [uri]})
        html = os.path.join(outdir, "gitlab-ci.report.html")
        assert os.path.isfile(html)
        msgs = client.messages()
        assert any(m["type"] == 2 and "no browser" in m["message"]
                   and html in m["message"] for m in msgs)
        assert not any("report opened" in m["message"] for m in msgs)

