# Changelog

## Unreleased

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
