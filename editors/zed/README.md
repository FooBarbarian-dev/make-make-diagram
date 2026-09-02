# Pipeview for Zed

Pipeline intelligence for GNU Make and GitLab CI inside Zed, powered by
the [pipeview](https://github.com/FooBarbarian-dev/make-make-diagram)
language server (`pipeview lsp`):

- **Inline diagnostics** on open/save: pipeview's parser findings —
  broken includes, unknown `needs:` targets, variable problems — appear
  on the lines that cause them, across the whole include tree. Works
  for GitLab CI trees, GitHub Actions workflow directories
  (`.github/workflows/`), and Makefiles alike. Analysis is fully
  offline and never runs Make enrichment (no `$(shell)` execution from
  the editor loop).
- **Hover docs** for predefined variables — the curated
  `CI_*`/`GITLAB_*` catalog in GitLab files and the
  `GITHUB_*`/`RUNNER_*` catalog in workflow files, same content as the
  report's Variables tab: what each is, an example value, when it is
  set.
- **Clickable references**: `include:local` entries in GitLab files,
  local `uses: ./…` reusable workflows and composite actions in GitHub
  workflow files (document links).
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

Zed 0.205 or newer (extension API 0.7) and Python 3.10+ with pipeview
installed. The extension finds the server as `pipeview` on PATH, falls
back to `python3 -m pipeview` (`python`, then `py -3 -m pipeview` on
Windows), or uses an explicit binary from settings (below). Zed's YAML
support is built in; Makefile buffers need a Make language extension
installed.

**Windows.** `pip install .` puts `pipeview.exe` into Python's `Scripts`
directory, which the python.org installer leaves off PATH by default —
the extension then reaches pipeview through the `py` launcher, which is
always on PATH. If Zed still reports it missing, install with
`py -m pip install .` or point the `binary` setting below at
`…\Scripts\pipeview.exe` (double the backslashes in JSON).

**WSL.** Zed's remote development runs `pipeview lsp` inside the distro;
install pipeview there. The report code action opens the report in your
Windows browser: the server detects WSL and hands the file to `wslview`
(wslu — preinstalled on the Ubuntu images) or PowerShell. Set `BROWSER`
to prefer a Linux browser instead.

## Installation

### From a release (no Rust toolchain)

Download `pipeview-zed-vX.Y.Z.zip` (or `.tar.gz`) from the matching
[`zed-vX.Y.Z` release](https://github.com/FooBarbarian-dev/make-make-diagram/releases)
and extract it. The `pipeview/` folder inside holds `extension.toml`
beside the compiled `extension.wasm` — the same layout Zed's extension
registry serves, and the only form Zed can install (a bare `.wasm` is
not). In Zed: `zed: extensions` → **Install Dev Extension** → choose
that `pipeview/` folder. Same steps on Windows, macOS, and Linux.

### From source

```bash
git clone https://github.com/FooBarbarian-dev/make-make-diagram.git
cd make-make-diagram && pip install .
```

In Zed: `zed: extensions` → **Install Dev Extension** → choose
`editors/zed/`. (Zed compiles the extension with your Rust toolchain;
`rustup target add wasm32-wasip2` first if needed.) Or build the release
archive yourself with `editors/zed/scripts/package.sh` (`make
zed-package`) and install its `dist/` output as above.

## Settings

Configured in Zed's `settings.json` under the `pipeview` language
server:

```jsonc
{
  "lsp": {
    "pipeview": {
      // Explicit server binary (optional; default: pipeview on PATH,
      // then python3 -m pipeview / py -3 -m pipeview). Arguments default
      // to ["lsp"]. Windows: "C:\\Users\\me\\...\\Scripts\\pipeview.exe"
      "binary": { "path": "/path/to/pipeview" },
      "initialization_options": {
        "upstream": true,        // resolve cross-repo includes via the
                                 // repo's git remote for reports
        "upstreamRemote": "",    // "" = tracking remote, else origin
        "outputDir": ""          // "" = ~/.cache/pipeview/lsp/<slug>
                                 // (%LOCALAPPDATA%\pipeview\lsp on Windows)
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
scripts/package.sh                             # or: make zed-package — the
                                               # release archives, in dist/
```

`scripts/package.sh` is what CI and the release workflow run: it checks
`extension.toml` (required fields, version in step with `Cargo.toml`),
stages `extension.toml` + `extension.wasm` (+ LICENSE, README,
CHANGELOG — deliberately no `Cargo.toml`, which would make Zed rebuild
on install) and writes the `.zip`/`.tar.gz`.

To try a branch's build without merging, run the *Preview release*
workflow on it (or push a `preview/<name>` tag): it attaches these
archives — and the wheel they need — to a `preview/<branch>`
pre-release, installable exactly like a real one. See
[docs/release-pipelines.md](../../docs/release-pipelines.md).

The extension is ~80 lines on purpose: every feature lives in
`pipeview lsp` so other editors (and the VS Code extension, as a
follow-up) can share it.
