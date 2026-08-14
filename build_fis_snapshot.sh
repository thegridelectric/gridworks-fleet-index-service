#!/usr/bin/env bash
# (re)build the FIS Sema snapshot from fis_seed_request.yaml (homed here in the
# consumer repo) and copy it into src/fis/sema/. Drives the generator in the
# sibling sema repo; the only sema-side write is its gitignored output/ scratch.
# Local (Python) class names come from the seed request's local_names rule.
#
# --allow-staged: the closure includes staging words, so this is a DEV-ONLY
# snapshot (marked in indexes/staging.yaml and by a README banner in the
# vendored tree). That is correct for now — the build runs on d1 first. Drop
# the flag once every word in the seed is published; if prepare then refuses,
# promote the offending word (`sema promote <word> <ver>`) rather than
# keeping the flag to paper over it.
set -euo pipefail

fis_root="$(cd "$(dirname "$0")" && pwd)"
sema_root="$(cd "$fis_root/../sema" && pwd)"
seed="$fis_root/fis_seed_request.yaml"

cd "$sema_root"
uv run sema snapshot prepare --allow-staged "$seed"
uv run sema snapshot build --package-name fis

rsync -a --delete --exclude='__pycache__' --exclude='.DS_Store' \
  output/sema/ "$fis_root/src/fis/sema/"
echo "fis snapshot rebuilt (local names from seed request) + copied to src/fis/sema/."
