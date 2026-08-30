# Changelog

Releases of the VS Code extension are cut by release-please from
Conventional Commits touching `editors/vscode/`, tagged `vscode-vX.Y.Z`,
with the packaged `.vsix` attached to the GitHub Release.

## 0.1.0 (unreleased seed)

Initial extension: **Pipeline Report for This Repo** (analyzes the open
repository with `--upstream` on by default, report rendered in a webview
panel), per-file reports via context menus, regenerate-last,
`gitlab report`/`sync` flows with the rollup opened when produced,
GitLab token storage in VS Code secrets (passed as
`PIPEVIEW_GITLAB_TOKEN`), an integrated-terminal flow for the
interactive `pipeview gitlab auth`, and a Pipeview output channel with
the CLI's full output. Disabled in untrusted workspaces.
