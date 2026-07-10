# Sovereign Demographic Engine — Live Status

_Updated by debug agent at `2026-07-10T05:18:54Z`_

**Health:** `OK`

## Finding counts

| Severity | Count |
|----------|------:|
| critical | 0 |
| error | 0 |
| warn | 0 |
| info | 1 |

## Findings

- **info** `all_migrated`: All agents marked has_migrated — next force-migrate should auto-reset

## Pattern memory

- `episodes=96 buckets=5`
- `weights α=1.720 β=1.776 γ=1.616 δ=0.500`
- `policy remit_frac=0.210 migrate_thr=0.550 risk_bias=-0.060`
- `metrics yield=0.544 migrate_ok=0.954 inflow_total=58651.8`

## Quick commands

```bash
# Full offline prototype (clean economy + learn + settle)
python scripts/prototype.py

# Debug agent
python bridge/debug_agent.py --fix --sync-docs

# Spatial corridor burn-in
python layer4_spatial/spatial_engine.py --proto --no-api
```

## Stack rules

- Custom spatial only (`sovereign-spatial`) — no Godot/Unity/Unreal
- Pattern memory in `state/pattern_memory.json` evolves across runs
- Debug report: `state/debug_report.json`
