# pipeview examples

Runnable demos that exercise pipeview's interesting behavior. Each produces a
self-contained HTML report you can open in any browser — no server, no network.

## make-project

A small multi-directory C project with a root Makefile, an included
`config.mk`, a pattern rule (`%.o: %.c`), an order-only prerequisite
(`builddir`), `##` docstrings on phony targets, and `$(MAKE) -C sub/`
recursion into a subdirectory.

```bash
pipeview examples/make-project -o examples/out
open examples/out/Makefile.report.html
```

What to look for in the report:

- **Dependency Graph**: the DAG shows normal prerequisite edges, the
  dashed order-only edge to `builddir`, the pattern rule node, and the
  recursive-make invocation into `sub/`.
- **Task Catalog**: `build`, `test`, `clean`, `deploy`, and `sub` appear
  with their `##` descriptions and `make <target>` invocation commands.
- **Variable Explorer**: `CC`, `CFLAGS`, `LDFLAGS`, `TARGET`, and
  `DEPLOY_HOST` show up with event timelines — defined in `config.mk`,
  some overridden in the root Makefile.
- **File Map**: the include tree (`Makefile` → `config.mk`) and the
  recursive-make link to `sub/Makefile`.

## gitlab-project

A GitLab CI pipeline with four stages, a `needs:` DAG that lets `lint`
and `unit_tests` skip ahead of `integration_tests`, an `extends:` chain
through `.base` → `.docker_base`, a local include (`ci/deploy.yml`), and
a `when: manual` production gate.

The pipeline also references `project: 'devops/shared-templates'` and
`extends: .notify_on_deploy` (defined in the local include). The remote
project include is unresolvable — pipeview shows it as a ghost node with
a diagnostic, demonstrating graceful degradation.

```bash
pipeview examples/gitlab-project -o examples/out
open examples/out/gitlab-ci.report.html
```

What to look for in the report:

- **Dependency Graph**: the `needs:` DAG differs from stage ordering —
  `unit_tests` and `lint` both need only `build_wheel`, while
  `integration_tests` needs `build_image`. Ghost nodes appear dashed.
- **Task Catalog**: `deploy_production` shows the `manual` flag.
- **Variable Explorer**: variables at global, template, and job scopes
  with an event timeline showing inheritance through `extends:`.
- **File Map**: the local include resolves; the `project:` include shows
  as unresolved with a diagnostic.

## gitlab-whatif-project

A GitLab CI pipeline that deliberately contains the classic
duplicate-pipeline problem: a no-rules job (branch pipelines by default),
a job whose final rule is a bare `when: on_success` catch-all, and a job
with explicit rules for both branch and MR pipelines — plus a dotenv
artifact chain and a child pipeline.

```bash
pipeview examples/gitlab-whatif-project -o examples/out/whatif
open examples/out/whatif/gitlab-ci.report.html
```

What to look for — open the **What-If** tab:

- Pick **Push to a branch** with **an open MR uses this branch as
  source**: two pipeline sections render side by side, with `lint`,
  `integration_tests` (and friends) badged as duplicates. That single
  push starts both pipelines on real GitLab too.
- Uncomment the `workflow:` block at the top of the example's
  `.gitlab-ci.yml` (the documented dedup pattern), regenerate, and the
  branch pipeline collapses to "not created" while the MR is open.
- Switch to **Push to a branch → main** and set exact changed files:
  `deploy_production` flips between *runs*, *not added*, and *depends*
  based on whether `src/**/*` matched.
- `build` publishes a dotenv report — the banner shows which jobs'
  runtime environments it extends, and why that can never affect rules.
- `docs_pipeline` spawns a child pipeline where
  `CI_PIPELINE_SOURCE=parent_pipeline` — the `publish_docs` rule matches,
  and a `merge_request_event` rule never would.

## torture-project

A deliberately hostile Makefile that stress-tests the report UI's overflow
policy: a 200-character unbroken variable value, a 150-character include
path (which is also unresolvable, exercising the diagnostics list), a
300-character one-line recipe, and a very long target name.

```bash
pipeview examples/torture-project -o examples/out/torture
open examples/out/torture/Makefile.report.html
```

What to look for in the report: nothing. Nothing should escape its
container at any viewport width from 1280px up — long values truncate with
the full text available on hover and in the detail panel, code blocks
scroll horizontally inside themselves, and paths middle-truncate keeping
the tail.
