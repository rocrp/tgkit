set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Run the test suite
test:
    uv run pytest -q

# Format and auto-fix lint findings
lint:
    uvx ruff format src tests
    uvx ruff check --fix src tests

# CI gate: formatting, lint and tests must all pass untouched
check:
    uvx ruff format --check src tests
    uvx ruff check src tests
    uv run pytest -q
