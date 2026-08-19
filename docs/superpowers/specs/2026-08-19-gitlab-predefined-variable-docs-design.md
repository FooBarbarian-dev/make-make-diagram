# GitLab CI predefined-variable documentation — design

Date: 2026-08-19
Status: proposed — awaiting user review. (Planned in an autonomous session:
the decisions below are recommendations with alternatives recorded, not
choices already approved in dialogue.)

## Problem

GitLab CI reports are full of predefined variable names the report never
explains. A rule trace shows `$CI_PIPELINE_SOURCE == "merge_request_event"`,
the per-pipeline "variables in effect" table lists thirty `CI_*` rows, the
What-If variables panel groups unknowns into buckets, and the Variables tab
lists every `CI_*`/`GITLAB_*` name referenced in the YAML — but the only
explanation anywhere is one generic sentence ("A GitLab predefined variable:
the runner provides its value for every job"). A reader who does not already
know what `CI_COMMIT_REF_NAME` is, what values `CI_PIPELINE_SOURCE` can take,
or when `CI_COMMIT_BRANCH` is unset has to leave the report — which defeats
an offline report.

Goal: every predefined variable a report surfaces gets a short, accurate
summary — what it is, an example value, and when GitLab sets it (and, where
that is the interesting part, when it is *unset*).

## What exists today (anchors)

- The parser already labels referenced-but-never-assigned `CI_*`/`GITLAB_*`
  names with `origin = "predefined"`
  (`gitlab_parser.py:_label_predefined_variables`, `_PREDEFINED_VAR_RE`).
  They appear in the Variables tab with an origin chip and a generic detail
  sentence (`report.html` `showVarDetail`, `ORIGIN_TIPS`).
- The What-If simulator builds a full predefined env per candidate pipeline
  (`whatif.js` `buildEnv` + `CONTROLLED`, ~40 names), with verified
  set/unset facts recorded as code comments.
- Predefined names render in four What-If places: the "variables in effect"
  disclosure (`wiEnvTable`), rule-trace variable lines (`wiVarsLine`), the
  three-bucket unknown-variables panel (`wiUpdateUnknownVars`), and workflow
  traces.
- Tooltip precedent exists: the `data-tip` CSS pattern (sentence-length tips
  wrap, max-width 280px), and Make automatic variables (`$@`, `$<`, …) are
  already "recognized, tooltipped with their meaning" in recipe text.
- Job script text renders `$VAR` references as clickable links that round-trip
  to the variable's detail panel — so scripts need no separate tooltip
  treatment; enriching the detail panel covers them.

## Decisions

### 1. One curated catalog, Python-side, embedded in the model

New module `pipeview/parsers/gitlab_predefined.py` exporting
`PREDEFINED_VAR_DOCS: dict[str, dict]`. Entry fields:

- `summary` — one sentence: what the variable is.
- `example` — one realistic value, always displayed prefixed "e.g." (it is
  an illustration, never simulation output).
- `set_when` — when GitLab sets it ("every job", "branch pipelines only",
  "merge request pipelines", "jobs with an environment", …).
- `unset_when` *(optional)* — when it is notably absent; often the gotcha
  ("unset in merge request and tag pipelines").
- `note` *(optional)* — one extra gotcha sentence, only where a documented
  surprise exists (e.g. `CI_PIPELINE_SOURCE` is `push` for both branch and
  tag pipelines; `CI_MERGE_REQUEST_SOURCE_BRANCH_SHA` is set-but-empty in
  detached MR pipelines).

`parse_gitlab` attaches the whole catalog as
`report.annotations["predefined_var_docs"]`. Rationale for whole-catalog
(no filtering to referenced names): the reference section wants the full
list, the size is trivial (~80 entries ≈ 15–20 KB in a report that already
inlines dagre), and no filtering logic means no filtering bugs. The
annotation is additive — `SCHEMA_VERSION` stays 3. Make reports never get
the key, so the renderer stays format-neutral: every new UI behavior keys
off the annotation's presence, exactly like the What-If tab keys off
`annotations["whatif"]`. `model.json` gains the docs for free.

### 2. Catalog scope: simulated + common, honest fallback for the rest

Two tiers, one dict:

1. **Mandatory:** every name `whatif.js` sets or controls (`buildEnv`,
   `CONTROLLED`) — enforced by a test (below). These docs restate the same
   verified facts the simulator encodes, in the same language.
2. **Curated common set:** `CI_JOB_*` (name/stage/id/url/token/status),
   `CI_PIPELINE_*` (id/iid/url/created_at), `CI_PROJECT_*`
   (id/dir/url/title/root_namespace), `CI_REGISTRY*`, `CI_SERVER_*`,
   `CI_API_V4_URL`, `CI_ENVIRONMENT_*`, `CI_RUNNER_*`, `CI_NODE_*`,
   `CI_CONCURRENT_*`, `CI_DEPLOY_FREEZE`, `CI_KUBERNETES_ACTIVE`,
   `GITLAB_USER_*`, `CI_EXTERNAL_PULL_REQUEST_*`, and the remaining
   `CI_MERGE_REQUEST_*` / `CI_COMMIT_*` names. Target ≈ 80 entries total.

Not the entire GitLab list. Any predefined name with no catalog entry keeps
today's honest generic treatment ("GitLab sets this at runtime") — the
report never invents a description. Catalog text contains no URLs: links are
dead weight in an offline report (and keep the no-network scan trivially
clean).

### 3. Surfaces — a combination, not a new top-level tab

**(a) Tooltips wherever a predefined name renders in the What-If tab.**
A shared helper `preVarHtml(name)` wraps the name in a span whose `data-tip`
is `summary` + " e.g. `example`." + `set_when` (compact: tooltips cap at
~280px wide and wrap). Wired into: `wiEnvTable` rows, `wiVarsLine` trace
lines, all three `wiUpdateUnknownVars` buckets, and workflow-trace vars.
Answers "what is this?" exactly where the name confronts the reader.

**(b) Variable Explorer detail enrichment.** In `showVarDetail`, when
`v.origin === 'predefined'` and a catalog entry exists, replace the generic
sentence with the full entry: summary, "e.g." example in a value block,
"GitLab sets it: …", "Unset: …" when present, and the gotcha note. The
table row's origin chip tooltip shows the per-variable summary instead of
the one-size-fits-all `ORIGIN_TIPS` line.

**(c) Reference section in the Variables tab.** A collapsed
`<details>` block below the variables table — "GitLab predefined variables
reference (N)" — listing the whole catalog with per-entry summary, example,
and when-set. Its own small filter input. Entries referenced by this
configuration sort first and carry a "used here" chip that jumps to the
variable's row/detail. Rendered only when the annotation is present, so Make
reports are pixel-identical.

Rejected alternatives:

- **Separate top-level tab.** Most discoverable as a document, but makes six
  tabs for GitLab, splits variable knowledge across two tabs, and is the
  most new UI for the least contextual payoff — readers meet unfamiliar
  names *inside* What-If traces and the Variables tab, so the docs should
  attach there. The collapsed reference section keeps the browsable-list
  benefit without the tab.
- **Tooltips only.** Cheapest, but nothing browsable, no room for the
  gotcha notes (which are the real value), and nothing in `model.json`.
- **JS data blob in the template** (like the dagre vendoring). Works, but
  the catalog would be invisible to pytest and the renderer would carry
  GitLab knowledge with no data driving it — the Python catalog matches the
  established "Python compiles, JS interprets" split and the "renderer
  consumes only the model" rule.

### 4. Honesty rules

- Docs must never contradict the simulator. For every name in `buildEnv` /
  `CONTROLLED`, the entry's set/unset text restates the fact the code
  implements (e.g. `CI_COMMIT_BRANCH`: "unset in merge request and tag
  pipelines"; `CI_OPEN_MERGE_REQUESTS`: "set in every branch pipeline whose
  branch is the source of an open MR — the key to the dedup patterns").
- Examples are labeled "e.g." everywhere they render; the "variables in
  effect" table keeps showing the *simulated* value, with the tooltip
  carrying the doc.
- Uncatalogued predefined names keep the existing generic wording — no
  entry, no claim.

## Error handling

Parser philosophy applies: the catalog is static data, so the only failure
modes are lookup misses — and a miss degrades to today's generic text at
every surface. No diagnostics, no new report states. Make reports: the
annotation is absent and every new code path is dormant.

## Testing

1. **Catalog schema (pytest):** every key matches `^(CI|GITLAB)_`; required
   fields present and non-empty; summaries single-sentence-ish (length cap);
   no `http` substring anywhere in the catalog.
2. **Simulator coverage (pytest):** regex-scan the shipped template sources
   (`whatif.js`, `report.html`) for `\b(?:CI|GITLAB)_[A-Z_]+\b`; every name
   found must have a catalog entry. This pins tier 1 and catches drift when
   a future simulator change introduces a new variable.
3. **Renderer (pytest):** a generated GitLab report contains the annotation
   and a known summary string; a generated Make report contains neither.
   The existing no-network scan covers the new content automatically.
4. **UI plumbing:** extend the existing fixture-driven renderer tests — a
   fixture whose rules reference a catalogued variable (e.g.
   `rules_manual`) renders the enriched detail text.

No new JS test runner work: the surfaces are rendering-only; the evaluator
is untouched.

## Build order

1. `gitlab_predefined.py` catalog + parser hook + tests 1–3.
2. Variables tab: detail-panel enrichment, row-chip tooltip, reference
   section (test 4).
3. What-If tooltips via the shared helper (env table, traces, buckets,
   workflow vars).
4. README (Variable Explorer + What-If sections) + CHANGELOG.

Each step lands green on its own; step 1 is pure Python and conflict-light
while other work is in flight on this branch.

## Out of scope

- Documenting *every* GitLab predefined variable (curated catalog with an
  honest fallback instead).
- Per-variable "since GitLab X.Y" availability notes.
- Linking to or fetching live GitLab docs (offline guarantee).
- Documenting project/group-level custom variables (unknowable offline).
- Changing what the simulator sets — this feature is documentation only.
