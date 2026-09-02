# Release Pipelines

This project uses `release-please` to automate versioning and releases —
for the core package and for each editor extension, as separate
components of one manifest.

When commits are pushed to the `main` branch, the
`.github/workflows/release.yml` GitHub Actions workflow runs.
`release-please` analyzes the commit history (using Conventional
Commits) and automatically creates or updates a "Release PR" per
component that has releasable changes. Commits are attributed to a
component by the files they touch.

Once a Release PR is approved and merged into `main`, `release-please`
tags the release, updates that component's changelog, and creates a
GitHub Release. A publish job then attaches the component's artifacts.

## Components

| Path | Component | Release type | Tag | Versioned files | Release artifacts |
|---|---|---|---|---|---|
| `.` | pipeview (core + CLI) | python | `vX.Y.Z` | `pyproject.toml`, `pipeview/__init__.py` | sdist + wheel |
| `editors/vscode` | VS Code extension | node | `vscode-vX.Y.Z` | `package.json` (+ lockfile) | packaged `.vsix` |
| `editors/zed` | Zed extension | rust | `zed-vX.Y.Z` | `Cargo.toml`, `extension.toml` (via the `x-release-please-version` annotation) | `pipeview-zed-vX.Y.Z.zip` + `.tar.gz` — the installable extension directory (`extension.toml` beside `extension.wasm`, built by `editors/zed/scripts/package.sh`); install via *Install Dev Extension*, no Rust toolchain |

**Never hand-bump versions** in any of the files above — release-please
owns them all, and per component they must agree (the core has a test
asserting `pyproject.toml` matches `pipeview/__init__.py`; the Zed
extension's `Cargo.toml` and `extension.toml` are both updated by the
release PR).

Scoping commits helps attribution read well (`feat(vscode): …`,
`feat(zed): …`), but the file paths are what actually route a commit to
a component — a commit touching only `editors/zed/` can never bump the
core.

## GitHub Token Permissions

Because `release-please` automates the creation of Pull Requests, the
default `GITHUB_TOKEN` must have the appropriate permissions. In your
repository settings (Settings -> Actions -> General -> Workflow
permissions), ensure that you have checked:
- "Read and write permissions"
- "Allow GitHub Actions to create and approve pull requests"

Alternatively, you can set the `token:` parameter in the
`release-please` step to use a Personal Access Token (PAT) with `repo`
scope if you prefer not to enable this repository-wide setting.
