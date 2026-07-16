## Install pipeview into the active Python environment
install:
	pip install .

## Install in editable mode with dev dependencies
dev:
	pip install -e ".[dev]"

## Run the test suite
test:
	python -m pytest tests/ -v

## Lint the codebase
lint:
	ruff check .

## Regenerate example reports into examples/out/
examples: install
	pipeview examples/make-project -o examples/out
	pipeview examples/gitlab-project -o examples/out || test $$? -eq 1
	pipeview examples/torture-project -o examples/out/torture || test $$? -eq 1

## Run pipeview on this repo's own Makefile
self: install
	pipeview Makefile -o examples/out

.PHONY: install dev test lint examples self
