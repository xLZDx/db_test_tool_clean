"""Inspect the full content of suspicious lines."""
with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()

# Print lines 864-876 in full (not truncated)
print("=== Lines 864-876 ===")
for i in range(863, 876):
    print(f"{i+1:4}: {repr(lines[i])}")

print()
print("=== Lines 740-748 ===")
for i in range(739, 748):
    print(f"{i+1:4}: {repr(lines[i])}")
