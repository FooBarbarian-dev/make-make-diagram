# Pipeview for Zed

Pipeline intelligence for GNU Make and GitLab CI inside Zed, powered by
the [pipeview](https://github.com/FooBarbarian-dev/make-make-diagram)
language server (`pipeview lsp`):

- **Inline diagnostics** on open/save: pipeview's parser findings —
  broken includes, unknown `needs:` targets, variable problems — appear
  on the lines that cause them, across the whole include tree. Analysis
  is fully offline and never runs Make enrichment (no `$(shell)`
  execution from the editor loop).
- **Hover docs** for predefined `CI_*`/`GITLAB_*` variables — the same
  curated catalog as the report's Variables tab: what each is, an
  example value, when GitLab sets it.
- **Clickable `include:local` entries** (document links).
- **Pipeline reports**: a code action (`cmd-.` / `ctrl-.`) —
  *Pipeview: open pipeline report (browser)* — generates the full
  interactive report (Graph, Tasks, Variables, Files, What-If) and
  opens it in your default browser. Zed extensions cannot render
  webviews, and pipeview reports are deliberately self-contained
  `file://` HTML — so the browser is a complete viewer, offline.

Repo reports default to **`--upstream`**: pipeview uses the
repository's own git remote to fetch cross-repository `include:`s from
the same GitLab host, while your local files — uncommitted edits
included — remain the source of truth. Without a token the report still
generates; unresolved includes stay dashed ghost nodes.

See [editors/README.md](../README.md) for how the Zed and VS Code
integrations divide the same features.

## Requirements

Python 3.10+ with pipeview installed. The extension finds the server as
`pipeview` on PATH, falls back to `python3 -m pipeview`, or uses an
explicit binary from settings (below). Zed's YAML support is built in;
Makefile buffers need a Make language extension installed.

## Installation (dev extension)

```bash
git clone https://github.com/FooBarbarian-dev/make-make-diagram.git
cd make-make-diagram && pip install .
```

In Zed: `zed: extensions` → **Install Dev Extension** → choose
`editors/zed/`. (Zed compiles the extension with your Rust toolchain;
`rustup target add wasm32-wasip2` first if needed.)

## Settings

Configured in Zed's `settings.json` under the `pipeview` language
server:

```jsonc
{
  "lsp": {
    "pipeview": {
      // Explicit server binary (optional; default: pipeview on PATH,
      // then python3 -m pipeview). Arguments default to ["lsp"].
      "binary": { "path": "/path/to/pipeview" },
      "initialization_options": {
        "upstream": true,        // resolve cross-repo includes via the
                                 // repo's git remote for reports
        "upstreamRemote": "",    // "" = tracking remote, else origin
        "outputDir": ""          // "" = ~/.cache/pipeview/lsp/<slug>
      }
    }
  }
}
```

GitLab tokens resolve exactly like the CLI: `PIPEVIEW_GITLAB_TOKEN` /
`GITLAB_TOKEN` / `GITLAB_PRIVATE_TOKEN` from your shell environment, or
the config stored by a one-time `pipeview gitlab auth` in a terminal.
Remote-project reports, sync, and the cross-project rollup are terminal
flows in Zed (`pipeview gitlab report group/app`, `pipeview gitlab
sync`) — Zed extensions have no input UI to prompt for a project path.

## Development

```bash
cd editors/zed
cargo build --release --target wasm32-wasip2   # or: make zed (repo root)
```

The extension is ~80 lines on purpose: every feature lives in
`pipeview lsp` so other editors (and the VS Code extension, as a
follow-up) can share it.
