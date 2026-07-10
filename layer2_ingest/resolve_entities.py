#!/usr/bin/env python3
"""
Ingestion & Resolution Layer: Python + Splink (DuckDB, local).

Ingests raw CSV ethnosurvey data, probabilistic record linkage for
deduplication of human agents, then structures rows for the graph.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd
from splink.duckdb.linker import DuckDBLinker
from splink.duckdb.blocking_rule_library import block_on
import splink.duckdb.comparison_library as cl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bridge.graph_store import GraphBackend  # noqa: E402


def load_frame(csv_path: Path | None) -> pd.DataFrame:
    if csv_path and csv_path.exists():
        df = pd.read_csv(csv_path, dtype={"unique_id": str})
        return df

    # Built-in demo rows (same as production seed sample)
    return pd.DataFrame(
        {
            "unique_id": ["1", "2", "3"],
            "first_name": ["Jose", "Joseph", "Maria"],
            "last_name": ["Rojas", "Rojas", "Garcia"],
            "dob": ["1980-05-12", "1980-05-12", "1992-11-24"],
            "birth_location": ["Zacatecas", "Zacatecas", "Chihuahua"],
            "base_wealth": [1500, 1450, 2200],
            "risk_tolerance": [0.85, 0.80, 0.55],
            "network_size": [4, 3, 1],
            "origin_jobs": [12, 12, 18],
            "dest_jobs": [85, 85, 70],
        }
    )


def resolve(df: pd.DataFrame) -> pd.DataFrame:
    settings = {
        "link_type": "dedupe_only",
        "blocking_rules_to_generate_predictions": [
            block_on("last_name"),
            block_on("dob"),
        ],
        "comparisons": [
            cl.jaro_winkler_at_thresholds("first_name", [0.9, 0.7]),
            cl.exact_match("last_name"),
            cl.exact_match("dob"),
            cl.levenshtein_at_thresholds("birth_location", [2]),
        ],
        "retain_matching_columns": True,
        "retain_intermediate_calculation_columns": False,
    }

    linker = DuckDBLinker(df, settings)
    linker.estimate_u_using_random_sampling(max_pairs=1e6)
    # Small demo frames can fail EM if blocks are too sparse — fall back gracefully
    try:
        # Splink 3.x uses British spelling: maximisation
        linker.estimate_parameters_using_expectation_maximisation(block_on("dob"))
    except Exception as exc:
        print(f"[resolve] EM skipped ({exc}); using u-only estimates")

    df_predict = linker.predict(threshold_match_probability=0.85)
    resolved_clusters = linker.cluster_pairwise_predictions_at_threshold(
        df_predict, 0.85
    )
    return resolved_clusters.as_pandas_dataframe()


def clusters_to_persons(clean: pd.DataFrame, raw: pd.DataFrame) -> list[dict]:
    """Collapse Splink clusters into single Person records for the graph."""
    # Prefer raw attributes; attach cluster_id from clean
    raw = raw.copy()
    raw["unique_id"] = raw["unique_id"].astype(str)

    if "cluster_id" not in clean.columns:
        # No pairs clustered — treat each row as its own entity
        clean = raw.copy()
        clean["cluster_id"] = clean["unique_id"]

    id_col = "unique_id" if "unique_id" in clean.columns else clean.columns[0]
    cluster_map = (
        clean[[id_col, "cluster_id"]]
        .drop_duplicates(subset=[id_col])
        .set_index(id_col)["cluster_id"]
        .to_dict()
    )
    raw["cluster_id"] = raw["unique_id"].map(cluster_map).fillna(raw["unique_id"])

    persons: list[dict] = []
    used_ids: set[str] = set()
    jose_seed_assigned = False
    for cluster_id, group in raw.groupby("cluster_id", sort=True):
        # Canonical: first row by unique_id
        row = group.sort_values("unique_id").iloc[0]
        first = str(row.get("first_name", ""))
        last = str(row.get("last_name", ""))
        agent_id = f"agent_cluster_{cluster_id}"
        # Preserve seeded Jose_Rojas identity once for demo continuity
        if (
            not jose_seed_assigned
            and first.lower().startswith("jos")
            and last.lower() == "rojas"
        ):
            agent_id = "agent_8472"
            jose_seed_assigned = True

        # Guarantee unique agent ids even if Splink under-merges
        base_id = agent_id
        n = 2
        while agent_id in used_ids:
            agent_id = f"{base_id}_{n}"
            n += 1
        used_ids.add(agent_id)

        # If cluster has multiple members, prefer higher wealth as canonical capital
        wealth = float(group["base_wealth"].max()) if "base_wealth" in group else 1500.0
        risk = float(group["risk_tolerance"].mean()) if "risk_tolerance" in group else 0.75
        network = int(group["network_size"].max()) if "network_size" in group else 0

        persons.append(
            {
                "id": agent_id,
                "name": f"{first}_{last}".strip("_") or agent_id,
                "first_name": first,
                "last_name": last,
                "dob": str(row.get("dob", "")),
                "birth_location": str(row.get("birth_location", "")),
                "base_wealth": float(wealth or 1500),
                "risk_tolerance": float(risk or 0.75),
                "network_size": int(network or 0),
                "origin_jobs": float(row.get("origin_jobs", 10) or 10),
                "dest_jobs": float(row.get("dest_jobs", 50) or 50),
                "border_friction": 0.6,
                "cluster_id": str(cluster_id),
                "has_migrated": False,
                "member_count": int(len(group)),
            }
        )
    return persons


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Splink entity resolution")
    parser.add_argument(
        "--csv",
        type=Path,
        default=ROOT / "data" / "sample_ethnosurvey.csv",
        help="Path to ethnosurvey CSV",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        help="Resolve only; do not upsert into graph store",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "state" / "resolved_persons.json",
        help="Where to write resolved persons JSON",
    )
    args = parser.parse_args()

    raw = load_frame(args.csv)
    print(f"[resolve] loaded {len(raw)} raw records")

    clean = resolve(raw)
    cols = [c for c in ("cluster_id", "first_name", "last_name", "unique_id") if c in clean.columns]
    print(clean[cols].to_string(index=False) if cols else clean.head())

    persons = clusters_to_persons(clean, raw)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(persons, indent=2), encoding="utf-8")
    print(f"[resolve] wrote {len(persons)} entities → {args.out}")

    if not args.no_graph:
        backend = GraphBackend()
        try:
            n = backend.upsert_persons(persons)
            print(f"[resolve] upserted {n} Person nodes via {backend.backend}")
        finally:
            backend.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
