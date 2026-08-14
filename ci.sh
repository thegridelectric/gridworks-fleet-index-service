#!/bin/bash
# Run locally everything CI runs, so a push won't go red. Usage: ./ci.sh
#
# The db tests arrive with the FIS schema (build step 2) and bring a
# testcontainers Postgres with them; a docker precondition check belongs
# here from that commit on, not before.
set -euo pipefail

step() { printf '\n=== %s ===\n' "$1"; shift; "$@"; }

# Resolve the env exactly as CI does.
step "uv sync (locked)" uv sync --all-groups --locked

# lint
step "ruff check" uv run ruff check --no-fix .
step "ruff format --check" uv run ruff format --check .

step "tests" uv run pytest -q

printf '\nAll CI checks passed.\n'
