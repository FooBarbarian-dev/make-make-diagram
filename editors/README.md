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
| Repo report (Make, GitLab CI, GitHub Actions roots discovered) | `pipeview <path>` | command → webview(s) | code action → browser |
| Upstream reference ON by default (GitLab roots) | `--upstream` (opt-in) | on by default | on by default |
| Report for one file / workflow | path argument | context menus | code action in that buffer |
| Graph / Tasks / Variables / Files / What-If | HTML report | same HTML, webview | same HTML, browser |
| Remote project report, sync + rollup | `pipeview gitlab` / `pipeview github` | input-box commands (both providers) | terminal (`pipeview gitlab/github …`) |
| Token setup (GitLab & GitHub) | `… auth` subcommands | secret storage + terminal | terminal; same env/config chain |
| Inline diagnostics on save | exit codes / stderr | output channel | LSP diagnostics |
| Predefined variable docs (`CI_*` / `GITHUB_*`) | Variables tab | Variables tab | LSP hover, per-provider catalog |
| `include:local` / local `uses:` navigation | Files tab | Files tab | LSP document links |

The last three rows are `pipeview lsp` features that Zed gets first;
pointing the VS Code extension at the same server is the designated
follow-up, after which the matrix converges further.

When adding a feature, add it to the core, then wire it into each
extension in its native shape — and update this matrix, including an
honest “terminal” cell when an editor cannot express it.
