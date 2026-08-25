# Cross-file job merging to GitLab's semantics — design

Date: 2026-08-25
Status: implemented.

## Problem

A `.gitlab-ci.yml` that customizes a job an included file (typically a
GitLab template) defines — the standard Auto DevOps pattern:

```yaml
include:
  - template: Jobs/Build.gitlab-ci.yml
build:
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
```

came out wrong: the local `build:` **replaced** the template's whole
definition, so the job lost its script and the report emitted
"has no script, run, or trigger — GitLab rejects jobs with nothing to
execute" for pipelines that run fine. Reported by a user seeing exactly
these warnings on jobs that execute.

## Research: what GitLab actually does

Verified against GitLab source at v19.3.0 (the tag the bundled template
snapshot is pinned to), not just docs:

- **Include merging** — `Gitlab::Ci::Config::External::Processor#perform`:
  starts from `{}`, `deep_merge!`s each external file's hash **in include
  order** (so a later include beats an earlier one), then deep-merges the
  main file's own values on top (`append_inline_content!`). The position
  of the `include:` key inside the file is irrelevant. Each external
  file's hash is itself fully expanded first
  (`External::File::Base#to_hash` → `expanded_content_hash`), so nested
  includes resolve depth-first: a file's own content beats what it
  includes. Net: the merged config is a **post-order deep merge**, main
  file last.
- **deep_merge semantics** (ActiveSupport): when a key exists on both
  sides, two hashes merge recursively; anything else — arrays (`script:`,
  `rules:`), scalars, nil — is **replaced whole** by the winning side.
- **`extends`** — `Gitlab::Ci::Config::Extendable::Entry#extend!`: runs
  **after** include merging on the merged job table; parents are
  recursively flattened, merged left-to-right (later parents override
  earlier), then the child deep-merges on top. Our flattener already did
  exactly this; the bug was upstream of it — it operated on a job table
  where cross-file definitions had clobbered each other.

## Decisions

### 1. Deep-merge same-name jobs at registration

`_parse_file_inner` used to do `state.job_configs[job_id] = config`
(whole-object replacement). It now deep-merges the new definition onto
the existing one with the same `_deep_merge` the extends flattener uses
(hashes per key, arrays/scalars replace). Because files are processed in
GitLab's merge order (below), "later registration wins per key" is
exactly GitLab's precedence. The `##` docstring survives from the earlier
definition when the override has none.

Provenance is kept, not just merged away: `state.job_merged_sources`
records every definition site in merge order, surfaced as an info
diagnostic ("Job 'build' is also defined in [template]
Jobs/Build.gitlab-ci.yml:4 — GitLab deep-merges the two, this definition
taking precedence …") and as a `merged_from` node annotation for the
report/JSON.

### 2. Process files in GitLab's merge order

`include:` handling moved to the top of `_parse_file_inner`, before the
file's own keys — the include key's position in the file no longer
affects precedence, matching the Processor. With post-order processing in
place, "later file wins" holds for the top-level keys too, so their
ad-hoc first-wins guards became wrong and were replaced:

- `stages:` — always replaced by a later main-scope file (arrays replace
  whole; root processed last so the root still wins; among includes, the
  last one with `stages:` wins). Child-pipeline files no longer race for
  the parent's stage list (`not namespace` guard).
- `default:` and the deprecated top-level defaults — accumulate via
  `_deep_merge` per key (an include's `image:` and the root's `tags:`
  both apply; per-key conflicts go to the later file). Deprecation
  diagnostic still fires once per key.
- `workflow:` — accumulated per key into `state.workflow_merged` and
  processed once after parsing: an include can contribute `rules:` while
  the root contributes only `name:`. The old code cleared include rules
  whenever a later file mentioned `workflow:` at all. Child-pipeline
  workflow handling (entry file wins) is unchanged. The
  `workflow_root`/`workflow_root_has_rules` flags died with the old
  incremental logic.

`_collect_globals` in the what-if compiler already implemented root-wins
explicitly per event, so global `variables:` precedence was and stays
correct; only event display order changed (includes now listed before the
root's overriding event, which reads as the actual precedence).

### 3. What did NOT change

The extends flattener (already GitLab-exact: later parents override
earlier, child on top, `_deep_merge` leaves), `!reference` resolution,
within-file duplicate keys (YAML last-wins plus the existing warning),
and child-pipeline namespacing.

## Testing

`TestCrossFileJobMerge` (parser): rules-only local override keeps the
included script and produces no "has no script" warning; variables hashes
merge key by key while script/rules/stage replace whole; later include
beats earlier; `include:` at the bottom of the file changes nothing;
stages/default/workflow per-key precedence; `extends: [a, b]` order; and
extends resolving against the *merged* table (root overriding a hidden
job an include defines). The user-reported scenario is covered end to end
twice more: offline against the real bundled `Jobs/Build` template
(`test_gitlab_templates.py`) and through the remote files-strategy fetch
(`test_gitlab_remote.py`).
