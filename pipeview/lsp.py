"""`pipeview lsp` — a small language server over stdio.

Editor-agnostic pipeview features for editors that speak LSP (the Zed
extension is the first client): parser diagnostics published on
open/save — for Makefiles, GitLab CI trees, and GitHub Actions
workflow directories alike — hover docs for predefined CI variables
(the GitLab and GitHub catalogs), document links for include:local and
local `uses:` references, and code actions that generate the pipeline
report and open it in the default browser — the report is
self-contained file:// HTML, so any browser is a full viewer.

Stdlib-only JSON-RPC 2.0 with Content-Length framing. Protocol stdout
is sacred: report generation calls the ordinary CLI in-process with
stdout captured, and logging goes to stderr.

The analysis never runs Make enrichment (no `$(shell)` execution from
an editor loop) and never touches the network on save — `--upstream`
fetching happens only inside the explicit report commands, governed by
the client's initializationOptions.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import logging
import ntpath
import os
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

from pipeview.browser import open_in_browser
from pipeview.cli import main as _cli_main
from pipeview.parsers.github_parser import parse_github
from pipeview.parsers.github_predefined import (
    PREDEFINED_VAR_DOCS as GITHUB_VAR_DOCS,
)
from pipeview.parsers.gitlab_parser import parse_gitlab
from pipeview.parsers.gitlab_predefined import (
    PREDEFINED_VAR_DOCS as GITLAB_VAR_DOCS,
)
from pipeview.parsers.make_parser import parse_makefile

log = logging.getLogger(__name__)

_MAKE_NAMES = ("Makefile", "makefile", "GNUmakefile")
_MAX_WALK_UP = 30

_SEVERITY = {"error": 1, "warning": 2, "info": 3}

CMD_OPEN_REPORT = "pipeview.openReport"
CMD_OPEN_REPORT_OFFLINE = "pipeview.openReportOffline"

ANNOUNCEMENT = (
    "pipeview attached: diagnostics on save, hover on CI_*/GITHUB_* "
    "variables, and the code action (ctrl-. / cmd-.) \u201cPipeview: open "
    "pipeline report (browser)\u201d on any line of a Makefile, "
    ".gitlab-ci.yml, or .github/workflows file."
)

_LOCAL_INCLUDE_RE = re.compile(
    r"""(?:-\s*)?local:\s*(['"]?)(?P<path>[^'"#\s]+)\1"""
)
# GitHub local references: `uses: ./.github/workflows/x.yml` (reusable
# workflow) or `uses: ./path/to/action` (local composite action).
_USES_LOCAL_RE = re.compile(
    r"""uses:\s*(['"]?)(?P<path>\./[^'"\s#@]+)\1"""
)
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def read_message(stream) -> dict | None:
    """One framed JSON-RPC message, or None on EOF/garbage framing."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break  # end of headers
        if line.lower().startswith(b"content-length:"):
            try:
                length = int(line.split(b":", 1)[1])
            except ValueError:
                return None
    if length is None:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # The frame was consumed whole, so the stream is still in sync —
        # skip the unparseable body rather than treating it as EOF (only
        # framing damage, where our position is unknown, ends the loop).
        log.warning("skipping unparseable message body (%d bytes)", length)
        return {}


def write_message(stream, message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    stream.write(b"Content-Length: %d\r\n\r\n" % len(body))
    stream.write(body)
    stream.flush()


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def utf16_to_index(line: str, col: int) -> int:
    """Python string index for a UTF-16 code-unit column (LSP positions
    default to UTF-16, and this server never negotiates otherwise)."""
    units = 0
    for i, ch in enumerate(line):
        if units >= col:
            return i
        units += 2 if ord(ch) > 0xFFFF else 1
    return len(line)


def index_to_utf16(line: str, index: int) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in line[:index])


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    path = unquote(parsed.path)
    if os.name == "nt":
        host = parsed.netloc
        if host and host.lower() != "localhost":
            # file://server/share/x is a UNC path (\\server\share\x):
            # network shares and \\wsl.localhost\<distro>\... worktrees
            # opened as local folders. Dropping the authority would
            # resolve it against the current drive instead.
            return ntpath.abspath("\\\\" + host + path.replace("/", "\\"))
        if re.match(r"^/[A-Za-z]:", path):
            path = path[1:]
        return ntpath.abspath(path)
    return os.path.abspath(path)


def path_to_uri(path: str) -> str:
    return Path(os.path.abspath(path)).as_uri()


def find_root(path: str) -> tuple[str, str] | None:
    """(root_path, kind) the buffer at `path` belongs to, or None.

    Makefiles and .gitlab-ci.yml are roots themselves; a YAML file in
    .github/workflows/ belongs to that directory (the GitHub Actions
    root); *.mk and other YAML files belong to the nearest ancestor
    directory holding a root. Unrelated YAML gets None — the server
    stays silent on it.
    """
    base = os.path.basename(path)
    if base in _MAKE_NAMES:
        return path, "makefile"
    if base == ".gitlab-ci.yml":
        return path, "gitlab_yaml"
    if base.endswith((".yml", ".yaml")):
        d = os.path.dirname(os.path.abspath(path))
        if (os.path.basename(d) == "workflows"
                and os.path.basename(os.path.dirname(d)) == ".github"):
            return d, "github_workflows"
    if base.endswith(".mk"):
        names, kind = _MAKE_NAMES, "makefile"
    elif base.endswith((".yml", ".yaml")):
        names, kind = (".gitlab-ci.yml",), "gitlab_yaml"
    else:
        return None
    d = os.path.dirname(os.path.abspath(path))
    for _ in range(_MAX_WALK_UP):
        for name in names:
            candidate = os.path.join(d, name)
            if os.path.isfile(candidate):
                return candidate, kind
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent
    return None


def cache_home() -> str:
    """The user's cache directory: $XDG_CACHE_HOME / ~/.cache, or
    %LOCALAPPDATA% on Windows."""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
        return local
    return os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache")


def default_outdir(root_path: str) -> str:
    cache = cache_home()
    root_dir = os.path.dirname(os.path.abspath(root_path))
    digest = hashlib.sha1(root_dir.encode()).hexdigest()[:10]
    return os.path.join(cache, "pipeview", "lsp",
                        f"{os.path.basename(root_dir) or 'root'}-{digest}")


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------

class LspServer:
    """Protocol-level state machine; `send` receives every server->client
    message (notifications), so tests drive it without any I/O."""

    def __init__(self, send):
        self._send = send
        self.docs: dict[str, str] = {}          # uri -> buffer text
        self.options: dict = {}                  # initializationOptions
        # analysis root -> uris we last published diagnostics for, so a
        # re-analysis clears its own stale uris and nobody else's
        self._published: dict[str, set[str]] = {}
        self._shutdown_received = False
        self.exited = False
        self.exit_code = 0

    # -- dispatch -----------------------------------------------------------

    def dispatch(self, message: dict) -> dict | None:
        method = message.get("method")
        params = message.get("params") or {}
        msg_id = message.get("id")
        if method is None:
            return None  # a response to a server request — nothing pending
        try:
            handler = self._handlers().get(method)
            if handler is None:
                if msg_id is not None:
                    return _error(msg_id, -32601, f"method not found: {method}")
                return None
            result = handler(params)
        except Exception as e:   # never kill the stream over one message
            log.exception("error handling %s", method)
            if msg_id is not None:
                return _error(msg_id, -32603, f"{type(e).__name__}: {e}")
            return None
        if msg_id is not None:
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        return None

    def _handlers(self):
        return {
            "initialize": self._initialize,
            "initialized": self._initialized,
            "shutdown": self._shutdown,
            "exit": self._exit,
            "$/cancelRequest": lambda p: None,
            "textDocument/didOpen": self._did_open,
            "textDocument/didChange": self._did_change,
            "textDocument/didSave": self._did_save,
            "textDocument/didClose": self._did_close,
            "textDocument/hover": self._hover,
            "textDocument/documentLink": self._document_links,
            "textDocument/codeAction": self._code_actions,
            "workspace/executeCommand": self._execute_command,
        }

    # -- lifecycle ----------------------------------------------------------

    def _initialize(self, params: dict):
        opts = params.get("initializationOptions")
        if isinstance(opts, dict):
            self.options = opts
        import pipeview
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 1,
                                     "save": True},
                "hoverProvider": True,
                "documentLinkProvider": {},
                "codeActionProvider": {"codeActionKinds": ["source"]},
                "executeCommandProvider": {
                    "commands": [CMD_OPEN_REPORT, CMD_OPEN_REPORT_OFFLINE],
                },
            },
            "serverInfo": {"name": "pipeview",
                           "version": pipeview.__version__},
        }

    def _initialized(self, params: dict):
        # Editors that host this server cannot add palette commands for
        # it (Zed extensions register language servers, nothing else), so
        # a first-time user has no way to discover what just attached.
        # Say it once per server start; initializationOptions
        # {"announce": false} silences it.
        if self.options.get("announce", True):
            self._show_message(3, ANNOUNCEMENT)
        return None

    def _shutdown(self, params: dict):
        self._shutdown_received = True
        return None

    def _exit(self, params: dict):
        self.exited = True
        # The spec: exit without a prior shutdown request is an error exit.
        self.exit_code = 0 if self._shutdown_received else 1
        return None

    # -- document sync ------------------------------------------------------

    def _did_open(self, params: dict):
        doc = params["textDocument"]
        self.docs[doc["uri"]] = doc.get("text", "")
        self._analyze(doc["uri"])

    def _did_change(self, params: dict):
        changes = params.get("contentChanges") or []
        if changes:
            # full sync: the last change carries the whole document
            self.docs[params["textDocument"]["uri"]] = changes[-1].get("text", "")

    def _did_save(self, params: dict):
        uri = params["textDocument"]["uri"]
        if "text" in params:
            self.docs[uri] = params["text"]
        self._analyze(uri)

    def _did_close(self, params: dict):
        self.docs.pop(params["textDocument"]["uri"], None)

    # -- diagnostics --------------------------------------------------------

    def _analyze(self, uri: str) -> None:
        root = find_root(uri_to_path(uri))
        if root is None:
            return  # unrelated buffer: stay silent
        root_path, kind = root
        try:
            if kind == "makefile":
                report = parse_makefile(root_path)  # never enriched here
            elif kind == "github_workflows":
                report = parse_github(root_path)
            else:
                report = parse_gitlab(root_path)
        except Exception:
            log.exception("analysis of %s failed", root_path)
            return
        # Diagnostic source paths are relative to the root's directory —
        # which is the root itself for the (directory) workflows root.
        root_dir = root_path if os.path.isdir(root_path) \
            else os.path.dirname(root_path)
        # Sourceless diagnostics pin to a real file, never a directory.
        pin = root_path if os.path.isfile(root_path) else uri_to_path(uri)
        per_file: dict[str, list[dict]] = {}
        for d in report.diagnostics:
            fp, line = pin, 0
            if d.source and d.source.file:
                candidate = d.source.file
                if not os.path.isabs(candidate):
                    candidate = os.path.join(root_dir, candidate)
                # Aliased paths (bundled templates etc.) are not real
                # files — pin their diagnostics to the root instead.
                if os.path.isfile(candidate):
                    fp = candidate
                    line = max(0, (d.source.line or 1) - 1)
            per_file.setdefault(os.path.abspath(fp), []).append({
                "range": {"start": {"line": line, "character": 0},
                          "end": {"line": line, "character": 10000}},
                "severity": _SEVERITY.get(d.severity, 3),
                "source": "pipeview",
                "message": d.message,
            })
        new_uris = set()
        for fp, diags in per_file.items():
            file_uri = path_to_uri(fp)
            new_uris.add(file_uri)
            self._publish(file_uri, diags)
        for stale in self._published.get(root_path, set()) - new_uris:
            self._publish(stale, [])
        self._published[root_path] = new_uris

    def _publish(self, uri: str, diagnostics: list[dict]) -> None:
        self._send({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diagnostics},
        })

    # -- hover --------------------------------------------------------------

    def _hover(self, params: dict):
        uri = params["textDocument"]["uri"]
        pos = params["position"]
        text = self.docs.get(uri)
        if text is None or not _is_yaml(uri):
            return None
        lines = text.split("\n")
        if pos["line"] >= len(lines):
            return None
        line = lines[pos["line"]]
        char = utf16_to_index(line, pos["character"])
        root = find_root(uri_to_path(uri))
        github = root is not None and root[1] == "github_workflows"
        catalog = GITHUB_VAR_DOCS if github else GITLAB_VAR_DOCS
        provider = "GitHub Actions" if github else "GitLab CI"
        for m in _WORD_RE.finditer(line):
            if m.start() <= char <= m.end():
                doc = catalog.get(m.group(0))
                if doc is None:
                    return None
                parts = [
                    f"**{m.group(0)}** — predefined {provider} variable",
                    doc["summary"],
                    f"- Example: `{doc['example']}`",
                    f"- Set: {doc['set_when']}",
                ]
                if doc.get("unset_when"):
                    parts.append(f"- Not set: {doc['unset_when']}")
                if doc.get("note"):
                    parts.append(f"- Note: {doc['note']}")
                return {
                    "contents": {"kind": "markdown",
                                 "value": "\n\n".join(parts)},
                    "range": {
                        "start": {"line": pos["line"],
                                  "character": index_to_utf16(line, m.start())},
                        "end": {"line": pos["line"],
                                "character": index_to_utf16(line, m.end())},
                    },
                }
        return None

    # -- document links -----------------------------------------------------

    def _document_links(self, params: dict):
        uri = params["textDocument"]["uri"]
        text = self.docs.get(uri)
        if text is None or not _is_yaml(uri):
            return []
        path = uri_to_path(uri)
        root = find_root(path)
        if root is not None and root[1] == "github_workflows":
            # `uses: ./…` resolves against the repository root
            repo_root = os.path.dirname(os.path.dirname(root[0]))
            return self._scan_links(text, _USES_LOCAL_RE, repo_root,
                                    action_dirs=True)
        root_dir = os.path.dirname(root[0] if root else path)
        return self._scan_links(text, _LOCAL_INCLUDE_RE, root_dir)

    def _scan_links(self, text: str, pattern: re.Pattern, base: str,
                    action_dirs: bool = False) -> list[dict]:
        links = []
        for i, line in enumerate(text.split("\n")):
            m = pattern.search(line)
            # a '#' before the match means the reference is commented out
            if not m or "#" in line[: m.start()]:
                continue
            rel = m.group("path")
            if rel.startswith("./"):
                rel = rel[2:]
            target = os.path.normpath(os.path.join(base, rel.lstrip("/")))
            if not os.path.isfile(target):
                if not action_dirs or not os.path.isdir(target):
                    continue
                # a local composite action: the directory's action.yml
                for name in ("action.yml", "action.yaml"):
                    candidate = os.path.join(target, name)
                    if os.path.isfile(candidate):
                        target = candidate
                        break
                else:
                    continue
            links.append({
                "range": {
                    "start": {"line": i,
                              "character": index_to_utf16(line, m.start("path"))},
                    "end": {"line": i,
                            "character": index_to_utf16(line, m.end("path"))},
                },
                "target": path_to_uri(target),
                "tooltip": "Open referenced file",
            })
        return links

    # -- report commands ----------------------------------------------------

    def _code_actions(self, params: dict):
        path = uri_to_path(params["textDocument"]["uri"])
        root = find_root(path)
        if root is None:
            return []
        _, kind = root
        uri = params["textDocument"]["uri"]
        actions = [_action("Pipeview: open pipeline report (browser)",
                           CMD_OPEN_REPORT, uri)]
        if kind == "gitlab_yaml" and self._upstream_enabled():
            actions.append(_action(
                "Pipeview: open pipeline report without upstream fetch",
                CMD_OPEN_REPORT_OFFLINE, uri))
        return actions

    def _execute_command(self, params: dict):
        command = params.get("command")
        args = params.get("arguments") or []
        if command not in (CMD_OPEN_REPORT, CMD_OPEN_REPORT_OFFLINE):
            raise ValueError(f"unknown command {command!r}")
        if not args:
            raise ValueError("missing document uri argument")
        root = find_root(uri_to_path(str(args[0])))
        if root is None:
            self._show_message(2, "pipeview: this file belongs to no "
                                  "Makefile or .gitlab-ci.yml root")
            return None
        root_path, kind = root
        argv = self.report_argv(root_path, kind,
                                upstream=command == CMD_OPEN_REPORT)
        log.info("generating report: pipeview %s", " ".join(argv))
        buf = io.StringIO()
        # cli.main prints to stdout — the LSP channel. Capture it.
        with contextlib.redirect_stdout(buf):
            try:
                code = _cli_main(argv)
            except SystemExit as e:
                # argparse rejects an argv it can't parse (e.g. an
                # outputDir starting with '-') via SystemExit, which must
                # not take the server down with it.
                code = e.code if isinstance(e.code, int) else 2
        from pipeview.cli import _output_basename
        outdir = argv[argv.index("-o") + 1]
        html = os.path.join(outdir,
                            f"{_output_basename(root_path, kind)}.report.html")
        if os.path.isfile(html):
            opened = _open_in_browser(html)
            if not opened:
                self._show_message(2, "pipeview: report generated but no "
                                      "browser could be opened — open it "
                                      f"manually: {html}")
            elif code == 0:
                self._show_message(3, f"pipeview: report opened ({html})")
            else:
                self._show_message(2, "pipeview: report opened with "
                                      f"diagnostics — see its Files tab ({html})")
            self._analyze(str(args[0]))  # refresh inline diagnostics too
        else:
            tail = "; ".join(buf.getvalue().strip().splitlines()[-3:])
            self._show_message(1, f"pipeview: report generation failed "
                                  f"(exit {code}). {tail}")
        return None

    def report_argv(self, root_path: str, kind: str, upstream: bool) -> list[str]:
        """CLI argv for one report — public so clients of the module (and
        tests) can see exactly what a command will run."""
        outdir = self.options.get("outputDir") or default_outdir(root_path)
        argv = [root_path, "-o", outdir, "--format", "html"]
        if kind == "gitlab_yaml" and upstream and self._upstream_enabled():
            argv.append("--upstream")
            remote = self.options.get("upstreamRemote")
            if remote:
                argv.extend(["--upstream-remote", str(remote)])
        return argv

    def _upstream_enabled(self) -> bool:
        return bool(self.options.get("upstream", True))

    def _show_message(self, type_: int, message: str) -> None:
        self._send({
            "jsonrpc": "2.0",
            "method": "window/showMessage",
            "params": {"type": type_, "message": message},
        })


def _open_in_browser(html: str) -> bool:
    """Open the report (see pipeview.browser for the per-platform and
    WSL handling) and say whether anything was launched.

    The launchers' children inherit fd 1 — the LSP protocol channel —
    and browsers print to it ("Opening in existing browser session.").
    Point fd 1 at devnull around the launch so their chatter cannot
    corrupt the framing; redirect_stdout alone only covers Python-level
    writes, not the file descriptor."""
    saved = os.dup(1)
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 1)
        finally:
            os.close(devnull)
        return open_in_browser(html)
    finally:
        os.dup2(saved, 1)
        os.close(saved)


def _action(title: str, command: str, uri: str) -> dict:
    return {
        "title": title,
        "kind": "source",
        "command": {"title": title, "command": command, "arguments": [uri]},
    }


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message}}


def _is_yaml(uri: str) -> bool:
    return uri.endswith((".yml", ".yaml"))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def serve(instream=None, outstream=None) -> int:
    stdin = instream if instream is not None else sys.stdin.buffer
    stdout = outstream if outstream is not None else sys.stdout.buffer
    server = LspServer(lambda msg: write_message(stdout, msg))
    while not server.exited:
        message = read_message(stdin)
        if message is None:
            break
        response = server.dispatch(message)
        if response is not None:
            write_message(stdout, response)
    return server.exit_code


def main(argv: list[str] | None = None) -> int:
    if argv and "-v" in argv:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                            format="%(asctime)s %(levelname)-7s %(name)s: "
                                   "%(message)s")
    log.info("pipeview lsp: serving on stdio")
    return serve()
