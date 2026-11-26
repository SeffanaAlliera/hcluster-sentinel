#!/usr/bin/env bash
# Runs every automated test in the repo.
set -euo pipefail
cd "$(dirname "$0")/.."
echo "== SQL (real migrations in PGlite) =="; (cd supabase/tests && npm ci --silent && node run.mjs | tail -3 && node upgrade.mjs | tail -2)
echo "== Agent (python unittest) =="; (cd agent && python3 -W ignore -I -m unittest discover -s tests 2>&1 | tail -4)
echo "== Web typecheck + unit + end-to-end (real route + SQL + Python agent) =="; (cd web && npm ci --silent && npx tsc --noEmit && npx vitest run 2>&1 | tail -8)
