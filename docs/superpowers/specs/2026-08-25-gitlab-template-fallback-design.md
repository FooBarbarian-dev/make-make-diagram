# Bundled fallback for GitLab's built-in CI templates — design

Date: 2026-08-25
Status: implemented.

## Problem

The remote-fetch feature's `files` strategy fails to load GitLab's own
`include:template` files (the tree GitLab ships in
`lib/gitlab/ci/templates`). A pipeline that includes
`Jobs/Build.gitlab-ci.yml` or `Security/SAST.gitlab-ci.yml` came out with
warnings and ghost jobs even against a healthy instance, and offline
`pipeview <path>` runs always ghosted template includes.

## API research: why the template API cannot serve them

Probed empirically against gitlab.com (which runs the newest GitLab), so
this is not a version problem:

- `GET /api/v4/templates/gitlab_ci_ymls` lists **88 keys, none containing
  a slash**. The listing is the "dropdown" set: top-level template names
  plus the *basenames* of the `Pages/`, `Verify/` and `Security/`
  category directories, all without the `.gitlab-ci.yml` suffix
  (`Security/SAST.gitlab-ci.yml` → key `SAST`).
- `GET …/gitlab_ci_ymls/Jobs%2FBuild`, `…/Jobs/Build`,
  `…/Jobs%2FBuild.gitlab-ci.yml`: **404, every spelling**. `Jobs/*` and
  `Workflows/*` are simply not reachable through the REST API — the API's
  finder only knows the dropdown categories, while `include:template`
  inside GitLab resolves against the full tree.
- The flattened keys serve stubs where GitLab moved a template: key
  `SAST` returns `Security/SAST.gitlab-ci.yml`, whose entire body is
  `include: - template: Jobs/SAST.gitlab-ci.yml` — an include the API
  then cannot serve. So any real security/Auto-DevOps pipeline is
  *guaranteed* to hit unfetchable templates.

The previous code asked the API for `Jobs/Deploy` / `Jobs/Deploy.gitlab-ci.yml`
style keys; the test fake accepted them, the real API never does.

## Decisions

### 1. Ship a snapshot of the template tree inside the package

`pipeview/data/gitlab_ci_templates/` is a verbatim copy of
`lib/gitlab/ci/templates` from `gitlab-org/gitlab` at a pinned release tag
(133 files, ~730 KB raw, ~70 KB compressed in the wheel), refreshed by
`scripts/update_gitlab_templates.py` (stdlib-only; downloads the archive
for the newest stable `vX.Y.Z-ee` tag or a `--ref` you name, rewrites the
directory, and records provenance in `_meta.json`). The directory carries
the gitlab repository's LICENSE (the templates live outside `doc/`, `ee/`
and `jh/`, so they are MIT) and a README that says never to edit by hand.

`pipeview/gitlab_templates.py` is the runtime accessor: `template_path()`
(suffix-tolerant, refuses to resolve outside the snapshot),
`bundled_version()` for provenance messages, `template_names()`,
`bundled_meta()`.

Why bundle rather than fetch from gitlab.com on demand: the design rule
"network access only inside `pipeview gitlab`, only to the host the user
named" survives, air-gapped/corporate installs keep working, and the
offline CLI gets the same fix for free. The cost — the snapshot can lag
the user's instance — is stated in every diagnostic that uses it.

### 2. Fetch layer: ask the instance with keys that can work, then fall back

`_template` in `fetch.py` now tries `template_api_keys(name)` in order:
the suffix-stripped name, the category-flattened basename when the name
sits directly under `Pages/`/`Verify/`/`Security/`, then the raw name.
The instance's copy wins when the API can serve it — it matches the
instance's GitLab version and covers custom instance templates. When the
API cannot (`Jobs/*`, `Workflows/*`, anything else nested), the bundled
copy is materialized into `_external/templates/<name>` exactly like an
API-served one, with an info note and a `FetchedFile.source` naming the
snapshot version. Only when both miss does the include stay a ghost, and
the warning now says which of the two lookups failed. Bundled templates
that `include:` further templates recurse through the same path.

Rejected alternative: `POST /projects/:id/ci/lint` with a synthetic
`include: [{template: …}]` body would return the instance's authentic
copy — but the files strategy usually runs *because* lint is unavailable,
and it needs a second auth surface for a marginal freshness win.

### 3. Parser: resolve `include:template` offline from the same snapshot

`parse_gitlab(…, bundled_templates=True)`: when neither the external
resolver (remote flow) nor anything else resolves a template include, the
parser parses the bundled file in place. Files outside the repo tree get
display paths through a new path-alias mechanism (`_ParserState.rel()`),
so the File Map shows `[template] Jobs/Build.gitlab-ci.yml` — the same
convention the lint strategy already uses — instead of a `../../…`
relpath into site-packages. An info diagnostic names the snapshot version
per resolved template; a still-unknown template's warning says it is
"not in pipeview's bundled GitLab X.Y.Z templates".

This deliberately amends the previous "offline behavior is byte-for-byte
unchanged" stance: resolving from files shipped inside the package is
still fully offline, and it un-ghosts the single most common unresolved
include in real configs. `--no-bundled-templates` (both the main CLI and
`pipeview gitlab`) restores the old behavior; the flag threads through
`generate_report`/`fetch_config`/`run_tui` (the TUI takes it via its
injectable `generate`).

### 4. Tests mirror the real API from now on

`FakeGitLab.get_ci_template` now 404s any key containing a slash or a
suffix, exactly like real GitLab — the old fake accepted `Security/SAST`,
which is how this bug shipped. New tests cover the key-candidate order,
a category template served under its flattened key, `Jobs/*` falling back
to the bundled snapshot (asserting on stable facts of the real content:
`Jobs/Build` defines `build`, `Security/SAST` includes `Jobs/SAST`),
recursion in both the fetch layer and the offline parser, the traversal
guard, the opt-out flag on both CLIs, and `_meta.json` consistency.

## As-built notes

- Snapshot pinned at `v19.3.0-ee` (GitLab 19.3.0), 133 templates; wheel
  packaging verified (hatchling includes package data by default).
- The alias display uses a space (`[template] Jobs/Build.gitlab-ci.yml`)
  to match the lint strategy's File Map entries; edges, include-gates and
  what-if keys all use the same string, so nothing downstream splits it.
