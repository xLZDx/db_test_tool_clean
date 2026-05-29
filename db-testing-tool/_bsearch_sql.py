"""Binary search: find which part of SELECT causes ORA-00907."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))
select_lines = lines[sel_start:]

def try_count(n_lines, label):
    """Try SELECT COUNT(*) FROM (<first n_lines of SELECT>) to isolate ORA-00907."""
    chunk = "\n".join(select_lines[:n_lines])
    test_sql = f"SELECT COUNT(*) AS CNT FROM (\n{chunk}\n)"
    r = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": test_sql, "row_limit": 1},
        timeout=60,
    )
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        err = str(detail.get("error", ""))[:120]
        print(f"  FAIL [{label}, lines 1-{n_lines}]: {err}")
        return False
    else:
        d = r.json()
        print(f"  PASS [{label}, lines 1-{n_lines}]: rows={d.get('rows', [])}")
        return True

# Binary search: 50%, 25/75%...
total = len(select_lines)
print(f"Total SELECT lines: {total}")

# Try full
print("\n--- Full SELECT ---")
try_count(total, "full")

# Try just columns (up to FROM)
from_line = next(i for i, l in enumerate(select_lines) if l.strip().upper().startswith("FROM"))
print(f"\nFROM clause at SELECT-relative line {from_line+1}")

# Try SELECT columns + first FROM only (no JOINs)
print("\n--- Just SELECT col list + FROM (no JOINs) ---")
try_count(from_line + 1, "cols+from")

# Try with half the JOINs
mid = from_line + (total - from_line) // 2
print(f"\n--- First half JOINs (lines 1-{mid}) ---")
ok = try_count(mid, "half")

if not ok:
    # First quarter of JOINs
    q1 = from_line + (total - from_line) // 4
    print(f"\n--- First quarter JOINs (lines 1-{q1}) ---")
    try_count(q1, "quarter")
else:
    # Third quarter
    q3 = from_line + 3*(total - from_line) // 4
    print(f"\n--- Three-quarter JOINs (lines 1-{q3}) ---")
    try_count(q3, "three-quarter")
