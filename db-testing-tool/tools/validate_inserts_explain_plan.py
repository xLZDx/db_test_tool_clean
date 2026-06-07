#!/usr/bin/env python3
"""Validate generated control-table INSERTs against a LIVE Oracle via EXPLAIN PLAN.

For each DRD it builds the v5.4 DRD-driven INSERT (the same builder the
control-table panel + /control-table/regenerate-with-corrections use) and runs
``EXPLAIN PLAN FOR <insert>`` against the configured Oracle datasource. EXPLAIN
PLAN performs a full parse + semantic check (every table/column must exist,
syntax must be valid) WITHOUT inserting any data, so:

    PASS  -> the statement is DB-valid (clean insert)
    FAIL  -> a real error (ORA-xxxxx); the message tells whether it is a
             generator bug or a known DRD-vs-physical-schema mismatch.

Operator rule (2026-06-07): the generated inserts should PASS, and only FAIL on
KNOWN mismatches -- this harness is how we tell the difference, repeatably.

Usage:
    python -m tools.validate_inserts_explain_plan            # all 3 taxlot DRDs, datasource id=2
    python -m tools.validate_inserts_explain_plan --ds 2
    python -m tools.validate_inserts_explain_plan --drd "data/taxlot/DRD_Activity_Fact.xlsx" --table AVY_FACT --profile avy

Writes a report to data/insert_db_validation_<UTC>.md (+ .csv twin).
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import gc
import re
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import text

from app.database import sync_engine
from app.connectors.factory import get_connector
from app.models.datasource import DataSource
from app.services.universal_insert_builder_v54 import build_to_dir

_REPO = Path(__file__).resolve().parents[1]

# (label, DRD path relative to repo, target_table override or "", profile)
DEFAULT_DRDS = [
    ("AVY", "data/taxlot/DRD_Activity_Fact.xlsx", "AVY_FACT", "avy"),
    ("CLOSE", "data/taxlot/DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx", "", "auto"),
    ("OPEN", "data/taxlot/DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx", "", "auto"),
]


def build_insert(xlsx_path: Path, target_table: str, profile: str) -> str:
    td = Path(tempfile.mkdtemp(prefix="validate_ins_"))
    try:
        xp = td / "drd.xlsx"
        xp.write_bytes(xlsx_path.read_bytes())
        out = td / "out"
        build_to_dir(xp, None, out, target_schema="", target_table=target_table, profile=profile)
        gen = out / "generated_insert_select_candidate.sql"
        return gen.read_text(encoding="utf-8") if gen.exists() else ""
    finally:
        gc.collect()
        shutil.rmtree(td, ignore_errors=True)


def load_datasource(ds_id: int) -> DataSource:
    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": ds_id}).fetchone()
    if row is None:
        raise SystemExit(f"datasource id={ds_id} not found")
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    return ds


def explain_plan(cur, raw, insert_sql: str) -> tuple[bool, str]:
    try:
        cur.execute("EXPLAIN PLAN SET STATEMENT_ID='validate' FOR " + insert_sql)
        raw.rollback()
        return True, ""
    except Exception as exc:  # noqa: BLE001 -- report any ORA error verbatim
        raw.rollback()
        return False, (str(exc).strip().splitlines() or [type(exc).__name__])[0]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2, help="datasource id (default 2 = FREEPDB1_LOCAL)")
    ap.add_argument("--drd", default="", help="single DRD path (overrides the default set)")
    ap.add_argument("--table", default="", help="target table for --drd")
    ap.add_argument("--profile", default="auto", help="profile for --drd")
    args = ap.parse_args()

    drds = ([("CUSTOM", args.drd, args.table, args.profile)] if args.drd else
            [(lbl, str(_REPO / p), t, pr) for lbl, p, t, pr in DEFAULT_DRDS])

    ds = load_datasource(args.ds)
    conn = get_connector(ds)
    raw = conn._direct_connect()
    cur = raw.cursor()
    print(f"Connected to {conn._dsn()} as {conn.username}\n")

    results = []
    for label, path, ttbl, profile in drds:
        p = Path(path)
        row = {"label": label, "drd": p.name, "target_table": "", "sql_len": 0,
               "verdict": "", "ora_error": ""}
        if not p.exists():
            row["verdict"] = "BUILD_FAIL"
            row["ora_error"] = "DRD file not found"
            results.append(row)
            continue
        try:
            sql = build_insert(p, ttbl, profile)
        except Exception as exc:  # noqa: BLE001
            row["verdict"] = "BUILD_FAIL"
            row["ora_error"] = f"{type(exc).__name__}: {exc}"[:300]
            results.append(row)
            continue
        if "INSERT INTO" not in sql.upper():
            row["verdict"] = "BUILD_FAIL"
            row["ora_error"] = "builder produced no INSERT"
            results.append(row)
            continue
        m = re.search(r"INSERT\s+INTO\s+(\S+)", sql, re.I)
        row["target_table"] = m.group(1) if m else "?"
        row["sql_len"] = len(sql)
        ok, err = explain_plan(cur, raw, sql)
        row["verdict"] = "PASS" if ok else "FAIL"
        row["ora_error"] = err
        results.append(row)
        print(f"[{label}] {row['target_table']} sql_len={row['sql_len']} -> {row['verdict']}"
              + (f"  {err}" if err else ""))

    cur.close()
    raw.close()

    # report (md + csv twin)
    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = _REPO / "data" / f"insert_db_validation_{ts}.md"
    csvp = _REPO / "data" / f"insert_db_validation_{ts}.csv"
    npass = sum(1 for r in results if r["verdict"] == "PASS")
    lines = [f"# INSERT DB validation (EXPLAIN PLAN) — {ts}",
             f"Datasource id={args.ds} ({conn._dsn()}) as {conn.username}",
             f"PASS {npass}/{len(results)}", "",
             "| DRD | target | sql_len | verdict | ORA error |",
             "|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r['label']} ({r['drd']}) | {r['target_table']} | {r['sql_len']} | {r['verdict']} | {r['ora_error']} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csvp.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["label", "drd", "target_table", "sql_len", "verdict", "ora_error"])
        w.writeheader()
        w.writerows(results)
    print(f"\nReport: {md}\n        {csvp}")
    return 0 if npass == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
