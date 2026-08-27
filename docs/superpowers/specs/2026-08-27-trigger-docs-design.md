# Trigger docs: automated per-scenario markdown pipeline docs — design

Date: 2026-08-27
Status: implemented (as-built notes at the end)

## Problem

Teams want internal markdown docs that demo specific pipeline flows per
trigger — which jobs run on a push to `main`, on a release tag, on the
nightly schedule — as plain sentences plus mermaid diagrams, readable where
devs already read docs: GitLab's file viewer (which renders mermaid
natively). There are many possible triggers, so the interesting set must be
defined once and applied to **every** project `pipeview gitlab sync`
tracks, without generating an HTML report and hand-transcribing it into
markdown per trigger per project.

Everything semantic already exists: `compile_whatif()` turns rules into an
evaluatable program in Python, and the What-If tab's JS interprets it. The
missing pieces are a Python interpreter of that same program and a markdown
renderer over its results.

## Decisions (the brainstorm, resolved)

### What a "trigger" is

A named **What-If scenario**: an event preset plus starting state — the
same config object the What-If tab builds (event source, ref, MR knobs,
changed files, simulated variables). Not just the event kind (too coarse:
"push" without a ref can't document the flows people ask about), and not
GitLab `trigger:` jobs (those appear *inside* docs, as boundary nodes).

### Where docs live, and who writes them there

Docs are for each project's own devs and belong in that project's repo
(e.g. `docs/ci/`). pipeview **stays read-only against GitLab**: it writes a
ready-to-commit folder beside each HTML report; a human or an external
script copies it into the repo and commits. Rejected: giving pipeview a
write-scoped token to push doc commits or MRs (a real trust expansion for a
tool whose pitch includes "read_api only"), and baking in per-project CI
jobs (still possible externally — generation is just a CLI — but not
pipeview's business).

### Depth: outcome + brief why

Each job gets its verdict plus **one literal line derived from the deciding
rule**, shown where it earns its place (manual gates, exclusions,
trigger-specific jobs): `` only when `CI_COMMIT_TAG` matches `^v` ``. The
tone is terse-but-literal — no phrase-book translating rules into friendly
prose (it would drift from truth). Rejected: full rule-trace parity
(hundreds of lines for real pipelines; the HTML report does "why didn't X
run" better interactively), outcome-only (loses the "why" that saves
opening the report), and collapsed `<details>` traces (file bloat; may
return later as an opt-in).

### Trigger jobs stop at the boundary

A `trigger:` job renders with its verdict, target and semantics — "spawns
downstream `group/other @ main` (strategy: depend; forwards `DEPLOY`)" —
and is never expanded, child pipelines included. Each project's docs stand
alone. Following the chain within the tracked set (rollup territory) was
considered and deferred.

### Flow: one shared scenarios file + a `--trigger-docs` flag (Flow A)

The user authors one YAML file of named scenarios, versioned wherever they
like, and passes it to the commands they already run. Alternatives:

- **(B) Author in the report** — an "Export scenario" button in the
  What-If tab serializing the current knobs to a YAML stanza, plus a
  "Copy as markdown" sibling for the current evaluation. Deferred to
  phase 2 as the *authoring aid*; generation stays CLI-only.
- **(C) Config-stored scenarios** (`pipeview gitlab scenario add …`,
  mirroring `track`) — rejected: scenarios become per-user invisible
  state, exactly the wrong shape for team docs, and flags are clumsy for
  variable maps and file lists.

No config-stored state in v1 at all; the flag is the whole interface.

### Authoring UX: helpers only — no TUI, no second binary

`pipeview scenarios init|check|preview` (below). A curses scenario editor
was rejected: the What-If tab is already the best interactive scenario
editor this codebase owns (every knob, live per-job feedback), and phase
2's export button makes it the authoring UI. A second console-script entry
point was rejected too: `scenarios` routes from `cli.py` exactly like
`gitlab` does, keeping one binary and one `--help`.

### Naming and idempotency

- The per-project index is **`pipeline-triggers.md`**, not `README.md` —
  it must not collide with or impersonate a project README once copied
  into a repo. Known trade-off: GitLab auto-renders only `README.md` in
  directory views; clarity wins (an `--index-name` flag can come later).
- **No generation timestamp anywhere.** Provenance is commit SHA +
  pipeview version + scenario hash (a short digest of the scenario's
  normalized definition, so an edited scenario is detectable), and
  regeneration with unchanged inputs is byte-identical — committed docs
  never show diff noise.

## CLI surface

New offline subcommand group (no network, usable before any GitLab setup):

```
pipeview scenarios init [PATH]        # commented starter file
                                      #   default ./pipeview-scenarios.yaml; refuses to overwrite
pipeview scenarios check PATH         # validate: YAML, schema, duplicate ids, unknown keys/events
                                      #   exit 0 clean / 1 warnings / 2 unusable
pipeview scenarios preview PATH REPO [--scenario ID]
                                      # render doc(s) for one local checkout to stdout
```

Generation is a flag on the existing commands:

```
pipeview <path> --trigger-docs scenarios.yaml -o out/
pipeview gitlab sync    -o reports/ --trigger-docs scenarios.yaml
pipeview gitlab report group/app --trigger-docs scenarios.yaml
```

Output layout follows the existing slug naming (`report_slug()` in
`gitlab/report.py`):

```
# sync / gitlab report:            # local runs:
<outdir>/<slug>.trigger-docs/      <outdir>/<root-slug>.trigger-docs/
  pipeline-triggers.md               (e.g. gitlab-ci.trigger-docs/)
  push-main.md
  release-tag.md
```

Exit codes keep pipeview's convention: docs diagnostics contribute to the
0/1 verdict; docs failing never blocks report generation (exit 2 remains
"no report produced").

## The scenarios file

One file, applied to every project in the run. Scenario keys are the
What-If tab's config object spelled in snake_case — a deliberate 1:1 so
the mental model is shared and phase 2's export button is a trivial
serializer. Event ids are the JS scenario ids verbatim.

```yaml
version: 1                      # required; this schema is version 1
scenarios:
  - id: push-main               # required, unique, [a-z0-9-]+ → filename push-main.md
    title: Push to main         # optional; defaults from id
    intro: |                    # optional prose, copied verbatim under the title
      What runs on every merge to main.
    event: push_branch          # required: push_branch | push_tag | mr |
                                #   schedule | web | api | trigger
    branch: main                # branch-ref events; default: the simulated default branch
    variables: { DEPLOY: "1" }  # simulated project-level variables
    changed_files: [src/a.py]   # optional; omitted = unknown → honest "depends"
    diagrams: [dag]             # optional: dag (default) · lifecycle
  - id: release-tag
    event: push_tag
    tag: v1.2.3                 # an example ref; each project's rules decide what matches
  - id: mr-to-main
    event: mr
    target: main                # MR knobs: target, draft, mr_flavor
    draft: false                #   (detached | merged_result | merge_train), mr_labels
  - id: nightly
    event: schedule
    ref_kind: branch            # schedule/web/api/trigger run on branch or tag
```

Remaining knobs (`open_mr` + `target`/`draft` on `push_branch`,
`new_branch`, `tag_protected`, `mr_labels`) mirror the tab the same way;
the loader validates the exact key set per event and rejects unknowns.

`check` warnings include: a `push_tag` scenario with a branch-looking ref,
variables shadowing GitLab predefined names, an event key that doesn't
apply to the chosen event (e.g. `target` on a `schedule`).

## Generated docs

### Per-scenario doc anatomy (`push-main.md`)

````markdown
# Push to main

<!-- pipeview-trigger-doc: project=group/app ref=main commit=abc1234
     scenario=push-main scenario-hash=9f2e pipeview=0.9.0 -->
*Generated by pipeview from `group/app @ main` (abc1234) — do not edit by hand.*

What runs on every merge to main.

**Scenario:** push to branch `main` · variables: `DEPLOY=1` · changed files: not specified

## Outcome

One pipeline, 14 jobs: **11 run**, 1 manual gate, 1 delayed, 1 depends.

## Branch pipeline (`main`)

```mermaid
flowchart LR
  …stage subgraphs, needs edges, verdict shapes…
```

| Job | Stage | Verdict | Why |
|---|---|---|---|
| compile | build | runs | — |
| deploy_prod | deploy | **manual gate** | `CI_COMMIT_TAG` unset → tag rule skipped; manual on protected branch |
| e2e | deploy | *depends* | `rules:changes` — no changed-files list in this scenario |
| notify | deploy | ▶ trigger | spawns downstream `group/other @ main` (strategy: depend; forwards `DEPLOY`) |

<details><summary>Jobs not in this pipeline (3)</summary> …job + one-line reason… </details>
````

- The scenario line makes each doc self-describing — no reader needs the
  scenarios file.
- Job tables are stage-ordered (the listing convention the What-If text
  summary already established).
- Multi-pipeline scenarios (e.g. `push_branch` with `open_mr`) get a
  fan-out mini-diagram at the top — event → one node per candidate
  pipeline — plus a duplicate-jobs line, then one `##` section per
  candidate pipeline. Not-created pipelines appear with their reason.
- A `needs:` on a job missing from the pipeline renders as a ⚠ blockquote
  naming it a pipeline-creation failure.
- The optional `lifecycle` diagram is a `sequenceDiagram` (developer →
  GitLab → stages → human approval → downstream boundary), emitted only
  when a scenario asks for it — for all-automatic pipelines it adds
  nothing over the flowchart.

### Mermaid conventions

- **Shape and label encode verdicts, never color alone** — GitLab renders
  mermaid in its own light/dark themes, so hard-coded fills go muddy.
  Manual gate = hexagon + "(manual)", delayed = "⏱ <duration>", depends =
  dashed border + "?", trigger = rounded "▶ spawns …" terminal node. A
  one-line legend sits under each graph.
- **Only real `needs:` edges are drawn.** Jobs without `needs:` wait on
  the previous stage; the left→right stage subgraphs carry that meaning
  and the legend says so. Drawing every implicit barrier edge is noise.
- **Not-added jobs never appear in graphs** — they live in the collapsed
  `<details>` table with one-line reasons.
- **Size guardrail:** past a constant number of in-pipeline jobs
  (initially 60) the flowchart degrades to one summary node per stage
  ("test — 14 jobs") with a note; the job table stays complete. GitLab's
  renderer chokes on huge graphs.

### Index (`pipeline-triggers.md`)

One table — scenario · event · pipelines · jobs run · gates — each row
linking to its doc, with the same provenance header, the regeneration
command spelled out, and a row for any skipped-invalid scenario naming its
error (absence is never silent).

### Honesty rules

Carried over from What-If verbatim: *depends* is never resolved
optimistically and its "Why" cell names the missing information; the
simulated protected-refs world (`main`, `dev`) is stated in the index;
nothing is guessed. Docs contain no `http(s)://` references — downstream
projects are named as `group/other @ ref` text, not links — so the
offline guarantee extends to them.

### Regeneration safety

Before writing, pipeview deletes only `.md` files carrying its own
`pipeview-trigger-doc` provenance marker in the target folder — a removed
scenario cannot leave a zombie doc, and a stray human-authored file is
warned about, never deleted.

## Architecture

Four pieces, riding the existing model/render split:

1. **`pipeview/scenarios.py`** — scenario file loader. YAML → typed
   `Scenario` records + diagnostics. Pure: no network, no model
   dependency. Schema validation and the `check` warnings live here, so
   the helper CLI and the generation path cannot disagree.
2. **`pipeview/parsers/gitlab_whatif_eval.py`** — the Python tri-state
   evaluator, the one genuinely new engine piece. Consumes the compiled
   program `compile_whatif()` already embeds in every GitLab model:
   candidate construction (`buildCandidates` semantics), per-candidate
   `CI_*` env synthesis, tri-state rule interpretation
   (true/false/unknown), workflow-rules pipeline gating, verdicts,
   duplicate detection, dotenv/`needs:` checks — a literal port of
   `whatif.js`'s interpreter, sitting next to the compiler it interprets.
   Returns, per candidate pipeline and job: the verdict **plus the
   deciding rule** (index, source text, and for *depends* the missing
   fact) — the raw material of the "Why" column. Guards on
   `WHATIF_VERSION`.
3. **`pipeview/render/trigger_docs.py`** — markdown renderer. Evaluation
   results + model + provenance → the `.md` texts. Shares mermaid
   escaping helpers with `exports.py` (factored out, not duplicated).
   Pure string-building; callers do file I/O. Provenance is passed in as
   data (tests inject fixed values).
4. **CLI wiring** — `scenarios` routes from `cli.py` like `gitlab` does;
   `--trigger-docs` added to the local CLI and to `gitlab sync`/`report`,
   chaining loader → evaluator → renderer per project after the model is
   built.

```
scenarios.yaml ─→ loader ─→ [Scenario]
project files ─→ existing parsers ─→ model (whatif program inside)
(Scenario × model) ─→ evaluator ─→ verdicts + deciding rules
                  ─→ renderer ─→ *.trigger-docs/*.md
```

**JS/Python lockstep** is the main long-term risk. The plan is the trick
the compiler already uses: semantics are pinned by pytest golden fixtures
— a table of (compiled program, scenario) → expected verdicts — and the
JS stays a dumb interpreter of the same program. Rule-semantics work
happens in the compiler, where both interpreters inherit it; the fixture
table is the parity contract both answer to, making evaluator-only drift
loud. Both interpreters' file headers point at the table and at each
other.

Explicitly **not** in v1: HTML/report changes (phase 2), config-stored
state, network changes. Make projects get an info diagnostic ("trigger
docs apply to GitLab CI configurations"), never an error; in mixed root
directories docs are generated for the GitLab roots only.

## Error handling

One bad input degrades one output, never the run:

- One invalid scenario → skipped with a named diagnostic; the rest render;
  the index lists the skipped scenario with its error.
- Scenario file unusable (unreadable, YAML parse failure) → docs
  generation fails with an error diagnostic; the HTML report still
  generates; exit 1.
- Opaque expressions already degrade at compile time; the evaluator maps
  them to *depends* — never a crash, never a guess.
- `WHATIF_VERSION` mismatch → that project's docs fail loudly with a clear
  message; nothing else affected.
- In `sync`, docs diagnostics join each entry's existing stderr printout;
  one project's docs failure doesn't touch its neighbors.

## Testing

- **Evaluator semantics** — table-driven fixtures: (config, scenario) →
  expected per-job verdicts *and deciding rule*. Coverage mirrors the
  What-If tab: tag vs branch push, MR flavors and drafts, schedules, the
  protected-refs world, variable precedence, unknown propagation,
  duplicates across candidates, `needs:` on missing jobs, `workflow:rules`
  suppressing a pipeline. This table doubles as the JS-parity contract.
- **Golden docs** — generation over `examples/gitlab-whatif-project` with
  a committed scenarios fixture, compared byte-for-byte against committed
  expected markdown (fixed provenance injected).
- **Determinism** — regenerating unchanged inputs is byte-identical
  (sorted iteration everywhere; no timestamps), asserted by running
  generation twice.
- **Hostile inputs** — job names with pipes, quotes, brackets, emoji must
  survive markdown tables and mermaid; the >60-job guardrail collapse is
  pinned.
- **CLI** — `init` refuses overwrite; `check` exit codes 0/1/2 against
  good/warned/broken fixtures; `preview` output.
- **Offline guarantee** — the existing no-`http(s)://` scan extends to
  generated `.md`.

## Out of scope (phase 2 candidates, in rough priority order)

1. What-If tab **"Export scenario"** button (serialize current knobs to a
   YAML stanza) and **"Copy as markdown"** button (render the current
   evaluation — or pinned delta — through the same markdown conventions).
2. `--check` staleness mode: regenerate, compare against a repo's
   committed docs via the provenance marker, exit non-zero on drift — a
   scheduled CI job can then police freshness with pipeview still
   read-only.
3. Per-scenario `projects:` include/exclude filters, for triggers that
   only make sense in some repos.
4. Following trigger chains within the tracked set (rollup integration);
   collapsed `<details>` full traces; `--index-name`.

## As-built notes

Implemented as specced, with these refinements found during the build:

- **Parity got a second lock.** Beyond the shared vector table
  (`tests/whatif_vectors.json`, now also run natively under pytest),
  `tests/test_whatif_parity.py` sweeps every gitlab fixture and both
  example projects across a 14-config matrix and requires the two
  interpreters' full JSON outputs to be deep-equal (434 cases at
  landing).
- **Mermaid ids are sanitized and uniquified** with `j_`/`s_`/`b_`
  prefixes so job names like `end`, emoji, pipes, quotes and brackets
  cannot break graphs or tables (pinned by the `hostile_names` fixture).
- **Boundary text names the target but no strategy** — the compiler does
  not capture `trigger:strategy`, so the docs do not claim one (the
  spec's example sketch showed "strategy: depend"; the honest form
  shipped instead).
- **Provenance `commit` is empty for now**: the fetch layer records the
  ref, not a resolved SHA, and local runs do not ask git. The marker
  field stays, for a later resolver (and `--check`).
- **`scenarios preview` exits on the scenarios file's health only**; the
  repo's own diagnostics are noted on stderr but belong to report
  generation.
- **Name collisions**: a hand-written file in the docs folder wins — the
  generated file is skipped with a warning ("never deleted" refined to
  "never deleted or overwritten").
- **`commit_message` joined the schema** (rules on `CI_COMMIT_MESSAGE`,
  "[skip ci]"-style, are common enough to belong in v1).
