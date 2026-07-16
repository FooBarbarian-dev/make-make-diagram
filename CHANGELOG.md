# Changelog

## Unreleased

Report UI overhaul (see `docs/ux-audit.md` for the full audit):

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
