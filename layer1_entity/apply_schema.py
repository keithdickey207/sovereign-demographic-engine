#!/usr/bin/env python3
"""Apply Neo4j Cypher schema (or seed local JSON graph fallback)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.graph_store import GraphBackend  # noqa: E402


def main() -> int:
    cypher_path = ROOT / "cypher" / "01_schema.cypher"
    backend = GraphBackend()
    try:
        result = backend.apply_cypher_file(cypher_path)
        print(f"[entity] schema applied via {result['backend']}: {result}")
        persons = backend.list_persons()
        print(f"[entity] Person nodes available: {len(persons)}")
        for p in persons:
            print(f"  - {p.get('id')}: {p.get('name')} wealth={p.get('base_wealth')}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
