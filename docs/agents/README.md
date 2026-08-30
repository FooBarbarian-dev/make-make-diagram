# Agent & design docs

Engineering documents produced while developing pipeview (largely in
AI-assisted sessions). They are kept because they record *why* the code is
the way it is — code comments, tests, and the CHANGELOG reference them by
path. None of this is user documentation: for that, see the
[user guide](../user-guide.md).

## Contents

- **[specs/](specs/)** — one design doc per feature, named
  `YYYY-MM-DD-<topic>-design.md`. Each captures the problem, the chosen
  design, rejected alternatives, and (after implementation) *as-built
  notes* describing where reality diverged from the plan. The CHANGELOG
  links each feature to its spec.
- **[parser-audit.md](parser-audit.md)** — conformance audit of the Make
  and GitLab CI parsers against real-tool semantics (every row probed,
  never inferred). It drove the parser-hardening pass;
  `tests/test_parser_audit.py` pins its findings so they cannot regress.
- **[ux-audit.md](ux-audit.md)** — audit of the HTML report UI with a
  numbered findings table (severity, fix, status). Referenced by
  `tests/test_html_renderer.py`.

## Conventions

- New design docs go in `specs/` following the existing naming pattern.
  After the feature lands, append as-built notes to the spec rather than
  rewriting its history.
- Implementation *plans* (milestone checklists) are working documents:
  delete them once every milestone has landed — the spec's as-built notes
  are the durable record. Specs and audits stay.
- If code or tests reference a doc here, moving or deleting it means
  updating those references in the same commit.
