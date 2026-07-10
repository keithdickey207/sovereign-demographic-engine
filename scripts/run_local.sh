#!/usr/bin/env bash
# Boot demographic stack fully local — custom spatial engine only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY=python3

MODE="${1:-proto}"
shift || true

case "$MODE" in
  proto|prototype)
    exec "$PY" scripts/prototype.py "$@"
    ;;
  results|show)
    exec "$PY" scripts/show_results.py --html "$@"
    ;;
  risk|hotspots)
    if [[ "$MODE" == "hotspots" ]]; then
      exec "$PY" scripts/risk_scan.py --hotspots "$@"
    fi
    exec "$PY" scripts/risk_scan.py "$@"
    ;;
  watch|train-watch)
    exec "$PY" scripts/train_watch_agents.py --train --scan "$@"
    ;;
  godmode|quest|ui)
    exec "$PY" scripts/run_godmode.py "$@"
    ;;
  debug)
    exec "$PY" bridge/debug_agent.py --fix --sync-docs "$@"
    ;;
  watch)
    exec "$PY" bridge/debug_agent.py --fix --sync-docs --watch "${1:-30}"
    ;;
  spatial)
    exec "$PY" layer4_spatial/spatial_engine.py --proto --no-api --quiet "$@"
    ;;
  full)
    echo "[run] entity schema"
    "$PY" layer1_entity/apply_schema.py
    echo "[run] resolve entities"
    "$PY" layer2_ingest/resolve_entities.py
    echo "[run] debug agent"
    "$PY" bridge/debug_agent.py --fix --sync-docs || true
    echo "[run] inference tick (pattern memory; Ollama optional)"
    "$PY" layer3_inference/decision_daemon.py --once --no-api --no-ollama
    echo "[run] custom spatial engine"
    exec "$PY" layer4_spatial/spatial_engine.py --proto --no-api
    ;;
  *)
    echo "Usage: $0 {proto|debug|watch|spatial|full} [args...]"
    exit 2
    ;;
esac
