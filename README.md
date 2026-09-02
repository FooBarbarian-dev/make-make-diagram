# pipeview

[![CI](https://github.com/FooBarbarian-dev/make-make-diagram/actions/workflows/ci.yml/badge.svg)](https://github.com/FooBarbarian-dev/make-make-diagram/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/FooBarbarian-dev/make-make-diagram?include_prereleases)](https://github.com/FooBarbarian-dev/make-make-diagram/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Offline interactive HTML reports for GNU Make, GitLab CI, and GitHub
Actions pipelines.

`pipeview` reads build-pipeline definition files and produces self-contained,
fully offline, interactive HTML reports that help people understand how the
pipeline works: what builds what, what tasks can be run, where variables come
from, and how files include each other.

It can also connect to a GitLab instance (`pipeview gitlab`) or to GitHub
(`pipeview github`), browse the projects you can access from a terminal
UI, and generate the same reports straight from what the server serves —
cross-repository `include:`s and reusable workflows resolved and all.
Working from a checkout, `--upstream` splits the difference: your local
files (uncommitted edits included) with the cross-repo includes fetched
from the GitLab host the repo's own git remote points at. And editor
integrations for [VS Code and Zed](editors/) put the whole thing in the
editor, defaulting to the open repository with `--upstream` on.

![Graph view of a GitLab CI report](docs/screenshots/graph-gitlab.png)
*The Graph view of a GitLab CI report: the `needs:` DAG, `extends:` template
edges, and a legend that doubles as the view's filters. See the
[user guide](docs/user-guide.md) for a full tour with more screenshots.*

## Quickstart

```bash
git clone https://github.com/FooBarbarian-dev/make-make-diagram.git && cd make-make-diagram
pip install .
pipeview examples/make-project -o examples/out
open examples/out/Makefile.report.html   # xdg-open on Linux, start on Windows
```

The report opens on the **Graph** tab — an interactive dependency DAG showing
build targets, prerequisites, pattern rules, and sub-make recursion. Click a
node to inspect it. Switch tabs to **Tasks** (runnable targets with
descriptions), **Variables** (searchable table with event timelines), or
**Files** (include tree and diagnostics). Everything works from `file://`
with no network.

For the GitLab example:

```bash
pipeview examples/gitlab-project -o examples/out
open examples/out/gitlab-ci.report.html
```

This report shows a `needs:` DAG that differs from stage order, an `extends:`
chain through templates, resolved includes plus a *ghost* one (a reference
that can't be resolved offline, drawn dashed), and a manual production gate.

For the GitHub Actions example:

```bash
pipeview examples/github-project -o examples/out
open examples/out/github-actions.report.html
```

One report covers the repository's whole `.github/workflows/` tree: each
workflow is a collapsible cluster, `needs:` edges form the DAG, a local
reusable workflow folds behind its caller's ▶ edge, and a
cross-repository `uses:` becomes a ghost with a diagnostic.

## Installation

### Standard install

```bash
git clone https://github.com/FooBarbarian-dev/make-make-diagram.git
cd make-make-diagram
pip install .
```

Or use `pipx` for an isolated CLI install:

```bash
pipx install .
```

To update later: `git pull` and re-run the install (`pip install .` or
`pipx upgrade pipeview`). `pipeview --version` shows what you have.

### Development

```bash
pip install -e ".[dev]"
pytest           # run the test suite
ruff check .     # lint
```

### No-install (run from checkout)

The only requirement is Python 3.10+ and PyYAML:

```bash
pip install PyYAML
python -m pipeview examples/make-project -o examples/out
```

### Air-gapped install

On a machine with internet access, build the wheels:

```bash
pip wheel . -w dist/
```

Transfer the `dist/` directory to the target machine, then:

```bash
pip install --no-index --find-links dist/ pipeview
```

After install, report generation performs zero network access unless you
explicitly ask for it — only the `pipeview gitlab` and `pipeview github`
subcommands and the opt-in `--upstream` flag talk to a network, and only
to the host you name (or your repo's own remote names). Generated
reports are self-contained HTML files that work from `file://` — no CDN,
no remote fonts, no fetches of any kind.

## HTML report views

The generated report is a single self-contained HTML file with four views
(five for GitLab CI and GitHub Actions). The
[user guide](docs/user-guide.md) walks through each one with screenshots.

1. **Graph** — Interactive dependency DAG with pan/zoom, click-to-inspect,
   focus mode (highlights the reachable subgraph, with direction —
   dependencies / dependents / both — and hop-depth controls), and a legend
   that doubles as the view options: edge-kind filters, node-kind toggles
   (stage lanes, templates, pattern rules, unresolved references), and
   collapsible groups — GitLab child pipelines, GitHub workflows (reusable
   ones stay folded behind their caller's edge), and recursive sub-makes
   fold into one expandable node each, with their trigger/recursion edge
   attached. Ghost nodes (unresolved references) appear dashed.

2. **Tasks** — Runnable targets/jobs listed with name, description,
   invocation command, and flags. The "what can I run?" page.

3. **Variables** — Searchable table of all variables with event
   timelines showing where each was defined, overridden, or appended, with
   scope and file:line. Recipe text renders `$(VAR)` references as clickable
   links. For GitLab CI, predefined `CI_*`/`GITLAB_*` variables carry curated
   docs — what each is, an example value, and when GitLab sets it — and a
   collapsible reference below the table documents the whole catalog, with
   the names this configuration references sorted first. GitHub Actions
   reports get the same treatment for `GITHUB_*`/`RUNNER_*` variables and
   the `github.*` expression-context fields.

4. **Files** — Tree of source files with include/recursion structure,
   per-file status, and all diagnostics.

5. **What-If** *(GitLab CI and GitHub Actions reports)* — A pipeline
   simulator. Pick an event (push, push with an open MR/PR, tag, MR/PR
   update, schedule, manual, API/trigger — or, for GitHub, pull request,
   manual dispatch with typed inputs, release), set the starting state
   (branch, MR/PR target/draft, changed files, project-level variables),
   and see every candidate pipeline GitLab
   would spawn — side by side, one graph per pipeline, with per-job
   verdicts (runs / manual / delayed / not added / depends) and a
   rule-by-rule trace for each. Duplicate jobs that would run in more than
   one pipeline for the same push are badged and summarized; dotenv
   artifact propagation and `needs:` on jobs missing from the pipeline
   (a real pipeline-creation failure) are called out. `rules:if`
   expressions are compiled at generation time; `rules:exists` is checked
   against the actual repo; anything unknowable (`rules:changes` without a
   changed-files list, variables defined nowhere) is shown as
   *depends* — never guessed. The simulated world assumes protected
   `main` and `dev` branches; every other branch is a generic unprotected
   feature branch. Predefined variable names throughout the tab carry
   documentation tooltips (what the variable is, an example value, and when
   GitLab sets it).

   Every evaluation also renders as a **plain-text job listing** — one
   section per candidate pipeline naming the jobs that would run, in
   stage order with stage and verdict — behind a collapsible block and a
   **Copy job list** button, ready to paste into an issue or chat.
   **Copy markdown** exports the same listing (or the delta) as markdown
   tables instead, for issues, MRs and wikis; **Export scenario** copies
   the current knobs as a trigger-docs YAML stanza, making the tab the
   authoring UI for the scenarios file (see *Markdown trigger docs*
   below). And
   **Pin as baseline** freezes the current scenario so you can flip any
   knob (the event preset included) and see the **delta**: per-pipeline
   diff graphs where added jobs are green, removed jobs red-dashed, and
   verdict changes amber (`runs → manual gate`), with a `+ / - / ~ / =`
   text diff (the button becomes **Copy delta**) that also names
   pipeline-level differences — a pipeline that exists on one side only,
   or one that would fail creation. Comparing *push to branch* against
   *tag push* — or against "MR closed", or a variable flipped — becomes
   one click instead of memory.

![What-If view: one push spawning two candidate pipelines](docs/screenshots/whatif-duplicates.png)
*What-If: a push to a feature branch with an open MR spawns two candidate
pipelines. The jobs that would run in both are badged as duplicates, with the
usual `workflow:rules` fix named.*

![Variables view with an event timeline](docs/screenshots/variables-make.png)
*Variables: every definition, override, and append with scope and file:line —
here, a sub-make's `?=` losing to a global `=` from `config.mk`.*

## Markdown trigger docs (`--trigger-docs`)

The What-If tab answers "what runs for this trigger?" interactively.
Trigger docs answer it as **committed markdown**: define the trigger
scenarios you care about once, in a small YAML file, and every report run
can also emit one doc per scenario — plain sentences, job tables with a
deciding-rule "why" column, and mermaid diagrams that GitLab's file
viewer renders natively.

```bash
pipeview scenarios init                                # writes pipeview-scenarios.yaml
pipeview scenarios check pipeview-scenarios.yaml       # validate before a big sync
pipeview scenarios preview pipeview-scenarios.yaml .   # iterate: docs to stdout

pipeview . --trigger-docs pipeview-scenarios.yaml -o out/                 # local checkout
pipeview gitlab sync -o reports/ --trigger-docs pipeview-scenarios.yaml   # every tracked project

pipeview scenarios verify pipeview-scenarios.yaml . docs/ci   # drift check (read-only,
                                                              #   for a scheduled CI job)
```

Scenarios work for GitLab CI and GitHub Actions configurations alike:
shared events (`push_branch`, `push_tag`, `schedule`) apply to both, and
a scenario whose event belongs to the other provider is skipped with a
note in the generated index — one scenarios file can serve a mixed fleet
(`open_mr` on a push even doubles as `open_pr` for GitHub repos).

You don't have to hand-write the YAML: the What-If tab's **Export
scenario** button copies the knobs you configured interactively as a
ready-to-paste stanza.

A scenario is a named What-If configuration — the same knobs as the tab,
spelled in YAML:

```yaml
version: 1
scenarios:
  - id: push-main            # → push-main.md
    title: Push to main
    event: push_branch       # push_branch | push_tag | schedule (both CI systems)
                             # mr | web | api | trigger (GitLab)
                             # pr | workflow_dispatch | release (GitHub)
    branch: main
  - id: release-tag
    event: push_tag
    tag: v1.2.3              # an example ref; each project's rules decide
  - id: nightly
    event: schedule
    variables: { NIGHTLY: "1" }
```

Each project in the run gets a self-contained folder beside its HTML
report — `<slug>.trigger-docs/` (locally, e.g. `gitlab-ci.trigger-docs/`)
with one `<id>.md` per scenario plus a
`pipeline-triggers.md` index — ready to be copied into that repo (say,
`docs/ci/`) and committed by you or your tooling. pipeview itself never
writes to GitLab. The docs carry no timestamps, so regenerating with
unchanged inputs is byte-identical and `git diff` answers "did anything
change?". Every generated file carries a provenance marker; regeneration
deletes only marker-bearing files, and a hand-written file in the folder
is warned about, never deleted or overwritten. `pipeview scenarios
verify` closes the loop: it compares a repo's committed docs against
fresh generation (provenance masked, so a newer pipeview regenerating
identical content is not drift) and exits non-zero when they've gone
stale — cron it in CI and doc freshness polices itself, with pipeview
still read-only.

The honesty rules match the What-If tab, because the same evaluation runs
(a Python twin of the report's inlined evaluator, pinned to it by a
shared vector suite): unknowables render as *depends* with the missing
fact named, never guessed; trigger jobs stop at the boundary ("spawns
downstream pipeline in `group/other`") rather than pretending to know the
downstream; and one event spawning several candidate pipelines gets a
fan-out diagram plus a duplicate-jobs callout. Verdicts are encoded in
node shape and label — manual gates as hexagons, delays as ⏱, *depends*
dashed with a `?` — so GitLab's light and dark themes both stay legible.

## Cross-repo includes from a checkout (`--upstream`)

`pipeview .` on a local checkout leaves `include:project`,
`include:remote` and `include:component` entries as dashed ghost nodes —
resolving them needs a network. `--upstream` resolves them using the one
reference the checkout already carries: its own git remote.

```bash
cd ~/src/app          # a checkout of gitlab.example.com:group/app
pipeview . --upstream -o report
```

pipeview asks git for the upstream (the current branch's tracking
remote, else `origin`, else the sole remote — override with
`--upstream-remote NAME`), reads the GitLab host and project path off
the remote URL (ssh, scp-style, and http(s) URLs all work), and fetches
*only* the externally-included files from that host — the same traversal
as the `files` strategy, `include:template` served by the instance API
with the bundled-snapshot fallback, nested includes inside fetched
repositories resolved against their own repos. Your local files remain
the source of truth: they are parsed from the working tree, uncommitted
edits and real line numbers included, and are never fetched. Fetched
files land under `<outdir>/fetched/<project>@upstream/` and the report's
Files view says where each came from.

Authentication reuses the `pipeview gitlab` machinery: `--token`, then
`$PIPEVIEW_GITLAB_TOKEN` / `$GITLAB_TOKEN` / `$GITLAB_PRIVATE_TOKEN`,
then the config stored by `pipeview gitlab auth`. Without a token (or
without a usable upstream) the report still generates — the includes
stay ghosts and a warning diagnostic says exactly what to do.

`--upstream` is the only way a plain `pipeview <path>` run touches a
network, and it never happens without the flag.

## Editor integrations (VS Code, Zed)

Editor integrations live under [`editors/`](editors/), both thin shells
over the same core — every feature belongs to the CLI, the report HTML,
or the `pipeview lsp` language server, never to extension code. See
[editors/README.md](editors/README.md) for the full feature matrix.

- **[VS Code](editors/vscode/)** — **Pipeview: Pipeline Report for This
  Repo** analyzes the open repository (with `--upstream` on by default,
  per the section above) and renders the report in a webview panel —
  Graph, Tasks, Variables, Files and What-If included, since it is the
  same self-contained HTML. Context menus cover single files; further
  commands run `pipeview gitlab report`/`sync` (the rollup opens when
  produced), store a token in VS Code secret storage, or open an
  integrated terminal for the interactive `pipeview gitlab auth`.
  Pipeline buffers also get `pipeview lsp`: inline diagnostics, hover
  docs, clickable includes, and a report code action that lands in the
  webview.
- **[Zed](editors/zed/)** — Zed extensions cannot show webviews, so the
  extension wires up `pipeview lsp` instead: pipeview's diagnostics
  appear inline on save, predefined `CI_*` variables get hover docs
  (the Variables-tab catalog), `include:local` entries become clickable
  links, and a code action generates the repo's report (upstream on by
  default) and opens it in the default browser — the report is
  self-contained `file://` HTML precisely so any browser is a full
  viewer.

`pipeview lsp` is editor-agnostic — both extensions host it — and also
ships as its own `pipeview-lsp` executable for editors that take a bare
binary path with no arguments (Zed's `binary.path` setting).

## Fetching from GitLab (`pipeview gitlab`)

Everything above works on local files. The `gitlab` subcommand — the **only**
part of pipeview that performs network access — fetches CI configuration
directly from a GitLab instance:

```bash
# One-time: create + store a read_api token (opens GitLab's prefilled form,
# you paste the token back; stored 0600 in ~/.config/pipeview/gitlab.json)
pipeview gitlab auth --host https://gitlab.example.com

# Interactive project browser (curses TUI)
pipeview gitlab

# Headless equivalents
pipeview gitlab projects --search api        # list what the token can see
pipeview gitlab report group/app --ref main  # fetch + generate one report
pipeview gitlab track group/app              # remember a project (default branch)
pipeview gitlab track group/app@release/2.0  # …or pin any branch/tag
pipeview gitlab sync -o reports/             # report on every tracked entry
```

Tracked entries are `group/app` (follows the project's default branch) or
`group/app@ref` (pinned to a branch or tag; `--ref` works too). A project
can be tracked at several refs at once; `sync` generates one report per
entry, and `untrack group/app` sweeps every ref of the project while
`untrack group/app@dev` removes just that one.

### Cross-project rollup

When `sync` generates two or more reports it also links them:
`trigger:project` jobs, `needs:project` artifact fetches, and
`include:project` files that point at another tracked project resolve
into real cross-project links, written to `rollup.report.html` (and
`rollup.json`) beside the per-project reports — pass `--no-rollup` to
skip it. The rollup is one more self-contained offline HTML file:

- **Fleet view** — one node per tracked `project@ref` (job counts, lint
  verdict, diagnostics), with the three relationship kinds as distinct,
  filterable edges. Projects referenced but not tracked appear dashed;
  tracking them and re-syncing links them.
- **Drill-down** — click a project to explore its job graph in place;
  trigger jobs marked `↪` jump across to the downstream project's graph,
  with a breadcrumb trail back. Detail panels spell out GitLab's trigger
  semantics: a strategy-less trigger job passes when the downstream
  pipeline is *created* (not when it succeeds), `mirror` tracks its
  status exactly, forwarded variables are listed.
- **Honesty rules** — resolution is static, from configuration only.
  Ref mismatches ("targets `prod`, tracked at `v2`"), CI-variable
  project/ref values, and dynamic child pipelines are flagged, never
  guessed, and upstream lists always say *within the tracked set*:
  configuration alone cannot reveal upstreams outside it.

Per-project reports stay unchanged except that a resolved ghost node's
panel now links to the rollup.

In the browser: `↑/↓` move, `/` searches server-side, `enter` opens a
project and then generates a report for the selected ref, `o` opens the
generated HTML, `?` shows all keys. `t` tracks/untracks: in the project
list it tracks the default branch (tracked projects sort first with a
`●`); in the ref picker it pins the selected branch or tag, with a `●` on
every tracked ref.

Tokens are looked up in order: `--token`, `$PIPEVIEW_GITLAB_TOKEN`,
`$GITLAB_TOKEN`, `$GITLAB_PRIVATE_TOKEN`, then the stored config. A first
token cannot be created via the API (the API requires a token), so `auth`
opens the prefilled personal-access-token form instead. Corporate TLS:
`--ca-bundle` (or the usual `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`).

### How cross-repo includes are resolved

Two strategies (`--strategy auto|lint|files`, default `auto`):

- **`lint`** — one call to GitLab's project-scoped CI Lint API
  (`GET /projects/:id/ci/lint`), whose `merged_yaml` response field is the
  complete configuration with **every** `include:` — cross-project,
  template, remote, and component — already expanded server-side, under
  your own permissions. No repository traversal needed. GitLab's own
  `errors`/`warnings` verdict lands in the report's diagnostics, and the
  response's provenance metadata (which file came from which repo) lands in
  the Files view.
- **`files`** — fetches the root CI file and walks `include:` recursively
  across repositories via the repository-files API (`include:project` files
  come from their own repos, templates from the template API, and so on).
  Used automatically when the lint endpoint is unavailable, or on request
  when you want real per-file line numbers instead of the merged view.

Fetched files are materialized under `<outdir>/fetched/<project>@<ref>/`
(cross-repo files under `_external/`), then the ordinary offline pipeline
runs — generated reports remain fully offline.

Same-name jobs defined in several files merge exactly as GitLab merges
them: includes first (later includes beat earlier ones), each file's own
content on top of what it includes, the root file winning overall —
hashes like `variables:` merge key by key, while `script:`, `rules:` and
other arrays or scalars are replaced whole. So a local job that only adds
`rules:` to a template job keeps the template's script, and the report
says where each merged job came from (a `merged_from` annotation plus an
info diagnostic naming both definition sites).

### Built-in `include:template` files

GitLab's built-in templates (`Jobs/Build.gitlab-ci.yml`,
`Security/SAST.gitlab-ci.yml`, …) ship inside the GitLab installation, and
its REST template API can only serve a fraction of them: it exposes the
flattened "dropdown" keys (top-level names plus the basenames of `Pages/`,
`Verify/` and `Security/`), so `Jobs/*`, `Workflows/*` and every
category-qualified spelling 404 on **every** GitLab version. pipeview asks
the instance first using the key spellings that can work, then falls back
to a bundled snapshot of the real template tree
(`pipeview/data/gitlab_ci_templates`, MIT-licensed, pinned to a GitLab
release recorded in its `_meta.json`) — with an info diagnostic naming the
snapshot version, since your instance's own copy may differ. The same
snapshot resolves `include:template` in fully-offline `pipeview <path>`
runs. `--no-bundled-templates` (both CLIs) restores the old
ghost-node behavior.

### When something goes wrong

`sync` and `report` print each entry's warning/error diagnostics to stderr
— including GitLab's own CI Lint verdict ("jobs:deploy config contains
unknown keys…"), fetch failures ("Cannot fetch group/lib@stable:ci/x.yml:
GitLab API 404…"), and unresolved includes — so a failing tracked project
tells you *what* failed, not just that it did.

For more, turn on logging:

```bash
pipeview gitlab sync -v         # fetch steps and decisions, as they happen
pipeview gitlab sync -vv        # + every HTTP request, with status and timing
pipeview gitlab report group/app --log-file debug.log   # full detail to a file
```

`-v` shows which strategy was chosen and why, every file fetched (source,
size, destination), ref resolutions, and all diagnostics including infos;
`-vv` adds each API call. The browser (`pipeview gitlab -v`) writes the
same log to `<outdir>/pipeview-gitlab.log` instead of the screen — curses
owns the terminal — and the status bar points there. `--log-file` always
captures debug-level detail regardless of `-v`.

## Fetching from GitHub (`pipeview github`)

The GitHub twin of `pipeview gitlab` — the same commands against
github.com or a GitHub Enterprise Server host:

```bash
# One-time: create + store a token (opens GitHub's prefilled form,
# you paste the token back; stored 0600 in ~/.config/pipeview/github.json)
pipeview github auth

# Interactive repository browser (the same curses TUI)
pipeview github

# Headless equivalents
pipeview github repos --search api            # list what the token can see
pipeview github report octo-org/app --ref main
pipeview github track octo-org/app            # remember a repo (default branch)
pipeview github track octo-org/app@release/2  # …or pin any branch/tag
pipeview github sync -o reports/              # report on every tracked entry
```

Tokens are looked up in order: `--token`, `$PIPEVIEW_GITHUB_TOKEN`,
`$GITHUB_TOKEN`, `$GH_TOKEN`, then the stored config. The host defaults
to `https://github.com`; point `--host` (or `$GITHUB_SERVER_URL`) at a
GitHub Enterprise Server and the client uses its `/api/v3` root
automatically.

Fetching has one strategy — GitHub has no server-side merged-config API —
the client lists `.github/workflows/` via the contents API, fetches every
workflow, and follows cross-repository reusable-workflow calls
(`jobs.<id>.uses: owner/repo/path@ref`) into
`<outdir>/fetched/<owner-repo>@<ref>/_external/`, nested calls included
(a `./` call inside another repository's workflow resolves in *that*
repository), up to GitHub's own limits of 4 nesting levels and 20
reusable workflows per run. The materialized tree then runs through the
ordinary offline pipeline, so a called workflow's jobs land in the report
as real nodes linked from their caller instead of ghosts.

`sync` with two or more tracked entries writes the same cross-repository
rollup GitLab syncs get (`rollup.report.html` + `rollup.json`, skip with
`--no-rollup`): reusable-workflow calls between tracked repositories
resolve into real links — ref mismatches ("targets `v2`, tracked at
`main`") are flagged, never guessed.

## The `##` docstring convention

Add a `##` comment above (or on the same line as) a target or job to document
it:

```makefile
## Build the project
build: main.o
	gcc -o app main.o

deploy: ## Deploy to production
	./deploy.sh
```

```yaml
## Run the test suite
test_job:
  stage: test
  script:
    - make test
```

Documented targets appear with descriptions in the Tasks view.
Undocumented targets show a nudge to add a `## comment`.

## Offline guarantee

Generated reports work from `file://` on machines with no network access. No
CDN references, no remote fonts, no fetches of any kind. This is enforced by
an automated test that scans every generated report for `http://` and
`https://` resource references.

The tool never makes network requests while generating a report, and
`pipeview <path>` never touches the network at all unless you pass the
explicit `--upstream` flag (which fetches only cross-repository includes,
from the host your repo's git remote names, before the offline pipeline
runs). Unresolvable includes
(GitLab `project:`, `remote:`, `component:`) become diagnostics and ghost
nodes, not download attempts. `include:template` entries are the one kind
that resolves without a network: GitLab's built-in templates are bundled
with pipeview (see above), read from disk, and clearly labeled
`[template]` in the report — disable with `--no-bundled-templates`.

The deliberate exceptions are the explicit `pipeview gitlab` /
`pipeview github` subcommands and the explicit `--upstream` flag, which
talk to exactly the host you point them at, materialize what they
fetched to disk, and then run the same offline pipeline — the reports
they produce are as offline as everything else.

## Make enrichment caveat

By default, pipeview runs `make -pqn -f <root>` to capture GNU Make's resolved
variable values and computed default goal. This runs Make's read phase, which
evaluates `:=` expansions and `$(shell ...)` calls.

**If your Makefile has side-effectful shell expansions, use `--no-enrich`.**
The static parser alone never executes anything.

If `make` is not on PATH, errors, or times out (30 seconds), enrichment is
silently skipped with an info diagnostic.

## CLI reference

```
pipeview <path> [-o OUTDIR] [--format FMTS] [--no-enrich] [--trigger-docs FILE]
                [--upstream] [--version]
pipeview scenarios [init|check|preview|verify] …
pipeview gitlab [browse|auth|projects|report|track|untrack|tracked|sync] …
pipeview github [browse|auth|repos|report|track|untrack|tracked|sync] …
pipeview lsp     # language server over stdio (used by editors/);
                 # also installed as the `pipeview-lsp` executable
```

| Flag | Default | Description |
|------|---------|-------------|
| `<path>` | *(required)* | File or directory to analyze (finds `Makefile`, `.gitlab-ci.yml`, and `.github/workflows/` in a directory) |
| `-o OUTDIR` | `./pipeview-out` | Output directory |
| `--format` | `html,json` | Comma-separated: `html`, `json`, `svg`, `dot`, `mmd` |
| `--no-enrich` | off | Skip the Make enrichment pass |
| `--trigger-docs FILE` | off | Also write per-scenario markdown docs (see above) |
| `--upstream` | off | Resolve cross-repo includes via the repo's git remote (see above) |
| `--upstream-remote NAME` | auto | Git remote to use with `--upstream` |
| `--token` | | `--upstream`: API token (else env vars / stored config) |
| `--ca-bundle` / `--insecure` / `--timeout` | | `--upstream`: TLS/HTTP knobs, as in `pipeview gitlab` |
| `-v` / `--log-file FILE` | off | Log fetch steps and decisions (`-vv`: every HTTP request) |
| `--version` | | Print version and exit |

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Clean — report(s) generated with no warnings or errors |
| 1 | Report(s) generated but with warning/error diagnostics |
| 2 | No report could be produced (no roots found, bad path) |

Info-only diagnostics still exit 0. With `--trigger-docs`, problems in the
scenarios file or the docs folder also floor the exit code at 1, even when
the report itself is clean.

## Architecture

Three layers, one package:

```
pipeview/
  cli.py             # argument parsing, root discovery, orchestration
  lsp.py             # `pipeview lsp` — stdio language server for editors/
  scenarios.py       # trigger-docs scenario file: schema + loader
  scenarios_cli.py   # `pipeview scenarios` — init/check/preview/verify helpers
  model.py           # normalized build model (dataclasses + serialization)
  gitlab_templates.py # lookup into the bundled template snapshot
  data/
    gitlab_ci_templates/  # snapshot of GitLab's built-in templates
                          # (pinned release recorded in _meta.json)
  parsers/
    make_parser.py   # static GNU Make parser
    gitlab_parser.py # GitLab CI YAML parser
    gitlab_predefined.py  # curated docs for predefined CI_*/GITLAB_* variables
    gitlab_whatif.py # compiles rules/only/except into an evaluatable program
    gitlab_whatif_eval.py # Python twin of the report's What-If evaluator
    github_parser.py # GitHub Actions workflow parser
    github_predefined.py  # curated docs for GITHUB_*/RUNNER_* + github.* fields
    github_whatif.py # compiles on:/if: into an evaluatable program
    github_whatif_eval.py # Python twin of the GitHub What-If evaluator
    enrich.py        # optional make -pqn enrichment pass
  gitlab/            # `pipeview gitlab` + `--upstream` — networked
    api.py           # stdlib GitLab REST client
    auth.py          # token resolution + prefilled-URL creation flow
    config.py        # ~/.config/pipeview/gitlab.json (hosts, tracked lists)
    fetch.py         # lint & files fetch strategies (+ the local-seed variant)
    upstream.py      # --upstream: git remote -> host/project, orchestration
    report.py        # fetched config -> the ordinary offline pipeline
    rollup.py        # cross-project link resolution for `sync` (shared
                     # by both providers)
    tui.py           # curses project browser (shared by both providers)
    cli.py           # subcommand parsing
  github/            # `pipeview github` — networked, mirrors gitlab/
    api.py           # stdlib GitHub REST client (github.com + GHES)
    auth.py          # token resolution + prefilled-URL creation flow
    config.py        # ~/.config/pipeview/github.json (hosts, tracked lists)
    fetch.py         # workflows + cross-repo reusable-workflow fetching
    report.py        # fetched workflows -> the ordinary offline pipeline
    cli.py           # subcommand parsing
  render/
    html.py          # single-file HTML report generator
    rollup_html.py   # rollup.report.html generator
    exports.py       # model.json, graph.dot, graph.mmd, graph.svg
    trigger_docs.py  # per-scenario markdown docs (--trigger-docs)
    mmd.py           # mermaid escaping helpers (exports + trigger docs)
    templates/       # report.html, rollup.html, whatif.js,
                     # whatif_github.js (inlined at generation time)
  vendor/
    dagre.min.js     # pinned dagre 0.8.5 for graph layout

editors/             # editor integrations (see editors/README.md)
  vscode/            # VS Code: TypeScript shell — spawns the CLI, shows
                     #   its HTML in webviews
  zed/               # Zed: wasm extension wiring up `pipeview lsp`
```

Parsers emit the normalized model. The renderer consumes only the model. The
renderer never asks "is this Make, GitLab, or GitHub?" — if it needed to,
the model schema would be wrong. (The What-If tab selects its evaluator
from the compiled program's provider tag — still data, not format
branching.)

## Development

```bash
pip install -e ".[dev]"
make test                   # run tests
make lint                   # lint
make build                  # build sdist + wheel into dist/
make examples               # regenerate example reports
make self                   # run pipeview on this repo's own Makefile
make vscode                 # build + unit-test the VS Code extension
make zed                    # build the Zed extension (wasm32-wasip2)
```

CI runs lint, the test suite (Python 3.10–3.13), a package build check,
and the editor-extension builds (VS Code unit tests + `.vsix` packaging,
Zed wasm build) on every pull request. Working conventions —
architecture invariants, testing rules, docs layout — live in
[AGENTS.md](AGENTS.md).

### Releases

Versioning and releases are automated with
[release-please](https://github.com/googleapis/release-please): merges to
`main` using [Conventional Commit](https://www.conventionalcommits.org/)
messages (`feat:`, `fix:`, `feat!:`, …) accumulate into a release PR, and
merging that PR bumps the version, updates the changelog, tags the
release, and publishes a GitHub Release with the sdist and wheel
attached — grab either from the
[releases page](https://github.com/FooBarbarian-dev/make-make-diagram/releases)
and `pip install` it directly, no clone needed.

The editor extensions release as their own components of the same
manifest (commits route by path — see
[docs/release-pipelines.md](docs/release-pipelines.md)): the VS Code
extension tags `vscode-vX.Y.Z` with the packaged `.vsix` attached
(installable via *Extensions: Install from VSIX…*), and the Zed
extension tags `zed-vX.Y.Z` with the built wasm attached for reference.

## Documentation

- **[User guide](docs/user-guide.md)** — a tour of every report view with
  screenshots, plus two worked examples: mapping a recursive Make build, and
  chasing a GitLab duplicate-pipeline problem through the What-If tab.
- **[examples/](examples/README.md)** — runnable demo projects with notes on
  what each report shows.
- **[docs/agents/](docs/agents/README.md)** — design specs and engineering
  audits that record why the code is the way it is (referenced from the
  changelog and code comments).
- **[AGENTS.md](AGENTS.md)** — how to work on this project: setup,
  invariants, testing conventions, and the release process.

## License

MIT
