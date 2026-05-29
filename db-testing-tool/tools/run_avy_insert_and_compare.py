"""Run AVY_FACT_SIDE insert (limited to 100 rows) and per-column parity checks.

Usage: run from repo root: python tools/run_avy_insert_and_compare.py
"""
import json
import re
import requests
import sys

BASE_URL = "http://127.0.0.1:8550"
ANALYZE_RESULT_PATH = "data/test_suites/avyfactside_e2e_response.json"
NVL_NULL_SENTINEL = "-999"
MAX_SAMPLE = 10

AUDIT_COLUMNS = {"CRT_DTM", "CRT_USR_NM", "LAST_UDT_DTM", "LAST_UDT_USR_NM", "SESN_NUM", "IND_UPDATE"}


def load_analyze_result(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def limit_insert_sql(insert_sql: str, max_rows: int = 100) -> str:
    sql = (insert_sql or "").strip()
    if not sql:
        raise ValueError("empty insert_sql")
    # remove leading/trailing semicolons
    sql = sql.rstrip("; \n\r")
    # find first SELECT occurrence
    m = re.search(r"\bSELECT\b", sql, flags=re.IGNORECASE)
    if not m:
        return sql
    prefix = sql[: m.start()].rstrip()
    select_body = sql[m.start() :].strip()
    # strip trailing semicolon from select body
    select_body = select_body.rstrip("; \n\r")
    wrapped = f"SELECT * FROM ({select_body}) q WHERE ROWNUM <= {max_rows}"
    return prefix + "\n" + wrapped


def pick_oracle_datasource():
    r = requests.get(f"{BASE_URL}/api/datasources", timeout=60)
    r.raise_for_status()
    ds = r.json()
    if not isinstance(ds, list) or not ds:
        raise RuntimeError("No datasources returned from API")

    # Try to find an Oracle datasource that has the expected source table(s).
    candidate_table = "CCAL_REPL_OWNER.TXN"
    for d in ds:
        dbt = (d.get("db_type") or "").lower()
        if "oracle" not in dbt:
            continue
        ds_id = int(d.get("id"))
        # quick probe: attempt a small query against the expected table
        probe_sql = f"SELECT 1 FROM {candidate_table} WHERE ROWNUM <= 1"
        try:
            probe_res = requests.post(f"{BASE_URL}/api/datasources/{ds_id}/query", json={"sql": probe_sql, "row_limit": 1}, timeout=30)
            if probe_res.ok:
                return d
        except Exception:
            # Not accessible on this datasource; try next
            continue

    # fallback: prefer any oracle datasource if probe failed
    for d in ds:
        if (d.get("db_type") or "").lower().strip() == "oracle" or "oracle" in (d.get("db_type") or "").lower():
            return d
    # fallback: return first datasource
    return ds[0]


def post_check_insert(ds_id: int, sql: str):
    payload = {"target_datasource_id": ds_id, "sql": sql, "execute": True}
    r = requests.post(f"{BASE_URL}/api/tests/control-table/check-insert", json=payload, timeout=900)
    return r


def run_query(ds_id: int, sql: str, row_limit: int = 50):
    payload = {"sql": sql, "row_limit": row_limit}
    r = requests.post(f"{BASE_URL}/api/datasources/{ds_id}/query", json=payload, timeout=300)
    r.raise_for_status()
    return r.json()


def main():
    print("Loading analyze result...", file=sys.stderr)
    data = load_analyze_result(ANALYZE_RESULT_PATH)
    insert_sql = data.get("generated_insert_sql") or data.get("insert_sql")
    if not insert_sql:
        print("No generated_insert_sql found in analyze result", file=sys.stderr)
        sys.exit(2)

    limited_sql = limit_insert_sql(insert_sql, max_rows=100)
    print(f"Prepared limited INSERT SQL ({len(limited_sql)} chars)", file=sys.stderr)

    print("Locating Oracle datasource...", file=sys.stderr)
    ds = pick_oracle_datasource()
    ds_id = int(ds["id"])
    print(f"Using datasource: {ds.get('name')} (id={ds_id}, type={ds.get('db_type')})", file=sys.stderr)

    print("Executing INSERT (execute=true)", file=sys.stderr)
    r = post_check_insert(ds_id, limited_sql)
    try:
        payload = r.json()
    except Exception:
        print("Check-insert did not return JSON:", r.status_code, r.text[:200], file=sys.stderr)
        sys.exit(3)

    if not r.ok or not payload.get("ok"):
        print("Insert execution failed:", file=sys.stderr)
        print(json.dumps(payload, indent=2)[:8000], file=sys.stderr)
        sys.exit(4)

    print("Insert executed OK:", payload.get("message"), file=sys.stderr)
    row_count = payload.get("rows_returned")
    print(f"rows_returned: {row_count}", file=sys.stderr)

    # Run per-column parity checks
    analysis_rows = data.get("analysis_rows") or []
    cols = [r.get("column") for r in analysis_rows if r.get("column") and r.get("column").upper() not in AUDIT_COLUMNS]
    print(f"Testing {len(cols)} columns for parity...", file=sys.stderr)

    report_lines = []
    summary = {"tested": 0, "mismatches": 0}

    for col in cols:
        summary["tested"] += 1
        col_u = col.upper()
        mismatch_sql = (
            f"SELECT COUNT(*) AS mismatch_count FROM IKOROSTELEV.AVY_FACT_SIDE S "
            f"JOIN TRANSACTIONS_OWNER.AVY_FACT T ON S.TXN_ID = T.TXN_ID "
            f"WHERE NVL(TO_CHAR(S.{col_u}), '{NVL_NULL_SENTINEL}') <> NVL(TO_CHAR(T.{col_u}), '{NVL_NULL_SENTINEL}')"
        )
        try:
            res = run_query(ds_id, mismatch_sql, row_limit=10)
            rows = res.get("rows") or []
            cnt = 0
            if rows:
                first = rows[0]
                # try common keys
                for k in ("MISMATCH_COUNT", "mismatch_count", "COUNT", "COUNT(*)"):
                    if k in first:
                        cnt = int(first[k] or 0)
                        break
                else:
                    # fallback: take first value
                    try:
                        val = list(first.values())[0]
                        cnt = int(val or 0)
                    except Exception:
                        cnt = 0
            else:
                cnt = 0
        except Exception as e:
            report_lines.append(f"{col_u}: ERROR running parity query: {e}")
            continue

        if cnt:
            summary["mismatches"] += 1
            report_lines.append(f"{col_u}: mismatches={cnt}")
            # fetch sample mismatches
            sample_sql = (
                f"SELECT S.TXN_ID AS TXN_ID, NVL(TO_CHAR(S.{col_u}), '{NVL_NULL_SENTINEL}') AS SRC, "
                f"NVL(TO_CHAR(T.{col_u}), '{NVL_NULL_SENTINEL}') AS TGT "
                f"FROM IKOROSTELEV.AVY_FACT_SIDE S "
                f"JOIN TRANSACTIONS_OWNER.AVY_FACT T ON S.TXN_ID = T.TXN_ID "
                f"WHERE NVL(TO_CHAR(S.{col_u}), '{NVL_NULL_SENTINEL}') <> NVL(TO_CHAR(T.{col_u}), '{NVL_NULL_SENTINEL}') "
                f"AND ROWNUM <= {MAX_SAMPLE}"
            )
            try:
                sample_res = run_query(ds_id, sample_sql, row_limit=MAX_SAMPLE)
                sample_rows = sample_res.get("rows") or []
                for rrow in sample_rows:
                    tx = rrow.get("TXN_ID") or rrow.get("txn_id") or rrow.get(list(rrow.keys())[0])
                    src = rrow.get("SRC") or rrow.get("src")
                    tgt = rrow.get("TGT") or rrow.get("tgt")
                    report_lines.append(f"  sample: TXN_ID={tx} SRC={src} TGT={tgt}")
            except Exception as e:
                report_lines.append(f"  sample query error: {e}")
        else:
            # no mismatches
            pass

    # Print final report
    header = [
        "AVY_FACT_SIDE 100-row parity report vs TRANSACTIONS_OWNER.AVY_FACT",
        "Generated SQL source: data/test_suites/avyfactside_e2e_response.json",
        f"Datasource used: {ds.get('name')} (id={ds_id})",
        f"Columns tested: {summary['tested']}",
        f"Columns with mismatches: {summary['mismatches']}",
        "",
    ]
    print("\n".join(header))
    if not report_lines:
        print("All tested columns matched for the sampled 100 rows.")
    else:
        for line in report_lines:
            print(line)


if __name__ == "__main__":
    main()
