#!/usr/bin/env bash
# Run the CI jobs (.github/workflows/ci.yml) locally, on a fresh clone of the current commit, so
# that only committed files count, exactly as on GitHub.
#
#   scripts/ci_local.sh [WORKDIR]            # python (3.12, 3.13), web, e2e
#   GALLERY=1 scripts/ci_local.sh [WORKDIR]  # also the gallery job (needs internet)
#
# WORKDIR (default: $TMPDIR/geoloupe-ci) receives the clone and one Python environment per job;
# delete it afterwards. Needs uv and Node (CI uses Node 24) on PATH, and Playwright's Chromium
# (npx playwright install chromium; set PLAYWRIGHT_BROWSERS_PATH to share one install).
# On shared machines, point UV_CACHE_DIR, UV_PYTHON_INSTALL_DIR and npm_config_cache to a
# project store rather than the home directory.
set -euo pipefail

REPO=$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)
WORK=${1:-${TMPDIR:-/tmp}/geoloupe-ci}
PYTHONS=${PYTHONS:-"3.12 3.13"}
unset VIRTUAL_ENV

step() { printf '\n== %s\n' "$*"; }

rm -rf "$WORK/repo"
mkdir -p "$WORK"
git clone -q "$REPO" "$WORK/repo"
git -C "$WORK/repo" checkout -q "$(git -C "$REPO" rev-parse HEAD)"
cd "$WORK/repo"
echo "commit $(git log --oneline -1)"

for py in $PYTHONS; do
  step "python $py"
  export UV_PROJECT_ENVIRONMENT="$WORK/venv$py"
  uv sync -q --all-extras --python "$py"
  uv run --no-sync ruff check src tests scripts examples
  uv run --no-sync ruff format --check src tests scripts examples
  uv run --no-sync mypy
  uv run --no-sync pytest -q -p no:warnings
done

step "web (node $(node --version))"
(cd frontend && npm ci --no-audit --no-fund >/dev/null && npm run lint && npm test && npm run build)

step "e2e (no extras)"
export UV_PROJECT_ENVIRONMENT="$WORK/venv-e2e"
uv sync -q
rm -rf demo
uv run --no-sync geoloupe demo-data demo --days 200
uv run --no-sync geoloupe serve demo/demo.yaml --port 8765 --open-root demo > "$WORK/e2e.log" 2>&1 &
SERVER=$!
trap 'kill $SERVER 2>/dev/null || true' EXIT
timeout 300 bash -c 'until curl -sf http://127.0.0.1:8765/api/health >/dev/null; do sleep 2; done'
(cd frontend && node e2e/smoke.mjs http://127.0.0.1:8765 "$WORK/shots")
kill $SERVER 2>/dev/null || true

if [ "${GALLERY:-0}" = 1 ]; then
  step "gallery (live public data)"
  last=${PYTHONS##* }
  export UV_PROJECT_ENVIRONMENT="$WORK/venv$last"
  uv run --no-sync geoloupe gallery --port 8766 > "$WORK/gallery.log" 2>&1 &
  SERVER=$!
  trap 'kill $SERVER 2>/dev/null || true' EXIT
  timeout 180 bash -c 'until curl -sf http://127.0.0.1:8766/api/health >/dev/null; do sleep 2; done'
  (cd frontend && node e2e/gallery.mjs http://127.0.0.1:8766 "$WORK/gallery-shots")
fi

step "all CI jobs passed (commit $(git log --oneline -1 | cut -c1-60))"
