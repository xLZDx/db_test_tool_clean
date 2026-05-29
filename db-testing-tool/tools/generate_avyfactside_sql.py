"""Generate CREATE and INSERT SQL for AVY_FACT_SIDE with all columns NOT NULL.

Creates:
- docs/avyfactside_create_369_not_null.sql
- docs/avyfactside_insert_100_rows.sql

Usage: python tools/generate_avyfactside_sql.py
"""
import json
import re
from datetime import datetime

COLS_JSON = "avyfactside_columns.json"
DDL_SRC = "avyfactside_ddl.sql"
OUT_CREATE = "docs/avyfactside_create_369_not_null.sql"
OUT_INSERT = "docs/avyfactside_insert_100_rows.sql"


def clean_col_name(name: str) -> str:
    # replace newline with underscore and collapse non-alnum to underscore
    s = name.replace("\n", "_")
    s = re.sub(r"[^A-Za-z0-9_]", "_", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_")
    if not s:
        s = "COL"
    # ensure it doesn't start with digit
    if s[0].isdigit():
        s = "C_" + s
    return s.upper()


if __name__ == '__main__':
    # load columns list
    with open(COLS_JSON, "r", encoding="utf-8") as f:
        cols_raw = json.load(f)

    cols_clean = [clean_col_name(c) for c in cols_raw]

    # Read original DDL and create a NOT NULL version if available
    try:
        with open(DDL_SRC, "r", encoding="utf-8") as f:
            ddl = f.read()
        create_sql = ddl.replace(" NULL", " NOT NULL")
    except FileNotFoundError:
        # fallback: generate CREATE from columns
        create_lines = ["DROP TABLE IKOROSTELEV.AVY_FACT_SIDE;", "", "CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE ("]
        for c in cols_clean:
            create_lines.append(f"    {c} VARCHAR2(4000) NOT NULL,")
        # remove trailing comma on last line
        create_lines[-1] = create_lines[-1].rstrip(',')
        create_lines.append(");")
        create_sql = "\n".join(create_lines)

    # write create SQL
    with open(OUT_CREATE, "w", encoding="utf-8") as f:
        f.write(f"-- Generated: {datetime.utcnow().isoformat()}Z\n")
        f.write(create_sql)

    # generate INSERT that creates 100 sample rows using LEVEL
    n = len(cols_clean)
    select_exprs = []
    for i, c in enumerate(cols_clean, start=1):
        # produce stable sample values per column
        select_exprs.append(f"'S{i:03d}_' || LEVEL")

    select_block = ",\n       ".join(select_exprs)

    insert_sql = (
        "-- Generated sample insert: 100 rows\n"
        "-- Insert uses positional column order; ensure this matches your target table.\n"
        "INSERT INTO IKOROSTELEV.AVY_FACT_SIDE\n"
        "SELECT\n       "
        + select_block
        + "\nFROM dual\nCONNECT BY LEVEL <= 100;\n"
    )

    with open(OUT_INSERT, "w", encoding="utf-8") as f:
        f.write(f"-- Generated: {datetime.utcnow().isoformat()}Z\n")
        f.write(insert_sql)

    print("Wrote:", OUT_CREATE)
    print("Wrote:", OUT_INSERT)
