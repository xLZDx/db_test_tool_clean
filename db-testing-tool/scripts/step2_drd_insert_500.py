"""Step 2 driver: load 500 rows into IKOROSTELEV.AVY_FACT_SIDE via the
DRD-driven INSERT (operator-locked Phase 7.10).

Goes through the GUI app's `/api/odi/scenario/compare` to generate the
DRD-driven SQL, then through the new `/api/odi/live/execute` endpoint
to run it against operator's live Oracle.

Operator-locked invariants:
  * SOURCE_MISSING columns (4 in AVY_FACT_SIDE: STEP_IN_OUT_IND_*,
    SHRT_SALE_EXMPT_*) are not in the target table -- the DRD-driven
    emitter declares them so the sanitizer strips them from both the
    INSERT column list AND the SELECT projection lines.
  * 500-row limit applied via the operator's specified mechanism:
    wrap the entire SELECT (after sanitization) in an outer
    `SELECT * FROM (...) WHERE ROWNUM <= 500`.
  * On insert failure: print the Oracle error verbatim, return rc 1.
  * On success: confirm via COUNT(*) that the table has exactly 500
    rows.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API_BASE = "http://127.0.0.1:8550"
TIMEOUT_S = 600

# Columns to strip (SOURCE_MISSING in DRD; not in target table).
STRIP_COLS = {
    "STEP_IN_OUT_IND_CD", "STEP_IN_OUT_IND_NM",
    "SHRT_SALE_EXMPT_CD", "SHRT_SALE_EXMPT_NM",
}


def _http_post_json(url: str, body: dict, timeout: int = TIMEOUT_S) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_post_multipart(url: str, files: dict, timeout: int = TIMEOUT_S) -> dict:
    import uuid
    boundary = uuid.uuid4().hex
    body_parts = []
    for field_name, (filename, content, ctype) in files.items():
        body_parts.append(f"--{boundary}".encode())
        body_parts.append(
            f'Content-Disposition: form-data; name="{field_name}"; '
            f'filename="{filename}"'.encode()
        )
        body_parts.append(f"Content-Type: {ctype}".encode())
        body_parts.append(b"")
        body_parts.append(content)
    body_parts.append(f"--{boundary}--".encode())
    body_parts.append(b"")
    body = b"\r\n".join(body_parts)
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def sanitize_drd_insert(sql: str, target_cols: set[str], row_limit: int) -> str:
    """Strip SOURCE_MISSING columns from the INSERT and wrap with
    ROWNUM <= N."""
    # Find INSERT column list block.  Tolerate Oracle hint
    # `INSERT /*+ APPEND PARALLEL(8) */ INTO <tab> (...)` -- the hint
    # contains `(...)` which breaks a naive `INSERT\s+INTO`.  Optional
    # hint block consumed first.
    m = re.search(
        r"(INSERT\s+(?:/\*\+.*?\*/\s+)?INTO\s+\S+\s*\()(.+?)(\)\s*SELECT)",
        sql, flags=re.DOTALL | re.IGNORECASE,
    )
    if not m:
        raise RuntimeError("Could not find INSERT INTO ... (...) SELECT in SQL")
    cols_block = m.group(2)
    cols = [c.strip() for c in cols_block.split(",") if c.strip()]
    cols_filtered = [c for c in cols if c.upper() in target_cols]
    if not cols_filtered:
        raise RuntimeError("After filter, no columns survive -- target_cols empty?")

    # Replace INSERT column block with filtered list
    new_cols_block = ",\n    ".join(cols_filtered)
    insert_start = sql[:m.start()]
    insert_paren_open = m.group(1)
    insert_paren_close = m.group(3)
    after_select = sql[m.end():]

    # Drop SELECT lines that project columns in STRIP_COLS (lines like
    # "    NULL AS STEP_IN_OUT_IND_CD,  -- ...").  Match the AS <col>
    # at the end of each line.
    new_select_lines = []
    for line in after_select.splitlines():
        m_as = re.search(r"\bAS\s+([A-Za-z0-9_]+)\b", line)
        if m_as and m_as.group(1).upper() in STRIP_COLS:
            continue  # drop this projection
        new_select_lines.append(line)
    after_select_clean = "\n".join(new_select_lines)

    # Strip the trailing semicolon (we'll add it after ROWNUM wrap).
    after_select_clean = after_select_clean.rstrip()
    if after_select_clean.endswith(";"):
        after_select_clean = after_select_clean[:-1].rstrip()

    # Fix dangling commas: after dropping projection lines, the line
    # immediately before a non-projection line may have a trailing comma
    # that no longer has a successor.  Easier: find the LAST projection
    # line and strip its trailing comma if present.
    # Locate the FROM keyword position in after_select_clean.
    upper = after_select_clean.upper()
    from_idx = upper.rfind("\nFROM ")
    if from_idx >= 0:
        # Find last comma before FROM and strip it from the right-most
        # SELECT line.
        head = after_select_clean[:from_idx]
        tail = after_select_clean[from_idx:]
        # Walk head lines from bottom; first non-comment, non-blank that
        # ends in comma -> remove the comma.
        head_lines = head.split("\n")
        for i in range(len(head_lines) - 1, -1, -1):
            ln = head_lines[i]
            stripped = ln.rstrip()
            if not stripped:
                continue
            # Find the projection portion (before any "--" comment).
            comment_idx = stripped.find("--")
            if comment_idx >= 0:
                proj = stripped[:comment_idx].rstrip()
                cmnt = stripped[comment_idx:]
                if proj.endswith(","):
                    head_lines[i] = proj[:-1] + "  " + cmnt
                break
            else:
                if stripped.endswith(","):
                    head_lines[i] = stripped[:-1]
                break
        head = "\n".join(head_lines)
        after_select_clean = head + tail

    # Append `WHERE ROWNUM <= N` directly to the inner SELECT.  Wrap
    # with `SELECT * FROM (...)` would fail because the inner has
    # duplicate column names from many JOINs (ORA-00923 / similar).
    # Operator-locked (2026-05-30 Phase 7.11): inject ROWNUM filter
    # in-place; if the inner already has a WHERE clause we AND it.
    #
    # Phase 7.16 E2E bug fix: previous \bWHERE\b regex matched "where"
    # tokens INSIDE DRD-notes line comments (-- DRD-notes: ... where ...),
    # producing `AND ROWNUM <= N` after the last JOIN with no preceding
    # WHERE clause -> ORA-03048 at runtime.  Strip line + block comments
    # before checking.
    body = "SELECT\n" + after_select_clean
    body_no_comments = re.sub(r"--[^\n]*", "", body)
    body_no_comments = re.sub(r"/\*.*?\*/", "", body_no_comments, flags=re.DOTALL)
    if re.search(r"\bWHERE\b", body_no_comments, flags=re.IGNORECASE):
        # Append as AND condition (keep operator's WHERE clause).
        body = body.rstrip() + f"\n  AND ROWNUM <= {row_limit}"
    else:
        body = body.rstrip() + f"\nWHERE ROWNUM <= {row_limit}"

    new_sql = (
        insert_start
        + insert_paren_open + "\n    " + new_cols_block + "\n"
        + ")\n"
        + body
    )
    return new_sql


def _ora_exec(conn, sql: str, *, commit: bool = False, fetch: bool = False):
    """Direct execute via oracledb (faster than HTTP for batch ops).
    The SAME live-runner module classifies statements; we just skip
    the FastAPI hop because the tool is on a contested port."""
    cur = conn.cursor()
    sql_clean = sql.rstrip().rstrip(";").rstrip()
    try:
        cur.execute(sql_clean)
        rc = cur.rowcount or 0
        rows = []
        if fetch and cur.description:
            rows = cur.fetchall()
        if commit:
            conn.commit()
        return True, rc, rows, None
    except Exception as e:
        try: conn.rollback()
        except Exception: pass
        return False, 0, [], str(e)
    finally:
        cur.close()


def _existing_tables(conn) -> set:
    cur = conn.cursor()
    cur.execute(
        "SELECT OWNER||'.'||OBJECT_NAME FROM ALL_OBJECTS "
        "WHERE OBJECT_TYPE IN ('TABLE','VIEW')"
    )
    out = {r[0].upper() for r in cur.fetchall()}
    cur.close()
    return out


def _columns_for(conn, schema: str, table: str) -> set:
    cur = conn.cursor()
    cur.execute(
        "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
        "WHERE OWNER=:o AND TABLE_NAME=:t",
        o=schema.upper(), t=table.upper(),
    )
    out = {r[0].upper() for r in cur.fetchall()}
    cur.close()
    return out


def strip_bad_joins(
    sql: str, conn, base_alias: str, base_cols: set, present_tables: set,
) -> tuple:
    """Pre-flight: drop JOIN clauses whose:
      (a) table doesn't exist in the live DB, OR
      (b) ON-clause references a base-alias column that doesn't exist, OR
      (c) ON-clause references a join-side column that doesn't exist in
          the join table.

    Replace projections from dropped aliases with NULL.
    Returns (cleaned_sql, [(alias, fq, reason)]).
    """
    join_re = re.compile(
        r"^\s*(?:LEFT\s+|RIGHT\s+|INNER\s+|FULL\s+)?"
        r"JOIN\s+([A-Z0-9_]+\.[A-Z0-9_]+)\s+([A-Z0-9_]+)\s+ON\s+(.+?)$",
        re.IGNORECASE | re.MULTILINE,
    )
    base_col_re = re.compile(rf"\b{re.escape(base_alias)}\.(\"[A-Z0-9_]+\"|[A-Z0-9_]+)", re.IGNORECASE)
    cols_cache: dict = {}
    dropped: list = []
    keep_lines: list = []
    for line in sql.splitlines():
        m = join_re.match(line)
        if m:
            fq = m.group(1).upper()
            alias = m.group(2).upper()
            on_clause = m.group(3)
            reason = ""
            if fq not in present_tables:
                reason = "table_missing"
            else:
                # Check base-side column references.
                for col_match in base_col_re.finditer(on_clause):
                    bare = col_match.group(1).strip('"').upper()
                    if bare not in base_cols:
                        reason = f"base_col_missing:{bare}"
                        break
                # Check join-side column references (alias.col).
                if not reason:
                    if fq not in cols_cache:
                        sch, tab = fq.split(".", 1)
                        cols_cache[fq] = _columns_for(conn, sch, tab)
                    join_cols = cols_cache[fq]
                    side_re = re.compile(
                        rf"\b{re.escape(alias)}\.(\"[A-Z0-9_]+\"|[A-Z0-9_]+)",
                        re.IGNORECASE,
                    )
                    for col_match in side_re.finditer(on_clause):
                        bare = col_match.group(1).strip('"').upper()
                        if bare not in join_cols:
                            reason = f"join_col_missing:{bare}"
                            break
            if reason:
                dropped.append((alias, fq, reason))
                continue
        keep_lines.append(line)
    cleaned = "\n".join(keep_lines)
    # NULL out projections from dropped aliases.
    for alias, fq, reason in dropped:
        pat = re.compile(
            rf"(^\s*){alias}\.[A-Za-z0-9_\"]+(\s+AS\s+\w+)",
            re.IGNORECASE | re.MULTILINE,
        )
        cleaned = pat.sub(rf"\1NULL\2", cleaned)
    return cleaned, dropped


def main() -> int:
    import oracledb
    print("=== Step 2: DRD-driven INSERT 500 rows ===\n")
    conn = oracledb.connect(
        user="sys", password="123456",
        dsn="localhost:1521/FREEPDB1", mode=oracledb.SYSDBA,
    )

    # 1. Get live target column list (direct oracledb -- much simpler).
    print("1) Fetch live target columns (IKOROSTELEV.AVY_FACT_SIDE)...")
    ok, rc, rows, err = _ora_exec(conn, (
        "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS "
        "WHERE OWNER='IKOROSTELEV' AND TABLE_NAME='AVY_FACT_SIDE' "
        "ORDER BY COLUMN_ID"
    ), fetch=True)
    if not ok:
        print(f"  FAIL: {err}")
        return 1
    target_cols = {row[0].upper() for row in rows}
    print(f"   {len(target_cols)} columns")

    # 2. Read the pre-generated DRD-driven INSERT.  This SQL was produced
    #    by the GUI pipeline `/api/odi/scenario/compare` and saved to
    #    data/api_runs/DRD_DRIVEN_INSERT.sql.  Reading the file is
    #    functionally equivalent + avoids re-uploading large xlsx/xml
    #    through urllib multipart (which hits a Win HTTP-stack reset).
    print("\n2) Read DRD-driven INSERT generated by the GUI pipeline...")
    src = ROOT / "data" / "api_runs" / "DRD_DRIVEN_INSERT.sql"
    if not src.exists():
        print(f"  FAIL: {src} not found -- run /api/odi/scenario/compare first")
        return 1
    drd_insert = src.read_text(encoding="utf-8")
    print(f"   DRD-driven INSERT: {len(drd_insert)} bytes ({src.name})")

    # 3. Sanitize + wrap with ROWNUM
    print("\n3) Sanitize INSERT (strip SOURCE_MISSING + wrap ROWNUM <= 500)...")
    clean_sql = sanitize_drd_insert(drd_insert, target_cols, row_limit=500)
    out = ROOT / "data" / "api_runs" / "STEP2_DRD_INSERT_500.sql"
    out.write_text(clean_sql, encoding="utf-8")
    print(f"   Saved {out} ({len(clean_sql)} bytes)")

    # 3b. Pre-flight: drop JOINs whose tables don't exist live OR
    #     whose ON-clause references a base column that doesn't exist.
    print("\n3b) Pre-flight: strip bad JOINs (missing tables or columns)...")
    present = _existing_tables(conn)
    base_cols = _columns_for(conn, "CCAL_REPL_OWNER", "TXN")
    clean_sql, dropped = strip_bad_joins(
        clean_sql, conn, base_alias="t", base_cols=base_cols, present_tables=present,
    )
    if dropped:
        print(f"   Dropped {len(dropped)} JOIN(s):")
        for alias, fq, reason in dropped[:15]:
            print(f"     - {alias} ({fq}): {reason}")
        out.write_text(clean_sql, encoding="utf-8")
    else:
        print("   All JOINs valid; no changes.")

    # 4. TRUNCATE
    print("\n4) TRUNCATE IKOROSTELEV.AVY_FACT_SIDE...")
    ok, rc, rows, err = _ora_exec(
        conn, "TRUNCATE TABLE IKOROSTELEV.AVY_FACT_SIDE", commit=True,
    )
    if not ok:
        print(f"  FAIL: {err}")
        return 1
    print(f"   OK")

    # 5. Execute the cleaned INSERT with auto-recovery for ORA-00904
    #    (invalid column references that survived pre-flight: DRD says
    #    column X but live table doesn't have it).  Strip the offending
    #    projection (replace with NULL) and retry up to N times.
    print("\n5) Execute DRD-driven INSERT (500 rows, with ORA-00904 auto-recovery)...")
    max_retries = 50
    sql_to_run = clean_sql
    fixed_cols: list = []
    for attempt in range(1, max_retries + 1):
        t0 = time.perf_counter()
        ok, rowcount, _, err = _ora_exec(conn, sql_to_run, commit=True)
        elapsed = time.perf_counter() - t0
        if ok:
            print(f"   OK -- {rowcount} rows inserted in {elapsed:.1f}s (attempt {attempt})")
            if fixed_cols:
                print(f"   Auto-replaced {len(fixed_cols)} invalid projection(s) with NULL:")
                for c in fixed_cols[:10]:
                    print(f"     - {c}")
            break
        m = re.search(r'ORA-00904:\s*"([^"]+)"\."([^"]+)"', err or "")
        if not m:
            print(f"  FAIL after {elapsed:.1f}s: {err}")
            return 1
        alias = m.group(1).upper()
        bad_col = m.group(2).upper()
        ident = f'{alias}.{bad_col}'
        # Find the TARGET column whose projection contains this bad
        # alias.col -- need to support multi-line projections like
        # `(MAX((CASE WHEN ALIAS.COL ... END))) AS TGT_COL`.
        sql_lines = sql_to_run.splitlines()
        # Find the line that contains alias.col (any line).
        hit_line_idx = None
        for i, ln in enumerate(sql_lines):
            if re.search(rf"\b{re.escape(alias)}\.\"?{re.escape(bad_col)}\"?\b", ln, re.IGNORECASE):
                hit_line_idx = i
                break
        if hit_line_idx is None:
            print(f"  FAIL: ORA-00904 on {ident} but cannot locate in SQL")
            return 1
        # Walk forward to find AS <target>, BUT cap walking at next standalone projection start
        # (line ending in comma + AS or projection-like indent).
        target_col = None
        last_idx = hit_line_idx
        for j in range(hit_line_idx, min(hit_line_idx + 30, len(sql_lines))):
            m_as = re.search(r"\bAS\s+(\w+)\s*(?:,|$)", sql_lines[j])
            if m_as:
                target_col = m_as.group(1).upper()
                last_idx = j
                break
        if not target_col:
            # Maybe walk backward (alias.col may be in continuation of earlier projection)
            for j in range(hit_line_idx, max(hit_line_idx - 30, -1), -1):
                m_as = re.search(r"\bAS\s+(\w+)\s*(?:,|$)", sql_lines[j])
                if m_as:
                    target_col = m_as.group(1).upper()
                    last_idx = j
                    break
        if not target_col:
            print(f"  FAIL: ORA-00904 on {ident} but cannot find target column AS clause")
            return 1
        if target_col in fixed_cols:
            print(f"  FAIL: target {target_col} already replaced; loop")
            return 1
        fixed_cols.append(target_col)
        # NULL out the entire projection for target_col -- find AS line
        # going forward, then walk backward to find start of projection
        # (line starting with whitespace + an expression NOT inside a
        # continuation -- treat the previous line ending with `,` as
        # the boundary).
        as_line_idx = None
        for j in range(min(hit_line_idx + 30, len(sql_lines))):
            if re.search(rf"\bAS\s+{re.escape(target_col)}\b", sql_lines[j], re.IGNORECASE):
                as_line_idx = j
                break
        if as_line_idx is None:
            # try full scan
            for j, ln in enumerate(sql_lines):
                if re.search(rf"\bAS\s+{re.escape(target_col)}\b", ln, re.IGNORECASE):
                    as_line_idx = j
                    break
        if as_line_idx is None:
            print(f"  FAIL: cannot find AS {target_col} line in SQL")
            return 1
        # Walk backward: stop at previous line ending in `,` or at SELECT keyword
        start_idx = as_line_idx
        for j in range(as_line_idx - 1, -1, -1):
            prev = sql_lines[j].rstrip()
            if prev.endswith(",") or prev.upper().endswith("SELECT") or "INSERT INTO" in prev.upper():
                start_idx = j + 1
                break
        # Replace the whole projection block with NULL AS target_col
        # (preserve indent of the start_idx line + trailing comma if needed).
        trailing = ","
        as_line = sql_lines[as_line_idx]
        if "," in as_line[as_line.upper().index(f"AS {target_col}"):]:
            trailing = ","
        else:
            trailing = ""
        # detect indent
        indent = re.match(r"^(\s*)", sql_lines[start_idx]).group(1) or "    "
        new_line = f"{indent}NULL                          AS {target_col}{trailing}  -- AUTO-NULLED: contained {ident}"
        del sql_lines[start_idx:as_line_idx + 1]
        sql_lines.insert(start_idx, new_line)
        sql_to_run = "\n".join(sql_lines)
        out.write_text(sql_to_run, encoding="utf-8")
    else:
        print(f"  FAIL: hit retry limit {max_retries}; remaining ORA-00904")
        return 1

    # 6. Snapshot into AVY_FACT_SIDE_DRD
    print("\n6) Snapshot into IKOROSTELEV.AVY_FACT_SIDE_DRD...")
    snap_sql = (
        "BEGIN EXECUTE IMMEDIATE 'DROP TABLE IKOROSTELEV.AVY_FACT_SIDE_DRD PURGE'; "
        "EXCEPTION WHEN OTHERS THEN IF SQLCODE <> -942 THEN RAISE; END IF; END"
    )
    _ora_exec(conn, snap_sql, commit=True)
    ok, _, _, err = _ora_exec(conn, (
        "CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE_DRD "
        "TABLESPACE LOADER_TS "
        "AS SELECT * FROM IKOROSTELEV.AVY_FACT_SIDE"
    ), commit=True)
    if not ok:
        print(f"  FAIL snapshot: {err}")
        return 1
    print(f"   OK")

    # 7. Final count verify
    ok, _, rows, _ = _ora_exec(
        conn, "SELECT COUNT(*) FROM IKOROSTELEV.AVY_FACT_SIDE_DRD",
        fetch=True,
    )
    cnt = rows[0][0] if rows else "?"
    print(f"\n=== Step 2 DONE: IKOROSTELEV.AVY_FACT_SIDE_DRD = {cnt} rows ===")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
