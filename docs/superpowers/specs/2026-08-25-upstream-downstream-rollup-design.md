# Upstream/downstream pipeline links + tracked-set rollup — design

Date: 2026-08-25
Status: approved, in implementation.

## Problem

A GitLab CI graph stops at the project boundary. `trigger:` jobs that
start pipelines in *other* projects come out as dead-end ghost nodes
(`downstream:group/app`), `needs:project` artifact edges come out as
annotated ghosts, and nothing shows the reverse direction — which tracked
projects trigger *this* one. Users who maintain a constellation of
projects wired together with trigger jobs have no way to see the fleet.

Now that `pipeview gitlab` tracks a per-host list of `project[@ref]`
entries and `sync` regenerates all their reports in one run, the tracked
set is a natural universe to link within: every tracked project's config
is already fetched, so cross-project references between tracked projects
can be resolved statically and the downstream edges inverted to answer
"what triggers me" — for the tracked set, honestly labeled as such.

Goals:

1. Parse `trigger:` semantics into a typed record (project, ref,
   strategy, forward) instead of display-string annotations.
2. After `sync`, resolve cross-project references between tracked
   projects and emit a **rollup report** — a fleet-level graph of
   projects and their trigger/artifact/include relationships, with
   drill-down into each project's job graph and portal-style navigation
   across trigger edges.
3. Grow the graph-exploration controls both report types need: node-kind
   toggles, collapsible groups (child pipelines / sub-makes), and
   focus direction+depth.

## Research: what YAML can and cannot know

Verified against GitLab's documentation sources (doc/ci/yaml,
doc/ci/pipelines/downstream_pipelines, doc/api):

- **YAML declares only downstream edges.** No keyword in a downstream
  project names its upstream; upstream identity exists at runtime only
  (`CI_PIPELINE_SOURCE`, `CI_UPSTREAM_*` in 18.9+) or via API — and REST
  has no upstream field at all (only GraphQL `Pipeline.upstream`).
  Statically, "who triggers X" is answerable **only by scanning candidate
  projects' configs** — which is exactly what the tracked set provides.
  Upstreams outside the tracked set are unknowable; the UI must say
  "no known upstreams among tracked projects", never "no upstreams".
- **Three look-alike constructs with different semantics:**
  - `trigger:project [+ trigger:branch]` — multi-project pipeline: runs
    in the other project on its own ref/config, affects *that* ref's
    status. Default is fire-and-forget: the trigger job passes when the
    downstream pipeline is *created*, not when it passes.
    `strategy: mirror` (18.2+) tracks the downstream status exactly;
    `depend` is the older form with documented mismatches.
  - `trigger:include:project` — the config *file* lives in another
    project, but the resulting pipeline is a **child of the current
    project** (same project/ref/SHA). Must render as a child pipeline,
    not a cross-project edge.
  - `needs:project` — artifact download only (Premium): grabs artifacts
    from the latest successful run of a job on a ref, never triggers,
    never waits. An artifact-flow edge, not a control edge.
- `trigger:include:artifact` (dynamic child pipelines) is generated at
  runtime and statically unresolvable by construction — stays a ghost
  labeled with the generating job.
- `trigger:project` and `trigger:branch` accept CI/CD variables. Static
  expansion is unreliable (precedence, pipeline-time values), so values
  containing `$` are recorded raw and marked unresolved rather than
  guessed.
- When `trigger:branch` is omitted the downstream runs on **its own
  default branch** — which the tracked entry's report already resolved
  at fetch time (`annotations.gitlab_remote.ref`), so ref matching can
  be exact without extra network calls.

None of this needs new API surface: the configs are already on hand from
`sync`. Pipeline-*run* linking (the bridges / `trigger_jobs` REST
endpoint, GraphQL upstream) is a different, runtime feature — explicitly
out of scope here.

## Decisions

### 1. Static config linking, tracked set only

The rollup answers "which tracked projects *can* trigger which", from
configuration alone. Rejected for v1: walking actual pipeline runs
(needs pagination, retention, GraphQL for upstream, and a live-status
story that fights the offline guarantee) and auto-fetching untracked
downstream projects (surprise network fan-out under someone else's
token budget; the user's remedy is one `pipeview gitlab track` away, and
the rollup's external ghosts tell them which projects would benefit).

### 2. A typed trigger record on the node (schema v4)

`_process_trigger` grows a structured
`Node.annotations["trigger_info"]`:

```
{"mode": "multi_project" | "child",
 "project": "group/app",          # raw YAML value (multi_project)
 "ref": "main" | None,            # raw trigger:branch value
 "strategy": "mirror"|"depend"|None,
 "forward": {"yaml_variables": bool, "pipeline_variables": bool},
 "includes": [...],               # child mode: include entry summaries
 "unresolved": ["project uses CI variables", ...]}
```

The existing display annotations (`trigger`, `trigger_strategy`) stay
untouched so current reports render identically. `needs:project` ghost
nodes already carry `cross_project_need` annotations with project/job/ref
— the resolution pass consumes annotations, never parses node-id strings
(the `child.yml::job` vs `group/proj::job` id ambiguity stays confined
to display). SCHEMA_VERSION bumps to 4; additive, older JSON still loads.

### 3. Rollup is a separate artifact, not a mega-report

`sync` keeps emitting one report per entry, then a resolution pass over
the in-memory reports builds `rollup.report.html` + `rollup.json` in the
same outdir. Rejected: merging all projects into one `Report` — the
model's flat variable/stage tables and same-name job merging are correct
within one pipeline and wrong across projects, and the established
presentation ("pipelines are separate graphs", per the what-if design
and parser audit) argues against one canvas.

The rollup document embeds each project's full model JSON (marginal cost
is tens of KB per project against ~300 KB of fixed page overhead) plus a
link table:

```
{"schema_version": 1, "host": ..., "generated_at": ..., "tool_version": ...,
 "projects": [{"entry": "group/app@v2", "project": "group/app",
               "ref": "v2",            # resolved ref
               "report_html": "group-app@v2.report.html",
               "generated_at": ..., "lint_valid": ..., "web_url": ...,
               "counts": {...}, "model": {...Report v4 JSON...}}],
 "links": [{"kind": "trigger"|"needs_project"|"include",
            "src": {"project": 0, "node": "deploy", "file": ..., "line": ...},
            "dst": {"project": 1 | null, "path": "group/infra",
                    "ref": "prod" | null, "job": "build" | null},
            "strategy": ..., "forward": {...},
            "caveats": ["ref_mismatch: tracked v2, trigger targets prod",
                        "ref uses CI variables", ...]}],
 "externals": [{"path": "group/other", "kinds": ["trigger"], "refs": [...]}]}
```

Link sources: `trigger_info` records (mode `multi_project`),
`cross_project_need` ghosts, and `include:project` provenance (the lint
strategy's `gitlab_remote.includes` entries and the files strategy's
`_external/<proj>@<ref>/` materializations).

### 4. Ref matching is exact, with caveats instead of silence

A reference to project P at target ref R (explicit `trigger:branch`, or
P's default branch when omitted — known from P's own report) matches
tracked entries of P case-insensitively. Every tracked entry of P gets a
link edge; each edge is *clean* when R equals that entry's resolved ref,
otherwise carries a `ref_mismatch` caveat naming both refs. `$`-bearing
project or ref values produce `uses CI variables` caveats (project
unresolvable → the link lands in `externals`). Untracked targets stay
external ghost projects on the fleet view. Nothing is guessed; every
degraded link says why.

### 5. Rollup page: fleet view + drill-down + portals

A new template (`rollup.html`) reusing the report's design tokens and
interaction idioms (legend-as-filter, right-hand detail panel, theme
toggle, `.loc` provenance) but its own page — the report template stays
format-neutral and single-project.

- **Fleet view (landing):** one dagre graph, nodes = tracked
  `project@ref` (name, job count, lint verdict, diagnostics badge) plus
  dashed ghost nodes for external references; edges = the three
  relationship kinds, dual-encoded (color + dash + label) and filterable
  from the legend. Caveat edges carry a warning glyph.
- **Drill-down:** clicking a project swaps the canvas to that project's
  job graph rendered from its embedded model (jobs, stages, needs /
  stage_order / extends edges). Trigger jobs whose target is tracked
  render as **portal nodes**; activating one navigates to the downstream
  project's graph and flashes the far end. A breadcrumb bar
  (`Fleet ▸ app@main ▸ infra@prod`) records the path and navigates back.
- **Detail panel:** project panel (counts, lint, generated-at, triggers
  in/out with the tracked-set caveat, "open full report" relative link);
  job panel (stage, trigger record explained in GitLab terms — including
  "passes when the downstream is created, not when it succeeds" for
  strategy-less triggers); link panel (kind, strategy, forwarded
  variables, caveats spelled out).
- **Search** spans all embedded projects, results grouped by project.
- Snapshot honesty: each project card shows its generated-at; the page
  warns if the spread exceeds an hour (possible once rollups are built
  from re-runs rather than one sync pass).

Per-project reports get one addition: when the rollup pass resolves one
of their ghosts, the ghost node gains
`annotations["rollup_link"] = {"project": ..., "entry": ..., "rollup": "rollup.report.html"}`
and the report is re-rendered so its detail panel offers "tracked as
`infra@prod` — open the rollup to explore" (relative link, offline-safe).

### 6. Sync mechanics

`sync` builds the rollup by default when ≥2 entries generated
successfully; `--no-rollup` skips it; failures of individual entries
degrade the rollup to the successful subset (with a diagnostic naming
the missing entries). The TUI is untouched in v1 — sync prints the
rollup path like any other written file.

### 7. Graph exploration controls (shared template)

In the existing report (and mirrored in rollup drill-downs):

- **Node-kind toggles** — the legend grows a "Nodes" section beside the
  edge-kind filters: stages, templates, ghosts on/off. Hiding a node
  kind removes it and its edges from layout, same re-layout path the
  edge filters use.
- **Collapsible groups** — jobs are grouped by their namespace (GitLab
  child pipelines `child.yml::…`, sub-makes `ns:…`). Each group renders
  collapsed by default as a single expandable node ("▸ child.yml ·
  N jobs") with boundary-crossing edges deduplicated onto it; expanding
  draws the members inside a dagre compound cluster (the vendored dagre
  supports compound graphs; unused until now). This supplies the group
  concept ux-audit finding 14 deferred.
- **Focus shaping** — click-to-focus gains a direction control
  (dependencies / dependents / both) and a hop-depth limit. The
  existing BFS becomes direction- and depth-aware; defaults (both, ∞)
  reproduce today's behavior exactly.

All controls keyboard-operable, dual-encoded, AA in both themes, no
persistence — per the documented design constraints.

### 8. Script-safe model injection

`render_html` currently splices `report.to_json()` raw into a
`<script>` block; a recipe containing `</script>` breaks the page.
All JSON destined for inline `<script>` is escaped by replacing every
`<` with the JSON escape `\u003c` (valid inside JSON strings, and `<`
cannot occur outside them in serialized JSON) — report and rollup alike. Model JSON is
spliced after the other placeholders so payload content can never
collide with a pending placeholder.

### 9. Zero new dependencies, offline guarantee unchanged

The rollup is generated from data already fetched by `sync`; the page is
one self-contained HTML file; links between generated files are
relative. No new network calls, no new deps, and `pipeview <path>`
remains untouched.

## Module map

```
pipeview/gitlab/rollup.py     # resolution pass: [(entry, Report)] -> rollup dict
pipeview/render/rollup_html.py# rollup dict -> rollup.report.html
pipeview/render/templates/rollup.html
pipeview/parsers/gitlab_parser.py  # trigger_info records (schema v4)
pipeview/gitlab/cli.py        # sync: rollup emission + --no-rollup
pipeview/render/html.py       # <-escaped JSON splice
pipeview/render/templates/report.html  # node toggles, groups, focus, rollup_link panel
pipeview/model.py             # SCHEMA_VERSION = 4
```

## Testing

- Parser: `trigger_info` records for string/dict/project/branch/strategy/
  forward forms; child vs multi-project mode; `$`-variable marking;
  dynamic-artifact includes stay unresolved; existing annotations
  unchanged.
- Rollup resolution (pure, no network): clean link, ref mismatch (pinned
  and default-branch cases), variables in project/ref, untracked
  externals, needs:project links, include links from both fetch
  strategies, multiple entries of one project, case-insensitive paths.
- Sync integration with the FakeGitLab stub: two linked projects →
  rollup written, links correct, `--no-rollup` honored, single-entry
  sync writes none, per-entry failure degrades with diagnostic,
  ghost nodes gain `rollup_link` and reports re-render.
- Renderer: rollup HTML passes the offline-resources scan; `</script>`
  in a recipe/variable no longer escapes the script block (report and
  rollup); rollup content spot-checks (project names, link caveats).
- Controls: template-level checks (legend sections, focus controls
  markup) plus model-side group-derivation tests.

## Build order

1. Script-safe JSON splice (+ regression test).
2. `trigger_info` records + schema v4.
3. `rollup.py` resolution pass (pure) + tests.
4. Sync wiring (`--no-rollup`, re-render with `rollup_link`).
5. `rollup.html` + `rollup_html.py`.
6. Graph controls in `report.html` (node toggles → groups → focus).
7. Docs: README, CHANGELOG.
