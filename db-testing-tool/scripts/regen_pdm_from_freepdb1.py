"""Thin CLI wrapper around the in-app PDM regenerator.

Operator-locked architecture (2026-05-30 Phase 7.15):
    The bulk-extract logic now lives in the application service
    (`app.services.schema_kb_service.oracle_bulk_extract_schema`) so
    the GUI Schema Browser -> Generate PDM flow benefits automatically.
    This script is a 50-line convenience wrapper for terminal use:
    it builds an in-memory DataSource row, calls `build_pdm_catalog`,
    and writes the result to `data/local_kb/schema_kb_ds_99.json` --
    matching the file shape the comparator + emitter expect.

Run:
    python scripts/regen_pdm_from_freepdb1.py

Output:
    data/local_kb/schema_kb_ds_99.json
    Per-schema progress + spot-check on critical AVY_FACT_SIDE tables.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models.datasource import DataSource  # noqa: E402
from app.services.schema_kb_service import build_pdm_catalog  # noqa: E402

KB_PATH = ROOT / "data" / "local_kb" / "schema_kb_ds_99.json"
DS_ID = 99


def _build_inmemory_ds() -> DataSource:
    """Build an unsaved DataSource row pointed at FREEPDB1 with SYSDBA
    credentials.  Not persisted to the SQLite metadata DB -- only used
    for this one extraction.
    """
    ds = DataSource(
        id=DS_ID,
        name="FREEPDB1 (live regenerated via Phase 7.15)",
        db_type="oracle",
        host="localhost",
        port=1521,
        database_name="FREEPDB1",
        username="sys",
        password="123456",
        extra_params=json.dumps({"mode": "SYSDBA", "service_name": "FREEPDB1"}),
    )
    return ds


def main() -> int:
    print("=== Regen PDM from live FREEPDB1 (via in-app service) ===\n")
    t0 = time.perf_counter()
    ds = _build_inmemory_ds()
    pdm = build_pdm_catalog(ds, selected_schemas=None, operation_id=None)
    elapsed = time.perf_counter() - t0

    n_schemas = len(pdm.get("schemas") or [])
    n_tables = sum(len(s.get("tables") or []) for s in pdm.get("schemas") or [])
    n_fks = len(pdm.get("relationships") or [])
    print(f"\nDone in {elapsed:.1f}s -- {n_schemas} schemas / "
          f"{n_tables} tables / {n_fks} FK relationships")

    payload = {
        "datasource_id": DS_ID,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdm": pdm,
        "ldm": {},
    }
    KB_PATH.parent.mkdir(parents=True, exist_ok=True)
    KB_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    size_mb = KB_PATH.stat().st_size / (1024 * 1024)
    print(f"\nWrote {KB_PATH} ({size_mb:.2f} MB)")

    # Spot-check critical tables for AVY_FACT_SIDE emitter.
    print("\nSpot-check critical tables:")
    critical = [
        ("CCAL_REPL_OWNER", "TXN"),
        ("CCAL_REPL_OWNER", "APA"),
        ("CCAL_REPL_OWNER", "FIP"),
        ("CCAL_REPL_OWNER", "CL_VAL"),
        ("CCSI_OWNER", "AR_DIM"),
        ("TRANSACTIONS_OWNER", "AVY_FACT"),
    ]
    table_index = {(s["schema"].upper(), t["name"].upper()): t
                   for s in (pdm.get("schemas") or [])
                   for t in (s.get("tables") or [])}
    for sch, tbl in critical:
        t = table_index.get((sch, tbl))
        if t:
            print(f"  OK  {sch}.{tbl}: {len(t.get('columns') or [])} cols, "
                  f"{len(t.get('primary_keys') or [])} PKs, "
                  f"{len(t.get('foreign_keys') or [])} FKs")
        else:
            print(f"  MISS {sch}.{tbl} -- not in FREEPDB1")

    return 0


if __name__ == "__main__":
    sys.exit(main())
