"""
_build_drd_insert.py

Reads DRD_Activity_Fact.xlsx sheet "Table-View (2)" and builds a clean,
369-column INSERT INTO IKOROSTELEV.AVY_FACT_SIDE based on DRD mapping:
  Col B  = target column
  Col Y  = source schema
  Col Z  = source table
  Col AA = source attribute / column
  Col AD = transformation / join condition

Strategy:
  - NOT NULL cols: always use reliable NVL fallback expressions
  - TXN direct cols: TXN.src_col
  - Lookup cols with parseable "X.a = Y.b" join in col AD:
      LEFT JOIN schema.table ALIAS_N ON ALIAS_N.join_col = TXN.ref_col
      then ALIAS_N.src_col AS target_col
  - Lookup cols with unparseable / partial SQL transforms: NULL AS target_col
  - Cols with no source: NULL AS target_col

Safety rules applied to generated SQL:
  - All identifiers stripped of spaces
  - No inline parenthetical comments
  - No semicolons inside the query
  - Alias names capped at 28 chars
  - ROWNUM <= 5 to limit to first 5 TXN rows
"""

import openpyxl, re, requests, json, os

BASE = "http://127.0.0.1:8550"
DS = 3
DRD_PATH = "DRD_Activity_Fact.xlsx"
SHEET = "Table-View (2)"
TARGET = "IKOROSTELEV.AVY_FACT_SIDE"
MAIN_FROM = "CCAL_REPL_OWNER.TXN TXN"

# TXN columns that appear in DRD transforms but DON'T exist in the live table.
INVALID_TXN_COLS = {"CCY_CODE"}

# Tables known to have many rows per TXN (fan-out risk).
# These use scalar subqueries (ROWNUM <= 1) instead of LEFT JOINs to avoid
# exploding the row count.
SCALAR_SUBQUERY_TABLES = {
    "APA",          # APASEC/APACSH: multiple records per TXN
    "TXN_RLTNP",    # relationship bridge: many related TXNs per TXN
    "TXN_AVY_CL",   # advisory category bridge: many per TXN
    "AR_DIM",       # time-effective history: multiple rows per AR_ID
    "J$TXN",        # flashback/audit table: unknown cardinality
}

# ─── NOT NULL columns: always use these guaranteed-non-null expressions ───────
NOT_NULL_EXPR = {
    "EXG_DIM_ID":               "-1",
    "AR_DIM_ID":                "NVL(TXN.AR_ID, -1)",
    "OFST_AR_DIM_ID":           "-1",
    "ACG_TP_DIM_ID":            "-1",
    "CASH_POS_TP_DIM_ID":       "-1",
    "SBC_CCY_DIM_ID":           "-1",
    "SEC_PD_DIM_ID":            "-1",
    "CASH_PD_DIM_ID":           "-1",
    "LGCY_CNCL_CMPLN_RSN_DIM_ID":  "NVL(TXN.LGCY_CNCL_CMPLN_RSN_TP_ID, -1)",
    "LGCY_CNCL_CMPLN_SRC_DIM_ID":  "NVL(TXN.LGCY_CNCL_CMPLN_SRC_TP_ID, -1)",
    "LGCY_MKT_TP_DIM_ID":       "NVL(TXN.LGCY_MKT_TP_ID, -1)",
    "LGCY_TRD_CPCTY_TP_DIM_ID": "NVL(TXN.LGCY_TRD_CPCTY_TP_ID, -1)",
    "SRC_PCS_TP_DIM_ID":        "NVL(TXN.SRC_PCS_TP_ID, -1)",
    "SRC_ENTR_CNL_TP_DIM_ID":   "-1",
    "TRD_SLCT_TP_DIM_ID":       "-1",
    "TXN_SRC_STM_DIM_ID":       "-1",
    "REL_TXN_SRC_STM_DIM_ID":   "-1",
    "BKR_AR_DIM_ID":            "NVL(TXN.AR_ID, -1)",
    "TD_DIM_ID":                "-1",
    "TD":                       "NVL(TXN.TD, SYSDATE)",
    "SD_DIM_ID":                "-1",
    "TXN_ID":                   "TXN.TXN_ID",
    "CRT_DTM":                  "NVL(TXN.CRT_DTM, SYSDATE)",
    "CRT_USR_NM":               "NVL(TXN.CRT_USR_NM, 'SYSTEM')",
    "ACTV_F":                   "NVL(TXN.ACTV_F, 'Y')",
    "LAST_UDT_USR_NM":          "NVL(TXN.LAST_UDT_USR_NM, 'SYSTEM')",
    "LAST_UDT_DTM":             "NVL(TXN.LAST_UDT_DTM, SYSDATE)",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def gv(row, idx):
    """Get cell value as stripped string or ''."""
    if idx < len(row) and row[idx] is not None:
        return str(row[idx]).strip()
    return ""

def clean_id(s):
    """Strip all whitespace from an identifier string."""
    return re.sub(r"\s+", "", s).upper() if s else ""

def extract_col_name(s):
    """Extract just the leading Oracle identifier from a DRD src_col cell.
    Handles cells like 'SRC_STM_ID (from T2)' → 'SRC_STM_ID'.
    """
    if not s:
        return ""
    m = re.match(r"([A-Za-z_$][A-Za-z0-9_$#]*)", s.strip())
    return m.group(1).upper() if m else ""

def cap_alias(s, maxlen=28):
    """Truncate alias to Oracle-safe length."""
    return s[:maxlen]

# Aliases that represent TXN in DRD transform free-text.
_TXN_ALIASES = {"T", "TXN"}


def extract_join_parts(transform_text, src_table=""):
    """
    Extract (table_join_col, txn_col) for the source table from col AD text.

    Strategy:
    1. Multi-line SQL: find the JOIN line containing src_table, then look for
       an ON X.col = T.col clause on that line or the next 3 lines.
       If the ON condition doesn't reference TXN on one side → multi-hop → None.
    2. Single-line ("Look up using ...") → generic first-match scan.

    Returns (table_col, txn_col) or None.
    """
    if not transform_text:
        return None
    raw = str(transform_text)
    # Strip parenthetical analyst notes
    clean_text = re.sub(r"\([^)]*\)", " ", raw)
    # Strip trailing semicolons (not mid-expression)
    clean_text = re.sub(r";\s*$", "", clean_text, flags=re.MULTILINE)

    lines = clean_text.split("\n")
    is_multiline = len(lines) > 1

    if is_multiline and src_table:
        src_upper = src_table.upper()
        # Use word-boundary pattern so "AVY_CL" doesn't match inside "TXN_AVY_CL"
        tbl_pattern = re.compile(r"\b" + re.escape(src_upper) + r"\b", re.IGNORECASE)
        for i, line in enumerate(lines):
            lu = line.upper()
            if tbl_pattern.search(lu) and ("JOIN" in lu or "FROM" in lu):
                # Combine this line + next 3 for the ON clause search
                combined = " ".join(lines[i : i + 4])
                m = re.search(
                    r"\bON\b\s+(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)",
                    combined, re.IGNORECASE
                )
                if m:
                    t1, c1, t2, c2 = m.groups()
                    if t1.upper() in _TXN_ALIASES:
                        if c1.upper() in INVALID_TXN_COLS:
                            return None
                        return (c2.upper(), c1.upper())
                    elif t2.upper() in _TXN_ALIASES:
                        if c2.upper() in INVALID_TXN_COLS:
                            return None
                        return (c1.upper(), c2.upper())
                    # ON clause found but neither side is TXN → multi-hop
                    return None
        return None   # src_table not in any JOIN line

    # Single-line / "Look up using" style: generic scan
    for m in re.finditer(r"\b(\w+)\.(\w+)\s*=\s*(\w+)\.(\w+)", clean_text, re.IGNORECASE):
        t1, c1, t2, c2 = m.groups()
        if t1.upper() in _TXN_ALIASES:
            if c1.upper() in INVALID_TXN_COLS:
                return None
            return (c2.upper(), c1.upper())
        elif t2.upper() in _TXN_ALIASES:
            if c2.upper() in INVALID_TXN_COLS:
                return None
            return (c1.upper(), c2.upper())
    return None


# ─── Parse DRD ────────────────────────────────────────────────────────────────
print(f"Loading {DRD_PATH} …")
wb = openpyxl.load_workbook(DRD_PATH, read_only=True, data_only=True)
ws = wb[SHEET]
all_rows = list(ws.iter_rows(values_only=True))

drd = []        # list of dicts in DRD order
seen_targets = set()

for i in range(12, len(all_rows)):      # data starts at row 13 (index 12)
    r = all_rows[i]
    raw_target = gv(r, 1)
    target = clean_id(raw_target)
    if not target or len(target) > 60:
        continue
    if target in seen_targets:
        continue
    seen_targets.add(target)

    src_schema = clean_id(gv(r, 24)) or "CCAL_REPL_OWNER"
    src_table  = clean_id(gv(r, 25))
    src_col    = extract_col_name(gv(r, 26))
    transform  = gv(r, 29)

    drd.append({
        "target":     target,
        "src_schema": src_schema,
        "src_table":  src_table,
        "src_col":    src_col,
        "transform":  transform,
    })

print(f"Parsed {len(drd)} unique DRD target columns")

# Load actual AVY_FACT_SIDE column SIZE info (from Oracle all_tab_columns)
col_sizes = {}
col_sizes_path = "data/avyfactside_col_sizes.json"
if os.path.exists(col_sizes_path):
    with open(col_sizes_path, encoding="utf-8") as f:
        col_sizes = json.load(f)
    print(f"Loaded {len(col_sizes)} column size entries from {col_sizes_path}")

def apply_varchar_limit(expr, tgt_col):
    """Wrap expr with SUBSTR(expr, 1, N) when target is VARCHAR2(N<=200).
    Prevents ORA-12899 when source values exceed target column size.
    Skips NULL, pure numeric literals, date functions, and already-SUBSTR'd exprs.
    """
    if expr == "NULL":
        return expr
    col_info = col_sizes.get(tgt_col, {})
    if col_info.get("dtype") != "VARCHAR2":
        return expr   # NUMBER, DATE, etc. — no truncation needed
    max_len = col_info.get("length")
    if not max_len or max_len > 200:
        return expr   # Large VARCHAR2 — unlikely to overflow
    if re.match(r"^-?\d+(\.\d+)?$", expr.strip()):
        return expr   # Pure numeric literal
    if "SYSDATE" in expr.upper():
        return expr   # Date expression
    if "SUBSTR" in expr.upper():
        return expr   # Already wrapped
    return f"SUBSTR({expr}, 1, {max_len})"

# Load actual AVY_FACT_SIDE column order from the saved JSON (ground truth)
cols_json_path = "data/avyfactside_cols.json"
if os.path.exists(cols_json_path):
    with open(cols_json_path, encoding="utf-8") as f:
        cols_data = json.load(f)
    # Expecting list of {name, nullable, data_type} or similar
    if isinstance(cols_data, list):
        if isinstance(cols_data[0], dict):
            actual_cols = [c.get("name", c.get("COLUMN_NAME", "")).upper() for c in cols_data]
        else:
            actual_cols = [str(c).upper() for c in cols_data]
    elif isinstance(cols_data, dict):
        actual_cols = [k.upper() for k in cols_data.keys()]
    else:
        actual_cols = []
    print(f"Loaded {len(actual_cols)} actual table columns from {cols_json_path}")
else:
    actual_cols = [m["target"] for m in drd]
    print("No cols JSON found – using DRD column order")

# Build a lookup: target → DRD mapping entry
drd_by_target = {m["target"]: m for m in drd}


# ─── Build SQL pieces ─────────────────────────────────────────────────────────
# Two-pass approach:
#   Pass 1: walk DRD rows in actual_cols order, extract join parts per row,
#           register each unique (schema, table, table_col, txn_col) join once.
#   Pass 2: build SELECT list using the assigned aliases.

# join_registry: (schema, table, table_col, txn_col) → alias
join_registry = {}
alias_counts  = {}   # table_base → int

def get_alias(schema, table, table_col, txn_col):
    key = (schema, table, table_col, txn_col)
    if key in join_registry:
        return join_registry[key]
    base = cap_alias(table, 20)
    alias_counts[base] = alias_counts.get(base, 0) + 1
    alias = cap_alias(f"{base}_{alias_counts[base]}")
    join_registry[key] = alias
    return alias

# ── Pass 1: register all needed joins ──────────────────────────────────────
for target in actual_cols:
    if target in NOT_NULL_EXPR:
        continue
    m = drd_by_target.get(target)
    if m is None:
        continue
    src_table = m["src_table"]
    src_schema = m["src_schema"]
    src_col   = m["src_col"]
    transform = m["transform"]
    if src_table and src_table != "TXN" and src_col:
        # Skip fan-out tables in the join registry — they use scalar subqueries
        if src_table not in SCALAR_SUBQUERY_TABLES:
            parts = extract_join_parts(transform, src_table)
            if parts:
                get_alias(src_schema, src_table, parts[0], parts[1])

# ── Pass 2: build SELECT + JOIN clauses ──────────────────────────────────────
col_list     = []
select_exprs = []
stats = {"not_null": 0, "txn_direct": 0, "lookup_join": 0, "null_complex": 0, "null_no_src": 0}

for target in actual_cols:
    m = drd_by_target.get(target)
    col_list.append(target)

    # 1. NOT NULL fallback
    if target in NOT_NULL_EXPR:
        select_exprs.append(NOT_NULL_EXPR[target])
        stats["not_null"] += 1
        continue

    # 2. No DRD mapping
    if m is None:
        select_exprs.append("NULL")
        stats["null_no_src"] += 1
        continue

    src_table  = m["src_table"]
    src_schema = m["src_schema"]
    src_col    = m["src_col"]
    transform  = m["transform"]

    # 3. Direct TXN
    if src_table == "TXN" and src_col:
        select_exprs.append(f"TXN.{src_col}")
        stats["txn_direct"] += 1
        continue

    # 4. Lookup with extractable join
    if src_table and src_col:
        parts = extract_join_parts(transform, src_table)
        if parts:
            # 4a. Fan-out table → scalar subquery (ROWNUM <= 1 per TXN row)
            if src_table in SCALAR_SUBQUERY_TABLES:
                tbl_col, txn_col = parts
                sq = (f"(SELECT {src_col} FROM {src_schema}.{src_table} "
                      f"WHERE {tbl_col} = TXN.{txn_col} AND ROWNUM <= 1)")
                select_exprs.append(sq)
                stats["lookup_join"] += 1
                continue
            # 4b. Dimension table → LEFT JOIN alias
            alias = join_registry.get((src_schema, src_table, parts[0], parts[1]))
            if alias:
                select_exprs.append(f"{alias}.{src_col}")
                stats["lookup_join"] += 1
                continue

    # 5. NULL fallback
    select_exprs.append("NULL")
    stats["null_complex"] += 1

# ── Apply VARCHAR2 size limits across all SELECT expressions ─────────────────
select_exprs = [apply_varchar_limit(e, c) for e, c in zip(select_exprs, col_list)]

# ── Build unique JOIN clauses in registry order ───────────────────────────────
seen_aliases = set()
join_clauses = []
for (schema, table, tbl_col, txn_col), alias in join_registry.items():
    if alias in seen_aliases:
        continue
    seen_aliases.add(alias)
    join_clauses.append(
        f"LEFT JOIN {schema}.{table} {alias}\n"
        f"    ON {alias}.{tbl_col} = TXN.{txn_col}"
    )

print(f"\nColumn coverage:")
for k, v in stats.items():
    print(f"  {k:20}: {v}")
print(f"\nTotal INSERT cols: {len(col_list)}")
print(f"Total LEFT JOINs:  {len(join_clauses)}")


# ─── Assemble SQL ─────────────────────────────────────────────────────────────
indent4 = "    "

insert_cols = f",\n{indent4}".join(col_list)
select_parts = []
for col, expr in zip(col_list, select_exprs):
    select_parts.append(f"{indent4}{expr} AS {col}")
select_body = ",\n".join(select_parts)
join_body = "\n".join(join_clauses)

sql = (
    f"INSERT INTO {TARGET} (\n"
    f"{indent4}{insert_cols}\n"
    f")\n"
    f"SELECT\n"
    f"{select_body}\n"
    f"FROM {MAIN_FROM}\n"
    f"{join_body}\n"
    f"WHERE TXN.ACTV_F = 'Y'\n"
    f"AND ROWNUM <= 5"
)

print(f"\nGenerated SQL: {len(sql):,} chars, {sql.count(chr(10))} lines")

# Save for inspection
os.makedirs("data", exist_ok=True)
out_path = "data/drd_full_insert.sql"
with open(out_path, "w", encoding="utf-8") as f:
    f.write(sql)
print(f"Saved to {out_path}")

# Quick sanity checks on the generated SQL
issues = []
lines_sql = sql.split("\n")
for i, line in enumerate(lines_sql, 1):
    # No stray semicolons inside the body (only at very end if needed)
    if ";" in line and i < len(lines_sql):
        issues.append(f"  Line {i}: stray semicolon: {line[:80]}")
    # No raw parenthetical prose (heuristic: long paren strings with spaces)
    if re.search(r"\([A-Za-z ]{30,}\)", line):
        issues.append(f"  Line {i}: suspicious inline comment: {line[:80]}")

if issues:
    print(f"\n⚠  Potential issues found ({len(issues)}):")
    for iss in issues[:20]:
        print(iss)
else:
    print("\nNo obvious syntax issues detected")

# Show first 30 SELECT lines and last 10 JOIN/WHERE lines
print("\n--- First 30 lines ---")
for i, line in enumerate(lines_sql[:30], 1):
    print(f"{i:4}: {line}")
print("...")
print("--- Last 20 lines ---")
for i, line in enumerate(lines_sql[-20:], len(lines_sql)-19):
    print(f"{i:4}: {line}")
