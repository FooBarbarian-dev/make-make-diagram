# Changelog

Releases of the VS Code extension are cut by release-please from
Conventional Commits touching `editors/vscode/`, tagged `vscode-vX.Y.Z`,
with the packaged `.vsix` attached to the GitHub Release.

## [0.2.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/vscode-v0.1.1...vscode-v0.2.0) (2026-09-02)


### Features

* **vscode:** host pipeview lsp — diagnostics, hover, links, report action ([a80d82c](https://github.com/FooBarbarian-dev/make-make-diagram/commit/a80d82cb44413faacdbaf7aca2b67de7f0703891))

## [0.1.1](https://github.com/FooBarbarian-dev/make-make-diagram/compare/vscode-v0.1.0...vscode-v0.1.1) (2026-09-02)


### Bug Fixes

* **vscode:** work on Windows — py launcher, batch wrappers, UTF-8, shell-free auth ([7a27376](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7a27376afccf2c8c569d1f8ee1e66ae61f06e601))

## [0.1.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/vscode-v0.0.1...vscode-v0.1.0) (2026-09-01)


### Features

* editor extensions (VS Code, Zed), upstream include resolution, and pipeview lsp ([7939b68](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7939b6885d1d289ab4206ef8b9d87007690c7d04))
* **vscode:** GitHub remote report, sync, auth, and token commands ([684ae5e](https://github.com/FooBarbarian-dev/make-make-diagram/commit/684ae5ef281b4965332c02ce5caa3352a35aa2f4))
* **vscode:** ship LICENSE and changelog for automated releases ([347c5bf](https://github.com/FooBarbarian-dev/make-make-diagram/commit/347c5bfcc3292d320e44965a3e2103c3a86bedda))

## 0.1.0 (unreleased seed)

Initial extension: **Pipeline Report for This Repo** (analyzes the open
repository with `--upstream` on by default, report rendered in a webview
panel), per-file reports via context menus, regenerate-last,
`gitlab report`/`sync` flows with the rollup opened when produced,
GitLab token storage in VS Code secrets (passed as
`PIPEVIEW_GITLAB_TOKEN`), an integrated-terminal flow for the
interactive `pipeview gitlab auth`, and a Pipeview output channel with
the CLI's full output. Disabled in untrusted workspaces.
