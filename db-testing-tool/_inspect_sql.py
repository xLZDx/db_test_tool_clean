import json, re

with open(r"C:\GIT_Repo\db-testing-tool\data\phase2_analyze_result.json") as f:
    d = json.load(f)

sql = d["generated_sql"]
lines = sql.split("\n")
print(f"Total lines: {len(lines)}")
print(f"Total chars: {len(sql)}")
print()

# Show first 30 lines (INSERT + column list start)
print("=== First 25 lines ===")
for i, l in enumerate(lines[:25], 1):
    print(f"{i:4}: {l}")

print()
print("=== Last 30 lines ===")
for i, l in enumerate(lines[-30:], len(lines)-29):
    print(f"{i:4}: {l}")

print()
# Find the SELECT keyword (main SELECT)
for i, l in enumerate(lines):
    if l.strip().upper().startswith("SELECT"):
        print(f"SELECT at line {i+1}: {l[:80]}")
        break

# Count semicolons (embedded ones = problem)
semicolons = [(i+1, l) for i, l in enumerate(lines) if ";" in l]
print(f"\nLines with semicolons ({len(semicolons)} total):")
for ln, l in semicolons:
    print(f"  line {ln}: {l[:80]}")

# Check for WITH clause / CTE
for i, l in enumerate(lines[:10]):
    if "WITH" in l.upper():
        print(f"\nCTE at line {i+1}: {l}")
