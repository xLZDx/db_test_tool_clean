#!/usr/bin/env python3
"""Generate DDL, INSERT, and validation scripts for AVY_FACT_SIDE control table test suite."""
import json
from pathlib import Path

# Load columns from DRD
with open('drd_columns.json', 'r') as f:
    all_cols = json.load(f)

# Filter out metadata rows (first 9 rows that start with numbers and dashes)
# Actual columns start from "Logical Name of Attribute" onwards
data_cols = [col for col in all_cols if not col.startswith(('2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.', 'Note:', 'Lighthouse'))]

# Clean up column names - remove special characters, make them SQL-safe
def sanitize_col_name(col):
    """Convert readable column name to valid SQL identifier."""
    col = col.strip()
    # Replace spaces and special chars with underscores
    col = col.replace(' ', '_')
    col = col.replace('(', '')
    col = col.replace(')', '')
    col = col.replace('-', '')
    col = col.replace('/', '_')
    col = col.replace(',', '')
    col = col.replace('.', '')
    # Ensure it doesn't start with a number
    while col and col[0].isdigit():
        col = col[1:]
    # Truncate to 30 chars if needed (Oracle limit)
    col = col[:30]
    return col.upper()

sql_safe_cols = [sanitize_col_name(col) for col in data_cols]
# Remove duplicates while preserving order
seen = set()
final_cols = []
for col in sql_safe_cols:
    if col and col not in seen:
        final_cols.append(col)
        seen.add(col)

print(f"Extracted {len(final_cols)} valid data columns from {len(all_cols)} total")
print(f"\nFirst 10 columns: {final_cols[:10]}")
print(f"Last 10 columns: {final_cols[-10:]}\n")

# Generate DDL statement
ddl_statement = """DROP TABLE IKOROSTELEV.AVY_FACT_SIDE;

CREATE TABLE IKOROSTELEV.AVY_FACT_SIDE (
    """
ddl_cols = []
for col in final_cols:
    ddl_cols.append(f"    {col} VARCHAR2(4000) NULL")

ddl_statement += ",\n".join(ddl_cols)
ddl_statement += "\n);"

# Generate INSERT statement columns clause
insert_cols_str = ",\n    ".join(final_cols)
insert_select_str = ",\n    ".join([f"NULL  -- {col}" for col in final_cols])

insert_statement = f"""INSERT INTO IKOROSTELEV.AVY_FACT_SIDE (
    {insert_cols_str}
)
SELECT
    {insert_select_str}
FROM DUAL;"""

# Generate validation SQL
validation_sql = """SELECT
    COUNT(*) as total_rows,
    SUM(CASE WHEN ROWID IS NULL THEN 1 ELSE 0 END) as null_rowid_count
FROM IKOROSTELEV.AVY_FACT_SIDE;"""

# Save to files
print("Generating SQL scripts...")

with open('avyfactside_ddl.sql', 'w') as f:
    f.write(ddl_statement)
print(f"✓ Created: avyfactside_ddl.sql ({len(ddl_statement)} bytes)")

with open('avyfactside_insert.sql', 'w') as f:
    f.write(insert_statement)
print(f"✓ Created: avyfactside_insert.sql ({len(insert_statement)} bytes)")

with open('avyfactside_validation.sql', 'w') as f:
    f.write(validation_sql)
print(f"✓ Created: avyfactside_validation.sql ({len(validation_sql)} bytes)")

# Save column list
with open('avyfactside_columns.json', 'w') as f:
    json.dump(final_cols, f, indent=2)
print(f"✓ Created: avyfactside_columns.json ({len(final_cols)} columns)")

print(f"\nSummary:")
print(f"  Table: IKOROSTELEV.AVY_FACT_SIDE")
print(f"  Columns: {len(final_cols)}")
print(f"  Target schema: CCAL_REPL_OWNER (via schema bug fix)")
print(f"\nNext step: Create TFS test plan and suites with these scripts")
