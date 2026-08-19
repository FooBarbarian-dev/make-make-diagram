# pipeview

Offline interactive HTML reports for GNU Make and GitLab CI pipelines.

`pipeview` reads build-pipeline definition files and produces self-contained,
fully offline, interactive HTML reports that help people understand how the
pipeline works: what builds what, what tasks can be run, where variables come
from, and how files include each other.

## Quickstart

```bash
git clone https://github.com/FooBarbarian-dev/make-make-diagram.git && cd make-make-diagram
pip install .
pipeview examples/make-project -o /tmp/pipeview-demo
open /tmp/pipeview-demo/Makefile.report.html   # or xdg-open on Linux
```

The report opens with the **Dependency Graph** — an interactive DAG showing
build targets, prerequisites, pattern rules, and sub-make recursion. Click a
node to inspect it. Switch tabs to the **Task Catalog** (runnable targets with
descriptions), the **Variable Explorer** (searchable table with event
timelines), or the **File Map** (include tree and diagnostics). Everything
works from `file://` with no network.

For the GitLab example:

```bash
pipeview examples/gitlab-project -o /tmp/pipeview-demo-gl
open /tmp/pipeview-demo-gl/gitlab-ci.report.html
```

This report shows a `needs:` DAG that differs from stage order, an `extends:`
chain through templates, resolved and ghost includes, and a manual production
gate.

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
python -m pipeview examples/make-project -o /tmp/pipeview-demo
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

After install, the tool performs zero network access, ever. Generated reports
are self-contained HTML files that work from `file://` — no CDN, no remote
fonts, no fetches of any kind.

## HTML report views

The generated report is a single self-contained HTML file with four views
(five for GitLab CI):

1. **Dependency Graph** — Interactive DAG with pan/zoom, click-to-inspect,
   focus mode (highlights the reachable subgraph), edge-kind filters, and a
   legend. Ghost nodes (unresolved references) appear dashed.

2. **Task Catalog** — Runnable targets/jobs listed with name, description,
   invocation command, and flags. The "what can I run?" page.

3. **Variable Explorer** — Searchable table of all variables with event
   timelines showing where each was defined, overridden, or appended, with
   scope and file:line. Recipe text renders `$(VAR)` references as clickable
   links.

4. **File Map** — Tree of source files with include/recursion structure,
   per-file status, and all diagnostics.

5. **What-If** *(GitLab CI reports only)* — A pipeline simulator. Pick an
   event (push, push with an open MR, tag, MR update, schedule, manual,
   API/trigger), set the starting state (branch, MR target/draft, changed
   files, project-level variables), and see every candidate pipeline GitLab
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
   feature branch.

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

Documented targets appear with descriptions in the Task Catalog view.
Undocumented targets show a nudge to add a `## comment`.

## Offline guarantee

Generated reports work from `file://` on machines with no network access. No
CDN references, no remote fonts, no fetches of any kind. This is enforced by
an automated test that scans every generated report for `http://` and
`https://` resource references.

The tool itself never makes network requests while generating a report.
Unresolvable includes (GitLab `project:`, `remote:`, `template:`,
`component:`) become diagnostics and ghost nodes, not download attempts.

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
pipeview <path> [-o OUTDIR] [--format FMTS] [--no-enrich] [--version]
```

| Flag | Default | Description |
|------|---------|-------------|
| `<path>` | *(required)* | File or directory to analyze |
| `-o OUTDIR` | `./pipeview-out` | Output directory |
| `--format` | `html,json` | Comma-separated: `html`, `json`, `svg`, `dot`, `mmd` |
| `--no-enrich` | off | Skip the Make enrichment pass |
| `--version` | | Print version and exit |

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Clean — report(s) generated with no warnings or errors |
| 1 | Report(s) generated but with warning/error diagnostics |
| 2 | No report could be produced (no roots found, bad path) |

## Architecture

Three layers, one package:

```
pipeview/
  cli.py             # argument parsing, root discovery, orchestration
  model.py           # normalized build model (dataclasses + serialization)
  parsers/
    make_parser.py   # static GNU Make parser
    gitlab_parser.py # GitLab CI YAML parser
    enrich.py        # optional make -pqn enrichment pass
  render/
    html.py          # single-file HTML report generator
    exports.py       # model.json, graph.dot, graph.mmd, graph.svg
    templates/       # HTML/CSS/JS template (inlined at generation time)
  vendor/
    dagre.min.js     # pinned dagre 0.8.5 for graph layout
```

Parsers emit the normalized model. The renderer consumes only the model. The
renderer never asks "is this Make or GitLab?" — if it needed to, the model
schema would be wrong.

## Development

```bash
pip install -e ".[dev]"
make test                   # run tests
make lint                   # lint
make examples               # regenerate example reports
make self                   # run pipeview on this repo's own Makefile
```

## License

MIT
