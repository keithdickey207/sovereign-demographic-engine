# Sovereign Demographic Engine — Stack Pointer

**This repo:** `sovereign-demographic-engine`  
**Role:** Local-first entity resolution + pattern-learning inference + **custom** spatial sim + debug agent  
**Blueprint link:** `~/projects/SOVEREIGN_EARTH_ENGINE.md`  
**Live status:** `STATUS.md` (written by `bridge/debug_agent.py`)

| Resource | Path |
|----------|------|
| Global stack index | `~/projects/SOVEREIGN_EARTH_ENGINE.md` |
| Entity schema | `cypher/01_schema.cypher` |
| Resolution | `layer2_ingest/resolve_entities.py` |
| Inference + patterns | `layer3_inference/decision_daemon.py` |
| Pattern memory | `bridge/pattern_memory.py` → `state/pattern_memory.json` |
| Debug agent | `bridge/debug_agent.py` → `state/debug_report.json` |
| Spatial (custom, no third-party game engines) | `layer4_spatial/spatial_engine.py` |
| Prototype runner | `scripts/prototype.py` |
| Private viewport | `layer4_spatial/private_viewport.py` |

**Explicit non-deps:** the spatial engine · Unity · Unreal · cloud graph SaaS · cloud LLM APIs

**Prototype:** `python scripts/prototype.py` or `./scripts/run_local.sh proto`  
