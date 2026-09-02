# Editor integrations

pipeview ships in several forms, organized like this:

```
pipeview/          # the core app and CLI — every feature lives here
editors/vscode/    # VS Code extension
editors/zed/       # Zed extension
```

Both extensions are thin shells over the same core, which is what keeps
them in step: **every feature belongs to the core** (the CLI, the
generated report HTML, or the `pipeview lsp` language server) **and
none to extension code**. An extension only decides how its editor
triggers the core and where the results appear.

The editors differ in what extensions are allowed to do, so the same
features arrive through different organs:

- **VS Code** extensions can register commands, webviews, input boxes
  and secret storage — so it spawns the CLI directly and renders the
  report HTML in a webview panel.
- **Zed** extensions are WebAssembly components that can register
  language servers (and slash commands / context servers) — nothing
  else: no webviews, no palette commands, no input UI. So Zed talks to
  `pipeview lsp`, and reports open in the default browser — pipeview
  reports are self-contained `file://` HTML precisely so that any
  browser is a full viewer.

## Feature matrix

| Feature | CLI | VS Code | Zed |
|---|---|---|---|
| Repo report (Make, GitLab CI, GitHub Actions roots discovered) | `pipeview <path>` | command / code action → webview(s) | code action → browser |
| Upstream reference ON by default (GitLab roots) | `--upstream` (opt-in) | on by default | on by default |
| Report for one file / workflow | path argument | context menus | code action in that buffer |
| Graph / Tasks / Variables / Files / What-If | HTML report | same HTML, webview | same HTML, browser |
| Remote project report, sync + rollup | `pipeview gitlab` / `pipeview github` | input-box commands (both providers) | terminal (`pipeview gitlab/github …`) |
| Token setup (GitLab & GitHub) | `… auth` subcommands | secret storage + terminal | terminal; same env/config chain |
| Inline diagnostics on save | exit codes / stderr | LSP diagnostics (+ output channel) | LSP diagnostics |
| Predefined variable docs (`CI_*` / `GITHUB_*`) | Variables tab | LSP hover, per-provider catalog | LSP hover, per-provider catalog |
| `include:local` / local `uses:` navigation | Files tab | LSP document links | LSP document links |

The last three rows are `pipeview lsp` features, and both extensions
host the same server. The one place they diverge is the server's report
code action: it opens the report in a browser, which is what Zed needs;
the VS Code client rewrites that action into its own webview command
before the editor sees it (`vscode-languageclient` middleware), so no
report feature is implemented twice.

Starting the server is the extension's job in VS Code (it spawns the
located CLI as `pipeview lsp`). In Zed the extension only supplies the
default: a user-configured `lsp.pipeview.binary` is applied by Zed
itself, with `path` run verbatim and `arguments` defaulting to none —
which is why the server also ships as its own `pipeview-lsp`
executable, usable as a bare binary path.

When adding a feature, add it to the core, then wire it into each
extension in its native shape — and update this matrix, including an
honest “terminal” cell when an editor cannot express it.
