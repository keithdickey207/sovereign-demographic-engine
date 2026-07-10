# Sovereign Demographic Engine

**Local-first · air-gap capable · pattern-learning · self-debugging**

Four-layer demographic / migration simulation. Entity resolution, graph storage,
behavioral inference, spatial kinematics, **online pattern memory**, and a
**debug agent** all run on your machine.

**Spatial layer is a custom engine (`sovereign-spatial`). We do not use Godot,
Unity, Unreal, or any commercial game stack.**

## One-command prototype

```bash
cd ~/projects/sovereign-demographic-engine
source .venv/bin/activate

# Clean corridor burn-in + learning + debug report
python scripts/prototype.py

# Or via shell helper
./scripts/run_local.sh proto

# Stack learning: 3 corridor episodes (policy evolves)
python scripts/prototype.py --episodes 3
```

## Global trafficking RISK scan (known high-involvement focus)

Prevention / research screen — **not** proof of crime, **not** how-to guidance.

```bash
cd ~/projects/sovereign-demographic-engine
source .venv/bin/activate

# Rank publicly tagged high-involvement hotspots worldwide
python scripts/risk_scan.py --hotspots
python scripts/risk_scan.py --hotspots --kind sex
python scripts/risk_scan.py --hotspots --kind labor

# Scan a place
python scripts/risk_scan.py --area "Tenancingo"
python scripts/risk_scan.py --area "Benin City"
python scripts/risk_scan.py --area "El Paso"
python scripts/risk_scan.py --lat 13.75 --lon 100.50

# List coverage
python scripts/risk_scan.py --list
```

Reports: `state/RISK_REPORT.md`, `state/risk_report.json`  
Data: `data/global_nodes.json`, `data/global_corridors.json`

## Train watch agents (people / groups)

Protection-oriented agents learn profiles for vulnerable people, recruitment
clusters, and demand/network patterns — for triage, not stalking.

```bash
# Train + scan
python scripts/train_watch_agents.py --train --scan

# Only train / only scan
python scripts/train_watch_agents.py --train --epochs 15
python scripts/train_watch_agents.py --scan

# List what agents look for
python scripts/train_watch_agents.py --list
```

Outputs: `state/watch_agents.json`, `state/watch_hits.json`, `state/WATCH_REPORT.md`

## God Mode UI (desktop + Meta Quest)

Live **reality picture** from buildings + WiFi heat + printers + motion + Quest pose + spatial agents.

```bash
cd ~/projects/sovereign-demographic-engine
source .venv/bin/activate
python scripts/run_godmode.py
# or: ./scripts/run_local.sh godmode
```

- **Desktop:** open `http://127.0.0.1:8771/`
- **Meta Quest:** same Wi‑Fi → Quest Browser → `http://<your-pc-lan-ip>:8771/` → **ENTER VR / GOD MODE**

### Live sensor ingest (real devices)

```bash
# WiFi scan payload (phone/ESP/pi)
curl -X POST http://127.0.0.1:8771/api/ingest/wifi \
  -H 'Content-Type: application/json' \
  -d '{"aps":[{"ssid":"Office","signal":80,"bssid":"aa:bb:cc:dd:ee:ff"}]}'

# Printer activity
curl -X POST http://127.0.0.1:8771/api/ingest/printer \
  -H 'Content-Type: application/json' \
  -d '{"name":"OPS-MFP-01","jobs":3,"activity":0.9}'

# Motion / PIR
curl -X POST http://127.0.0.1:8771/api/ingest/motion \
  -H 'Content-Type: application/json' \
  -d '{"sensor_id":"motion_gate","active":true,"x":5,"y":95}'

# Quest pose is posted automatically from the WebXR page
```

| Piece | Path |
|-------|------|
| UI + WebXR | `layer5_godmode/static/godmode.html` |
| Sensor hub | `layer5_godmode/sensor_hub.py` |
| Buildings | `data/buildings.json` |
| Live field | `state/reality_field.json` |

## What you get

| Piece | Path | Role |
|-------|------|------|
| 1 Entity | `cypher/`, `layer1_entity/` | Neo4j schema *or* local JSON graph |
| 2 Ingest | `layer2_ingest/` | Splink + DuckDB linkage |
| 3 Inference | `layer3_inference/` | Pattern memory + optional Ollama |
| 4 Spatial | `layer4_spatial/` | Custom gravity + capital pulses |
| Learn | `bridge/pattern_memory.py` | Online weights / remittance policy |
| Debug | `bridge/debug_agent.py` | Audit, auto-fix, STATUS.md |

### Agent state machine

```
STAY → MIGRATING → REMITTING → SETTLED
```

### Pattern learning

Each completed agent episode updates:

- **Gravity weights** (α jobs, β network, γ friction, δ wealth)
- **Remittance fraction** (how much capital returns home per pulse)
- **Migrate threshold / risk bias**
- **Feature-bucket action rates** (wealth × network × pull × friction)

State file: `state/pattern_memory.json` (persists across runs).

### Debug agent

```bash
# Audit only
python bridge/debug_agent.py

# Repair drift (edge bloat, wealth ratchet, bad resolved ids) + STATUS.md
python bridge/debug_agent.py --fix --sync-docs

# Continuous watch (every 30s)
python bridge/debug_agent.py --fix --sync-docs --watch 30
# or: ./scripts/run_local.sh watch 30
```

Writes:

- `state/debug_report.json` — machine-readable findings
- `STATUS.md` — live human status

## Layers in detail

```bash
# Schema (Neo4j if up, else state/local_graph.json)
python layer1_entity/apply_schema.py

# Resolve demo ethnosurvey
python layer2_ingest/resolve_entities.py

# Inference (offline pattern memory; add Ollama with default flags)
python layer3_inference/decision_daemon.py --once --no-api --no-ollama

# Spatial prototype (reset + clean origin + force-migrate + until-settled)
python layer4_spatial/spatial_engine.py --proto --no-api

# Optional private ASCII viewport (separate terminal, engine with API)
python layer4_spatial/spatial_engine.py --proto
python layer4_spatial/private_viewport.py
```

Optional Neo4j (still local Docker):

```bash
docker compose up -d
export NEO4J_PASSWORD=sovereign_local
python layer1_entity/apply_schema.py
```

## Defaults on this host

| Service | Endpoint / path |
|---------|-----------------|
| Ollama (optional) | `http://localhost:11434` model `llama3.2` |
| Inference API | `http://127.0.0.1:8767` |
| Spatial API | `http://127.0.0.1:8768` |
| Graph fallback | `state/local_graph.json` |
| Pattern memory | `state/pattern_memory.json` |
| Debug report | `state/debug_report.json` |
| Live status | `STATUS.md` |

## Stack principles

- **No cloud LLM** — Ollama optional; pattern memory always on
- **No SaaS graph** — Neo4j via Docker *or* local JSON
- **No game-engine vendor** — `spatial_engine.py` only
- **Self-healing** — debug agent repairs common demo drift
- **Evolving policy** — remittance + migration params learn from episodes

## License

MIT — WSDS / 04901 Studio
