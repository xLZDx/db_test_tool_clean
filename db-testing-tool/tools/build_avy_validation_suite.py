#!/usr/bin/env python3
"""Build + SAVE a full AVY (+ IMP_OTSND_AVY_FACT) validation test suite into the
tool's Test Case Management (the `test_cases` table the /tests UI reads).

For each target table it persists a runnable suite against FREEPDB1 (ds 2), with
the control copy in the user's own schema (IKOROSTELEV):

  1. DDL          CREATE the empty control table (structural copy).
  2. INSERT(dml)  load it via the v18 KB-resolved, V9-staged INSERT.
  3. row_count    source (v18 SELECT) COUNT == target (control table) COUNT.
  4. schema_match control table column set == production column set.
  5. null_check   key column has no NULLs in the control table (expected 0).
  6. uniqueness   key column has no duplicates in the control table (expected 0).

Idempotent: the suite folder's prior cases are removed before re-inserting, so
re-running does not duplicate. Writes data/avy_validation_suite_<UTC>.md (+ .csv).

Usage: python -m tools.build_avy_validation_suite [--ds 2] [--control-schema IKOROSTELEV]
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import re
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import text

from app.database import sync_engine
from app.connectors.factory import get_connector
from app.models.datasource import DataSource
from app.services.v18_insert import build_v18_insert_to_dir, V18BuildError

_REPO = Path(__file__).resolve().parents[1]
_FOLDER = "AVY Validation Suite (v18 / V9-staged)"

# (label, DRD rel path, owner, table, profile, key column)
TARGETS = [
    ("AVY", "data/taxlot/DRD_Activity_Fact.xlsx", "TRANSACTIONS_OWNER", "AVY_FACT", "avy", "TXN_ID"),
    ("IMP_OTSND", "temp files/DRD_IMPACT_Outstanding_Activity_FACT (1).xlsx",
     "TRANSACTIONS_OWNER", "IMP_OTSND_AVY_FACT", "auto", "IMP_OTSND_AVY_FACT_ID"),
]


def _select_only(sql: str) -> str | None:
    m = re.search(r"\bINSERT\s+INTO\s+[A-Z0-9_$#]+\.[A-Z0-9_$#]+\s*\(", sql, re.I)
    if not m:
        return None
    p = sql.find("(", m.end() - 1)
    depth = 0
    for i in range(p, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return (sql[i + 1:].strip().rstrip(";").rstrip()) or None
    return None


def _inner_stg(insert_sql: str) -> str | None:
    """Extract the staged CTE's inner materialize block (the join with raw cols),
    so a row-count can be taken WITHOUT recomputing the wide projection (the wall)."""
    m = re.search(r"WITH\s+stg\s+AS\s*\(\s*(.*?)\n\)\s*SELECT", insert_sql, re.S | re.I)
    return m.group(1).strip() if m else None


def _cases(label, owner, table, key, ds, cschema, insert_sql, select_sql, staged, note):
    ctrl = f"{cschema}.{table}"
    prod = f"{owner}.{table}"
    common = dict(source_datasource_id=ds, target_datasource_id=ds, is_active=1,
                  is_ai_generated=0, tolerance=0.0, mapping_table=table)
    # idempotent DDL: create-if-not-exists (swallow ORA-00955) THEN truncate, so each
    # run loads a FRESH empty control table (clean-and-load ETL -> no accumulation /
    # double counts / false uniqueness violations across re-runs).
    ddl = (f"BEGIN "
           f"BEGIN EXECUTE IMMEDIATE 'CREATE TABLE {ctrl} AS SELECT * FROM {prod} WHERE 1=0'; "
           f"EXCEPTION WHEN OTHERS THEN IF SQLCODE != -955 THEN RAISE; END IF; END; "
           f"EXECUTE IMMEDIATE 'TRUNCATE TABLE {ctrl}'; "
           f"END;")
    # row-count source: count the staged JOIN (cheap) not the 369-col projection (the wall)
    inner = _inner_stg(insert_sql) if staged else None
    src_count = (f"WITH stg AS (\n{inner}\n) SELECT COUNT(*) AS CNT FROM stg"
                 if inner else f"SELECT COUNT(*) AS CNT FROM (\n{select_sql}\n)")
    # schema diff (both directions) -> 0 when control matches production
    schema_diff = (
        f"SELECT (SELECT COUNT(*) FROM (SELECT column_name FROM all_tab_columns "
        f"WHERE owner='{owner}' AND table_name='{table}' "
        f"MINUS SELECT column_name FROM all_tab_columns WHERE owner='{cschema}' AND table_name='{table}')) "
        f"+ (SELECT COUNT(*) FROM (SELECT column_name FROM all_tab_columns "
        f"WHERE owner='{cschema}' AND table_name='{table}' "
        f"MINUS SELECT column_name FROM all_tab_columns WHERE owner='{owner}' AND table_name='{table}')) "
        f"AS CNT FROM dual")
    rows = [
        # custom_sql with NO expected_result == "ran to completion = pass" (DDL/loader)
        dict(name=f"{label} :: 1. DDL create control table", test_type="custom_sql",
             target_query=ddl, severity="high",
             description=f"Step 1 (DDL) -- create the empty control table {ctrl} (structural copy of {prod}). "
                         f"Idempotent: re-run swallows ORA-00955.",
             **common),
        dict(name=f"{label} :: 2. INSERT (v18 KB-resolved{', V9-staged' if staged else ''})",
             test_type="custom_sql", target_query=insert_sql, severity="high",
             description=(f"Step 2 (DML loader) -- load {ctrl} via the v18 KB-resolved INSERT"
                          f"{' (V9 MATERIALIZE CTE staging)' if staged else ''}. {note}".strip()),
             **common),
        dict(name=f"{label} :: 3. row_count source==target", test_type="row_count",
             source_query=src_count,
             target_query=f"SELECT COUNT(*) AS CNT FROM {ctrl}",
             severity="high",
             description="Validation (row_count) -- staged-join row count must equal the loaded control-table count.",
             **common),
        dict(name=f"{label} :: 4. schema_match control==production", test_type="custom_sql",
             target_query=schema_diff, expected_result="0", severity="medium",
             description="Validation (schema_match) -- control table column set must equal production "
                         "(MINUS both ways = 0 differences).",
             **common),
        dict(name=f"{label} :: 5. null_check key {key}", test_type="null_check",
             target_query=f"SELECT COUNT(*) AS CNT FROM {ctrl} WHERE {key} IS NULL",
             severity="high",
             description=f"Validation (null_check) -- key column {key} must have no NULLs in the control table.",
             **common),
        dict(name=f"{label} :: 6. uniqueness key {key}", test_type="uniqueness",
             target_query=(f"SELECT {key} FROM {ctrl} GROUP BY {key} HAVING COUNT(*) > 1"),
             severity="high",
             description=f"Validation (uniqueness) -- key column {key} must be unique (zero duplicate groups).",
             **common),
    ]
    return rows


_COLS = ("name", "test_type", "source_datasource_id", "target_datasource_id",
         "source_query", "target_query", "expected_result", "tolerance", "severity",
         "description", "is_active", "is_ai_generated", "mapping_table")


def _make_len_resolver(ds_id: int):
    """DB-backed VARCHAR pad-width resolver: MAX(LENGTH) over the column's
    numeric-only values (the ISO-code width), ignoring junk. Returns a cached
    callable; None on any failure so V13 stays a no-op rather than emit a bad width."""
    try:
        with sync_engine.begin() as c:
            row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": ds_id}).fetchone()
        if row is None:
            return None
        ds = DataSource()
        for k, v in row._mapping.items():
            setattr(ds, k, v)
        raw = get_connector(ds)._direct_connect()
    except Exception:  # noqa: BLE001
        return None
    cache: dict = {}
    ident = re.compile(r"^[A-Za-z0-9_$#]+$")

    def resolve(owner: str, table: str, col: str):
        key = (owner.upper(), table.upper(), col.upper())
        if key in cache:
            return cache[key]
        val = None
        if all(ident.match(x) for x in key):
            try:
                cur = raw.cursor()
                cur.execute(f'SELECT MAX(LENGTH("{key[2]}")) FROM "{key[0]}"."{key[1]}" '
                            f'WHERE REGEXP_LIKE("{key[2]}", \'^[0-9]+$\')')
                r = cur.fetchone()
                cur.close()
                val = int(r[0]) if r and r[0] else None
            except Exception:  # noqa: BLE001
                val = None
        cache[key] = val
        return val

    return resolve


def _make_type_resolver(ds_id: int):
    """DB-backed target-column data-type resolver (for the V14 NVL default)."""
    try:
        with sync_engine.begin() as c:
            row = c.execute(text("SELECT * FROM datasources WHERE id=:i"), {"i": ds_id}).fetchone()
        if row is None:
            return None
        ds = DataSource()
        for k, v in row._mapping.items():
            setattr(ds, k, v)
        raw = get_connector(ds)._direct_connect()
    except Exception:  # noqa: BLE001
        return None
    cache: dict = {}

    def resolve(owner: str, table: str, col: str):
        key = (owner.upper(), table.upper(), col.upper())
        if key in cache:
            return cache[key]
        try:
            cur = raw.cursor()
            cur.execute("SELECT data_type FROM all_tab_columns WHERE owner=:o "
                        "AND table_name=:t AND column_name=:c", {"o": key[0], "t": key[1], "c": key[2]})
            r = cur.fetchone()
            cur.close()
            val = r[0] if r else None
        except Exception:  # noqa: BLE001
            val = None
        cache[key] = val
        return val

    return resolve


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ds", type=int, default=2)
    ap.add_argument("--control-schema", default="IKOROSTELEV")
    ap.add_argument("--only", default="", help="comma-separated target labels to include "
                    "(e.g. AVY); default = all")
    ap.add_argument("--folder", default=_FOLDER, help="Test Case Management folder name")
    args = ap.parse_args()
    cschema = args.control_schema.strip().upper()
    folder_name = args.folder.strip() or _FOLDER
    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    targets = [t for t in TARGETS if (not only or t[0].upper() in only)]

    # V13 length resolver (pad width from the VARCHAR column's numeric-value width;
    # correct against clean data, no-op on the dev mirror's garbage CCY).
    len_resolver = _make_len_resolver(args.ds)
    type_resolver = _make_type_resolver(args.ds)

    all_rows, summary = [], []
    for label, rel, owner, table, prof, key in targets:
        drd = _REPO / rel
        note = ""
        staged = False
        if not drd.exists():
            summary.append((label, table, "DRD_MISSING", 0))
            continue
        td = Path(tempfile.mkdtemp(prefix=f"suite_{label}_"))
        try:
            res = build_v18_insert_to_dir(drd, td / "o", target_schema=owner,
                                          target_table=table, profile=prof,
                                          control_schema=cschema,
                                          varchar_len_resolver=len_resolver,
                                          target_type_resolver=type_resolver)
            insert_sql = res["generated_sql"]
            staged = bool(res.get("staged"))
            select_sql = _select_only(insert_sql) or insert_sql
            rows = _cases(label, owner, table, key, args.ds, cschema, insert_sql,
                          select_sql, staged, note)
            all_rows.extend(rows)
            summary.append((label, table, f"BUILT staged={staged} biz_stubs={len(res['business_stub_columns'])}", len(rows)))
        except V18BuildError as exc:
            summary.append((label, table, f"BUILD_FAIL: {str(exc)[:80]}", 0))
        except Exception as exc:  # noqa: BLE001
            summary.append((label, table, f"ERROR {type(exc).__name__}: {str(exc)[:70]}", 0))
        finally:
            shutil.rmtree(td, ignore_errors=True)

    # persist: get-or-create folder, clear its prior cases, insert fresh
    created = 0
    with sync_engine.begin() as c:
        fid = c.execute(text("SELECT id FROM test_folders WHERE name=:n"), {"n": folder_name}).scalar()
        if fid is None:
            c.execute(text("INSERT INTO test_folders (name) VALUES (:n)"), {"n": folder_name})
            fid = c.execute(text("SELECT id FROM test_folders WHERE name=:n"), {"n": folder_name}).scalar()
        old = [r[0] for r in c.execute(
            text("SELECT test_case_id FROM test_case_folders WHERE folder_id=:f"), {"f": fid}).fetchall()]
        for tcid in old:
            c.execute(text("DELETE FROM test_runs WHERE test_case_id=:i"), {"i": tcid})
            c.execute(text("DELETE FROM test_case_folders WHERE test_case_id=:i"), {"i": tcid})
            c.execute(text("DELETE FROM test_cases WHERE id=:i"), {"i": tcid})
        for r in all_rows:
            payload = {k: r.get(k) for k in _COLS}
            cols = ", ".join(_COLS)
            binds = ", ".join(f":{k}" for k in _COLS)
            c.execute(text(f"INSERT INTO test_cases ({cols}) VALUES ({binds})"), payload)
            tcid = c.execute(text("SELECT id FROM test_cases WHERE name=:n ORDER BY id DESC LIMIT 1"),
                             {"n": r["name"]}).scalar()
            c.execute(text("INSERT INTO test_case_folders (test_case_id, folder_id) VALUES (:t, :f)"),
                      {"t": tcid, "f": fid})
            created += 1

    ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    md = _REPO / "data" / f"avy_validation_suite_{ts}.md"
    csvp = _REPO / "data" / f"avy_validation_suite_{ts}.csv"
    lines = [f"# AVY validation test suite -- saved to Test Case Management ({ts})",
             f"Folder: **{folder_name}**  |  datasource ds={args.ds}  |  control schema {cschema}",
             f"Test cases created: **{created}**", "",
             "| target | table | build | cases |", "|---|---|---|---|"]
    for lbl, tbl, st, n in summary:
        lines.append(f"| {lbl} | {tbl} | {st} | {n} |")
    lines += ["", "| name | test_type | severity |", "|---|---|---|"]
    for r in all_rows:
        lines.append(f"| {r['name']} | {r['test_type']} | {r['severity']} |")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with csvp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "test_type", "severity", "mapping_table", "description"])
        for r in all_rows:
            w.writerow([r["name"], r["test_type"], r["severity"], r.get("mapping_table", ""), r["description"]])

    print(f"Folder '{folder_name}' -> {created} test cases saved")
    for lbl, tbl, st, n in summary:
        print(f"  [{lbl}] {tbl}: {st} ({n} cases)")
    print(f"Report: {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
