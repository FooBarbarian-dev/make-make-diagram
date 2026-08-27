# Trigger docs — implementation plan

Date: 2026-08-27
Spec: `docs/superpowers/specs/2026-08-27-trigger-docs-design.md`
Status: complete — all five milestones landed (see the spec's as-built
notes for refinements found during the build)

Working rules for every milestone: write the failing test first; keep the
suite and `ruff check .` green at every commit; no network in any new code
path; match the surrounding comment style. Each milestone is independently
landable.

## Milestone 1 — Scenario loader (`pipeview/scenarios.py`)

1.1 `Scenario` dataclass + `load_scenarios(path) -> (list[Scenario], list[Diagnostic])`.
    - Event ids: `push_branch | push_tag | mr | schedule | web | api | trigger`
      (the `whatif.js` scenario ids verbatim).
    - Per-event allowed-key table (snake_case spellings of the JS config
      knobs: `branch`, `tag`, `ref_kind`, `open_mr` {`target`, `draft`},
      `target`, `draft`, `mr_flavor`, `mr_labels`, `tag_protected`,
      `new_branch`, `changed_files`, `variables`, `diagrams`); unknown keys
      and inapplicable keys rejected with named diagnostics.
    - `id` validated `[a-z0-9-]+`, unique; `version: 1` required.
    - `scenario_hash`: short digest of the normalized stanza (sorted-key
      JSON), stored on the record for provenance.
    - Failure shape: one bad scenario → one diagnostic + that scenario
      skipped; file-level failure → error diagnostic, empty list.
1.2 `check`-level warnings: `push_tag` with a branch-looking ref, variables
    shadowing predefined names (reuse the `gitlab_predefined` catalog),
    `diagrams` entries outside `dag | lifecycle`.

Tests: `tests/test_scenarios.py` — a good file parses to typed records
with hashes; each malformed case produces its diagnostic and skips only
itself; warning cases. Verify: `pytest tests/test_scenarios.py && ruff check .`

## Milestone 2 — Python tri-state evaluator (`pipeview/parsers/gitlab_whatif_eval.py`)

The port of `whatif.js`'s interpreter over the compiled program. Split:

2.1 Expression interpreter: tri-state eval of the compiled AST against an
    env (`==`, `!=`, `=~`, `!~`, truthiness, and/or/not, unknown
    propagation, opaque → unknown).
    Test: run the `expr` section of `tests/whatif_vectors.json` (37
    vectors) natively in pytest — no node required.
2.2 Candidate construction + env synthesis: port `buildCandidates` and the
    per-candidate `CI_*` env (branch/tag refs, MR flavors and draft,
    `new_branch`, the protected-refs world, no-push-event sources).
2.3 `workflow:rules` pipeline gating: port `walk()` including the
    conditional-creation collapse ("created whichever way the unknown rule
    goes") and uncertain-variable carry.
2.4 Per-job verdicts: rule walk → runs / manual (blocking|optional) /
    delayed / not added / depends, capturing **the deciding rule** (index,
    source text, and for *depends* the missing fact). Matrix `×N` counts.
2.5 Cross-candidate duplicates; `needs:` on missing jobs
    (pipeline-creation failure); dotenv propagation notes — the
    `analyzeArtifacts` subset the docs consume.
2.6 `WHATIF_VERSION` guard; header comments in both `whatif.js` and this
    module naming `whatif_vectors.json` as the shared parity contract and
    pointing at each other.

Tests: `tests/test_whatif_eval_py.py` — runs **both** sections of
`whatif_vectors.json` (the 41 `scenarios` vectors' `config` objects are
already the What-If config verbatim) natively under pytest, plus new
table-driven cases for deciding-rule capture. New semantics vectors are
added to the shared JSON so node pins the JS side identically (the
existing `tests/test_whatif_evaluator.py` harness picks them up unchanged).
Verify: `pytest tests/test_whatif_eval_py.py tests/test_whatif_evaluator.py`

## Milestone 3 — Markdown renderer (`pipeview/render/trigger_docs.py`)

3.1 Factor `_escape_mmd` / `_mmd_id` out of `render/exports.py` into a
    shared helper module; `exports.py` re-imports. No behavior change —
    `tests/test_exports.py` stays green untouched.
3.2 Per-scenario doc: provenance comment + visible line (provenance passed
    in as data; **no timestamps**), scenario line, outcome summary,
    per-candidate `flowchart LR` (stage subgraphs, `needs:` edges only,
    shape-not-color verdict conventions, legend line, stage-collapse
    guardrail constant `= 60`), stage-ordered job table with the Why
    column, not-added `<details>` block, ⚠ blockquotes, fan-out diagram
    when >1 candidate, opt-in `lifecycle` sequence diagram.
3.3 Index `pipeline-triggers.md`: summary table, provenance, regeneration
    command, skipped-scenario rows.
3.4 Folder writer: creates `*.trigger-docs/`, deletes only `.md` files
    bearing the `pipeview-trigger-doc` marker, warns on strangers.

Tests: `tests/test_trigger_docs.py` — golden docs over
`examples/gitlab-whatif-project` with a committed scenarios fixture and
fixed provenance, compared byte-for-byte; run-twice determinism; hostile
job names (pipes, quotes, brackets, emoji) through tables and mermaid;
guardrail collapse; marker-based deletion (zombie removed, stranger
survives with warning). Extend the offline scan in
`tests/test_html_renderer.py` to generated `.md`.

## Milestone 4 — CLI wiring

4.1 `pipeview scenarios` sub-CLI (`pipeview/scenarios_cli.py`), routed
    from `cli.py` exactly like `gitlab`: `init` (commented starter
    template, refuses overwrite), `check` (exit 0/1/2), `preview PATH REPO
    [--scenario ID]` to stdout.
4.2 `--trigger-docs FILE` on the local CLI: after model build, GitLab
    roots get `<outdir>/<root-slug>.trigger-docs/`; Make roots get the
    info diagnostic; docs diagnostics join the exit-code verdict without
    ever blocking report generation.
4.3 `--trigger-docs FILE` on `pipeview gitlab report` and `sync`:
    `<outdir>/<slug>.trigger-docs/` per entry (slug from `report_slug()`);
    diagnostics join each entry's stderr printout; one entry's docs
    failure stays its own.

Tests: extend `tests/test_cli.py` (init/check/preview exit codes and
output; local `--trigger-docs` end-to-end tree); sync/report wiring tested
the way `tests/test_gitlab_remote.py` already fakes the API. Smoke:
`pipeview examples/gitlab-whatif-project --trigger-docs <fixture> -o /tmp/td`

## Milestone 5 — Docs and finish

5.1 README: a "Trigger docs" section (scenarios file, commands, the
    read-only handoff story) + CLI reference rows; CHANGELOG entry.
5.2 Flip the spec's Status to implemented, with as-built notes for any
    drift (the existing specs' convention).
5.3 Full `pytest`, `ruff check .`, `make examples` still clean.

## Risks to watch

- **JS/Python drift** is the standing risk; the shared vector file is the
  mitigation — semantics changes must land as vectors first.
- **RE2 vs Python `re`**: GitLab rejects lookaround/backreferences (the
  compiler already flags non-RE2 patterns); the Python evaluator must
  treat flagged patterns the way the JS does (evaluate, badge the caveat)
  rather than diverging.
- **Renderer size creep**: `render/trigger_docs.py` should stay a
  string-builder; anything semantic belongs in the evaluator where
  vectors pin it.
