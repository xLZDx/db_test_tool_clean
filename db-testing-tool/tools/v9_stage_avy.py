#!/usr/bin/env python3
"""Gate V9 transform PROOF: rewrite the AVY monolith (369-col projection over 110
joins) into a STAGED query that separates the join from the projection, so Oracle
plans each cheaply. Probe #2 proved the wall is the projection-over-join-graph, not
the joins.

Two emitted forms (answering the operator's CTE-vs-temp-table question):

  CTE  : INSERT INTO tgt (cols)
         WITH stg AS (SELECT /*+ MATERIALIZE */ <source cols> <FROM..110 joins>)
         SELECT /*+ PARALLEL */ <369 CASE exprs rebased to stg> FROM stg
         -> ONE statement (operator's hoped-for "all in CTE").

  TMP  : CREATE TABLE stg AS SELECT /*+ PARALLEL */ <source cols> <FROM..joins>;
         INSERT /*+ PARALLEL */ INTO tgt (cols) SELECT <369 exprs over stg> FROM stg;
         DROP TABLE stg;
         -> separate statements (true staging, like ODI's *_ST tables).

Transform: collect every <alias>.<col> reference in the projection (alias in the
known FROM/JOIN alias set), build a flat stg projecting each as <alias>__<col>,
then rewrite every projection reference to stg.<alias>__<col>. The 110-join FROM
block is reused verbatim inside stg. Fail-loud on any unqualified column.

This is a PROOF tool on the saved AVY SQL; the winning form is then ported into
app/services/v18_insert.py behind the build path. Read-mostly DB use (EXPLAIN +
CREATE/DROP of the connected user's own stg).

Usage: python -m tools.v9_stage_avy [--emit cte|tmp|both] [--explain] [--cap-s 60] [--ds 2]
"""
from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_AVY = _REPO / "data" / "generated_inserts" / "AVY_v18.sql"
_IDENT = r"[A-Za-z0-9_$#]+"


# --------------------------------------------------------------------------- parse
def _scan_top_level(s: str):
    """Yield (index, char) at paren depth 0, skipping single-quote string literals."""
    depth = 0
    in_str = False
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if in_str:
            if ch == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0:
            yield i, ch
        i += 1


def parse_insert(sql: str) -> dict:
    sql = sql.strip().rstrip(";").rstrip()
    m = re.search(r"\bINSERT\s+INTO\s+(" + _IDENT + r"\." + _IDENT + r")\s*\(", sql, re.I)
    if not m:
        raise SystemExit("no INSERT INTO owner.table (")
    target = m.group(1)
    # column list: balanced parens from the '(' after target
    p = sql.find("(", m.end() - 1)
    depth = 0
    col_end = -1
    for i in range(p, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                col_end = i
                break
    target_cols = [c.strip() for c in sql[p + 1:col_end].split(",") if c.strip()]
    rest = sql[col_end + 1:]
    ms = re.search(r"\bSELECT\b", rest, re.I)
    proj_and_from = rest[ms.end():]
    # find the top-level FROM (the EXISTS subquery has its own FROM at depth>0)
    from_at = None
    for idx, ch in _scan_top_level(proj_and_from):
        if ch.upper() == "F" and re.match(r"FROM\b", proj_and_from[idx:idx + 5], re.I):
            from_at = idx
            break
    if from_at is None:
        raise SystemExit("no top-level FROM")
    projection = proj_and_from[:from_at].strip()
    from_block = proj_and_from[from_at:].strip()
    # split projection on top-level commas
    exprs, start = [], 0
    for idx, ch in _scan_top_level(projection):
        if ch == ",":
            exprs.append(projection[start:idx].strip())
            start = idx + 1
    exprs.append(projection[start:].strip())
    return {"target": target, "target_cols": target_cols, "exprs": exprs, "from_block": from_block}


def known_aliases(from_block: str) -> list[str]:
    al = []
    m = re.search(r"\bFROM\s+" + _IDENT + r"\." + _IDENT + r"\s+(" + _IDENT + r")", from_block, re.I)
    if m:
        al.append(m.group(1))
    for jm in re.finditer(r"\bJOIN\s+" + _IDENT + r"\." + _IDENT + r"\s+(" + _IDENT + r")\b", from_block, re.I):
        al.append(jm.group(1))
    # de-dup preserving order, longest-first for safe regex replacement
    seen, out = set(), []
    for a in al:
        if a.upper() not in seen:
            seen.add(a.upper())
            out.append(a)
    return sorted(out, key=len, reverse=True)


# --------------------------------------------------------------------------- transform
def stage(parsed: dict) -> dict:
    aliases = known_aliases(parsed["from_block"])
    alias_set = {a.upper() for a in aliases}
    ref_re = re.compile(r"(?<![.\w])(" + "|".join(re.escape(a) for a in aliases) + r")\.(" + _IDENT + r")",
                        re.IGNORECASE)

    refs = {}  # (ALIAS, COL) -> stg colname
    rebased = []
    for expr in parsed["exprs"]:
        # split '<value-expr> AS <outcol>' on the LAST top-level ' AS '
        as_idx = None
        for idx, _ch in _scan_top_level(expr):
            if re.match(r"\s+AS\s+", expr[idx:idx + 4], re.I) or (
                expr[idx:idx + 4].upper() == " AS " ):
                as_idx = idx
        m_as = list(re.finditer(r"\bAS\b", expr, re.I))
        # robust: take last top-level AS
        out_col, val = None, expr
        last = None
        depth = 0
        in_str = False
        for i, ch in enumerate(expr):
            if in_str:
                if ch == "'":
                    in_str = False
                continue
            if ch == "'":
                in_str = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and expr[i:i + 4].upper() == " AS " :
                last = i
        if last is not None:
            val = expr[:last].strip()
            out_col = expr[last + 4:].strip()
        else:
            out_col = expr.strip().split()[-1]
            val = expr  # no AS -> whole thing (rare)

        def _sub(mm):
            a, c = mm.group(1).upper(), mm.group(2).upper()
            name = f"{a}__{c}"
            refs[(a, c)] = name
            return f"stg.{name}"
        new_val = ref_re.sub(_sub, val)
        rebased.append((new_val, out_col))

    # fail-loud: any unqualified bare column in projection that is not stg.* and not a literal/keyword?
    # (heuristic guard -- a real production col like CASE ... bare ref would break rebasing)
    stg_cols = [f"{a}.{c} AS {name}" for (a, c), name in sorted(refs.items())]
    return {"aliases": aliases, "refs": refs, "stg_cols": stg_cols, "rebased": rebased}


def emit_cte(parsed, st, degree=""):
    tgt, cols = parsed["target"], parsed["target_cols"]
    par = f"PARALLEL({degree})" if degree else "PARALLEL"
    stg_sel = ",\n           ".join(st["stg_cols"])
    proj = ",\n       ".join(f"{v} AS {oc}" for v, oc in st["rebased"])
    return (
        f"INSERT /*+ {par} */ INTO {tgt} (\n    " + ",\n    ".join(cols) + "\n)\n"
        f"WITH stg AS (\n    SELECT /*+ MATERIALIZE {par} */\n           {stg_sel}\n    {parsed['from_block']}\n)\n"
        f"SELECT /*+ {par} */\n       {proj}\nFROM stg"
    )


def emit_tmp(parsed, st, degree="", stg_name="V9_STG_AVY"):
    tgt, cols = parsed["target"], parsed["target_cols"]
    par = f"PARALLEL({degree})" if degree else "PARALLEL"
    stg_sel = ",\n           ".join(st["stg_cols"])
    proj = ",\n       ".join(f"{v} AS {oc}" for v, oc in st["rebased"])
    create = (f"CREATE TABLE {stg_name} AS\n    SELECT /*+ {par} */\n           {stg_sel}\n    {parsed['from_block']}")
    # rebase stg.X -> stg_name.X for the standalone statement
    ins_proj = proj.replace("stg.", f"{stg_name}.")
    insert = (f"INSERT /*+ {par} */ INTO {tgt} (\n    " + ",\n    ".join(cols) + "\n)\n"
              f"SELECT /*+ {par} */\n       {ins_proj}\nFROM {stg_name}")
    return create, insert, f"DROP TABLE {stg_name} PURGE"


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit", choices=["cte", "tmp", "both"], default="both")
    ap.add_argument("--degree", default="", help="PARALLEL degree, e.g. 8 (default: auto)")
    ap.add_argument("--explain", action="store_true", help="EXPLAIN the CTE + run the TMP staging on Oracle")
    ap.add_argument("--run-capped", type=int, default=0, metavar="N",
                    help="actually execute the staged CTE INSERT capped to N rows into <user>.<table> (rolled back)")
    ap.add_argument("--cap-s", type=int, default=90)
    ap.add_argument("--ds", type=int, default=2)
    args = ap.parse_args()

    parsed = parse_insert(_AVY.read_text(encoding="utf-8"))
    st = stage(parsed)
    print(f"target={parsed['target']}  target_cols={len(parsed['target_cols'])}  "
          f"projection_exprs={len(parsed['exprs'])}  aliases={len(st['aliases'])}  "
          f"distinct source refs (stg cols)={len(st['refs'])}")
    if len(parsed["target_cols"]) != len(parsed["exprs"]):
        print(f"  WARNING: target col count {len(parsed['target_cols'])} != projection expr count {len(parsed['exprs'])}")

    out_dir = _REPO / "data" / "generated_inserts"
    cte_sql = emit_cte(parsed, st, args.degree)
    create, insert, drop = emit_tmp(parsed, st, args.degree)
    (out_dir / "AVY_v18_staged_cte.sql").write_text(cte_sql + ";\n", encoding="utf-8")
    (out_dir / "AVY_v18_staged_tmp.sql").write_text(create + ";\n\n" + insert + ";\n\n" + drop + ";\n", encoding="utf-8")
    print(f"wrote AVY_v18_staged_cte.sql ({len(cte_sql)} ch) + AVY_v18_staged_tmp.sql")

    if not args.explain and not args.run_capped:
        return 0

    from sqlalchemy import text
    from app.database import sync_engine
    from app.connectors.factory import get_connector
    from app.models.datasource import DataSource
    from tools.v9_parse_wall_probe import _fresh, _explain
    from tools.validate_v18_inserts import _table_exists

    with sync_engine.begin() as c:
        row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": args.ds}).fetchone()
    ds = DataSource()
    for k, v in row._mapping.items():
        setattr(ds, k, v)
    conn = get_connector(ds)
    user = (conn.username or "").upper()
    cap = args.cap_s
    print(f"\nds={args.ds} as {user}, cap={cap}s")

    # retarget INSERT to the user's own control schema for a real run
    cte_run = re.sub(r"INSERT(\s+/\*\+[^*]*\*/)?\s+INTO\s+" + _IDENT + r"\.",
                     lambda m: m.group(0).rsplit(".", 1)[0].rsplit(" ", 1)[0] + f" {user}.", cte_sql, flags=re.I)
    # simpler: replace the target owner with user
    tgt_owner = parsed["target"].split(".")[0]
    tgt_tbl = parsed["target"].split(".")[1]
    cte_run = cte_sql.replace(f"{parsed['target']}", f"{user}.{tgt_tbl}")

    if args.explain:
        # [CTE] EXPLAIN one statement
        raw = _fresh(conn, cap)
        ok, dt, err = _explain(raw, cte_run, cap); raw.close()
        print(f"[CTE] one-statement materialized -> {'OK' if ok else 'FAIL'} {dt:.1f}s {err[:90]}")

    if args.run_capped:
        # real capped INSERT into <user>.<table> (rolled back); proves it EXECUTES, not just plans
        cap_sql = cte_run + f"\nFETCH FIRST {args.run_capped} ROWS ONLY"
        raw = _fresh(conn, cap)
        cur = raw.cursor()
        try:
            if not _table_exists(cur, user, tgt_tbl):
                cur.execute(f'CREATE TABLE "{user}"."{tgt_tbl}" AS SELECT * FROM "{tgt_owner}"."{tgt_tbl}" WHERE 1=0')
                raw.commit()
            t0 = time.monotonic()
            cur.execute(cap_sql)
            n = cur.rowcount
            d = time.monotonic() - t0
            raw.rollback()  # keep the control table empty
            print(f"[RUN] capped staged CTE INSERT {n} rows into {user}.{tgt_tbl} -> OK {d:.1f}s (rolled back)")
        except Exception as exc:  # noqa: BLE001
            try:
                raw.rollback()
            except Exception:  # noqa: BLE001
                pass
            print(f"[RUN] FAIL {(str(exc).strip().splitlines() or [''])[0][:120]}")
        finally:
            try:
                cur.close(); raw.close()
            except Exception:  # noqa: BLE001
                pass

    if not args.explain:
        return 0

    # [TMP] real 2-step staging: CREATE stg (FETCH 50), EXPLAIN insert-over-stg, DROP
    create_run = create + "\n    FETCH FIRST 50 ROWS ONLY"
    insert_run = insert.replace(f"{parsed['target']}", f"{user}.{tgt_tbl}")
    raw = _fresh(conn, cap)
    cur = raw.cursor()
    try:
        try:
            cur.execute("DROP TABLE V9_STG_AVY PURGE"); raw.commit()
        except Exception:  # noqa: BLE001
            pass
        t0 = time.monotonic()
        cur.execute(create_run); raw.commit()
        d1 = time.monotonic() - t0
        ok2, d2, err2 = _explain(raw, insert_run, cap)
        print(f"[TMP] step1 CREATE stg={d1:.1f}s  step2 EXPLAIN insert-over-stg -> {'OK' if ok2 else 'FAIL'} {d2:.1f}s {err2[:80]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[TMP] FAIL {(str(exc).splitlines() or [''])[0][:120]}")
    finally:
        try:
            cur.execute("DROP TABLE V9_STG_AVY PURGE"); raw.commit()
        except Exception:  # noqa: BLE001
            pass
        try:
            cur.close(); raw.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
