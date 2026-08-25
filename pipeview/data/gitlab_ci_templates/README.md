# Bundled GitLab CI templates

Verbatim snapshot of GitLab's built-in `include:template` files
(`lib/gitlab/ci/templates` in [gitlab-org/gitlab](https://gitlab.com/gitlab-org/gitlab)) at
**v19.3.0-ee** (GitLab 19.3.0), 133 templates.

pipeview uses these as an offline fallback when resolving
`include: template:` entries, because GitLab's REST template API cannot
serve most of them (it only exposes the flattened "dropdown" keys — see
`docs/superpowers/specs/2026-08-25-gitlab-template-fallback-design.md`).

The files are MIT-licensed (the gitlab repository's LICENSE covers
everything outside `doc/`, `ee/` and `jh/`); the license text at this ref
sits alongside in `LICENSE`. Do not edit these files by hand — refresh the
snapshot with `python scripts/update_gitlab_templates.py`.
