# VS Code extension + upstream include resolution — design

Date: 2026-08-29
Status: approved for implementation

## The idea

Two connected deliverables:

1. **Upstream include resolution** (core, CLI): when analyzing a *local
   checkout*, use the repository's own git upstream (its `origin` /
   tracking remote) as the reference for where cross-repository pipeline
   files live. `include:project`, `include:remote`, `include:component`
   and instance templates stop being ghost nodes: pipeview infers the
   GitLab host and project path from the remote URL, fetches exactly the
   externally-included files, and the ordinary offline pipeline runs —
   local files still parsed from the working tree (real line numbers,
   uncommitted edits included).

2. **VS Code extension**: pipeview inside the editor. Defaults to the
   currently open repository *with the upstream reference on*, renders
   the self-contained HTML report in a webview panel, and exposes the
   rest of the tool (report on a file, remote GitLab reports, sync +
   rollup, token setup) through commands — same support as the CLI,
   because it *is* the CLI underneath.

## Why now

Today the gap between the two modes is awkward: `pipeview .` is fully
offline (cross-repo includes ghost), while `pipeview gitlab report
group/app` shows the resolved picture but of the *committed* config, not
the working tree. Anyone editing `.gitlab-ci.yml` locally wants both:
their edits, with the includes resolved. The repo already knows where
those includes live — its own upstream. The VS Code extension makes that
loop tight: edit, regenerate, inspect, What-If.

## Part 1 — upstream include resolution

### Approaches considered

- **A. Reuse the `files` fetch strategy with a local seed (chosen).**
  Walk the local include tree from disk; every *non-local* include is
  fetched from the upstream host by the existing `_FilesFetcher`
  machinery (same traversal, caps, template fallback, materialization
  under `fetched/…/_external/`), then `parse_gitlab` runs on the local
  root with the existing `external_resolver` + `local_roots` hooks.
  Pros: small, honest, one traversal implementation to maintain; the
  parser is untouched. Cons: a pre-walk of local includes duplicates a
  little of what the parser will do again (cheap, bounded).
- **B. Teach the parser to fetch lazily.** Give `parse_gitlab` a network
  resolver that fetches on first miss. Rejected: it puts network inside
  the offline parser, the exact boundary this codebase is built around
  ("network access ends before the parse step").
- **C. `git fetch` the other repositories.** Clone/fetch included
  projects via git instead of the API. Rejected: needs credentials per
  repo, downloads far more than the ≤150 included files, and cannot
  serve `include:template` / `include:remote` / components.

### Upstream detection (`pipeview/gitlab/upstream.py`, new)

- `parse_remote_url(url) -> (host, project_path) | None` — pure. Accepts
  scp-style ssh (`git@host:group/app.git`), `ssh://git@host[:port]/…`,
  `https://` / `http://` (userinfo stripped), trims a trailing `.git`
  and `/`. Port in ssh URLs is dropped for the API host (the API speaks
  https on the standard port); https URLs keep their port. Returns the
  normalized base URL (`https://host[:port]`) and the project path
  (`group/sub/app`). ssh URLs assume the API at `https://<host>`.
- `detect_upstream(repo_dir, remote=None) -> Upstream` — runs git.
  Raises `UpstreamError` with a human message on every failure (git not
  installed, not a repository, no remotes, URL not parseable). Remote
  selection order: explicit `remote` argument → the current branch's
  tracking remote (`git rev-parse --abbrev-ref --symbolic-full-name
  @{upstream}`) → `origin` if it exists → the sole remote → error naming
  the remotes that do exist. Also captures the repo toplevel
  (`git rev-parse --show-toplevel`) and the current branch name (for
  messages; falls back to `HEAD`).
- `Upstream` dataclass: `host`, `project_path`, `remote_name`, `url`,
  `toplevel`, `branch`.

### Local-seed fetch (`pipeview/gitlab/fetch.py`)

New `_LocalFilesFetcher(_FilesFetcher)` plus a public
`fetch_local_externals(client, project, ref, repo_root, root_file, *,
bundled_templates=True) -> FetchResult`:

- Seeds from the local root file *on disk*; local files are read, walked
  for `include:` entries, and **never** added to `FetchResult.files`
  (nothing local is materialized or fetched — the working tree is the
  truth, even when it disagrees with what is committed upstream).
- `include:local` in main-repo context resolves on disk against
  `repo_root`, wildcards expanded against the working tree with the same
  `_wildcard_regex` semantics; unreadable/missing files are skipped
  silently (the parser reports them properly later). A seen-set guards
  include cycles; the local walk shares the MAX_FILES ceiling.
- `include:project` / `template:` / `remote:` / `component:` reuse the
  parent class untouched — fetched into `_external/…`, nested includes
  inside fetched repos keep full `files`-strategy semantics (their
  `include:local` fetches from *their* repo).
- `strategy` field: `"upstream"`. `root_rel` is unused by the caller
  (the parse root is the local file) but set for symmetry.
- An `include:project` pointing at the upstream project itself fetches
  the *committed* copy from the remote, like GitLab would — the report's
  provenance says so.

### CLI wiring (`pipeview/cli.py`)

New flags on the main command (all inert unless `--upstream` is given;
report generation stays zero-network by default — the offline guarantee
moves from "the `gitlab` subcommand is the only networked code" to "the
`gitlab` subcommand and the explicit `--upstream` flag"):

- `--upstream` — resolve cross-repo includes of GitLab CI roots via the
  repo's git remote.
- `--upstream-remote NAME` — override remote selection.
- `--token` — API token (else the usual env vars / stored config,
  via `auth.resolve_token`).
- `--ca-bundle` / `--insecure` / `--timeout` — TLS/HTTP knobs matching
  `pipeview gitlab`.
- `-v` / `--log-file` — the same logging setup the gitlab CLI has, so
  fetch decisions are observable (the extension pipes this to its
  output channel).

Orchestration lives in `upstream.resolve_upstream_includes(root_path,
repo_dir, outdir, …) -> UpstreamResolution` (resolver, local_roots,
diagnostics, annotation, repo_root). `cli.py` calls it per GitLab root
and passes the hooks into `parse_gitlab` (with `repo_root` = git
toplevel, matching GitLab's resolution base). Every failure mode
degrades: a warning diagnostic states what happened (no upstream, no
token — naming how to provide one, fetch errors) and the report renders
with ghosts, exit code 1 via the normal diagnostics path. Externals are
materialized under `<outdir>/fetched/<project-slug>@upstream/`.

The report gains `annotations["gitlab_upstream"] = {host, project,
remote, url, branch}` (machine-readable, mirroring `gitlab_remote`) and
an info diagnostic naming the upstream used. Make roots ignore
`--upstream` (nothing to resolve).

## Part 2 — VS Code extension

### Approaches considered

- **A. Thin TypeScript extension over the CLI (chosen).** Spawn the
  installed `pipeview`, show the self-contained report HTML in a
  webview. Pros: full feature parity by construction (Graph, Tasks,
  Variables, Files, What-If, deltas, exports all live in the HTML
  already); one engine to maintain; the extension is a few hundred lines.
  Cons: requires Python + pipeview installed (surfaced with a clear
  error and docs).
- **B. Reimplement parsing/rendering in TypeScript.** Rejected: months
  of duplication of two parsers + evaluator that are pinned together by
  parity suites; guaranteed drift.
- **C. Language-server architecture.** Rejected: pipeview is a report
  generator, not a per-keystroke service; a spawn per generation is the
  right weight.

### Layout

```
vscode-extension/
  package.json        # manifest: commands, settings, activation
  tsconfig.json
  .vscodeignore
  README.md           # install + usage (marketplace-facing)
  src/
    extension.ts      # activation, command registration
    cli.ts            # locate + spawn the pipeview CLI (pure helpers exported)
    panel.ts          # webview panel per report file
    gitlab.ts         # remote-project flows (report/sync/auth terminal)
  src/test/           # node:test unit tests for the pure helpers
```

Toolchain: `tsc` only (no bundler), Node ≥ 18, `engines.vscode ^1.85`.
`npm run build` compiles, `npm test` runs `node --test` over the
compiled pure helpers, `npm run lint` is `tsc --noEmit`. A root
`make vscode` target wires build+test in. Packaging with
`npx @vscode/vsce package` is documented, not automated.

### Behavior

- **`pipeview.showReport` — "Pipeview: Pipeline Report for This Repo"**
  (the default flow). Picks the workspace folder (active editor's, else
  the single folder, else quick-pick), runs
  `pipeview <folder> -o <outdir> --format html` **with `--upstream` by
  default** (`pipeview.useUpstream`, default true; plus
  `--upstream-remote` when configured), under a progress notification.
  Parses `Report generated:` lines from stdout and opens each
  `*.report.html` in a webview panel. Exit 2 → error notification with
  a "Show output" action; exit 1 → the panel opens plus a status-bar
  warning pointing at the Files tab / output channel.
- **`pipeview.showReportForFile`** — same, for the file under the
  cursor / right-clicked in the explorer (Makefiles, `*.mk`, `*.yml`).
- **`pipeview.gitlabReport`** — input box `group/project[@ref]`, runs
  `pipeview gitlab report …`, opens the result. Host/token resolution is
  the CLI's own (env, stored config).
- **`pipeview.gitlabSync`** — runs `pipeview gitlab sync`, opens
  `rollup.report.html` when produced.
- **`pipeview.setGitLabToken` / `clearGitLabToken`** — stores a token in
  VS Code SecretStorage; injected as `PIPEVIEW_GITLAB_TOKEN` into every
  spawn (only when the variable isn't already set in the environment).
  For interactive first-time auth, `pipeview.gitlabAuth` opens an
  integrated terminal prefilled with `pipeview gitlab auth` (the flow
  needs a TTY).
- When a run warns that no token was found for the upstream host, the
  extension surfaces one notification offering "Set GitLab token".

### Settings

| Setting | Default | Meaning |
|---|---|---|
| `pipeview.pythonPath` | `""` | Interpreter for `-m pipeview` fallback (auto: `python3`, then `python`) |
| `pipeview.cliPath` | `""` | Explicit pipeview executable; empty → `pipeview` on PATH → `<python> -m pipeview` |
| `pipeview.outputDirectory` | `""` | Where reports go; empty → the extension's per-workspace storage dir (keeps repos clean) |
| `pipeview.useUpstream` | `true` | Pass `--upstream` for repo/file reports |
| `pipeview.upstreamRemote` | `""` | Pass `--upstream-remote` when non-empty |
| `pipeview.extraArgs` | `[]` | Appended verbatim to report invocations |

### Security posture

- `capabilities.untrustedWorkspaces.supported: false` — the extension
  stays disabled in untrusted workspaces (report generation runs
  `make -pqn` enrichment by default, and `--upstream` reads the repo's
  git config; both are workspace-trust-gated actions). Users who want
  no enrichment even in trusted workspaces add `--no-enrich` to
  `pipeview.extraArgs`.
- Tokens live in SecretStorage or the CLI's own 0600 config, never in
  settings.json.
- Webview: `enableScripts: true`, `retainContextWhenHidden: true`; the
  report is self-contained (no external fetches by construction — the
  repo's offline test enforces it), so no `localResourceRoots` are
  needed and the default webview CSP-less document is acceptable
  because its content comes from pipeview's own renderer.

## Error handling summary

| Failure | Behavior |
|---|---|
| `--upstream` but git missing / no remote / unparseable URL | warning diagnostic naming the reason, ghosts stay, exit 1 |
| `--upstream` but no token | warning diagnostic naming the env vars / `pipeview gitlab auth`, ghosts stay |
| Fetch errors mid-walk | existing `_FilesFetcher` notes (warnings), partial resolution |
| Extension: CLI not found | error notification linking install docs; settings named |
| Extension: exit 2 | error notification + output channel |
| Extension: exit 1 | report shown, non-blocking warning |

## Testing

- `tests/test_upstream.py` (new): URL-parsing table (ssh scp, ssh://
  with port, https with/without `.git`, userinfo, subgroups, rejects);
  `detect_upstream` against temp git repos (skipped when git is absent)
  — tracking remote preferred, origin fallback, sole remote, explicit
  override, error cases; `fetch_local_externals` against `FakeGitLab` +
  a temp working tree: project/template/remote includes fetched and
  resolved end-to-end through `parse_gitlab` (ghost becomes real node),
  local files never materialized, wildcard locals walked, uncommitted
  local content wins; CLI `--upstream` with detection/client
  monkeypatched: annotation present, no-token degradation, `--upstream`
  absent ⇒ zero network (no client constructed).
- Extension: `node --test` unit tests for the pure helpers (output-line
  parsing, argv assembly, CLI resolution order); `tsc --noEmit` clean.
  Webview/manual flows validated by hand, not by an Electron harness.
- The existing offline-guarantee test keeps passing untouched: upstream
  fetching happens before parse and only materializes files, exactly
  like `pipeview gitlab`.

## Out of scope

- Publishing to the VS Code marketplace (packaging documented only).
- A TUI-equivalent GitLab *browser* inside VS Code (the input-box +
  terminal-auth flows cover report/sync/track workflows; a tree view
  can come later).
- Using the upstream reference for rollup fleet views (rollup stays a
  `sync` feature).
- Watch-mode / regenerate-on-save (easy follow-up once this lands).
