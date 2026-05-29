"""Narrower binary search for ORA-00907 location."""
import requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))
select_lines = lines[sel_start:]
from_line = next(i for i, l in enumerate(select_lines) if l.strip().upper().startswith("FROM"))
total = len(select_lines)
print(f"FROM at sel-relative line {from_line+1}, total={total}")

def try_count(n_lines):
    chunk = "\n".join(select_lines[:n_lines])
    test_sql = f"SELECT COUNT(*) AS CNT FROM (\n{chunk}\n)"
    r = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": test_sql, "row_limit": 1},
        timeout=60,
    )
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        err = str(detail.get("error", ""))[:80]
        return False, err
    else:
        d = r.json()
        return True, str(d.get('rows', []))

# Binary search between from_line and 424
lo = from_line
hi = 424  # First quarter fails
while hi - lo > 2:
    mid = (lo + hi) // 2
    ok, msg = try_count(mid)
    abs_line = sel_start + mid
    print(f"lines 1-{mid} (abs line {abs_line}): {'PASS' if ok else 'FAIL'} {msg}")
    if ok:
        lo = mid
    else:
        hi = mid

print(f"\nBug is between SELECT-relative lines {lo} and {hi}")
print(f"Absolute lines: {sel_start + lo + 1} to {sel_start + hi + 1}")

# Show those lines
print(f"\n=== SELECT lines {lo+1} to {hi+3} ===")
for i in range(lo, min(hi + 3, len(select_lines))):
    print(f"  sel-{i+1} / abs-{sel_start+i+1}: {repr(select_lines[i])}")
