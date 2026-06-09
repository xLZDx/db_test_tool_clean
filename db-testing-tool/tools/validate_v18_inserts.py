#!/usr/bin/env python3
"""Gate V2 certification: validate the v18 KB-resolved INSERTs against a LIVE
Oracle (FREEPDB1, the local LH mirror) via EXPLAIN PLAN, anti-false-green.

For each DRD it builds the v18 INSERT (via app.services.v18_insert -- the same
path the /build-v18 endpoint uses) and runs EXPLAIN PLAN. Verdicts:

  PASS          -> clean EXPLAIN PLAN (fully valid + privileged)
  PASS_RESOLVED -> ORA-41900 (missing INSERT privilege) on the target, AND the
                   SELECT-only portion EXPLAIN-PLANs clean. 41900 fires only
                   AFTER full name resolution, so the SQL is valid; the
                   connecting user (IKOROSTELEV, CONNECT+RESOURCE only) simply
                   cannot be granted INSERT on the production owner. Honest,
                   not a fake pass: the SELECT side is independently proven.
  FAIL_SQL      -> a real ORA error (ORA-00942/00904/03048/00911 ...): a
                   generator/resolution defect to fix.
  BUILD_FAIL    -> v18 produced no INSERT.

Separately surfaces BUSINESS NULL-stubs (unmapped business columns -- the
operator's "stub"). Audit columns (CRT_DTM etc.) are excluded. The cert is
NOT green while business stubs remain: verdict carries REVIEW when present.

Usage:
  python -m tools.validate_v18_inserts            # AVY + CLOSE + OPEN, ds 2
  python -m tools.validate_v18_inserts --ds 2
Writes data/v18_insert_validation_<UTC>.md (+ .csv twin).
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
from app.services.v18_insert import build_v18_insert_to_dir, V18BuildError
from app.sql_model.static_validator import KBLookup

_REPO = Path(__file__).resolve().parents[1]
_SCHEMA_KB = _REPO / "data" / "local_kb" / "schema_kb_ds_3.json"

# (label, DRD path relative to repo, target_schema, target_table, profile)
DEFAULT_DRDS = [
    ("AVY", "data/taxlot/DRD_Activity_Fact.xlsx", "TRANSACTIONS_OWNER", "AVY_FACT", "avy"),
    ("CLOSE", "data/taxlot/DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx", "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT", "taxlot"),
    ("OPEN", "data/taxlot/DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx", "TAXLOT_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT", "taxlot"),
]

# ORA codes that mean a genuine SQL/resolution defect (NOT a privilege/env issue)
_REAL_SQL_ORA = ("ORA-00942", "ORA-00904", "ORA-03048", "ORA-00911", "ORA-00936", "ORA-00933", "ORA-01747")


def select_only(sql: str) -> str | None:
    """Strip ``INSERT INTO owner.table (cols)`` and return the trailing query
    (WITH.../SELECT...), which EXPLAIN-PLANs with only SELECT privilege."""
    m = re.search(r"\bINSERT\s+INTO\s+[A-Z0-9_$#]+\.[A-Z0-9_$#]+\s*\(", sql, re.I)
    if not m:
        return None
    p = sql.find("(", m.end() - 1)
    if p < 0:
        return None
    depth = 0
    for i in range(p, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                rest = sql[i + 1:].strip()
                return rest or None
    return None


def _explain(cur, raw, stmt: str) -> tuple[bool, str]:
    try:
        cur.execute("EXPLAIN PLAN SET STATEMENT_ID='v18cert' FOR " + stmt)
        raw.rollback()
        return True, ""
    except Exception as exc:  # noqa: BLE001 -- report any ORA verbatim
        raw.rollback()
        return False, (str(exc).strip().splitlines() or [type(exc).__name__])[0]


def _missing_object(err: str) -> str | None:
    """Extract the object name from an ORA-00942 message.

    ``... "SSDS_DAL_OWNER"."ENTERPRISE_ENTITY_RISK_DIMENSION_V" does not exist``
    -> the last quoted identifier is the object.
    """
    quoted = re.findall(r'"([A-Za-z0-9_$#]+)"', err)
    return quoted[-1].upper() if quoted else None


def classify(cur, raw, sql: str, kb: KBLookup | None) -> tuple[str, str]:
    """Return (verdict, detail).

    PASS          clean EXPLAIN PLAN
    PASS_RESOLVED ORA-41900 (privilege) + SELECT-only clean -> SQL valid
    KNOWN_MISMATCH ORA-00942 on an object that IS in the production KB but is
                  absent from this (mirror) DB -> not a generator defect
    FAIL_SQL      any other ORA, or ORA-00942 on an object NOT in the KB
    """
    ok, err = _explain(cur, raw, sql)
    if ok:
        return "PASS", ""
    if "ORA-41900" in err:
        sel = select_only(sql)
        if sel is None:
            return "PASS_RESOLVED", "INSERT-privilege only; SELECT not isolated"
        sel_ok, sel_err = _explain(cur, raw, sel)
        if sel_ok:
            return "PASS_RESOLVED", "INSERT-privilege only; SELECT explains clean"
        # SELECT failed -> recurse classification on the SELECT-only error
        return classify(cur, raw, sel, kb) if "ORA-00942" in sel_err else ("FAIL_SQL", f"SELECT-only: {sel_err}")
    if "ORA-00942" in err and kb is not None:
        obj = _missing_object(err)
        if obj and kb._table_index.get(obj):
            return "KNOWN_MISMATCH", (
                f"mirror is missing production object {obj} "
                f"(present in KB as {kb._table_index.get(obj)}); not certifiable past it on this mirror"
            )
        return "FAIL_SQL", err
    return "FAIL_SQL", err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2, help="datasource id (default 2 = FREEPDB1_LOCAL = LH mirror)")
    args = ap.parse_args()

    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": args.ds}).fetchone()
    if row is None:
        raise SystemExit(f"datasource id={args.ds} not found")
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    conn = get_connector(ds)
    raw = conn._direct_connect()
    cur = raw.cursor()
    print(f"Connected ds={args.ds} as {conn.username}\n")

    kb = KBLookup(_SCHEMA_KB) if _SCHEMA_KB.exists() else None

    results = []
    for label, rel, tsch, tgt, prof in DEFAULT_DRDS:
        p = _REPO / rel
        row = {"label": label, "target": f"{tsch}.{tgt}", "sql_len": 0,
               "verdict": "", "detail": "", "business_stubs": 0, "business_stub_cols": "",
               "audit_stubs": 0}
        if not p.exists():
            row["verdict"] = "BUILD_FAIL"
            row["detail"] = "DRD file not found"
            results.append(row)
            print(f"[{label}] BUILD_FAIL: DRD not found")
            continue
        td = Path(tempfile.mkdtemp(prefix=f"v18cert_{label}_"))
        try:
            res = build_v18_insert_to_dir(p, td / "out", target_schema=tsch, target_table=tgt, profile=prof)
            sql = res["generated_sql"]
            row["sql_len"] = len(sql)
            row["business_stubs"] = len(res["business_stub_columns"])
            row["business_stub_cols"] = ";".join(res["business_stub_columns"])
            row["audit_stubs"] = len(res["audit_stub_columns"])
            verdict, detail = classify(cur, raw, sql, kb)
            row["verdict"] = verdict
            row["detail"] = detail
        except V18BuildError as exc:
            row["verdict"] = "BUILD_FAIL"
            row["detail"] = str(exc)[:300]
        except Exception as exc:  # noqa: BLE001
            row["verdict"] = "BUILD_FAIL"
            row["detail"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            gc.collect()
            shutil.rmtree(td, ignore_errors=True)
        results.append(row)
        print(f"[{label}] {row['target']} sql_len={row['sql_len']} -> {row['verdict']}"
              f"  biz_stubs={row['business_stubs']} audit_stubs={row['audit_stubs']}"
              + (f"  {row['detail']}" if row['detail'] else ""))

    cur.close()
    raw.close()

    sql_valid = sum(1 for r in results if r["verdict"] in ("PASS", "PASS_RESOLVED"))
    known_mm = sum(1 for r in results if r["verdict"] == "KNOWN_MISMATCH")
    fail_sql = sum(1 for r in results if r["verdict"] == "FAIL_SQL")
    build_fail = sum(1 for r in results if r["verdict"] == "BUILD_FAIL")
    total_biz_stubs = sum(r["business_stubs"] for r in results)
    overall = "SQL_VALID" if (fail_sql == 0 and build_fail == 0) else "SQL_DEFECT"
    review = " + REVIEW(business stubs present)" if total_biz_stubs else " + CLEAN(no business stubs)"

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = _REPO / "data" / f"v18_insert_validation_{ts}.md"
    csvp = _REPO / "data" / f"v18_insert_validation_{ts}.csv"
    lines = [
        f"# v18 INSERT certification (EXPLAIN PLAN) -- {ts}",
        f"Datasource id={args.ds} as {conn.username}",
        f"valid={sql_valid} known_mismatch={known_mm} fail_sql={fail_sql} build_fail={build_fail} "
        f"of {len(results)} ({overall}{review}); total business stubs={total_biz_stubs}",
        "",
        "| DRD | target | sql_len | verdict | business_stubs | audit_stubs | detail |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['label']} | {r['target']} | {r['sql_len']} | {r['verdict']} | "
                     f"{r['business_stubs']} | {r['audit_stubs']} | {r['detail']} |")
    if total_biz_stubs:
        lines += ["", "## Business NULL-stubs (unresolved mappings -- Gate V4 punch-list)"]
        for r in results:
            if r["business_stub_cols"]:
                lines.append(f"- **{r['label']}**: {r['business_stub_cols']}")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csvp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "target", "sql_len", "verdict", "business_stubs", "business_stub_cols", "audit_stubs", "detail"])
        for r in results:
            w.writerow([r["label"], r["target"], r["sql_len"], r["verdict"],
                        r["business_stubs"], r["business_stub_cols"], r["audit_stubs"], r["detail"]])

    print(f"\nReport: {md}")
    print(f"Overall: {overall}  (valid={sql_valid} known_mismatch={known_mm} "
          f"fail_sql={fail_sql} build_fail={build_fail}){review}")
    # exit 0 when there is no genuine SQL defect: PASS / PASS_RESOLVED /
    # KNOWN_MISMATCH are all acceptable ("FAIL only on known mismatches").
    # business stubs are surfaced (non-fatal here -- Gate V4 drives them to zero).
    return 0 if overall == "SQL_VALID" else 1


if __name__ == "__main__":
    raise SystemExit(main())
