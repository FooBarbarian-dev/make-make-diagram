# Pipeview for VS Code

Offline interactive reports for GNU Make and GitLab CI pipelines, inside
the editor. This extension is a thin shell around the
[pipeview](https://github.com/FooBarbarian-dev/make-make-diagram) CLI:
the reports it shows are the same self-contained HTML files the CLI
generates — Graph, Tasks, Variables, Files, and the What-If pipeline
simulator all included — rendered in a webview panel.

## Requirements

- Python 3.10+ with pipeview installed (`pip install .` from the
  repository, or `pipx install .`). The extension finds it as `pipeview`
  on PATH, falls back to `python -m pipeview`, or uses the
  `pipeview.cliPath` / `pipeview.pythonPath` settings.
- Trusted workspace (report generation runs the CLI on your files; Make
  enrichment executes `make -pqn`).

## Commands

| Command | What it does |
|---|---|
| **Pipeview: Pipeline Report for This Repo** | Analyzes the open repository (Makefile and/or `.gitlab-ci.yml`) and opens the report(s). By default runs with `--upstream`: cross-repository `include:`s are resolved by fetching them from the GitLab host the repo's own git remote points at. |
| **Pipeview: Pipeline Report for This File** | Same, for the selected Makefile / `*.mk` / `*.yml` (also in the explorer and editor-title context menus). |
| **Pipeview: Regenerate Last Report** | Re-runs the previous generation (after editing pipeline files). |
| **Pipeview: GitLab: Report for a Remote Project…** | `pipeview gitlab report group/project[@ref]` — fetches straight from GitLab, no checkout needed. |
| **Pipeview: GitLab: Sync Tracked Projects** | `pipeview gitlab sync` — one report per tracked project, plus the cross-project rollup, which opens when produced. |
| **Pipeview: GitLab: Set API Token** | Stores a `read_api` token in VS Code secret storage; it is passed to the CLI as `PIPEVIEW_GITLAB_TOKEN` (an already-exported environment variable wins). |
| **Pipeview: GitLab: Authenticate (opens a terminal)** | Runs `pipeview gitlab auth` in an integrated terminal — the interactive flow that opens GitLab's prefilled token form and stores the result in pipeview's own config. |
| **Pipeview: GitLab: Clear Stored API Token** | Removes the secret-storage token. |

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
  extension's per-workspace storage, keeping your repository clean).
- `pipeview.useUpstream` — pass `--upstream` for repo/file reports
  (default: true).
- `pipeview.upstreamRemote` — which git remote to use.
- `pipeview.extraArgs` — extra CLI arguments (e.g. `--no-enrich`, `-v`).

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
