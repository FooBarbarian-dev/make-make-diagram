# GitLab CI "What-If" pipeline simulation — design

Date: 2026-08-18
Status: approved (brainstorm complete)

## Problem

GitLab makes it hard to see what would actually execute for a given event. A
single push to a branch with an open merge request makes GitLab evaluate **two
candidate pipelines** (a branch pipeline and a merge request pipeline); rules
mismatches silently drop jobs, duplicate them across both pipelines, or fail
pipeline creation entirely ("job needs X which is not in the pipeline"). None
of this is visible by reading the YAML.

pipeview already parses GitLab CI YAML into an offline HTML report. This
feature adds a **What-If tab**: a scratch-like starting state (event, ref, MR
state, changed files, variables) on the left, and the resulting pipeline
graph(s) on the right — including the duplicate branch+MR pair, child
pipelines, and artifact/dotenv flow annotations.

The goal is *"what is probably happening"* with every uncertainty labeled —
not perfect emulation.

## Decisions made during brainstorming

1. **Presets + custom overrides.** Built-in event scenarios plus live
   variable overrides in the report; re-evaluation happens in the browser.
2. **New What-If tab** alongside Graph/Tasks/Variables/Files. The existing
   Graph tab is untouched.
3. **In-report variable entry only.** No new CLI flags. User-set
   (project/group) variables are added in the report's panel; they sit at
   project-variable precedence (above all YAML-defined values).
4. **Architecture: Python compiles, JS interprets.** All GitLab semantics
   (expression parsing, only/except normalization, exists evaluation,
   scenario catalog) are compiled in Python where pytest can pin them.
   The report ships one small tri-state interpreter in JS. No dual
   evaluator.
5. **Simplified ref world.** Two protected long-lived branches `main`
   (default) and `dev`; one generic unprotected feature branch (name
   editable, default `feature/widget`); one tag (name editable). The
   feature branch carries the MR knobs (open MR?, target, draft) and the
   changed-files list.
6. **Sectioned pipeline graphs.** Each candidate pipeline renders as its own
   labeled dagre graph section; skipped jobs are ghosted with a "why";
   duplicates are cross-highlighted with a summary banner; child pipelines
   nest under their trigger job.
7. **Artifact & dotenv flow annotations** per scenario: broken
   `needs`/`dependencies` consumption (pipeline-creation failure), dotenv
   env propagation badges, and cross-pipeline artifact ambiguity when
   duplicates exist.

## Event model

The unit of simulation is an **event** that expands into one or more
**candidate pipelines** (this mirrors GitLab: both push pipelines and merge
request pipelines can be triggered by the same push event).

| Preset scenario | Candidate pipelines | Knobs |
|---|---|---|
| Push to branch | branch (`push`); + MR (`merge_request_event`) when an MR is open | branch (main/dev/feature), open MR, target, draft, changed files, commit message |
| Push new tag | tag (`push`) | tag name, protected |
| MR created / "Run pipeline" on MR | MR only | target, draft, changed files |
| Scheduled run | branch or tag (`schedule`) | target ref |
| Manual web run | branch (`web`) | ref |
| API / trigger token | branch (`api` / `trigger`) | ref |

A candidate pipeline **exists** iff `workflow:rules` allows it AND at least
one job survives its rules ("a pipeline does not run if no jobs are added").
Jobs with `trigger:include:local` that run spawn a nested child-pipeline
candidate evaluated with `CI_PIPELINE_SOURCE=parent_pipeline` (so
`merge_request_event` rules never match there — a documented gotcha the
simulation surfaces). Multi-project triggers stay unevaluated ghosts.

**Duplicates**: a job included in ≥2 top-level candidates of one event.

### Variable environments (doc-verified facts the simulator models)

- `CI_PIPELINE_SOURCE == "push"` for both branch and tag pipelines;
  distinguished by `CI_COMMIT_BRANCH` vs `CI_COMMIT_TAG` being set/unset.
- `CI_COMMIT_BRANCH` is **unset** in MR pipelines and tag pipelines. Unset ≠
  empty string: `$VAR == null` is true only for unset; bare `$VAR` is false
  for unset *and* empty.
- In MR pipelines `CI_COMMIT_REF_NAME` is the **source branch name**; the
  git ref path is exposed as `CI_MERGE_REQUEST_REF_PATH`.
- `CI_OPEN_MERGE_REQUESTS` is set in branch pipelines too when an open MR
  uses that branch as source — the key to the documented dedup patterns.
- Scheduled pipelines on a branch set `CI_COMMIT_BRANCH`; `rules:changes`
  evaluates **true** in pipelines with no push event (tag, schedule, manual,
  api/trigger, new-branch pushes).
- The strings `"false"` and `"0"` are truthy; only unset/empty are falsy.
- Env layering (low→high): predefined event matrix → global YAML
  `variables` → `workflow:rules:variables` → job `variables` →
  `rules:variables` → user overrides (project-variable level).

## Components

### 1. Compiler — `pipeview/parsers/gitlab_whatif.py` (Python)

Runs at the end of `parse_gitlab`. Produces:

- **Expression ASTs** from every `rules:if` / `workflow:rules:if` /
  `only:variables` string. Grammar: `==`, `!=`, `=~`, `!~`, `&&`, `||`, `!`,
  parentheses, `null`, single/double-quoted strings, `/regex/` literals with
  flags, `$VAR` references, bare-variable truthiness. Unparseable →
  `opaque` node (evaluates *unknown*) + warning diagnostic. One bad rule
  degrades one rule, never the report.
- **Legacy `only`/`except`** normalized into the same program shape,
  including the implicit default `only: [branches, tags]` for jobs with no
  `rules`/`only`/`except` (the #1 duplicate-pipeline cause).
- **`rules:exists`** evaluated now against the repo file list, baked in as a
  constant (`exists:project` → unknown).
- **`rules:changes`** kept as pattern lists for the JS evaluator to match
  against the scenario's changed-files input (tri-state when no list given).
- **Artifact info** per job: produces (`artifacts:paths`,
  `artifacts:reports:dotenv`) and consumption inputs (`needs` entries with
  optional/artifacts flags, `dependencies`, or stage-default).
- **Static lint**: GitLab's own "job may allow multiple pipelines to run for
  a single action" warning when a job's final rule is a bare `when:` with no
  condition.

Output locations: per-job program in `node.annotations["whatif"]`; scenario
catalog + workflow program + globals + ref world in
`report.annotations["whatif"]`. `SCHEMA_VERSION` bumps to 3. Make reports
lack the annotation → tab absent; the model stays format-neutral.

### 2. Evaluator — `pipeview/render/templates/whatif.js` (JS, DOM-free)

Inlined into the report at generation (like dagre.min.js — offline guarantee
intact). Exposes `PipeviewWhatIf.evaluateEvent(report, config)`:

- Tri-state logic (true/false/unknown) throughout; unknown propagates
  (`U && F = F`, `U || T = T`).
- First-match-wins rule walking; `workflow:rules` gates candidates first;
  `when: never/manual/delayed/on_failure/always`, `allow_failure`,
  `rules:variables` honored. An unknown condition forks the trace: job state
  becomes **conditional** with both outcomes and the condition it hinges on.
- Job states: `runs` / `manual` (blocking or not) / `delayed` / `skipped`
  (matched a `when: never`) / `not-added` (no rule matched) / `conditional`
  — each with a rule-by-rule trace.
- Regex via JS `RegExp`; non-RE2 constructs (lookaround/backrefs) get a
  compile-time warning that real GitLab would reject them; non-`/…/`
  right-hand sides fall back to the documented substring check with an
  "undocumented behavior" note.
- Glob matching for `changes:` mirrors fnmatch with
  PATHNAME|DOTMATCH|EXTGLOB (approximation, documented).
- Artifact analysis per candidate: broken needs/dependencies → red
  pipeline-creation-failure banner; dotenv producer→consumer badges;
  cross-pipeline artifact ambiguity note when duplicates exist.

DOM-free so plain `node` can run it in tests.

### 3. UI — What-If tab in `report.html`

Two-panel layout inside the tab:

- **Left — the scratchpad**: scenario preset picker; ref picker
  (main/dev/feature/tag per scenario) with editable feature/tag name; MR
  knobs (open MR, target main|dev, draft, MR flavor
  detached/merged-results/merge-train); commit message; changed-files
  textarea with *match all / match none / unknown* shortcuts; variables
  panel (add/override rows + auto-listed "referenced in rules but defined
  nowhere" variables with one-click define).
- **Right — results**: an event-level banner strip (duplicates count,
  cross-pipeline artifact notes), then one bounded graph section per
  candidate pipeline (dagre per section, `needs` edges, stage-ordered),
  child pipelines nested under their trigger job. Node styling by state;
  ghosted skipped jobs (toggle to hide). Clicking a job opens the detail
  panel with the what-if trace (rule-by-rule, with the matched rule's
  source line) above the normal job detail.
- Every knob change re-evaluates synchronously (models are small).

### 4. No CLI changes

`model.json` carries the what-if data automatically. dot/svg/mmd exports
unchanged.

## Error handling

Parser philosophy applies: degrade the one value, never the report.

- Unparseable `if` → opaque/unknown + warning diagnostic (generation time).
- Unknown-to-simulation constructs (`exists:project`,
  `changes:compare_to`, `!reference` to unresolved includes) → unknown with
  an explanatory note in the trace.
- Variables referenced in rules but never defined anywhere → treated as
  unset (GitLab's real behavior) and listed in the scratchpad for
  one-click definition.
- Jobs/rules living in unresolved remote includes are absent from the
  simulation; the tab shows the existing unresolved-include warning so the
  absence is explained.

## Testing

1. **Compiler (pytest)**: expression parser table tests (every operator,
   precedence, quotes, regex flags, null vs empty, truthiness of
   `"false"`/`"0"`, parse errors → opaque); only/except normalization incl.
   implicit defaults; exists baking against fixture repos; artifact
   extraction; lint warnings.
2. **Vector suite** (`tests/whatif_vectors.json`): hand-written
   expectations straight from the GitLab docs. Two kinds: expression
   vectors (source + env → verdict; pytest compiles source → AST via the
   Python parser, node evaluates) and scenario vectors (fixture YAML +
   scenario config → expected per-candidate, per-job states). The
   documented duplicate-causing configs and the three documented
   `workflow:rules` dedup patterns are fixtures: duplicates must appear,
   then disappear.
3. **JS evaluator**: `tests/run_whatif_vectors.js` runs the same vectors
   through the shipped `whatif.js` under `node`; the pytest wrapper skips
   with a notice when node is absent.
4. **Renderer**: what-if data present in generated GitLab reports, absent
   for Make; whatif.js inlined; the existing no-network scan covers the new
   code automatically.

## Build order

1. Compiler + parser hooks + schema bump + compiler tests.
2. `whatif.js` + vector suite + node test runner + `html.py` inlining.
3. What-If tab UI (scratchpad, sectioned graphs, traces, banners).
4. Artifact/dotenv annotations wired through UI.
5. New example project demonstrating duplicate pipelines + dedup +
   dotenv + child pipeline; README + CHANGELOG.

## As-built notes (implementation deltas)

- The scenario catalog and the predefined-variable matrix live as code in
  `whatif.js` (`buildCandidates` / `buildEnv`) rather than as data in the
  report annotation — writing the matrix as a data DSL added complexity
  without adding testability, since the node vector suite exercises the JS
  directly. The report annotation carries what genuinely varies per
  repository: workflow program, per-job programs, globals, stage order,
  ref world, lint.
- The changed-files knob gained "assume every pattern matches" /
  "assume nothing matches" shortcut modes in addition to the exact-paths
  list and *unknown*.
- Mini-graph edges draw dependency → dependent (execution flow, stages
  left-to-right), the reverse of the model's job → dependency direction
  used by the main Graph tab.

## Out of scope

- Fetching remote/project/template/component includes (offline guarantee).
- Simulating dotenv file contents or artifact effects on scripts (flow and
  existence only).
- Merge trains queue mechanics beyond `CI_MERGE_REQUEST_EVENT_TYPE=merge_train`.
- Project/group settings pipeview cannot know (merged-results enablement is
  a scratchpad knob, not detected).
- A CLI flag to bake variable values into reports (natural follow-up; the
  override mechanism already supports it schema-wise).
