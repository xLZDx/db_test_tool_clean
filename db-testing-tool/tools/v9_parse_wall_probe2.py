#!/usr/bin/env python3
"""Gate V9 diagnostic #2: is the AVY wall the JOIN-ORDER search over 110 tables,
or the 369-column projection? Probe #1 showed projection-only (TXN.TXN_ID) plans
in 3.5s -- but that may be Oracle ELIMINATING the 107 unreferenced outer joins.
This isolates that with /*+ NO_ELIMINATE_OJ */ (keep every outer join) and
/*+ ORDERED */ (fix join order, kill the search).

  P1  real 369-col projection + 110 joins (SELECT form)            -> baseline (expect timeout)
  P2  TXN.TXN_ID + NO_ELIMINATE_OJ (force all 110 joins to stay)   -> join-graph cost alone
  P3  TXN.TXN_ID + NO_ELIMINATE_OJ + ORDERED (fix order)           -> does fixing order rescue?

Verdict:
  P2 slow                 -> the wall is the 110-table JOIN GRAPH -> cure = ODI-style multi-step join staging.
  P2 fast but P1 slow     -> the wall is the PROJECTION -> cure = stage a flat table, project over it.
  P3 fast but P2 slow     -> it is join-ORDER search -> a LEADING/ORDERED plan (or staging) fixes it.

Usage: python -m tools.v9_parse_wall_probe2 [--cap-s 60] [--ds 2]
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from sqlalchemy import text

from app.database import sync_engine
from app.connectors.factory import get_connector
from app.models.datasource import DataSource
from tools.v9_parse_wall_probe import _split_avy, _explain, _fresh

_REPO = Path(__file__).resolve().parents[1]
_AVY = _REPO / "data" / "generated_inserts" / "AVY_v18.sql"


def _select_only(sql: str) -> str:
    """Strip INSERT INTO owner.table (cols) -> the trailing SELECT...; (no ';')."""
    m = re.search(r"\bINSERT\s+INTO\s+[A-Z0-9_$#]+\.[A-Z0-9_$#]+\s*\(", sql, re.I)
    p = sql.find("(", m.end() - 1)
    depth = 0
    for i in range(p, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return sql[i + 1:].strip().rstrip(";").rstrip()
    raise RuntimeError("no SELECT")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2)
    ap.add_argument("--cap-s", type=int, default=60)
    args = ap.parse_args()

    sql = _AVY.read_text(encoding="utf-8")
    from_line, joins = _split_avy(sql)
    full_sel = _select_only(sql)

    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": args.ds}).fetchone()
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    conn = get_connector(ds)
    cap = args.cap_s
    print(f"ds={args.ds} as {conn.username}, cap={cap}s\n")

    def from_all() -> str:
        return from_line + "\n    " + "\n    ".join(joins)

    # P1: real projection + joins
    raw = _fresh(conn, cap)
    ok, dt, err = _explain(raw, full_sel, cap); raw.close()
    print(f"[P1] real 369-col projection + 110 joins -> {'OK' if ok else 'FAIL'} {dt:.1f}s {err[:70]}")

    # P2: keep all outer joins, trivial projection
    raw = _fresh(conn, cap)
    ok2, dt2, err2 = _explain(raw, f"SELECT /*+ NO_ELIMINATE_OJ */ TXN.TXN_ID\n{from_all()}", cap); raw.close()
    print(f"[P2] TXN.TXN_ID + NO_ELIMINATE_OJ (all 110 stay) -> {'OK' if ok2 else 'FAIL'} {dt2:.1f}s {err2[:70]}")

    # P3: keep all + fix order
    raw = _fresh(conn, cap)
    ok3, dt3, err3 = _explain(raw, f"SELECT /*+ NO_ELIMINATE_OJ ORDERED */ TXN.TXN_ID\n{from_all()}", cap); raw.close()
    print(f"[P3] + ORDERED (fix join order) -> {'OK' if ok3 else 'FAIL'} {dt3:.1f}s {err3[:70]}")

    print("\n=== V9 PARSE-WALL VERDICT #2 ===")
    if not ok2:
        print("- P2 SLOW -> wall is the 110-table JOIN GRAPH. Cure = ODI-style MULTI-STEP JOIN staging.")
    elif ok2 and not ok:
        print("- P2 FAST, P1 SLOW -> wall is the PROJECTION. Cure = stage one FLAT table, project over it.")
    else:
        print("- P1 and P2 both OK at this cap -> raise --cap-s or re-measure; wall not reproduced.")
    if ok3 and not ok2:
        print("- P3 (ORDERED) FAST while P2 SLOW -> it is join-ORDER search; a fixed LEADING/ORDERED plan helps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
