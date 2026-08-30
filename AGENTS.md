# Working on pipeview

Guidance for coding agents (and humans) contributing to this repository.

## What this is

`pipeview` is a Python package (3.10+, only runtime dependency PyYAML) that
parses GNU Make and GitLab CI pipeline definitions into a normalized model
and renders self-contained, fully offline, interactive HTML reports. The
`pipeview gitlab` subcommand is the one networked piece: it fetches CI
config from a GitLab instance, then runs the same offline pipeline.

## Setup and everyday commands

```bash
pip install -e ".[dev]"   # editable install with pytest + ruff
make test                 # python -m pytest tests/ -v
make lint                 # ruff check .
make build                # python -m build  → sdist + wheel in dist/
make examples             # regenerate example reports into examples/out/
```

Some tests shell out to `make` (enrichment tests) and `node` (What-If
evaluator parity tests) and self-skip when those are missing. A full run
needs both on PATH — CI has them; install them locally for full coverage.
The suite is fast (~5s); run it and `ruff check .` before every commit.

## Architecture invariants — do not break these

1. **The model is the boundary.** Parsers (`pipeview/parsers/`) emit the
   normalized model (`pipeview/model.py`); renderers (`pipeview/render/`)
   consume only the model. The renderer never asks "is this Make or
   GitLab?" — if it needs to, the model schema is wrong. Fix the schema.
2. **Offline guarantee.** Report generation performs zero network access,
   and generated reports work from `file://` — no CDN, no remote fonts, no
   fetches. An automated test scans generated reports for `http(s)://`
   resource references. Only code under `pipeview/gitlab/` may touch the
   network, and only when the user runs `pipeview gitlab`.
3. **Honesty rules.** Anything unknowable is reported as *depends* /
   *ghost* / a named diagnostic — never guessed. Unknown constructs
   degrade the smallest possible unit (value → job/rule → file). If real
   `make` accepts a file, an "Unparseable line" warning is a parser bug
   (see `docs/agents/parser-audit.md`).
4. **JS/Python evaluator parity.** The What-If evaluator exists twice:
   inlined JS in the report (`pipeview/render/templates/whatif.js`) and a
   Python twin (`pipeview/parsers/gitlab_whatif_eval.py`). Both answer to
   `tests/whatif_vectors.json`, plus a full-output parity sweep
   (`tests/test_whatif_parity.py`, deep-equal JSON). Any behavior change
   must land in **both** interpreters **and** the shared vectors, in the
   same commit.
5. **Trigger docs are deterministic and provenance-marked.** Generated
   markdown carries no timestamps (unchanged inputs regenerate
   byte-identically) and every generated file carries a provenance marker;
   regeneration deletes only marker-bearing files.

## Testing conventions

- Write the failing test first; keep the suite and `ruff check .` green at
  every commit.
- Fixtures live under `tests/fixtures/{make,gitlab,trigger_docs}/<case>/`
  — one directory per scenario, real files, no mocks of the parsers.
- Parser-behavior claims are pinned by tests (`tests/test_parser_audit.py`
  pins `docs/agents/parser-audit.md`); if you fix parser behavior, add the
  pinning test in the same change.

## Style

- Ruff enforces `E, F, W, I` at line length 100 (`pyproject.toml`).
- Match the surrounding comment style; comments explain constraints and
  invariants, not what the next line does.
- User-facing diagnostics name the exact file/line/construct and what the
  user can do about it — vague warnings train users to ignore diagnostics.

## Docs layout

- `README.md` — front door: quickstart, feature tour, CLI reference.
- `docs/user-guide.md` — **the primary user document**: a full tour of
  every report view with screenshots (`docs/screenshots/`). Keep it
  accurate when behavior changes.
- `docs/agents/` — design specs and audits from development sessions; see
  `docs/agents/README.md` for what belongs there. New feature design docs
  go in `docs/agents/specs/`. Implementation plans are deleted once fully
  landed; specs get *as-built notes* appended instead.
- `CHANGELOG.md` — human-written narrative per release, linking features
  to their specs. release-please prepends generated sections on release
  (see below); you may edit the release PR to enrich them.

## Versioning and releases (automated)

Releases are automated with [release-please](https://github.com/googleapis/release-please)
on merges to `main` — see `.github/workflows/release.yml`.

- **Use Conventional Commits** for anything merged to `main` (squash-merge
  PR titles count): `feat: …` (minor bump), `fix: …` (patch),
  `feat!: …` or a `BREAKING CHANGE:` footer (major), and
  `docs:`/`ci:`/`chore:`/`refactor:`/`test:` for non-release-worthy work.
- release-please maintains a running release PR; merging that PR bumps the
  version, updates `CHANGELOG.md`, tags `vX.Y.Z`, creates the GitHub
  Release with notes, and CI attaches the sdist + wheel to it.
- **Never hand-bump versions.** The version lives in two places that must
  match — `pyproject.toml` and `pipeview/__init__.py` (`__version__`) —
  and release-please updates both. A test asserts they agree.

## CI

`.github/workflows/ci.yml` runs on every PR and push to `main`: ruff,
pytest across Python 3.10–3.13 (Ubuntu runners provide `make` and `node`),
and a package build check. Everything CI runs is reproducible locally with
the commands at the top of this file — a change is not done until all of
it passes.
