import json

with open(r"C:\GIT_Repo\db-testing-tool\data\phase2_analyze_result.json") as f:
    d = json.load(f)

sql = d["generated_sql"]
lines = sql.split("\n")

print("=== Lines 805-855 (around rogue semicolon at 811) ===")
for i, l in enumerate(lines[804:855], 805):
    print(f"{i:4}: {l}")

print()
print("=== Lines 930-945 (table name issue at 936) ===")
for i, l in enumerate(lines[929:945], 930):
    print(f"{i:4}: {l}")
