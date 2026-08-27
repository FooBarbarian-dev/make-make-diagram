# Changelog

## Unreleased

**Markdown trigger docs** (see
`docs/superpowers/specs/2026-08-27-trigger-docs-design.md`). Define the
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
  `docs/superpowers/specs/2026-08-27-trigger-docs-phase2-design.md`):
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
`docs/superpowers/specs/2026-08-25-whatif-text-listing-and-delta-design.md`).

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
`docs/superpowers/specs/2026-08-25-upstream-downstream-rollup-design.md`).
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
(see `docs/superpowers/specs/2026-08-25-gitlab-job-merge-semantics-design.md`).
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
`docs/superpowers/specs/2026-08-25-gitlab-template-fallback-design.md`).
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
`docs/superpowers/specs/2026-08-25-gitlab-remote-fetch-design.md`). A new
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
`docs/superpowers/specs/2026-08-19-gitlab-predefined-variable-docs-design.md`).
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
`docs/superpowers/specs/2026-08-18-gitlab-what-if-design.md`). Model schema
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

Parser conformance pass (see `docs/parser-audit.md` for the full
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

Also in this release — report UI overhaul (see `docs/ux-audit.md` for the
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
