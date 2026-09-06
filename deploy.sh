#!/bin/bash
set -euo pipefail
#
# Deploy FIS to its box — the one-word "go".
#
# Puts the box on the pushed tip of MAIN (clean checkout, never a dirty
# tree), syncs the locked deps, applies any new migration, restarts the
# service, and health-checks over ssh (FIS binds loopback, so /ping is only
# reachable on the box). Run from anywhere; needs your ssh access to the
# box (per-person key).
#
#   FIS_HOST=hw1-2.electricity.works ./deploy.sh
#
# Deploys main ONLY: merge dev → main and push first — that merge is the
# deploy decision.

HOST="${FIS_HOST:?set FIS_HOST to the broker box FIS runs on}"

echo "→ deploying origin/main to $HOST"
ssh "fis@$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail
cd ~/gridworks-fleet-index-service
git fetch origin
git switch main 2>/dev/null || git switch -c main origin/main
git reset --hard origin/main
~/.local/bin/uv sync --frozen
~/.local/bin/uv run alembic upgrade head
sudo systemctl restart fis-api
REMOTE

sleep 3
echo "→ health checks"
ssh "fis@$HOST" 'curl -fsS http://127.0.0.1:8080/ping >/dev/null && echo "  api: ok"; systemctl is-active fis-api; cd ~/gridworks-fleet-index-service && echo "  running: $(git log --oneline -1)"'
echo "→ deployed."
