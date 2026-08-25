# GitLab remote fetch + project browser TUI — design

Date: 2026-08-25
Status: implemented — see the as-built notes at the end.

## Problem

pipeview analyzes GitLab CI configuration you already have on disk. Real
configurations live in a company GitLab instance, spread across many
projects — the root `.gitlab-ci.yml` routinely `include:`s files from
*other* repositories (`include:project`), from instance templates
(`include:template`), from URLs (`include:remote`), and from CI/CD catalog
components (`include:component`). Today the user must clone every repo and
hand-assemble the file set before pipeview can draw a graph; cross-repo
includes come out as ghost nodes.

Goal: connect to a GitLab instance, browse the projects the user can
access, keep a tracked list of interesting ones, and generate the existing
offline reports directly from what GitLab serves — resolving cross-repo
includes without asking the user to clone anything.

## API research: is there an endpoint that returns the *whole* config?

Yes. The project-scoped CI Lint endpoint returns the fully resolved
configuration in one call:

```
GET /projects/:id/ci/lint?content_ref=<ref>
```

Response fields (verified against GitLab's API docs and the python-gitlab
client):

- `valid`, `errors`, `warnings` — GitLab's own verdict on the config.
- `merged_yaml` — the **complete configuration after every `include:` is
  expanded and every YAML anchor resolved**, including `include:project`
  files from other repositories, templates, remote URLs, and components.
  This is exactly the "full pipeline" the feature needs; no repository
  traversal required, no N+1 permission dance across repos — GitLab
  resolves everything server-side under the caller's own permissions.
- `includes` — provenance metadata for every file that was merged in
  (type, location, project/ref for cross-repo ones, blob/raw URLs).

Parameter history that matters for self-managed instances:

- GitLab ≥ 16.10: `content_ref` (which ref's config to lint) and
  `dry_run_ref` (simulation context when `dry_run=true`).
- GitLab < 16.10: the same two things were called `sha` and `ref`.
- The endpoint itself (with `merged_yaml`) exists since GitLab 13.x; the
  old *global* `POST /ci/lint` was removed in 16.0 and is not used here.

Because GitLab's API (Grape) ignores undeclared parameters, the client
sends both spellings (`content_ref` *and* `sha`) so one code path works
across versions.

Read access to the endpoint needs a token with `read_api` scope and at
least Reporter-ish visibility of the project's CI config (same permission
GitLab's own "CI Lint" page uses).

Rejected alternative: GraphQL `ciConfig` returns a similar merged view but
requires POSTing the raw YAML back to the server and speaks a different
auth/pagination dialect than everything else the feature needs (project
lists, refs, files). One REST client covers all of it.

## Decisions

### 1. Two fetch strategies, `lint` primary, `files` fallback

- **`lint` (primary).** One `GET /projects/:id/ci/lint?content_ref=<ref>`
  call. `merged_yaml` is written to the work directory as the root
  `.gitlab-ci.yml` and parsed by the existing parser unchanged. The
  response's `includes` metadata is recorded as extra `SourceFile` entries
  (status `ok`, display path like `[project:group/lib@main] ci/build.yml`)
  so the File Map still tells the provenance story, and GitLab's own
  `errors`/`warnings` become report diagnostics — an authoritative second
  opinion the offline parser can never give.
- **`files` (fallback).** For instances/permissions where the lint
  endpoint is unavailable (404 on ancient versions, 403 on locked-down
  projects) — or when the user explicitly wants real per-file line
  numbers — fetch the root CI file via the repository files API and walk
  `include:` recursively: `local` from the same project, `project` from
  the referenced repository (this is the cross-repo traversal), `template`
  from the instance template API, `remote` over plain HTTPS, `component`
  best-effort from the component project's `templates/` directory. Files
  materialize under the work directory (externals under `_external/…`),
  and the parser resolves them through a new resolver hook (below) instead
  of declaring ghosts.
- `--strategy auto|lint|files` picks; `auto` (default) tries `lint`,
  falls back to `files`, and says so in a diagnostic.

The root file honors the project's `ci_config_path` attribute (custom
paths, and the `path@group/project` form that points at another repo
entirely).

### 2. A resolver hook instead of parser forks

`parse_gitlab(path)` grows an optional keyword:
`parse_gitlab(path, external_resolver=…)`. The resolver is a callable the
fetch layer provides: given a non-local include dict
(`{"project": …, "file": …, "ref": …}`, `{"template": …}`, …) it returns
the materialized local path, or `None`. `_process_includes` consults it
before falling into the "unresolved → ghost" branch; a hit is parsed like
any local file. Offline behavior is byte-for-byte unchanged when no
resolver is passed — the hook has no default, no config, no globals.

### 3. Token handling: load from many places, create via prefilled URL

Resolution order (first hit wins): `--token` flag → `PIPEVIEW_GITLAB_TOKEN`
→ `GITLAB_TOKEN` → `GITLAB_PRIVATE_TOKEN` → the config file.

Creation: a first personal access token **cannot** be minted through the
API (creating a token requires a token — the `POST /user/personal_access_tokens`
endpoint is admin-only). The supported path is GitLab's prefilled form:

```
<host>/-/user_settings/personal_access_tokens?name=pipeview&scopes=read_api
```

`pipeview gitlab auth` opens that URL in the browser (falls back to
printing it), the user clicks "Create" and pastes the token back
(hidden input via `getpass`), the tool verifies it against `GET /user`,
reports who it authenticated as, and offers to store it. OAuth device
flow was considered and rejected: it needs an application registered by
an instance admin, which a user pointing pipeview at their company GitLab
cannot assume.

Storage: `~/.config/pipeview/gitlab.json` (respects `$XDG_CONFIG_HOME`),
written `0600`, keyed by host so multiple GitLab instances coexist. The
same file carries the per-host **tracked projects** list. Users who refuse
on-disk tokens simply rely on the env vars.

### 4. TUI: stdlib curses, thin over a headless core

`pipeview gitlab` (no subcommand) opens a curses browser:

- **Project list** — the projects the token can see (`membership=true`,
  ordered by last activity), with `/`-style incremental search delegated
  to the server's `search=` parameter, pagination on demand, and `t` to
  toggle a project in the tracked list. Tracked projects sort first and
  carry a `●` marker.
- **Project view** — pick a ref (default branch preselected, then
  branches, then tags), `enter` generates the report into the chosen
  output directory, `o` opens the generated HTML via `file://`.
- All list/window/sort logic lives in pure functions (`_visible_window`,
  `_order_refs`, `slugify`, …) so tests cover it without a terminal;
  `curses` is imported lazily and its absence (native Windows Python)
  produces a one-line hint (`pip install windows-curses`, or use the
  headless subcommands).

Every TUI capability has a headless twin — `projects`, `report`, `track`/
`untrack`/`tracked`, `sync` — because the browsing UX is sugar; scripts
and CI want flags.

### 5. The offline guarantee bends only where the user points it

The generated *reports* remain fully offline — the fetched YAML is
materialized to disk first, then the ordinary offline pipeline runs, and
the offline-resources test keeps scanning every report. Network access
exists **only** inside the explicit `pipeview gitlab` subcommand, only to
the host the user named. `pipeview <path>` still never touches the
network. TLS verifies by default; corporate CAs via `--ca-bundle` (or
`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`, which corporate setups usually
already export); `--insecure` exists, warns loudly, and is never sticky.

### 6. Zero new dependencies

`urllib.request` + `json` + `curses` + `getpass` + `webbrowser` are all
stdlib. PyYAML is already a dependency (the fallback strategy reads
`include:` lists). No `requests`, no `python-gitlab`, no TUI framework —
the air-gapped install story stays one wheel.

## Module map

```
pipeview/gitlab/
  __init__.py
  api.py      # GitLabClient: REST + pagination + TLS options + typed errors
  auth.py     # token resolution chain; interactive prefilled-URL setup
  config.py   # ~/.config/pipeview/gitlab.json — hosts, tokens, tracked lists
  fetch.py    # lint & files strategies -> FetchResult (materialized workdir)
  report.py   # FetchResult -> parse_gitlab -> provenance annotations -> render
  tui.py      # curses browser; pure helpers separated for tests
  cli.py      # `pipeview gitlab …` argparse + dispatch
```

`pipeview/cli.py` routes `argv[0] == "gitlab"` to `pipeview.gitlab.cli`
before its own parser runs (the existing positional-path interface is
otherwise unchanged; a local *directory* literally named `gitlab` is still
reachable as `./gitlab`).

Report annotations gain one additive key, `annotations["gitlab_remote"]`
(host, project path/name/web_url, ref, strategy, fetched-at timestamp,
lint verdict, includes provenance). Schema stays v3; Make reports and
offline GitLab reports never carry the key.

## Testing

All tests run without network: a `FakeGitLab` stub implements the client
surface. Covered: lint strategy end-to-end (merged_yaml → report, GitLab
errors → diagnostics, includes → File Map), auto-fallback on 403/404,
files strategy resolving `include:project`/`template`/`local` through the
resolver hook (no ghost jobs), include cycles across repos, `ci_config_path`
(custom and `@other/project` forms), token resolution order, config file
permissions (0600), track/untrack round-trip, TUI pure helpers, and CLI
exit codes. `api.py`'s URL/param encoding is tested by monkeypatching
`urlopen`.

## As-built notes

- Tracking became per-ref after user feedback: a tracked entry is either
  `group/app` (follows the default branch) or `group/app@ref` (pinned).
  Project paths cannot contain `@`, so the first `@` splits path from ref
  unambiguously even for refs containing `/` or `@`. `sync` generates one
  report per entry; TUI `t` tracks the default branch from the project
  list and pins the selected ref from the ref picker; bare `untrack`
  sweeps every ref of a project.

- The lint strategy sends `content_ref` and `sha` together (and
  `dry_run_ref` + `ref` when dry-running); verified harmless on modern
  Grape which ignores undeclared params.
- `include:component` resolution tries `templates/<name>.yml` then
  `templates/<name>/template.yml` at the version ref, with `~latest`
  falling back through the component project's newest tag; failures
  degrade to the ordinary unresolved-include diagnostic.
- `sync` exits 1 if any tracked project's report carried warnings/errors,
  mirroring the main CLI's exit-code contract.
- The TUI paints a status line during fetches ("loading page 2…",
  "linting group/app@main…") instead of threading; calls are fast enough
  that synchronous-with-feedback beats a worker queue's complexity.
