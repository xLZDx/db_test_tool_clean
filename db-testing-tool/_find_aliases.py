"""Find all single-letter table aliases used and check which are defined vs. just used."""
import re

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()

# Find all table definitions: "TABLE_NAME ALIAS" patterns in FROM/JOIN
table_defs = re.findall(
    r'\b(?:FROM|JOIN)\s+[\w.]+\s+([A-Z][A-Z0-9_]*)\b',
    sql, re.IGNORECASE
)
table_defs_upper = {t.upper() for t in table_defs}
print(f"Defined aliases ({len(table_defs_upper)}): {sorted(table_defs_upper)[:30]}...")

# Find all short aliases (1-3 chars) that appear as table refs like ALIAS.COLUMN
used_aliases = set(re.findall(r'\b([A-Z]{1,3})\.[A-Z_]', sql))
print(f"\nShort aliases used (1-3 chars): {sorted(used_aliases)}")
undefined_short = used_aliases - table_defs_upper
print(f"Potentially undefined short aliases: {sorted(undefined_short)}")

# Find all references to T.
t_refs = [(i+1, l) for i, l in enumerate(lines) if re.search(r'\bT\.', l)]
print(f"\nLines with 'T.' ({len(t_refs)} total):")
for ln, l in t_refs[:20]:
    print(f"  line {ln}: {l[:100]}")

# Check if TXN has TD and SRC_STM_ID columns 
print("\n=== Check: which columns does TXN alias use ===")
txn_cols = set(m.group(1) for m in re.finditer(r'\bTXN\.([A-Z_]+)', sql))
print(f"Columns referenced as TXN.X: {sorted(txn_cols)[:30]}")

# T.TD vs TXN.TD
t_cols = set(m.group(1) for m in re.finditer(r'\bT\.([A-Z_]+)', sql))
print(f"Columns referenced as T.X: {sorted(t_cols)}")
