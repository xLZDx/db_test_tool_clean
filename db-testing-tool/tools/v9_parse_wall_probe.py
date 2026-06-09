#!/usr/bin/env python3
"""Gate V9 diagnostic: WHY can't Oracle plan the AVY 110-join INSERT, and is the
cure a CTE (one statement) or staged temp tables (many statements)?

Runs four controlled experiments against FREEPDB1 (ds 2) as IKOROSTELEV on the
already-saved data/generated_inserts/AVY_v18.sql. Read-mostly: EXPLAIN PLAN with
rollback; the temp-table experiment creates + drops IKOROSTELEV.V9_PROBE_* only.

Exploits the V8 guarantee that joins are topologically ordered -> any PREFIX of
the join list is self-consistent (every join's ON references only earlier
aliases), so we can sweep join-count without semantic breakage.

  A. projection-only, ALL joins   -> is the wall the JOIN count or the wide SELECT list?
  B. join-count sweep             -> at what K does parse blow past the cap?
  C. one CTE (WITH s AS ...)      -> does a single-statement CTE break the wall? (operator hypothesis)
  D. 2-step temp-table staging    -> does splitting into separate statements parse fast per step?

Usage: python -m tools.v9_parse_wall_probe   [--cap-s 30] [--ds 2]
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

_REPO = Path(__file__).resolve().parents[1]
_AVY = _REPO / "data" / "generated_inserts" / "AVY_v18.sql"


def _split_avy(sql: str) -> tuple[str, list[str]]:
    """Return (from_line, [join_line, ...]) from the AVY monolith.

    The FROM line is 'FROM CCAL_REPL_OWNER.TXN TXN'; join lines are the
    subsequent '    LEFT JOIN ... ON ...' lines (V8-ordered). Trailing ';' stripped.
    """
    lines = sql.split("\n")
    from_idx = next(i for i, ln in enumerate(lines) if re.match(r"\s*FROM\s+\S", ln, re.I))
    from_line = lines[from_idx].strip()
    joins = []
    for ln in lines[from_idx + 1:]:
        s = ln.strip().rstrip(";").rstrip()
        if re.match(r"(LEFT\s+|RIGHT\s+|INNER\s+|FULL\s+)?(OUTER\s+)?JOIN\b", s, re.I):
            joins.append(s)
    return from_line, joins


def _explain(raw, stmt: str, cap_s: int) -> tuple[bool, float, str]:
    cur = raw.cursor()
    t0 = time.monotonic()
    try:
        cur.execute("EXPLAIN PLAN SET STATEMENT_ID='v9probe' FOR " + stmt)
        dt = time.monotonic() - t0
        try:
            raw.rollback()
        except Exception:  # noqa: BLE001
            pass
        return True, dt, ""
    except Exception as exc:  # noqa: BLE001
        dt = time.monotonic() - t0
        try:
            raw.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False, dt, (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    finally:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass


def _fresh(conn, cap_s: int):
    raw = conn._direct_connect()
    try:
        raw.call_timeout = cap_s * 1000
    except Exception:  # noqa: BLE001
        pass
    return raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2)
    ap.add_argument("--cap-s", type=int, default=30, help="per-statement call_timeout (s)")
    args = ap.parse_args()

    sql = _AVY.read_text(encoding="utf-8")
    from_line, joins = _split_avy(sql)
    print(f"AVY monolith: {len(joins)} join lines (V8-ordered); base = {from_line}\n")

    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": args.ds}).fetchone()
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    conn = get_connector(ds)
    user = (conn.username or "").upper()
    cap = args.cap_s
    print(f"ds={args.ds} as {user}, cap={cap}s per statement\n")

    def from_with(k: int) -> str:
        return from_line + "\n    " + "\n    ".join(joins[:k])

    # --- A: projection-only, ALL joins -------------------------------------
    raw = _fresh(conn, cap)
    ok, dt, err = _explain(raw, f"SELECT TXN.TXN_ID\n{from_with(len(joins))}", cap)
    raw.close()
    print(f"[A] projection-only, ALL {len(joins)} joins  -> "
          f"{'OK' if ok else 'FAIL'} {dt:.1f}s {err}")
    a_joins_are_wall = not ok

    # --- B: join-count sweep ------------------------------------------------
    print("[B] join-count sweep (projection-only):")
    ks = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, len(joins)]
    ks = sorted({k for k in ks if k <= len(joins)})
    last_ok_k = 0
    first_fail_k = None
    for k in ks:
        raw = _fresh(conn, cap)
        ok, dt, err = _explain(raw, f"SELECT TXN.TXN_ID\n{from_with(k)}", cap)
        raw.close()
        print(f"    K={k:3d} -> {'OK ' if ok else 'FAIL'} {dt:6.1f}s {err[:60]}")
        if ok:
            last_ok_k = k
        elif first_fail_k is None:
            first_fail_k = k

    # --- C: one CTE (single statement) -------------------------------------
    # WITH s AS (base + first half joins) SELECT ... FROM s + (rebased second half).
    # We can't trivially rebase ON predicates onto a CTE alias, so the honest CTE
    # test = wrap the WHOLE thing as an inline view and select from it. Still ONE
    # statement -> proves whether single-statement CTE/inline-view changes parse.
    raw = _fresh(conn, cap)
    inline = f"WITH s AS (\nSELECT TXN.TXN_ID AS K\n{from_with(len(joins))}\n)\nSELECT /*+ PARALLEL */ COUNT(*) FROM s"
    ok, dt, err = _explain(raw, inline, cap)
    raw.close()
    print(f"\n[C] single-statement CTE wrap, ALL joins -> "
          f"{'OK' if ok else 'FAIL'} {dt:.1f}s {err[:80]}")

    # --- D: 2-step temp-table staging --------------------------------------
    # Step 1: materialize base + first half joins into a temp table.
    # Step 2: EXPLAIN base-on-temp + second half joins (rebased to the temp).
    # This is the genuine "separate statements" test. We only CREATE/DROP in our
    # OWN schema. If both steps parse fast where the monolith times out -> staging
    # is the cure, CTE is not.
    half = len(joins) // 2
    t1 = f'"{user}".V9_PROBE_S1'
    raw = _fresh(conn, cap)
    cur = raw.cursor()
    d_step1 = d_step2 = None
    d_err = ""
    try:
        try:
            cur.execute(f"DROP TABLE {t1} PURGE")
            raw.commit()
        except Exception:  # noqa: BLE001
            pass
        # Step 1 build (real CTAS, capped to a tiny row count so it returns fast)
        step1_sql = (f"CREATE TABLE {t1} AS SELECT /*+ PARALLEL */ TXN.TXN_ID AS K\n"
                     f"{from_with(half)}\nFETCH FIRST 50 ROWS ONLY")
        t0 = time.monotonic()
        cur.execute(step1_sql)
        raw.commit()
        d_step1 = time.monotonic() - t0
        # Step 2: EXPLAIN a select that joins the temp to the remaining joins.
        # The remaining joins reference TXN + earlier aliases; we re-expose them by
        # selecting from the SAME base+joins again but starting the chain at the temp.
        # Honest minimal test: EXPLAIN base + second-half joins alone (prefix-closed
        # only if second half is self-contained; many reference the FIRST half, so
        # this measures parse cost of ~half the joins as a separate statement).
        second = from_line + "\n    " + "\n    ".join(joins[half:])
        ok2, d_step2, err2 = _explain(raw, f"SELECT /*+ PARALLEL */ TXN.TXN_ID\n{second}", cap)
        d_err = err2 if not ok2 else ""
    except Exception as exc:  # noqa: BLE001
        d_err = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
    finally:
        try:
            cur.execute(f"DROP TABLE {t1} PURGE")
            raw.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            cur.close(); raw.close()
        except Exception:  # noqa: BLE001
            pass
    print(f"[D] 2-step temp staging: step1 CTAS({half} joins)="
          f"{('%.1fs' % d_step1) if d_step1 is not None else 'ERR'}  "
          f"step2 EXPLAIN({len(joins)-half} joins)="
          f"{('%.1fs' % d_step2) if d_step2 is not None else 'ERR'}  {d_err[:60]}")

    # --- verdict ------------------------------------------------------------
    print("\n=== V9 PARSE-WALL VERDICT ===")
    if a_joins_are_wall:
        print("- The wall is the JOIN COUNT (projection-only still fails) -> not the SELECT list.")
    else:
        print("- Projection-only PARSES -> the wall is the WIDE SELECT LIST, not raw join count.")
    print(f"- Last OK join-count K={last_ok_k}; first FAIL K={first_fail_k}.")
    print("- Compare [C] (CTE, one statement) vs [D] (staged, separate statements) above for the cure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
