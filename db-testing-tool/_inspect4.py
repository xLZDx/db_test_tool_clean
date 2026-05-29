"""Search for line comments and other issues in the SQL."""
with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()

# Line comments
print("=== Lines with -- comments ===")
for i, l in enumerate(lines):
    if '--' in l:
        # But not inside a string
        s = l.replace("'--'", "   ").replace("'-'", "  ")
        if '--' in s:
            print(f"  line {i+1}: {l[:120]}")

# Lines with =  but missing closing paren context
print("\n=== Check NVL calls ===")
import re
nvl_count = sum(1 for m in re.finditer(r'NVL\s*\(', sql, re.I))
print(f"NVL( count: {nvl_count}")

# Look for any odd patterns
print("\n=== Lines with potential issues ===")
for i, l in enumerate(lines):
    if ')AND' in l or ')OR' in l or ')WHERE' in l:
        print(f"  line {i+1}: {l[:120]}")
    # Check for double-quoted identifiers that Oracle might choke on
    if '"' in l:
        print(f"  double-quote at line {i+1}: {l[:120]}")

# Find the FROM clause start line
for i, l in enumerate(lines):
    if re.match(r'\s*FROM\s+\w', l, re.I):
        print(f"\nFROM clause at line {i+1}: {l[:80]}")
        break

# Show lines 370-375
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))
print(f"\nSELECT at line {sel_start+1}")
print(f"\n=== Lines {sel_start+1} to {sel_start+5} ===")
for i in range(sel_start, min(sel_start+5, len(lines))):
    print(f"  {i+1}: {repr(lines[i])}")

# Binary search - try sending just first 100 SELECT lines through the server
# (via COUNT wrapper) to isolate the problem area
print(f"\n=== Summary ===")
print(f"Total lines: {len(lines)}, SELECT starts at: {sel_start+1}")
select_lines = lines[sel_start:]
print(f"SELECT portion: {len(select_lines)} lines, {sum(len(l) for l in select_lines)} chars")
