set shell := ["bash", "-euo", "pipefail", "-c"]

default:
    @just --list

# Run the test suite
test:
    uv run pytest -q

# Format and auto-fix lint findings
lint:
    uv run ruff format src tests
    uv run ruff check --fix src tests

# CI gate: formatting, lint and tests must all pass untouched
check:
    uv run ruff format --check src tests
    uv run ruff check src tests
    uv run pytest -q
