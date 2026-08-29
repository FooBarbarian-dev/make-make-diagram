# Zed extension + editor-integration layout — design

Date: 2026-08-29
Status: approved for implementation

## The idea

Three connected deliverables:

1. **Repository layout for editor integrations**: the project now ships
   in several forms — the core library + CLI, a VS Code extension, and
   (from this spec) a Zed extension. Editor integrations move under one
   roof, `editors/`, with a shared README stating the parity philosophy
   and a feature matrix.
2. **`pipeview lsp`** (core, new subcommand): a small stdlib-only
   language server speaking LSP over stdio. It exists because Zed
   extensions cannot do what the VS Code one does (no webviews, no
   arbitrary commands) — but it is deliberately editor-agnostic, so it
   becomes the convergence point for every editor over time.
3. **Zed extension** (`editors/zed/`): a WebAssembly extension (Rust,
   `zed_extension_api` 0.7.0) that wires `pipeview lsp` up for YAML and
   Make buffers. Reports open in the default browser — pipeview reports
   are self-contained `file://` HTML by design, so the browser *is* the
   report viewer Zed lacks.

## Editor constraints, and what "parity" means here

Verified against `zed_extension_api` 0.7.0 (the crate's own source):
Zed extensions are wasm components that may register language servers,
slash commands, context servers and debug adapters — nothing else. No
webviews, no command-palette commands, no input boxes, no secret
storage. The wasm sandbox cannot spawn processes; native processes
happen only when Zed itself spawns a declared server binary.

So the two extensions deliver the same features through different
organs, both thin shells over the same core:

| Feature | CLI | VS Code | Zed |
|---|---|---|---|
| Repo report, upstream ON by default | `--upstream` (opt-in) | command → webview | code action → browser |
| Report for one file | path argument | context menus | code action in that buffer |
| Graph/Tasks/Variables/Files/What-If | HTML report | same HTML in webview | same HTML in browser |
| Remote project report / sync + rollup | `pipeview gitlab` | input-box commands | terminal (`pipeview gitlab …`) — no input UI for extensions |
| Token setup | `gitlab auth` | secret storage + terminal | terminal; same env/stored-config chain |
| Inline diagnostics on save | exit codes / stderr | output channel | LSP `publishDiagnostics` |
| Predefined `CI_*` variable docs | Variables tab | Variables tab (webview) | LSP hover, from the same curated catalog |
| `include:local` navigation | Files tab | Files tab (webview) | LSP document links |

The last three rows are LSP features Zed gets first; VS Code adopting
`pipeview lsp` as a client is the designated follow-up (not this
change), after which the matrix converges further. Parity is maintained
by keeping every feature in the core (CLI or LSP) and none in
extension code.

## Approaches considered for Zed

- **A. Language server + browser-opened reports (chosen).** Real
  editor value (diagnostics, hover, links) plus the full report a
  keystroke away. One editor-agnostic server in the core.
- **B. Slash-command extension only.** Rejected: wasm cannot run the
  evaluator or spawn the CLI, so a slash command could only paste file
  contents around — no reports, no diagnostics.
- **C. Wait for Zed webviews.** Rejected: no shipping date, and the
  browser already renders the self-contained report perfectly.

## Part 1 — repository layout

```
pipeview/            # the core app + CLI (unchanged)
editors/
  README.md          # parity philosophy + the matrix above
  vscode/            # moved from vscode-extension/ (git mv, content unchanged
                     #   except documented path references)
  zed/               # this spec
```

Root README, Makefile (`make vscode`, new `make zed`), and the two
extension READMEs update their paths. Nothing inside the VS Code
extension's behavior changes.

## Part 2 — `pipeview lsp` (`pipeview/lsp.py`)

Stdlib-only JSON-RPC 2.0 over stdio with `Content-Length` framing.
`pipeview lsp` routes there from the main CLI (like `gitlab` and
`scenarios`). Protocol stdout is sacred: any in-process report
generation runs under `redirect_stdout`, and logs go to stderr.

**Capabilities**: full-text document sync (open/change/save), hover,
document links, code actions, `workspace/executeCommand` with
`pipeview.openReport` and `pipeview.openReportOffline`.

**Analysis root**: for an opened/saved file, walk up from its directory
to the nearest directory holding `.gitlab-ci.yml` (for YAML buffers) or
a `Makefile`/`makefile`/`GNUmakefile` (for Make buffers); the file
itself wins when it *is* a root. YAML files with no `.gitlab-ci.yml`
ancestor get nothing — the server stays silent on unrelated YAML, since
Zed attaches it to the whole YAML language.

**Diagnostics** (on open and save; buffers are not materialized, so
mid-edit changes wait for the save): parse the root offline — never the
network, never Make enrichment (no `$(shell)` execution from an editor
loop) — and publish each diagnostic to the file its `source` names,
sourceless ones to the root file at line 0. Severity maps
error/warning/info → 1/2/3.

**Hover** (YAML): the word under the cursor, matched against
`PREDEFINED_VAR_DOCS` — summary, example, when set/unset — the same
curated catalog the report's Variables tab uses.

**Document links** (YAML): `local:` include values in the buffer become
links resolved against the analysis root's directory.

**Code actions** (any position, kind `source`): "Pipeview: open
pipeline report (browser)" and, when upstream is enabled, "… without
upstream fetch". Each returns a command the client sends back as
`workspace/executeCommand`.

**executeCommand**: the server builds the equivalent CLI argv for the
buffer's root — `-o <outputDir>`, `--format html`, plus `--upstream`
(and `--upstream-remote`) per settings — and calls `pipeview.cli.main`
in-process with stdout captured, then opens the generated
`*.report.html` with `webbrowser.open(file://…)` and reports the
outcome via `window/showMessage` (warnings — e.g. the no-token
degradation — surface there too). Report output defaults to
`$XDG_CACHE_HOME/pipeview/lsp/<slug>` so repositories stay clean.

**Settings** (`initializationOptions`, all optional):
`{"upstream": true, "upstreamRemote": "", "outputDir": ""}` — upstream
defaults ON, mirroring the VS Code extension; tokens resolve through
the usual chain (env vars, `pipeview gitlab auth` stored config) in the
server's own environment.

## Part 3 — Zed extension (`editors/zed/`)

- `extension.toml`: id `pipeview`, `schema_version = 1`, and
  `[language_servers.pipeview]` with `languages = ["YAML", "Make"]`
  (Make matches when a Make language extension is installed; unknown
  names simply never match).
- `Cargo.toml`: `zed_extension_api = "0.7.0"`, `crate-type = ["cdylib"]`,
  built for `wasm32-wasip2`.
- `src/lib.rs` (~60 lines): `language_server_command` resolves the
  server as VS Code does — `lsp.pipeview.binary.path` setting →
  `pipeview` on PATH (worktree `which`) → `python3`/`python`
  `-m pipeview` — always appending the `lsp` argument (a configured
  binary path gets `["lsp"]` as its default arguments) and passing the
  worktree shell env through so tokens flow.
  `language_server_initialization_options` forwards the user's
  `lsp.pipeview.initialization_options` verbatim.
- Users configure it in Zed `settings.json` under `"lsp": {"pipeview":
  {"binary": {…}, "initialization_options": {"upstream": false, …}}}`.
- README: install as dev extension, settings, the parity story, and the
  terminal flows for `gitlab report/sync/auth`.

## Error handling

| Failure | Behavior |
|---|---|
| pipeview binary missing (Zed) | `language_server_command` returns an error naming the install fix and the settings key |
| Buffer outside any pipeline root | no diagnostics, hover/links only if applicable — never an error |
| Report generation fails (exit 2) | `window/showMessage` error with the captured output's tail |
| Upstream degradations | already warnings in the report + diagnostics; repeated in the showMessage summary |
| Malformed JSON-RPC input | request-level error response; the server never crashes the stream |

## Testing

- `tests/test_lsp.py` (new): frame reader/writer round-trips; an
  in-process client driving the server class directly — initialize
  handshake capabilities; didOpen/didSave publish expected diagnostics
  for a broken fixture and clear them when fixed; silence on unrelated
  YAML; hover over `CI_COMMIT_SHA` yields the catalog text and nothing
  on unknown words; document links for `local:` includes; code actions
  listed; `executeCommand` with `webbrowser.open` monkeypatched
  generates the report file and "opens" it (upstream flag propagation
  asserted via the argv it builds).
- Zed extension: `cargo build --release --target wasm32-wasip2` must
  succeed (run in this change); behavior is a thin declaration, pinned
  by the compile.
- Existing suites untouched and green; `make vscode` unaffected by the
  move (path updated).

## Out of scope

- Publishing to the Zed extension registry (dev-extension install
  documented).
- VS Code consuming `pipeview lsp` (designated follow-up).
- Zed slash commands / context servers.
- Watch-mode report regeneration.
