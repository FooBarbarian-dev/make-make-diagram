# pipeview

Offline interactive HTML reports for GNU Make and GitLab CI pipelines.

`pipeview` reads build-pipeline definition files and produces self-contained, fully offline, interactive HTML reports that help people understand how the pipeline works: what builds what, what tasks can be run, where variables come from, and how files include each other.

## Installation

Requires Python 3.10+ and PyYAML.

```bash
pip install PyYAML
```

## Usage

```bash
# Analyze a Makefile
pipeview Makefile

# Analyze a GitLab CI file
pipeview .gitlab-ci.yml

# Analyze a directory (discovers Makefile and .gitlab-ci.yml)
pipeview .

# All output formats
pipeview Makefile --format html,json,svg,dot,mmd

# Custom output directory
pipeview Makefile -o reports/

# Skip the enrichment pass (see caveat below)
pipeview Makefile --no-enrich
```

### CLI reference

```
pipeview <path> [-o OUTDIR] [--format html,svg,dot,mmd,json] [--no-enrich] [--version]
```

- `<path>`: File (`Makefile`, `*.mk`, `*.yml`) or directory. A directory discovers root files (`Makefile`/`makefile`/`GNUmakefile` and `.gitlab-ci.yml`) at that directory's top level.
- `-o OUTDIR`: Output directory (default: `./pipeview-out/`).
- `--format`: Comma-separated formats (default: `html,json`).
- `--no-enrich`: Skip the Make enrichment pass.
- `--version`: Print version and exit.

### Exit codes

| Code | Meaning |
|------|---------|
| 0    | Clean — report(s) generated with no warnings or errors |
| 1    | Report(s) generated but with warning/error diagnostics |
| 2    | No report could be produced (no roots found, bad path) |

## The `##` docstring convention

Add a `##` comment above (or on the same line as) a target or job to document it:

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

Documented targets appear with descriptions in the Task Catalog view. Undocumented targets show a nudge to add a `## comment`.

## HTML report views

The generated HTML report is a single self-contained file with four views:

1. **Dependency Graph** — Interactive DAG with pan/zoom, click-to-inspect, focus mode (highlights the reachable subgraph), edge-kind filters, and a legend. Ghost nodes appear dashed.
2. **Task Catalog** — Runnable targets/jobs listed with name, description, invocation command, and flags. The "what can I run?" page.
3. **Variable Explorer** — Searchable table of all variables with event timelines showing where each was defined, overridden, or appended, with scope and file:line. Recipe text renders `$(VAR)` references as clickable links.
4. **File Map** — Tree of source files with include/recursion structure, per-file status, and all diagnostics.

## Offline guarantees

Generated reports work from `file://` on machines with no network access. No CDN references, no remote fonts, no fetches of any kind. This is enforced by an automated test that scans generated output for `http://` and `https://` resource references.

The tool itself never makes network requests while generating a report. Unresolvable includes (GitLab `project:`, `remote:`, `template:`, `component:`) become diagnostics and ghost nodes, not download attempts.

## Make enrichment caveat

By default, pipeview runs `make -pqn -f <root>` to capture GNU Make's resolved variable values and computed default goal. This runs Make's read phase, which evaluates `:=` expansions and `$(shell ...)` calls.

**If your Makefile has side-effectful shell expansions, use `--no-enrich`.** The static parser alone never executes anything.

If `make` is not on PATH, errors, or times out (30s), enrichment is silently skipped with an info diagnostic.

## v1 non-goals

These are explicitly out of scope for v1:

- Full `$(eval)` / `$(call)` expansion — renders as opaque expressions with an info diagnostic
- `$(shell ...)` evaluation in the static parser — captured as raw text, never executed
- Computed variable names (`$($(X))`) — recorded as unresolved with a diagnostic
- GitLab `project:`, `remote:`, `template:`, and `component:` includes — never fetched

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
  dagre.min.js       # pinned dagre 0.8.5 for graph layout
tests/
  fixtures/          # test corpus for both parsers
```

### Layer separation

Parsers emit the normalized model. The renderer consumes only the model. The renderer never asks "is this Make or GitLab?" — if it needed to, the model schema would be wrong.

### Design decisions

- **Edge kinds are preserved, not flattened.** Make `prerequisite` and GitLab `needs` are related but semantically different. The model preserves the distinction with typed edge kinds.
- **Ghosts are first-class.** Missing prerequisites, undefined extends targets, and unresolved includes all become ghost nodes with diagnostics. Nothing is silently omitted.
- **Both conditional branches are parsed.** Make conditionals (`ifeq`/`ifdef`/etc.) don't evaluate away — both branches are captured with condition annotations.
- **Variables track event history.** Each assignment is a separate event with operator, scope, file:line, and optional resolved value. This lets the variable explorer show the full "what happened" timeline.

## Adding a new parser

1. Create `pipeview/parsers/new_parser.py` with a function that takes a file path and returns a `Report`.
2. Use the existing model types — `Node`, `Edge`, `Variable`, `SourceFile`, `Diagnostic`.
3. Add fixture files under `tests/fixtures/new_format/`.
4. Register the format in `cli.py`'s `_classify_file` and `_parse_root` functions.
5. The renderer works unchanged — it only sees the model.

## Development

```bash
# Run tests
python -m pytest tests/ -v

# Generate a report
python -m pipeview.cli Makefile -o /tmp/report
```

## Runtime dependencies

- Python 3.10+
- PyYAML
- No other runtime dependencies

The vendored `dagre.min.js` (0.8.5) is committed and inlined into generated HTML at report time. No network access is needed at any point.
