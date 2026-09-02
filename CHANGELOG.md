# Changelog

Releases from 0.2.0 onward are cut by
[release-please](https://github.com/googleapis/release-please) from
Conventional Commits merged to `main`; generated sections are prepended
here and enriched by hand where a feature deserves the narrative.

## Unreleased

**Python 3.11 is the floor.** 3.10 is dropped from `requires-python`, the classifiers, ruff's target and the CI matrix; the release tooling (`scripts/package.sh`) already needed 3.11's `tomllib`, and the test suite now uses it too.

**`pipeview lsp` in VS Code; a `pipeview-lsp` executable; the Zed
`binary` setting told the truth.** The VS Code extension now hosts the
same language server Zed does (`vscode-languageclient`): inline
diagnostics on open/save, hover docs for predefined `CI_*`/`GITHUB_*`
variables, document links for `include:local` and local `uses:`, and
the report code action — rewritten client-side so it opens the webview
panel rather than a browser (`pipeview.languageServer: false` turns it
off). The `editors/README.md` matrix converges accordingly. On the Zed
side, a configured `lsp.pipeview.binary.path` was documented as
defaulting its arguments to `["lsp"]`; Zed in fact applies that
setting itself, bypassing the extension, and runs the path with no
arguments — so `pipeview` started as the report CLI and died with
`the following arguments are required: path`. The dead extension
branch is gone, the docs and the not-found message now say
`"arguments": ["lsp"]`, and the server is additionally installed as
`pipeview-lsp` (also `python -m pipeview.lsp`) so a bare binary path
works as Zed runs it.

**GitHub Actions in the editor integrations.** The GitHub Actions
support that landed on `main` flows through both editors: repo reports
include the `.github/workflows/` root (a workflows directory is now
itself a valid `pipeview <path>` target), `pipeview lsp` treats workflow
files as first-class — inline diagnostics across the workflows
directory, hover docs from the `GITHUB_*`/`RUNNER_*` catalog, clickable
local `uses:` references (reusable workflows and composite actions),
and the report code action — and the VS Code extension gains the GitHub
counterparts of its GitLab commands (remote report, sync, terminal
auth, token in secret storage as `PIPEVIEW_GITHUB_TOKEN`). The
`editors/README.md` feature matrix now spans both providers.

**Release automation for the editor extensions.** The VS Code and Zed
extensions become their own release-please components: commits touching
`editors/vscode/` or `editors/zed/` route to per-extension release PRs,
tagged `vscode-vX.Y.Z` / `zed-vX.Y.Z`, with the packaged `.vsix` (and
the built wasm, for reference) attached to their GitHub Releases. CI
gains extension jobs (VS Code build + unit tests + packaging, Zed wasm
build). See `docs/release-pipelines.md` for the component table.

**Zed extension, `pipeview lsp`, and the `editors/` layout** (see
`docs/agents/specs/2026-08-29-zed-extension-and-editor-layout-design.md`).

- Editor integrations now live under `editors/` — `editors/vscode/`
  (moved from `vscode-extension/`, unchanged) and the new
  `editors/zed/` — with `editors/README.md` stating the parity
  philosophy (every feature lives in the core: CLI, report HTML, or
  language server; extensions only decide how their editor triggers it)
  and a per-editor feature matrix.
- **`pipeview lsp`**: a stdlib-only language server over stdio. Parser
  diagnostics published on open/save (offline, never Make-enriched),
  hover docs for predefined `CI_*` variables from the curated catalog,
  document links for `include:local`, and code actions that generate
  the report via the ordinary CLI in-process (stdout captured — the
  protocol channel stays clean) and open it in the default browser.
  `initializationOptions`: `upstream` (default on, matching the VS Code
  extension), `upstreamRemote`, `outputDir` (default: a cache dir, so
  repositories stay clean). Unrelated YAML gets silence.
- **Zed extension** (`editors/zed/`): a wasm extension
  (`zed_extension_api` 0.7.0, ~80 lines by design) wiring
  `pipeview lsp` up for YAML and Make buffers. Zed has no webviews, so
  the report code action opens the self-contained `file://` HTML in the
  browser — a complete viewer by construction. Server resolution:
  settings binary → `pipeview` on PATH → `python -m pipeview`; the
  worktree shell env flows through so GitLab tokens reach `--upstream`
  runs. Remote-project report/sync stay terminal flows in Zed (no
  extension input UI); `make zed` builds for `wasm32-wasip2`.

**Upstream include resolution + VS Code extension** (see
`docs/agents/specs/2026-08-29-vscode-extension-upstream-includes-design.md`).

- `pipeview <path> --upstream` analyzes the local working tree as
  always — uncommitted edits, real line numbers — but resolves
  cross-repository includes (`project:`, `remote:`, `component:`, and
  instance templates) by fetching them from the GitLab host the
  repository's own git remote points at. Remote selection: explicit
  `--upstream-remote`, else the branch's tracking remote, else
  `origin`, else the sole remote; ssh/scp-style/http(s) remote URLs all
  parse. Local files are never fetched or materialized; externals reuse
  the `files`-strategy traversal (nested includes, template fallback,
  the 150-file ceiling) and land under
  `<outdir>/fetched/<project>@upstream/`. Auth reuses the
  `pipeview gitlab` chain (`--token`, env vars, stored config); every
  failure — no git, no remote, no token, fetch errors — degrades to a
  warning diagnostic with the fix named, ghosts intact. Reports gain a
  `gitlab_upstream` annotation. This is the one way a plain
  `pipeview <path>` run touches a network, and only with the flag. The
  main CLI also gains `-v`/`--log-file` (the gitlab CLI's logging).
- **VS Code extension** (`editors/vscode/`): a thin TypeScript shell
  over the CLI. "Pipeline Report for This Repo" defaults to the open
  repository with `--upstream` on and renders the self-contained report
  HTML in a webview panel — every view included, What-If and all, since
  it is the same file the CLI writes. Also: per-file reports via
  context menus, regenerate-last, `gitlab report`/`sync` flows (the
  rollup opens when produced), API-token storage in VS Code secrets
  (injected as `PIPEVIEW_GITLAB_TOKEN`), an integrated-terminal flow
  for interactive `pipeview gitlab auth`, and a Pipeview output channel
  carrying the CLI's full output. Disabled in untrusted workspaces.
  `make vscode` builds and unit-tests it.

## [0.4.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/v0.3.0...v0.4.0) (2026-09-02)


### Features

* **lsp:** announce what pipeview offers when it attaches ([cfcbbd7](https://github.com/FooBarbarian-dev/make-make-diagram/commit/cfcbbd7b9681bf94a33613068f13e234b40c105b))


### Bug Fixes

* **enrich:** pin the whole C locale for make -p, not just LANG ([e901221](https://github.com/FooBarbarian-dev/make-make-diagram/commit/e901221985ec8edeea0c6a65224eb95b783f1bcc))
* open reports on Windows and from inside WSL, keep UNC worktrees ([49c7079](https://github.com/FooBarbarian-dev/make-make-diagram/commit/49c707979ef756f6ae5d0816c75cc0cdee8fe840))
* **vscode:** work on Windows — py launcher, batch wrappers, UTF-8, shell-free auth ([7a27376](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7a27376afccf2c8c569d1f8ee1e66ae61f06e601))
* **zed:** find Python on Windows, ship an installable extension archive ([59c9632](https://github.com/FooBarbarian-dev/make-make-diagram/commit/59c96325e04f79139c23fc36e45edc31ad24c5bc))

## [0.3.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/v0.2.1...v0.3.0) (2026-09-01)


### Features

* editor extensions (VS Code, Zed), upstream include resolution, and pipeview lsp ([7939b68](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7939b6885d1d289ab4206ef8b9d87007690c7d04))
* GitHub Actions roots in pipeview lsp and CLI root discovery ([6080562](https://github.com/FooBarbarian-dev/make-make-diagram/commit/60805629c98796ddb3dcc9e611a11cb7623d1a05))
* **vscode:** GitHub remote report, sync, auth, and token commands ([684ae5e](https://github.com/FooBarbarian-dev/make-make-diagram/commit/684ae5ef281b4965332c02ce5caa3352a35aa2f4))
* **vscode:** ship LICENSE and changelog for automated releases ([347c5bf](https://github.com/FooBarbarian-dev/make-make-diagram/commit/347c5bfcc3292d320e44965a3e2103c3a86bedda))
* **zed:** ship LICENSE, changelog, and version annotation for automated releases ([1fb53ce](https://github.com/FooBarbarian-dev/make-make-diagram/commit/1fb53ce95a4adbc7f0791e92ec14ac9acf0dee79))


### Bug Fixes

* harden --upstream and pipeview lsp (self-review pass) ([12b9689](https://github.com/FooBarbarian-dev/make-make-diagram/commit/12b968921ff3ded018851214f1488778b9ac918d))


### Performance Improvements

* use CSafeLoader for PyYAML parsing ([77a7e0b](https://github.com/FooBarbarian-dev/make-make-diagram/commit/77a7e0b93b37a12b2fc53bee0e2e3d10fedd8f3e))


### Documentation

* Zed round — lsp in the CLI reference and architecture, changelog ([3491812](https://github.com/FooBarbarian-dev/make-make-diagram/commit/3491812aaab6962e5e38b9aaef6771b1622b6bf7))
* **zed:** document GitHub Actions support carried by pipeview lsp ([f93c665](https://github.com/FooBarbarian-dev/make-make-diagram/commit/f93c665e11310838a9c5baaf056e2fc46e6879b3))

## [0.2.1](https://github.com/FooBarbarian-dev/make-make-diagram/compare/v0.2.0...v0.2.1) (2026-08-29)


### Bug Fixes

* **ci:** Add required permissions and documentation for release-please ([24a0606](https://github.com/FooBarbarian-dev/make-make-diagram/commit/24a06063e5335358aad5416d0b03155781130222))
* **ci:** Resolve permissions error in release-please pipeline and document release setup ([36620b6](https://github.com/FooBarbarian-dev/make-make-diagram/commit/36620b695c169bf4d2a5a90465cbcc083dea2300))


### Documentation

* reorganize agent design docs into docs/agents/, add AGENTS.md ([7dd4b98](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7dd4b98ce323c8a5e92faaa9ad58d47d03645b2a))
* reorganize agent design docs into docs/agents/, add AGENTS.md ([4cf3d62](https://github.com/FooBarbarian-dev/make-make-diagram/commit/4cf3d62011ee42062fd59fb828d035c97e795b33))

## 0.2.0 (2026-08-29)

**Project infrastructure.** GitHub Actions CI (ruff + pytest across
Python 3.10–3.13 + a package build check on every PR), automated
releases via release-please (version bumps, changelog, GitHub Release
with sdist/wheel attached), a repo-root `AGENTS.md` with the working
rules for contributors and coding agents, and a docs reorganization:
design specs and audits moved to `docs/agents/` (completed
implementation plans deleted), with `docs/user-guide.md` remaining the
primary user document.

**Markdown trigger docs** (see
`docs/agents/specs/2026-08-27-trigger-docs-design.md`). Define the
trigger scenarios you care about once, in a YAML file, and every report
run can also emit committed-markdown docs — per scenario, per project.

- `--trigger-docs FILE` on `pipeview <path>`, `pipeview gitlab report`
  and `pipeview gitlab sync` writes a `<slug>.trigger-docs/` folder
  beside each report: one `<id>.md` per scenario — outcome summary,
  fan-out diagram when one event spawns several candidate pipelines,
  per-pipeline mermaid DAG with verdicts encoded in node shape (manual
  hexagons, ⏱ delays, dashed *depends*), stage-ordered job tables with a
  deciding-rule "Why" column, a collapsed not-added table, an opt-in
  lifecycle sequence diagram — plus a `pipeline-triggers.md` index.
  Trigger jobs stop at the boundary; unknowables stay *depends*; no
  timestamps, so unchanged inputs regenerate byte-identically.
  Regeneration deletes only files carrying the provenance marker;
  hand-written files in the folder are warned about, never deleted or
  overwritten. Doc problems never block report generation.
- New `pipeview scenarios` subcommand group (offline, one binary):
  `init` writes a commented starter file, `check` validates one
  (exit 0/1/2), `preview` renders docs for a local checkout to stdout.
- The engine piece: `parsers/gitlab_whatif_eval.py`, a Python twin of
  the report's inlined JS What-If evaluator. Both interpreters answer to
  `tests/whatif_vectors.json` (now also run natively under pytest) and to
  a full-output parity sweep over every gitlab fixture and example × 14
  configs (`tests/test_whatif_parity.py`) — deep-equal JSON required.
- Phase 2 (see
  `docs/agents/specs/2026-08-27-trigger-docs-phase2-design.md`):
  the What-If tab gains **Export scenario** (copy the current knobs as a
  scenarios-file YAML stanza — the tab becomes the authoring UI; pinned
  by a semantic round-trip test: exported YAML must evaluate identically
  after loading) and **Copy markdown** (the job listing or pinned delta
  as markdown tables, same wording as the plain listing). The schema
  gains `changed_files: all`, `open_mr` on schedule/web/api/trigger, and
  `open_mr.{mr_flavor, mr_labels}` so exports are lossless. And `pipeview scenarios verify FILE REPO
  DOCSDIR` is the read-only drift check: committed docs vs fresh
  generation with provenance masked, exit non-zero on drift — CI can
  police doc freshness without write access.

**What-If: copy/paste job listing + trigger delta comparison** (see
`docs/agents/specs/2026-08-25-whatif-text-listing-and-delta-design.md`).

- **Plain-text job listing**: every What-If evaluation renders a
  collapsible text block — one section per candidate pipeline (children
  indented) listing the jobs that would run in stage order with stage
  and verdict, a self-describing event header, and a duplicate-jobs
  footer. A **Copy job list** toolbar button copies it (async clipboard
  API with a hidden-textarea fallback; if both fail the text is selected
  for a manual Ctrl+C).
- **Pin as baseline → delta view**: freeze the current scenario, then
  change any knob — the event preset included — and the results column
  shows the delta. Per-pipeline diff graphs (MR-type candidates match
  MR-type, non-MR match non-MR, children by trigger job + file): added
  jobs green, removed red-dashed, verdict changes amber with a
  `runs → manual gate` sub-label; unmatched pipelines are flagged whole.
  The text block switches to a `+ / - / ~ / =` job-level diff (the copy
  button becomes **Copy delta**) with a `pipelines:` section whenever the
  pairing itself is the story — a pipeline on one side only, renamed
  across sides, or failing creation — and "same verdict but now in 2
  pipelines instead of 1" — the duplicate-pipeline case — counts as
  changed. While pinned, the skipped-jobs toggle (which only affects the
  normal graphs) is disabled with an explanation.
- The logic ships as DOM-free helpers in the inlined evaluator
  (`textSummary`, `diffEvents`, `textDiff`, `describeConfig`), pinned by
  a new node-driven suite (`tests/test_whatif_textdiff.py`) on the
  existing fixtures. Candidate results now also carry the MR `target`
  ref so listings can say `feature/widget → main`.

**Cross-project pipeline links + tracked-set rollup** (see
`docs/agents/specs/2026-08-25-upstream-downstream-rollup-design.md`).
`pipeview gitlab sync` now sees across the tracked set: references
between tracked projects resolve into real links instead of dead-end
ghosts, and a fleet-level **rollup report** is generated beside the
per-project reports.

- **Typed trigger records (model schema v4, additive)**: trigger jobs
  carry `annotations["trigger_info"]` — mode (`multi_project` vs
  `child`; `trigger:include:project` correctly stays a child), raw
  project/ref with uses-CI-variables flags, `strategy`,
  `trigger:forward` defaults, include summaries (dynamic
  `trigger:include:artifact` children marked unresolved). `needs:project`
  entries record project/job/ref on the needing job
  (`cross_project_needs`). Older report JSON still loads.
- **Rollup resolution** (`pipeview/gitlab/rollup.py`, pure): trigger /
  artifact-needs / config-include references match tracked entries with
  exact ref semantics — explicit `trigger:branch` against the entry's
  resolved ref, ref-less triggers against the downstream's default
  branch (known from its own report) — and every degraded link says why:
  ref mismatch, pinned-vs-default-branch, CI-variable values, untracked
  target. Untracked references aggregate into externals ("track them to
  link their pipelines"); reports generated far apart get a
  snapshot-skew warning.
- **`rollup.report.html`** — one more self-contained offline page:
  fleet graph (projects + dashed untracked externals; the three link
  kinds dual-encoded and filterable, caveat edges flagged ⚠), drill-down
  job graphs with `↪` portal jobs that jump along trigger edges,
  breadcrumbs, cross-project search, and panels that explain trigger
  semantics (created-vs-succeeded, mirror/depend, forwarded variables)
  and keep upstream lists honest ("within the tracked set").
  `rollup.json` carries the same document; `--no-rollup` opts out.
  Resolved reports re-render so ghost panels link to the rollup.
- **Graph exploration controls** (both report formats): the legend
  grows Nodes toggles (stage lanes, templates, pattern rules,
  unresolved references), collapsible Groups — child pipelines and
  sub-makes fold to one expandable node, boundary edges deduplicated
  onto it (with the invokes edge that previously only existed in the
  Files view), expanding draws a cluster outline — and Focus direction
  (dependencies / dependents / both) + hop-depth controls. Search and
  panel reveals auto-expand hidden targets.
- **Hardening**: inline model JSON is `<`-escaped so content
  containing `</script>` can no longer break the report page.

**Same-name jobs now merge across files the way GitLab merges them**
(see `docs/agents/specs/2026-08-25-gitlab-job-merge-semantics-design.md`).
A local job that customizes a job an included file defines — the standard
"override a template job's `rules:`" pattern — used to *replace* the
included definition wholesale, so the job lost its script and the report
warned "has no script, run, or trigger" about jobs that run fine.
Verified against GitLab's own source
(`Gitlab::Ci::Config::External::Processor`, `Extendable::Entry` at
v19.3.0) and fixed to match:

- **Deep merge in merge order**: definitions of the same job name merge
  per key — hashes (`variables:`, …) merge recursively, arrays and
  scalars (`script:`, `rules:`, `stage:`, …) are replaced whole — with
  includes merged first (later includes beating earlier, nested includes
  depth-first) and each file's own content beating what it includes; the
  root file always wins. The position of the `include:` key inside a file
  no longer affects precedence (it never does in GitLab).
- **Merges are visible**: an info diagnostic names both definition sites
  ("Job 'build' is also defined in [template] Jobs/Build.gitlab-ci.yml:4
  — GitLab deep-merges the two, this definition taking precedence…") and
  merged jobs carry a `merged_from` annotation listing every source in
  merge order.
- **Top-level keys follow the same rules now**: `stages:` is taken from
  the last file in merge order that defines it (previously first-wins),
  `default:` and the deprecated top-level defaults deep-merge per key
  across files, and `workflow:` accumulates per key — an include can
  supply `rules:` while the root supplies only `name:` (previously a
  later `workflow:` mention wiped included rules).
- `extends` was already GitLab-exact (later parents override earlier,
  child on top) and now operates on the correctly merged job table;
  covered by new tests including root files overriding hidden jobs that
  includes define.

**GitLab built-in templates resolve everywhere — bundled snapshot
fallback** (see
`docs/agents/specs/2026-08-25-gitlab-template-fallback-design.md`).
`include:template` entries used to come out as ghost jobs whenever the
template wasn't servable through GitLab's REST template API — which is
most of them: the API only exposes the flattened "dropdown" keys
(top-level names plus the basenames of `Pages/`/`Verify/`/`Security/`),
so `Jobs/*` and `Workflows/*` 404 in every spelling on **every** GitLab
version (verified against gitlab.com). Since `Security/SAST.gitlab-ci.yml`
is itself just a stub that includes `Jobs/SAST.gitlab-ci.yml`, any real
security/Auto-DevOps pipeline hit this.

- **Bundled template snapshot**: pipeview now ships a verbatim, MIT-licensed
  copy of GitLab's `lib/gitlab/ci/templates` tree
  (`pipeview/data/gitlab_ci_templates/`, 133 templates pinned at GitLab
  19.3.0, provenance in `_meta.json`), refreshed by the maintainer script
  `scripts/update_gitlab_templates.py`.
- **Remote fetch (`files` strategy)**: templates are requested from the
  instance's API first — now using key spellings that can actually work,
  including the category-flattened form (`Security/SAST.gitlab-ci.yml` →
  `SAST`) — and fall back to the bundled copy with an info diagnostic
  naming the snapshot version; nested `include:template` chains recurse.
  Only a template unknown to both stays a ghost, and the warning says
  which lookups failed.
- **Offline runs too**: `pipeview <path>` resolves `include:template` from
  the same snapshot — still zero network — showing template files as
  `[template] Jobs/Build.gitlab-ci.yml` in the File Map with their jobs as
  real nodes instead of ghosts.
- `--no-bundled-templates` (main CLI and `pipeview gitlab`) restores the
  previous ghost-node behavior.
- The test fake now mirrors the real template API (404 for any slashed or
  suffixed key) so this class of bug can't pass the suite again.

**`pipeview gitlab` — fetch straight from a GitLab instance** (see
`docs/agents/specs/2026-08-25-gitlab-remote-fetch-design.md`). A new
subcommand — the only part of pipeview that touches a network — connects to
a GitLab host, browses the projects your token can see, and generates the
ordinary offline reports from what GitLab serves, cross-repository
`include:`s resolved.

- **Curses TUI** (`pipeview gitlab`): project list with server-side search
  (`/`), track/untrack (`t`, tracked projects sort first), ref picker
  (default branch, branches, tags), report generation and open-in-browser
  (`o`). Every TUI action has a headless twin: `projects`, `report`,
  `track`/`untrack`/`tracked`, `sync` (reports for all tracked projects).
- **Tracking is per-ref, not default-branch-only**: entries are
  `group/app` (follows the default branch) or `group/app@ref` (pinned to
  any branch/tag — `track group/app@dev` or `--ref dev`; `report` accepts
  the same inline form). A project can be tracked at several refs; `sync`
  generates one report per entry; in the TUI, `t` in the project list
  tracks the default branch while `t` in the ref picker pins the selected
  ref (tracked refs carry a `●`). `untrack group/app` sweeps all refs,
  `untrack group/app@dev` removes one.
- **Two fetch strategies** (`--strategy auto|lint|files`). Primary: the
  project-scoped CI Lint API (`GET /projects/:id/ci/lint`), whose
  `merged_yaml` is the complete configuration with every include —
  cross-project, template, remote, component — expanded server-side in one
  call; GitLab's own errors/warnings become report diagnostics and its
  include-provenance metadata lands in the File Map. Fallback (older
  instances, restricted tokens, or on request for real per-file line
  numbers): recursive `include:` traversal across repositories via the
  files API, honoring custom `ci_config_path` (including the
  `file@group/project` form), wildcard local includes, nested
  cross-project `include:local`, templates, remote URLs, and best-effort
  CI/CD components.
- **Token handling**: resolution chain `--token` →
  `$PIPEVIEW_GITLAB_TOKEN` → `$GITLAB_TOKEN` → `$GITLAB_PRIVATE_TOKEN` →
  stored config; `pipeview gitlab auth` opens GitLab's prefilled
  personal-access-token form (`read_api` scope), verifies the pasted token
  against `/user`, and stores it 0600 in
  `~/.config/pipeview/gitlab.json` (which also carries the per-host
  tracked-projects list). TLS verifies by default; `--ca-bundle` for
  corporate CAs.
- **Parser hooks, offline-inert**: `parse_gitlab` gains optional
  `repo_root`, `external_resolver`, and `local_roots` keywords so the
  fetch layer can materialize cross-repo files and have them parsed as
  real files (no ghosts) with GitLab's nested-include semantics; offline
  behavior is unchanged when they're omitted. Reports gain one additive
  annotation, `annotations["gitlab_remote"]` (host, project, ref,
  strategy, lint verdict, include provenance) — schema stays v3.
- **Verbose logging and real error reporting**: `-v` logs fetch steps and
  decisions (strategy chosen and why, every file fetched with source and
  destination, ref resolutions), `-vv` adds each HTTP request with status
  and timing, `--log-file` captures full debug detail to a file; in the
  TUI, `-v` logs to `<outdir>/pipeview-gitlab.log` (curses owns the
  terminal) and the status bar points there. `sync` now prints each
  entry's warning/error diagnostics to stderr — GitLab's CI Lint verdict,
  fetch failures, unresolved includes — instead of a bare `[error]`
  marker; API errors carry the server's own explanation; top-level errors
  hint at `-v`, which also prints tracebacks.
- Zero new dependencies (stdlib urllib + curses); generated reports remain
  fully offline — fetched files are materialized under
  `<outdir>/fetched/<project>@<ref>/` first, then the ordinary offline
  pipeline runs.

Report UI: **tooltips never run off-screen**. Every `data-tip` hover
(variable docs, chips, toolbar buttons) now renders into one shared
`position: fixed` element that JS clamps to the viewport, replacing the
per-anchor CSS pseudo-element.

- Fixes doc tooltips getting cut off at the window edges — seen in the
  What-If "variables in effect" table, the unknown-variables panel, and
  the rule-trace job details; scroll containers can no longer clip a tip
  either. The one-off left-alignment patch for the unknown-variables
  panel is gone, superseded by the general clamp.
- Tips flip above the anchor when there is no room below, follow the
  anchor while a container scrolls under a hovering pointer, still show
  on keyboard focus, and now dismiss on Escape.
- Also resolves a silent `::after` collision between the tooltip and the
  splitter's grip line, which both claimed the same pseudo-element.
- Variables tab: a predefined name in the table now carries its docs
  tooltip on the name itself (dotted underline), matching the What-If
  views; the detail panel and origin chip already had them.

GitLab CI **predefined-variable docs** (see
`docs/agents/specs/2026-08-19-gitlab-predefined-variable-docs-design.md`).
GitLab reports now explain the `CI_*`/`GITLAB_*` variables they surface —
what each is, an example value, and when GitLab sets (or notably unsets) it.

- New curated catalog `pipeview/parsers/gitlab_predefined.py` (~100
  entries: summary, example, set/unset conditions, documented gotchas),
  embedded whole in `report.annotations["predefined_var_docs"]` and
  therefore in `model.json`. Additive — schema stays v3; Make reports are
  untouched.
- Variable Explorer: predefined variables get their own summary on the
  origin chip and a full docs block in the detail panel; a collapsible
  "GitLab predefined variables reference" below the table lists the whole
  catalog with a filter. Names referenced in this configuration sort
  first — "used here" links back to the variable's row, "in rules" marks
  rules/workflow-expression references.
- What-If tab: predefined names in the per-pipeline "variables in effect"
  table, the rule-by-rule traces, and the unknown-variables panel carry
  documentation tooltips (dotted underline = hover for what this is).
- Docs can never contradict the simulator: a test scans the shipped
  templates and fails on any predefined name without a catalog entry;
  entries restate the simulator's verified facts; examples are always
  labeled "e.g."; names outside the catalog keep the generic honest
  wording, never an invented description.

GitLab CI **What-If simulation** (see
`docs/agents/specs/2026-08-18-gitlab-what-if-design.md`). Model schema
bumped to v3 (`Node.annotations["whatif"]`, `Report.annotations["whatif"]`);
v2 JSON still loads.

- New What-If report tab: pick an event (branch push, push with an open
  MR, tag, MR update, schedule, manual, API/trigger) and a starting state
  (branch, MR knobs, changed files, project-level variable overrides) and
  see every candidate pipeline GitLab would spawn, one graph per pipeline,
  with per-job verdicts and rule-by-rule traces. Duplicate jobs across
  simultaneously-spawned pipelines are badged and summarized — the
  debugging story for "why did my push start two pipelines?".
- `pipeview/parsers/gitlab_whatif.py` compiles `rules:if` /
  `workflow:rules` / `only:variables` expressions to a JSON AST
  (unparseable → *unknown* + diagnostic), normalizes legacy `only/except`
  including the implicit `only: [branches, tags]` default, evaluates
  `rules:exists` against the repo at generation time, and extracts
  artifact produce/consume info (dotenv reports, needs, dependencies,
  trigger children).
- `templates/whatif.js`: a DOM-free tri-state interpreter (true / false /
  *unknown*) with GitLab's documented semantics — unset vs empty
  variables, truthy `"false"`/`"0"`, first-match-wins rules, workflow
  gating, `rules:changes` always-true in no-push-event pipelines, the
  predefined-variable matrix per pipeline type, child pipelines running as
  `parent_pipeline`, and pipeline-creation failure detection for `needs:`
  on excluded jobs. Tested under node against a hand-written vector suite
  drawn from the GitLab docs (`tests/whatif_vectors.json`), including the
  documented duplicate configs and the canonical `workflow:rules` dedup
  pattern.
- New example `examples/gitlab-whatif-project` demonstrating the duplicate
  problem, the dedup fix, dotenv flow, and a child pipeline.
- Static lint: GitLab's "job may allow multiple pipelines to run for a
  single action" warning is now emitted at generation time for final
  unconditional `when:` rules.
- Expert-review hardening (two adversarial review passes, every fix
  pinned by a doc/source-verified test): unsimulated `CI_*` variables
  evaluate as honest *unknown* (pin-able) instead of confidently unset;
  nested `rules:` arrays flatten; legacy `except:` ORs its clause kinds
  and singular source keywords work; `CI_OPEN_MERGE_REQUESTS` is set in
  scheduled/API/web/trigger branch pipelines; schedules/manual runs can
  target tags; child pipelines evaluate their own `workflow:rules` and
  globals (with `trigger:forward` semantics honored, including
  `yaml_variables: false`); `inherit:variables` filters rules
  evaluation; conditional `include:rules` gate their files' jobs;
  `parallel:matrix` expands before rules with per-instance axis
  variables and instance-name `needs`; configs GitLab rejects outright
  (dependencies⊄needs, circular needs, invalid expressions, broken
  `environment:on_stop`) surface as a red invalid-configuration state,
  and per-candidate creation failures get a distinct "✖ creation fails"
  chip excluded from run counts; manual-gate blocking defaults follow
  GitLab's `manual_action? && !has_rules?`; out-of-sync `on_stop`
  rules, failure-only artifact chains, and duplicated jobs sharing a
  `resource_group` are called out.

Parser conformance pass (see `docs/agents/parser-audit.md` for the full
construct-by-construct matrix, verdicts, and accepted limitations). Model
schema bumped to v2 (`VariableEvent.annotations`, `Variable.exported`,
`Variable.origin`, `Report.annotations`); v1 JSON still loads.

GNU Make parser:

- Directive tokenization: `export`/`unexport`/`override`/`private`/
  `undefine`/`define` peel off the front of a line in any stacking order —
  `export VAR ?= value` is a variable event with an exported mark, not an
  "Unparseable line" warning (the reported field failure), and
  `export VAR := x` no longer silently fabricates rule nodes.
- Assignment vs rule decided by reference-aware operator scanning, never by
  the first colon: `URL := https://example.com`, `foo: PATH := /usr:/bin`,
  the full operator set (`= := ::= :::= ?= += !=`), `\#` escapes, `$$`
  shell forms, empty values with trailing comments.
- Targets defined after first being referenced upgrade from ghost to real
  nodes (the `all: build test` idiom no longer renders ghosts).
- Static pattern rules (stem-substituted prerequisites), grouped targets
  (`&:`), double-colon recipe accumulation, make-style "overriding recipe"
  warnings, semicolon and explicit-empty recipes.
- `.PHONY` with variables, `.DEFAULT_GOAL :=`, `.EXPORT_ALL_VARIABLES`,
  `.ONESHELL`, `.SECONDEXPANSION` (named diagnostics), `.RECIPEPREFIX`
  honored; special targets never become graph nodes.
- Includes expand globs/`$(wildcard)` and simple `$(VAR)` paths; include and
  sub-make cycles are named diagnostics; recursion detection understands
  `${MAKE}`, `+$(MAKE)`, `cd dir && $(MAKE)`, `$(MAKE) -C $(VAR)`.
- Built-in defaults (CC, RM, SHELL, …) labeled instead of "unresolved";
  automatic variables excluded from the variable table and explained on
  hover in recipes; enrichment now captures `make -p` variable origins.
- CRLF/BOM tolerated; unclosed `define`/stray `endif` diagnosed; the
  "Unparseable line" diagnosis is retired — if real make accepts a file,
  an unparseable-class warning is treated as a pipeview bug (enforced by a
  contract test).

GitLab CI parser:

- `!reference` tags handled by a SafeLoader subclass: local targets splice
  per GitLab semantics; external targets (jobs from unfetchable includes)
  degrade one value — placeholder + ghost + named diagnostic with the real
  line — instead of erroring the whole file at line 1 (the reported field
  failure).
- Variable values keep the author's raw text (`yes`, `0777`, `12:30` no
  longer display as `True`/`511`/`750`); ambiguous unquoted scalars get an
  info note; hash-form variables parse value + description.
- Duplicate mapping keys diagnosed (YAML's silent last-wins data loss).
- `extends` follows GitLab's documented merge (hashes deep-merge, arrays
  replace, later parents win); children inherit script/stage/needs;
  replacement provenance shown ("script overrides `.base`"); cycles named.
- `default:`, deprecated top-level defaults, `inherit:` opt-outs honored.
- `needs: []` = "starts immediately" (no synthesized stage edges);
  `dependencies:` treated as artifact flow, never ordering; default stage
  list and `test` default for stage-less jobs; undeclared stages diagnosed
  as the pipeline errors they are; `.pre`/`.post` ordering.
- `pages` is a job again; `workflow:rules` becomes a report-level banner;
  `parallel`/`parallel:matrix` render one node with expansion counts;
  `environment`, `resource_group`, `retry`, `timeout`, `artifacts`, `cache`
  surfaced; `trigger:include:local` child pipelines parsed as linked
  sub-pipelines; `CI_*`/`GITLAB_*` labeled predefined; wildcard and
  conditional includes; string-form scripts and nested script arrays.
- Robustness: bool/int job keys no longer crash; multi-document files
  salvage the first document; big-file parse time roughly halved.

Report UI (existing component vocabulary only): exported/origin chips in
the variable explorer, event-level chips and notes on the timeline,
starts-immediately/parallel/delayed flags, matrix details, a pipeline-gate
banner, and hover explanations for automatic variables and recipe-line
prefixes.

Also in this release — report UI overhaul (see `docs/agents/ux-audit.md` for the
full audit):

- Repo-wide text overflow policy: ellipsis + full value on hover/in the
  detail panel for single-line contexts, horizontal scroll inside code
  blocks (with a soft-wrap toggle), middle-truncated paths. Verified
  against a new overflow torture example (`examples/torture-project`).
- Resizable, collapsible detail panel: drag splitter with min/max widths,
  double-click to reset, keyboard-resizable, width kept in memory for the
  session.
- Design token system (type scale, spacing scale, semantic colors) with
  automatic light/dark themes and a manual theme toggle.
- Graph: zoom in/out/fit/reset controls with a live zoom readout,
  selection/focus/dimmed node states, edge kinds distinguished by dash
  pattern (not color alone), collapsible legend that doubles as the edge
  filter, and pan-into-view when a node is selected from search.
- Tasks: sortable catalog with copy-to-clipboard invocations and labeled
  flag chips.
- Variables: sticky sortable table, labeled "unresolved" state, and a
  redesigned event timeline showing override diffs (old vs new together).
- Files: directory tree with per-file status chips, inline diagnostics,
  and severity icons + labels.
- Keyboard-first search (`/` or `Ctrl/Cmd-K`, arrow keys, Enter, Esc) with
  results grouped by type and matched substrings highlighted.
- Accessibility floor: visible focus everywhere, keyboard-operable tabs,
  rows, and splitter, WCAG AA contrast in both themes, reduced-motion
  support, ≥32px hit targets, tooltips on focus as well as hover, print
  stylesheet, inline SVG favicon (no favicon 404 from file://).
- Designed empty states, including "No diagnostics — everything resolved."

**Fixed**

- Mermaid graph export (`--format mmd`) now emits valid mermaid for Make
  graphs: node ids flatten to mermaid's identifier alphabet (`$(OBJS)`,
  `%.o` and friends previously produced parse errors) and labels are
  always quoted, matching the trigger-docs renderer.

**Documentation**

- New [user guide](docs/user-guide.md): a
  screenshot tour of every report view plus two worked examples — mapping
  a recursive Make build, and chasing a GitLab duplicate-pipeline problem
  through the What-If tab to a pinned-baseline delta and committed
  trigger docs.
- UI screenshots (captured from the bundled examples) in
  `docs/screenshots/`, with a few embedded in the README.
- README accuracy pass: view names now match the report's actual tab
  labels (Graph / Tasks / Variables / Files — the UI renamed them in the
  UX-audit pass, the README hadn't caught up), the architecture tree
  gained the files it was missing (`gitlab_templates.py`,
  `data/gitlab_ci_templates/`, `parsers/gitlab_predefined.py`,
  `gitlab/rollup.py`, `render/rollup_html.py`), and the exit-code table
  notes that trigger-docs problems floor the exit code at 1.
- Usability pass over both docs, every command verified by running it:
  the README's trigger-docs block now uses the filename `scenarios init`
  actually writes (`pipeview-scenarios.yaml`); the guide's trigger-docs
  flow runs end-to-end against the bundled example (it previously
  targeted `.`, which errors in this checkout) and gained the copy step
  and a scheduled-CI `verify` job; quickstarts standardize on the
  gitignored `examples/out/`; the guide opens with clone + prerequisites
  and cross-platform open instructions, and gained a keyboard-shortcut
  table, a "why didn't my job run on this MR?" recipe, a troubleshooting
  section, and plain-language glosses for ghost nodes, dotenv, roots,
  diagnostics, and provenance markers.

## 0.1.0

Initial release.

- GNU Make static parser: targets, prerequisites, pattern rules, order-only
  dependencies, variables (all operators), `include`/`-include` chains,
  `$(MAKE) -C` recursion detection, `ifeq`/`ifdef` conditionals (both
  branches captured), `define` blocks, `##` docstrings.
- GitLab CI parser: stages, jobs, `needs:` DAG, `extends:` chains with
  template inheritance, `include:` (local resolved, `project:`/`remote:`/
  `template:`/`component:` as ghost nodes), `rules:`/`when: manual`,
  `only:`/`except:`, `trigger:`, job and global variables.
- Optional Make enrichment pass (`make -pqn`) for resolved variable values
  and computed default goal, with `--no-enrich` opt-out.
- Single-file HTML report with four interactive views: Dependency Graph
  (dagre layout, pan/zoom, focus mode, edge filters, legend), Task Catalog,
  Variable Explorer (event timelines, clickable `$(VAR)` references), and
  File Map (include tree with diagnostics).
- Export formats: HTML, JSON model, DOT, Mermaid, SVG.
- Fully offline: no network access at generation time, no CDN references in
  output. Enforced by automated test.
- Ghost nodes for unresolvable references, with diagnostics.
- `python -m pipeview` support for running from a checkout.
