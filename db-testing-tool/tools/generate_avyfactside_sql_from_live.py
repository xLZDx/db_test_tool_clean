"""Generate CREATE and INSERT SQL using reports/avyfactside_live_columns.json as source.
Produces docs/avyfactside_create_369_not_null.sql and docs/avyfactside_insert_100_rows.sql (overwrites).
"""
import json
from datetime import datetime

IN_JSON = "reports/avyfactside_live_columns.json"
OUT_CREATE = "docs/avyfactside_create_369_not_null.sql"
OUT_INSERT = "docs/avyfactside_insert_100_rows.sql"


def col_type_spec(col):
    dt = (col.get('DATA_TYPE') or '').upper()
    if dt == 'NUMBER':
        p = col.get('DATA_PRECISION')
        s = col.get('DATA_SCALE')
        if p and s is not None:
            return f"NUMBER({p},{s})"
        if p:
            return f"NUMBER({p})"
        return "NUMBER"
    if dt.startswith('VARCHAR'):
        length = col.get('DATA_LENGTH') or 4000
        return f"VARCHAR2({length})"
    if dt == 'DATE':
        return 'DATE'
    if 'TIMESTAMP' in dt:
        return dt
    # fallback
    return 'VARCHAR2(4000)'


def sample_expr(col, idx):
    dt = (col.get('DATA_TYPE') or '').upper()
    name = col.get('COLUMN_NAME')
    if dt == 'NUMBER':
        # produce a numeric expression varying by LEVEL
        return f"({idx} + LEVEL - 1)"
    if dt == 'DATE':
        return "TO_DATE('2026-05-25','YYYY-MM-DD') + (LEVEL-1)"
    if 'TIMESTAMP' in dt:
        return "TO_TIMESTAMP('2026-05-25 00:00:00','YYYY-MM-DD HH24:MI:SS') + NUMTODSINTERVAL(LEVEL-1,'DAY')"
    if dt.startswith('VARCHAR') or dt == 'VARCHAR2':
        # safe string sample
        safe = name.replace("'", "''")
        return f"'{safe}_' || LEVEL"
    # fallback to string
    safe = name.replace("'", "''")
    return f"'{safe}_' || LEVEL"


if __name__ == '__main__':
    # handle possible BOM
    with open(IN_JSON, 'r', encoding='utf-8-sig') as f:
        cols = json.load(f)

    # create SQL
    lines = [f"-- Generated: {datetime.utcnow().isoformat()}Z", "DROP TABLE IKOROSTELEV.AVY_FACT_SIDE;", "", "CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE ("]
    for c in cols:
        name = c['COLUMN_NAME']
        t = col_type_spec(c)
        lines.append(f"    {name} {t} NOT NULL,")
    # remove trailing comma
    if lines[-1].endswith(','):
        lines[-1] = lines[-1].rstrip(',')
    lines.append(');')

    with open(OUT_CREATE, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))

    # insert SQL
    exprs = [sample_expr(c, i+1) for i,c in enumerate(cols)]
    select_block = ',\n       '.join(exprs)
    insert_sql = (
        f"-- Generated: {datetime.utcnow().isoformat()}Z\n"
        "-- Insert 100 sample rows (positional column order)\n"
        "INSERT INTO IKOROSTELEV.AVY_FACT_SIDE\n"
        "SELECT\n       "
        + select_block
        + "\nFROM dual\nCONNECT BY LEVEL <= 100;\n"
    )

    with open(OUT_INSERT, 'w', encoding='utf-8') as f:
        f.write(insert_sql)

    print('Wrote:', OUT_CREATE)
    print('Wrote:', OUT_INSERT)
