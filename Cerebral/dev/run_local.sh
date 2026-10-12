#!/usr/bin/env bash
# run_local.sh -- start Cerebral on http://localhost:8501 using the fake data.
#
#   bash dev/run_local.sh              # build fixture if missing, install it, start the app
#   PORT=8600 bash dev/run_local.sh    # different port
#   bash dev/run_local.sh --rebuild    # regenerate the fixture first
#   bash dev/run_local.sh --restore    # put back a real cerebral_dash.duckdb that was set aside
#
# The fixture is copied to Cerebral/cerebral_dash.duckdb, which is where the app
# looks first. That path is git-ignored (*.duckdb), so nothing shows up in git status.
# If a different cerebral_dash.duckdb is already there it is moved to
# cerebral_dash.duckdb.real-backup first, never overwritten.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$HERE/.." && pwd)"
FIXTURE="$HERE/fixture/cerebral_dash.duckdb"
TARGET="$APP_DIR/cerebral_dash.duckdb"
BACKUP="$TARGET.real-backup"
PORT="${PORT:-8501}"
PY="${PYTHON:-python}"

if [[ "${1:-}" == "--restore" ]]; then
  if [[ -f "$BACKUP" ]]; then mv -f "$BACKUP" "$TARGET"; echo "Restored $TARGET"; else rm -f "$TARGET"; echo "No backup; removed fixture copy."; fi
  exit 0
fi

if [[ "${1:-}" == "--rebuild" || ! -f "$FIXTURE" ]]; then
  echo "Building fixture..."
  "$PY" "$HERE/make_fixture.py"
fi

if [[ -f "$TARGET" ]] && ! cmp -s "$TARGET" "$FIXTURE"; then
  if [[ -f "$BACKUP" ]]; then
    echo "Replacing an older fixture copy at $TARGET"
  else
    mv "$TARGET" "$BACKUP"; echo "Existing data file set aside as $BACKUP"
  fi
fi
cp -f "$FIXTURE" "$TARGET"
echo "Fixture installed at $TARGET"

cd "$APP_DIR"
echo "Starting Cerebral at http://localhost:$PORT  (Ctrl-C to stop)"
exec "$PY" -m streamlit run cerebral_public.py \
  --server.headless true --server.port "$PORT" --browser.gatherUsageStats false
