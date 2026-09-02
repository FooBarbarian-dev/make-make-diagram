# Changelog

Releases of the Zed extension are cut by release-please from
Conventional Commits touching `editors/zed/`, tagged `zed-vX.Y.Z`, with
the built `zed_pipeview.wasm` attached to the GitHub Release for
reference (Zed compiles dev extensions itself).

## [0.2.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/zed-v0.1.0...zed-v0.2.0) (2026-09-02)


### Features

* **lsp:** announce what pipeview offers when it attaches ([cfcbbd7](https://github.com/FooBarbarian-dev/make-make-diagram/commit/cfcbbd7b9681bf94a33613068f13e234b40c105b))


### Bug Fixes

* **zed:** find Python on Windows, ship an installable extension archive ([59c9632](https://github.com/FooBarbarian-dev/make-make-diagram/commit/59c96325e04f79139c23fc36e45edc31ad24c5bc))

## [0.1.0](https://github.com/FooBarbarian-dev/make-make-diagram/compare/zed-v0.0.1...zed-v0.1.0) (2026-09-01)


### Features

* editor extensions (VS Code, Zed), upstream include resolution, and pipeview lsp ([7939b68](https://github.com/FooBarbarian-dev/make-make-diagram/commit/7939b6885d1d289ab4206ef8b9d87007690c7d04))
* **zed:** ship LICENSE, changelog, and version annotation for automated releases ([1fb53ce](https://github.com/FooBarbarian-dev/make-make-diagram/commit/1fb53ce95a4adbc7f0791e92ec14ac9acf0dee79))

## 0.1.0 (unreleased seed)

Initial extension: wires the `pipeview lsp` language server up for YAML
and Make buffers — inline parser diagnostics on open/save, hover docs
for predefined `CI_*` variables, clickable `include:local` entries, and
code actions that generate the interactive pipeline report (upstream on
by default) and open it in the default browser.
