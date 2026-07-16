# pipeview report — UX audit and design plan

Audit of `pipeview/render/templates/report.html` as of commit `3b4f924`.
Line references are to that revision. Status column is updated as findings
are fixed; see "Verification" at the end for how fixes were checked.

Severity scale: **P0** broken/data loss · **P1** materially hurts use ·
**P2** quality/polish · **P3** nice-to-have.

## Findings

### Reported defects (Phase 1)

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 1 | P0 | No overflow policy anywhere. Long tokens (paths, `$(VARS)`, one-line recipes, node names) overflow table cells, the detail panel, and the header; `word-break: break-all` on `.recipe` re-wraps shell lines in meaning-changing ways. | whole stylesheet; `.recipe` line 77 | Repo-wide policy: single-line contexts get `ellipsis` + full value in `title` and in the detail panel; prose wraps with `overflow-wrap: anywhere`; code sits in `<pre>` with horizontal scroll inside the block and an optional soft-wrap toggle; paths middle-truncate keeping the tail. Torture fixture added to examples. | Fixed |
| 2 | P0 | Detail panel is fixed at 380px — not resizable, and closing it is the only "resize". No splitter anywhere. | `.panel` line 57 | Drag splitter with visible grab affordance, `col-resize` cursor, min 280px / max 60vw, double-click resets to default, close button collapses entirely, width persisted in memory for the session (no `localStorage`). Graph keeps its viewport on resize (ResizeObserver re-centers). | Fixed |

### Layout & structure

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 3 | P1 | Views have inconsistent chrome: graph has a filter row, others have ad-hoc `<h2>`s inside padded divs with different max-widths (900px vs none). | `.catalog`, `.var-explorer`, `.file-map` | One skeleton: header → tabs → toolbar → scrollable content, identical on all four views; per-view toolbar row holds the view's controls. | Fixed |
| 4 | P1 | Switching views can shift layout: content column has one scrollbar that appears/disappears per view; no stable scrollbar gutter. | `.content` line 56 | `scrollbar-gutter: stable` on the scroll container; chrome never scrolls; each view scrolls its own content region. | Fixed |
| 5 | P1 | Empty states are blank or one bare sentence ("No runnable tasks found."). Zero diagnostics shows nothing at all — indistinguishable from a broken render. | `renderCatalog`, `renderFileMap` | Designed empty state block (title + one sentence of why) for: no tasks, no variables, no files, no search results, and the good state "No diagnostics — everything resolved." | Fixed |
| 6 | P2 | No defined minimum width; below ~900px the header wraps chaotically. | `.header` | Declare the tool desktop-only: `min-width: 960px` on body with horizontal page scroll below that. | Fixed |
| 7 | P2 | `<meta name="viewport">` pretends mobile support the layout doesn't deliver. | line 5 | Keep the tag but pair it with the explicit min-width policy above. | Fixed |

### Dependency graph

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 8 | P1 | Zoom controls exist but no reset, no zoom-level indicator; fit re-runs the whole dagre layout instead of just re-fitting the transform. | `graph-controls`, `layoutGraph` | +/−/fit/reset(100%) buttons plus a live percentage readout; fit only recomputes the transform. | Fixed |
| 9 | P1 | Node visual states are incomplete: no hover state, no selected state (selection is invisible on canvas), dimming exists only via inline opacity. | `layoutGraph`, `highlightReachable` | CSS classes for default / hover / selected / in-focus / dimmed / ghost; selected node gets an accent ring; states survive re-render. | Fixed |
| 10 | P1 | Selecting a node from search or the catalog opens the panel but never pans/zooms the graph to it — "selected somewhere off-canvas". | `showNodeDetail` | `revealNode(id)`: switch to graph if needed, pan/zoom the node into view, apply selected state. Used by search, catalog, panel links. | Fixed |
| 11 | P2 | Edge kinds are color-only in the graph itself for solid kinds (`needs`, `includes`, `invokes` all solid, different hues). Legend repeats the colors. | `edgeColors`/`edgeDash` | Distinct dash pattern per kind so kinds survive grayscale; legend shows the patterns; hues become theme tokens (fixed hex broke dark mode). | Fixed |
| 12 | P2 | Node labels: naive end-truncation at 25 chars, no `<title>`, so the full name is unreachable on canvas. | `layoutGraph` line 410 | Middle-truncate labels, add SVG `<title>`, full name in the detail panel. | Fixed |
| 13 | P2 | Legend is always-on and overlaps the graph on small canvases; filter row and legend duplicate the same list. | `.graph-legend` | Collapsible legend (`<details>`) that also carries the per-kind show/hide filters — one list, one place. | Fixed |
| 14 | P3 | Collapsed-by-default groups / expand-all for large graphs. | — | The model has no group concept; out of UI-layer scope. Focus mode (click = highlight reachable subgraph) covers the dimming half. | Deferred (needs model support) |

### Task catalog

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 15 | P1 | No copy-to-clipboard for invocations. | `renderCatalog` | "Copy" button per row with "Copied" confirmation; clipboard API with `execCommand` fallback for `file://`. | Fixed |
| 16 | P1 | Table not sortable; documented/undocumented rows visually identical apart from italics. | `renderCatalog` | Name column sortable (asc/desc, `aria-sort`); undocumented rows get a muted "No description" state with the `##` nudge phrased as guidance. | Fixed |
| 17 | P2 | Flags render as color-only tags (`tag-manual` yellow, `tag-phony` green) with no second channel beyond the text itself; default goal not surfaced. | `.tag-*` | Chips always carry their text label (kept), get consistent chip styling from tokens, and the default goal gets a "default" chip. | Fixed |
| 18 | P2 | Long docs/invocations overflow cells (no policy). | `renderCatalog` | Covered by finding 1: single-line ellipsis + `title`, detail panel has the full value. | Fixed |

### Variable explorer

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 19 | P1 | "unresolved" is a plain string in the value cell — indistinguishable from a variable whose literal value is the word "unresolved". | `renderVariables` line 605 | Labeled state chip ("unresolved"), styled distinctly, never an empty or ambiguous cell. | Fixed |
| 20 | P1 | No sticky header, no sortable columns; counts left-aligned like text. | `.var-table` | Sticky `<thead>`, sortable name/events/used-by, counts right-aligned tabular numerals. | Fixed |
| 21 | P1 | Event timeline is a flat `<li>` dump: no visual story, override diffs (old → new) not visible together. | `.event-timeline` | Redesigned timeline (the report's signature element): vertical rail, operator badge, scope + `file:line` per event, and for overrides the previous value struck alongside the new one. | Fixed |
| 22 | P2 | Two detail surfaces (side panel + inline `var-detail-area`) show the same data with different markup. | `showVarDetail`, `showVarInlineDetail` | One renderer, one surface: the detail panel. Inline duplicate removed. | Fixed |
| 23 | P2 | `file:line` references styled inconsistently (plain text here, `.event-source` there). | throughout | One `.loc` component: monospace, muted, middle-truncated path, full path in `title`. | Fixed |

### File map & diagnostics

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 24 | P1 | Flat file list, not a tree; no expand/collapse; include graph rendered as a second disconnected list of `src → dst` strings. | `renderFileMap` | Directory tree with expand/collapse and per-file status chips; include/invoke relationships shown inline on the file row. | Fixed |
| 25 | P1 | Diagnostics only exist as a flat list at the bottom; not shown inline on the file where they occur. | `renderFileMap` | Diagnostics nested under their file in the tree plus the flat list; badge in header jumps here. | Fixed |
| 26 | P1 | Severities are color-only backgrounds; fixed light-mode hexes break dark mode entirely (light pastel on dark bg). | `.diag-item.*`, `.file-status-*` | Severity = icon + text label + tokened colors with AA contrast in both themes. | Fixed |
| 27 | P2 | Diagnostic copy not actionable (parser-supplied messages are pass-through; the UI adds nothing). | `renderFileMap` | UI renders location + related node as links so every diagnostic has a "go look here" action. Message text itself is parser scope. | Fixed (UI part) |

### Search

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 28 | P1 | Mouse-only: no `/` or `Ctrl/Cmd-K` focus shortcut, no arrow-key navigation, no Enter/Esc handling. | search block line 710 | Full keyboard loop: `/` and `Ctrl/Cmd-K` focus, ↑/↓ move, Enter jumps, Esc closes then clears. | Fixed |
| 29 | P2 | Results are a flat list — no grouping by type, no counts, no match highlighting; zero results silently shows nothing. | same | Grouped by Tasks & targets / Variables / Files with counts, matched substring bolded, designed "No matches" state. | Fixed |

### Detail panel

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 30 | P1 | Anatomy varies by kind and by entry point; variable links inside recipes exist but node → variable → node round trip breaks (inline area vs panel). | `showNodeDetail`/`showVarDetail` | One anatomy: identity (name, kind chip, location) → description → flags → recipe → variables used → edges in/out. Every cross-link bidirectional. | Fixed |
| 31 | P2 | Long recipes/annotations stretch the panel; sections don't scroll internally. | `.panel` | Panel scrolls; `<pre>` blocks scroll horizontally inside themselves (finding 1). | Fixed |

### Interaction & accessibility

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 32 | P1 | Tabs are `<div>`s; table rows are click-only; no focus styles anywhere; nothing keyboard-operable except native inputs. | throughout | Real `<button role="tab">` tablist with arrow keys; rows focusable (`tabindex=0`, Enter/Space); `:focus-visible` outline token on every interactive element. | Fixed |
| 33 | P1 | Contrast failures: `--fg2: #555` on `#f5f6f8` passes, but `--ghost: #999` fails AA; tag text on pastel chips unverified; dark-mode fixed hexes (findings 26) fail badly. | tokens | All text/UI colors re-derived from the token table below; checked against AA (4.5:1 text, 3:1 UI). | Fixed |
| 34 | P2 | No `prefers-reduced-motion` handling (tab transition animates). | `.tab` | Media query collapses all transitions/animations to instant. | Fixed |
| 35 | P2 | Hit targets: panel close and zoom buttons are ~18–32px; several below 32px. | `.panel-close` | All controls min 32×32px. | Fixed |
| 36 | P2 | Tooltips only via `title` (hover-only, mouse-only). | throughout | Icon buttons get CSS tooltips shown on hover **and** focus; `title` kept for text truncation cases where the detail panel also has the full value. | Fixed |
| 37 | P2 | No favicon → 404 noise from `file://`; page title fine but brand casing inconsistent ("PipeView" vs "pipeview"). | `<head>` | Inline SVG favicon as data URI; brand normalized to "pipeview". | Fixed |
| 38 | P2 | No print stylesheet — printing emits the app chrome and a clipped graph. | — | `@media print`: chrome hidden, active view expanded to full width, panel content flows below, graph fitted to page. | Fixed |
| 39 | P3 | Theme: honors `prefers-color-scheme` but no manual toggle. | tokens | Three-state toggle (system → light → dark), in-memory only. | Fixed |

### Microcopy

| # | Severity | Issue | Location | Proposed fix | Status |
|---|----------|-------|----------|--------------|--------|
| 40 | P2 | Title Case labels ("Dependency Graph", "Task Catalog", "Event Timeline"); tab names are jargon-adjacent; nudge text terse ("no description — add a ## comment"). | throughout | Sentence case everywhere; tabs: Graph / Tasks / Variables / Files; nudge reworded as guidance; buttons say what they do ("Copy command"). | Fixed |

## Design plan (Phase 3)

**Direction.** A dense engineering instrument in the profiler/IDE lineage.
Quiet neutral surfaces, one restrained accent, craft spent on spacing and
type discipline. **Signature element: the variable event timeline** — a
vertical rail where each assignment event is a station: operator badge,
scope, location, and override diffs shown old-struck/new-highlighted in
place. Everything else stays quiet. (The graph's edge-kind language is
functional, not decorative: dash patterns carry the meaning.)

**Explicitly avoided:** cream + serif + terracotta; near-black + acid
green; hairline broadsheet. Both themes are cool neutrals with an indigo
accent chosen for AA contrast in each theme.

**Tokens** (all in one `:root` block; dark theme overrides the same names):

- **Type.** UI stack `system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`;
  mono stack `ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace`.
  Five sizes: 11 / 12 / 13 / 15 / 18px (`--fs-0..4`). Weights 400/500/600 only.
- **Color.** Semantic: `--surface`, `--surface-raised`, `--surface-sunken`,
  `--border`, `--text`, `--text-muted`, `--accent`, `--accent-contrast`,
  plus severity `--info/--warn/--error` each with a `-bg` tint pair, and
  per-edge-kind hues. Light: near-white cool grays, text `#1a2029`,
  accent `#3b5bdb`. Dark: `#15181d` surfaces, text `#e5e9ef`, accent `#8da2f2`.
- **Space.** 4px base: `--sp-1..6` = 4/8/12/16/24/32.
- **Detail.** One radius (6px; 4px for chips via calc), one shadow
  (`--shadow`), one focus ring (`--focus-ring`), shared `.btn`, `.chip`,
  `.input` components used by every view.

**Theme switching.** `data-theme` attribute on `<html>`: absent = follow
`prefers-color-scheme`; `light`/`dark` force. In-memory only.

## Verification

- `python -m pytest tests/` — all tests including the no-network scan pass
  on every regenerated output (make example, gitlab example, self-report,
  torture example).
- Torture fixture (`examples/torture-project/`): 200-char variable value,
  150-char include path, 300-char one-line recipe. Rendered and checked by
  headless Chromium at 1280px and 1920px: no element wider than its
  scrollport, no page-level horizontal scrollbar. See
  `tests/test_html_renderer.py::TestTortureExample`.
- Screenshots of all four views, light and dark, taken headless and
  reviewed against this plan. Browser coverage note: the dev environment
  ships Chromium only, so the checks ran there; the Firefox pass is still
  owed. Everything used (details/summary, position: sticky,
  scrollbar-gutter, pointer events, prefers-color-scheme) is supported in
  current Firefox.
- Keyboard walkthrough scripted headless: Tab reaches search, theme toggle,
  tabs, rows, copy buttons; arrow keys move tabs and search results; Enter
  activates; Esc dismisses; splitter is keyboard-resizable (arrow keys).
- Last-look rule applied twice: the always-on graph legend panel was demoted
  to a collapsible `<details>` (it was decoration most of the time and
  overlapped the canvas), and a literal `▸` glyph baked into directory rows
  was removed in favor of a single CSS marker that actually rotates with the
  open state.
- Fixed during browser verification (caught by the headless overflow check,
  not visible in code review): data tables overflowed their scroll container
  at 1280px with the panel open — fixed-width cells can't shrink a table
  below its natural width; switched to `table-layout: fixed` + proportional
  `<colgroup>`. Also: hidden diagnostics badge rendered as an empty chip
  (`.chip`'s display beat the `[hidden]` UA rule) and the format chip showed
  the raw `gitlab_ci` identifier.
