## Install pipeview into the active Python environment
install:
	pip install .

## Install in editable mode with dev dependencies
dev:
	pip install -e ".[dev]"

## Run the test suite
test:
	python -m pytest tests/ -v

## Build sdist + wheel into dist/
build:
	python -m build

## Lint the codebase
lint:
	ruff check .

## Regenerate example reports into examples/out/
examples: install
	pipeview examples/make-project -o examples/out
	pipeview examples/gitlab-project -o examples/out || test $$? -eq 1
	pipeview examples/gitlab-whatif-project -o examples/out/whatif || test $$? -eq 1
	pipeview examples/github-project -o examples/out/github || test $$? -eq 1
	pipeview examples/torture-project -o examples/out/torture || test $$? -eq 1

## Run pipeview on this repo's own Makefile
self: install
	pipeview Makefile -o examples/out

## Build and unit-test the VS Code extension (needs node + npm)
vscode:
	cd editors/vscode && npm install && npm test

## Build the Zed extension (needs rust + the wasm32-wasip2 target)
zed:
	cd editors/zed && cargo build --release --target wasm32-wasip2

## Build + package the Zed extension as Zed installs it (editors/zed/dist/)
zed-package:
	cd editors/zed && scripts/package.sh

.PHONY: install dev test build lint examples self vscode zed zed-package
