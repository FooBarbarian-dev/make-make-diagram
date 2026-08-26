# What-If: copy-paste job listing + trigger delta comparison — design

Date: 2026-08-25
Status: implemented (autonomous session — decisions recorded below for review)

## Problem

Two asks:

1. The What-If tab shows which jobs would run for a trigger, but only as
   graphs and detail panels. People want a **plain-text listing** of those
   jobs they can copy into a chat message, an issue, or a script.
2. "What changes between trigger types?" — e.g. *which jobs run on a tag
   push that don't run on a branch push?* — currently requires flipping the
   scenario radio back and forth and remembering. People want a **delta
   view** between two trigger configurations.

## Decisions (the brainstorm, resolved)

### Text listing

- **Where**: the What-If tab, always visible as a collapsible
  "plain-text listing" block at the top of the results column, plus a
  **Copy** button. The block is a `<pre>` so manual select-and-copy always
  works even where the clipboard API doesn't (`file://` in some browsers);
  the button uses `navigator.clipboard` with a hidden-textarea
  `execCommand('copy')` fallback.
- **What's in it**: one section per candidate pipeline (children indented,
  prefixed `child pipeline:`), listing only the jobs that *might run*
  (runs / manual / delayed / conditional) — that is the "jobs that would
  run for the trigger" ask. Job name first (column-cut friendly), then
  stage, then the state with the same wording the graphs use
  (`runs`, `manual (blocking)`, `delayed 30 minutes`, `depends: <cond>`,
  `runs ×N` for matrix jobs). A header line describes the event
  (scenario, ref, MR knobs, changed-files assumption, overrides), so a
  pasted listing is self-describing. Not-created pipelines appear with
  their reason; duplicate jobs get a footer line.
- **Where the logic lives**: `textSummary(report, result, config)` in
  `whatif.js` — DOM-free like the rest of the evaluator, so the node
  vector harness can pin the exact text.

### Delta between trigger types

Three approaches considered:

- **(a) Two-dropdown A/B picker** — pick scenario A and scenario B, see a
  diff. Rejected: a scenario is more than the radio button (branch name,
  MR knobs, changed files, variables); two full control panels would be
  needed to configure both sides.
- **(b) All-scenarios matrix table** — one column per preset, one row per
  job. Rejected as the primary UI: it isn't a graph (the ask), hides the
  knobs, and explodes with matrix jobs and child pipelines. May return
  later as a follow-up.
- **(c) Pin-as-baseline (chosen)** — a **Pin as baseline** button
  snapshots the current configuration + evaluation. The user then changes
  anything — the scenario radio for "delta between trigger types", but a
  branch name, a variable, or the changed-files knob work too — and the
  results column switches to a delta rendering against the pin. One set
  of controls, arbitrary comparisons, and the mechanism generalizes past
  trigger types for free. Unpin (or pin again) to leave/rebase.

Delta rendering, top to bottom:

- **Summary banner**: baseline label vs current label, with counts —
  N added, N removed, N changed, N unchanged (job-level, across the whole
  event including child pipelines; only pipelines that are actually
  created count, matching the duplicate-detection rule).
- **Delta graphs, one per pipeline pair**. Candidate matching uses the
  event model's own shape: an event spawns at most one MR-type top-level
  candidate and at most one non-MR top-level candidate, so MR matches MR
  and non-MR matches non-MR (a branch pipeline compares against a tag or
  scheduled pipeline — that *is* the trigger delta). Child pipelines
  match by (parent pair, trigger job, child file). A pipeline with no
  counterpart renders whole as added/removed. Each graph draws the union
  of jobs that might run on either side: green = added, red-dashed =
  removed, amber = state changed (sub-label shows `runs → manual`),
  normal = unchanged. Edges come from the current side where the job
  exists there, otherwise from the baseline side. Clicking a node opens
  the job detail for whichever side has it (current preferred).
- **Text diff**: the plain-text block switches to a `+ / - / ~ / =`
  job-level listing (with each side's pipelines named), and the Copy
  button copies that — the two features compose.
- The scenario controls stay live in delta mode; every knob change
  re-evaluates the current side against the frozen baseline.

## Components

1. **`whatif.js` (DOM-free, exported)**
   - `describeConfig(config)` → one-line human description of a scenario
     config (used for headers and the pin label).
   - `outcomeText(outcome)` → the state wording shared by listing + diff.
   - `textSummary(report, result, config)` → the plain-text listing.
   - `diffEvents(resultA, resultB)` → structured diff:
     `{jobs, order, counts, pairs}` where `pairs` carries matched
     candidate pairs with per-job delta states and the union id list, and
     `jobs` carries the event-level effective outcome per side
     (strongest state across created pipelines: runs > delayed > manual >
     conditional) plus pipeline membership.
   - `textDiff(report, diff, labelA, labelB)` → the `+/-/~/=` text.
2. **`report.html`**
   - Toolbar: `Copy text` button (`#wi-copy-text`), `Pin as baseline`
     toggle (`#wi-pin-baseline`).
   - `wiBaseline = {config, result, label} | null`; delta mode iff pinned.
   - Normal mode renders the listing block; delta mode renders banner +
     per-pair delta graphs (`wiDeltaGraphSvg`, a sibling of `wiGraphSvg`)
     + diff text block. CSS classes `d-added`, `d-removed`, `d-changed`
     on `.wi-node`.
3. **Tests**
   - `tests/run_whatif_textdiff.js` + `tests/test_whatif_textdiff.py`:
     node runs the shipped `whatif.js` on existing fixtures
     (`whatif_dup`, `whatif_invalid`, `whatif_forward`, `whatif_matrix`),
     pytest pins listing content and diff verdicts (e.g. `test_mr` is
     *removed* going from push-with-MR to tag push; closing the MR turns
     the catch-all's two-pipeline duplicate into a single run, surfaced
     as *changed*).
   - `test_html_renderer.py`: the new control ids ship in generated
     GitLab reports.

## Error handling

- Clipboard write failing → the button flashes "press Ctrl+C" and selects
  the `<pre>` content instead; the text is always visible for manual copy.
- Fatal config (GitLab would reject) → listing says so instead of listing
  jobs; pinning is still allowed (comparing a broken baseline against a
  fix is useful).
- Pin + identical config → banner says "baseline and current are
  identical" with zero-delta counts; graphs render unchanged-only.

## Out of scope

- Exporting the text listing via the CLI (`--format txt`) — natural
  follow-up if the in-report block proves useful.
- The all-scenarios matrix table (approach b).
- Diffing two different *reports* (config versions); this compares two
  scenario configurations against one report.
