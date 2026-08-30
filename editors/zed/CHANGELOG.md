# Changelog

Releases of the Zed extension are cut by release-please from
Conventional Commits touching `editors/zed/`, tagged `zed-vX.Y.Z`, with
the built `zed_pipeview.wasm` attached to the GitHub Release for
reference (Zed compiles dev extensions itself).

## 0.1.0 (unreleased seed)

Initial extension: wires the `pipeview lsp` language server up for YAML
and Make buffers — inline parser diagnostics on open/save, hover docs
for predefined `CI_*` variables, clickable `include:local` entries, and
code actions that generate the interactive pipeline report (upstream on
by default) and open it in the default browser.
