#!/bin/bash
# Run locally everything CI runs, so a push won't go red. Usage: ./ci.sh
#
# The db tests bring a testcontainers Postgres with them, so docker must
# be running for the suite to be meaningful (without it they skip).
set -euo pipefail

step() { printf '\n=== %s ===\n' "$1"; shift; "$@"; }

# Resolve the env exactly as CI does.
step "uv sync (locked)" uv sync --all-groups --locked

# lint
step "ruff check" uv run ruff check --no-fix .
step "ruff format --check" uv run ruff format --check .

step "tests" uv run pytest -q

printf '\nAll CI checks passed.\n'
