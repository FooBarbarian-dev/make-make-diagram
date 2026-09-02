# Pipeview for VS Code

Offline interactive reports for GNU Make, GitLab CI and GitHub Actions
pipelines, inside the editor. This extension is a thin shell around the
[pipeview](https://github.com/FooBarbarian-dev/make-make-diagram) CLI:
the reports it shows are the same self-contained HTML files the CLI
generates — Graph, Tasks, Variables, Files, and the What-If pipeline
simulator all included — rendered in a webview panel. Pipeline buffers
also get the `pipeview lsp` language server: inline diagnostics, hover
docs for predefined CI variables, and clickable includes (see
[Language server](#language-server)).

## Requirements

- Python 3.11+ with pipeview installed (`pip install .` from the
  repository, or `pipx install .`). The extension finds it as `pipeview`
  on PATH, falls back to `python3 -m pipeview` (on Windows: `python`,
  then the `py -3 -m pipeview` launcher), or uses the `pipeview.cliPath`
  / `pipeview.pythonPath` settings. Each candidate is checked with
  `--version`, so a stray Microsoft Store "python" stub is skipped.
- Trusted workspace (report generation runs the CLI on your files; Make
  enrichment executes `make -pqn`).

Platform notes:

- **Windows.** The python.org installer leaves `python.exe` and pip's
  `Scripts` directory off PATH by default; the `py` launcher fallback
  covers that. To pin an interpreter or a venv, set `pipeview.pythonPath`
  or `pipeview.cliPath` (`…\Scripts\pipeview.exe`; a `.cmd`/`.bat`
  wrapper works too). Report generation runs without a console window,
  and non-ASCII paths (a `C:\Users\José` profile) are handled.
- **WSL.** With the Remote - WSL extension the extension host runs inside
  the distro, so install pipeview there and everything — including the
  *Authenticate* terminal — runs in Linux. Report panels are webviews, so
  no browser is involved.

## Commands

| Command | What it does |
|---|---|
| **Pipeview: Pipeline Report for This Repo** | Analyzes the open repository (Makefile, `.gitlab-ci.yml`, and/or `.github/workflows/`) and opens the report(s). By default runs with `--upstream`: cross-repository `include:`s of GitLab roots are resolved by fetching them from the GitLab host the repo's own git remote points at. |
| **Pipeview: Pipeline Report for This File** | Same, for the selected Makefile / `*.mk` / `*.yml` (also in the explorer and editor-title context menus). |
| **Pipeview: Regenerate Last Report** | Re-runs the previous generation (after editing pipeline files). |
| **Pipeview: GitLab: Report for a Remote Project…** | `pipeview gitlab report group/project[@ref]` — fetches straight from GitLab, no checkout needed. |
| **Pipeview: GitLab: Sync Tracked Projects** | `pipeview gitlab sync` — one report per tracked project, plus the cross-project rollup, which opens when produced. |
| **Pipeview: GitLab: Set API Token** | Stores a `read_api` token in VS Code secret storage; it is passed to the CLI as `PIPEVIEW_GITLAB_TOKEN` (an already-exported environment variable wins). |
| **Pipeview: GitLab: Authenticate (opens a terminal)** | Runs `pipeview gitlab auth` in an integrated terminal (the terminal's process is pipeview itself, so it works the same under PowerShell, cmd, bash, and Remote-WSL) — the interactive flow that opens GitLab's prefilled token form and stores the result in pipeview's own config. |
| **Pipeview: GitLab: Clear Stored API Token** | Removes the secret-storage token. |
| **Pipeview: GitHub: Report for a Remote Repository…** / **Sync Tracked Repositories** | The same flows against GitHub (`pipeview github report owner/repo[@ref]`, `pipeview github sync`). |
| **Pipeview: GitHub: Set API Token / Authenticate / Clear Stored API Token** | GitHub counterparts of the GitLab commands; the token is passed as `PIPEVIEW_GITHUB_TOKEN`. |

Repo reports cover every root pipeview discovers — a `Makefile`,
`.gitlab-ci.yml`, and `.github/workflows/` each get their own report
panel.

## Language server

Opening a `.gitlab-ci.yml`, a `.github/workflows/*.yml`, a `Makefile`
or a `*.mk` starts `pipeview lsp` (the same CLI, found the same way):

- **Inline diagnostics** on open and save — pipeview's parser findings
  (broken includes, unknown `needs:` targets, variable problems) on the
  lines that cause them, across the whole include tree or workflows
  directory. Fully offline and never Make-enriched (no `$(shell)`
  execution from the editor loop).
- **Hover docs** for predefined variables: `CI_*`/`GITLAB_*` in GitLab
  files, `GITHUB_*`/`RUNNER_*` in workflow files — the Variables-tab
  catalog, inline.
- **Clickable references**: `include:local` entries and local
  `uses: ./…` reusable workflows / composite actions (document links).
- **Code action** (`Ctrl+.` / `Cmd+.`): *Pipeview: open pipeline
  report* — the same report the commands produce, in a webview panel
  (the server's own version of this action targets a browser; the
  extension rewrites it). GitLab roots also get *… without upstream
  fetch*.

YAML that belongs to no pipeline root gets nothing on purpose. Turn the
server off with `pipeview.languageServer: false`; that setting and the
CLI / upstream settings restart it when changed. The server's stderr
goes to the Pipeview output channel.

## The upstream default

“Report for This Repo” assumes the project you have open is a checkout
of the pipeline you care about, so it analyzes your *working tree* —
uncommitted edits included — and uses the repository's git remote (the
current branch's tracking remote, else `origin`) to resolve
`include:project`, `include:remote`, `include:component` and instance
templates from the same GitLab host.

Without a token the report still generates; unresolved cross-repo
includes appear as dashed ghost nodes and the extension offers to store
a token. Turn the behavior off with `pipeview.useUpstream: false` for
fully offline runs.

## Settings

- `pipeview.pythonPath` — interpreter for the `-m pipeview` fallback.
- `pipeview.cliPath` — explicit pipeview executable.
- `pipeview.outputDirectory` — where reports are written (default: the
  extension's per-workspace storage, keeping your repository clean); a
  relative path is resolved from the workspace folder.
- `pipeview.useUpstream` — pass `--upstream` for repo/file reports
  (default: true).
- `pipeview.upstreamRemote` — which git remote to use.
- `pipeview.extraArgs` — extra CLI arguments for report runs (e.g.
  `--no-enrich`, `-v`).
- `pipeview.languageServer` — run `pipeview lsp` for pipeline buffers
  (default: true).

The Pipeview output channel carries the CLI's full stdout/stderr —
add `-v` to `pipeview.extraArgs` to see fetch steps and decisions there.

## Development

```bash
cd editors/vscode
npm install
npm test          # tsc build + node --test unit tests
```

Launch the extension from VS Code with F5 (Extension Development Host),
or package it with `npx @vscode/vsce package`.

To try a branch's build without merging, run the *Preview release*
workflow on it (or push a `preview/<name>` tag): it attaches the
`.vsix` — and the wheel it needs — to a `preview/<branch>` pre-release;
install with *Extensions → ··· → Install from VSIX…*. See
[docs/release-pipelines.md](../../docs/release-pipelines.md).
