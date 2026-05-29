"""Fix T. alias → TXN. and test."""
import re, requests

BASE = "http://127.0.0.1:8550"
DS = 3

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

# Fix T. → TXN. (only as table alias ref, not T.D in column names etc)
# Replace \bT\. that's a table alias usage (T.TD, T.SRC_STM_ID, T.AR_ID)
original_count = len(re.findall(r'\bT\.', sql))
sql2 = re.sub(r'\bT\.', 'TXN.', sql)
new_count = len(re.findall(r'\bTXN\.', sql2))
print(f"Fixed T. → TXN.: {original_count} replacements made")

# Save
with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed2.sql", "w") as f:
    f.write(sql2)

lines = sql2.splitlines()
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))
select_lines = lines[sel_start:]

def try_count(n_lines, label=""):
    chunk = "\n".join(select_lines[:n_lines])
    test_sql = f"SELECT COUNT(*) AS CNT FROM (\n{chunk}\n)"
    r = requests.post(
        f"{BASE}/api/datasources/{DS}/query",
        json={"sql": test_sql, "row_limit": 1},
        timeout=120,
    )
    if r.status_code == 500:
        detail = r.json().get("detail", {})
        err = str(detail.get("error", ""))[:120]
        print(f"  FAIL{' ['+label+']' if label else ''}: {err}")
        return False, err
    else:
        d = r.json()
        print(f"  PASS{' ['+label+']' if label else ''}: rows={d.get('rows', [])}")
        return True, ""

total = len(select_lines)
print(f"SELECT lines: {total}")

print("\n--- Full SELECT with T.→TXN. fix ---")
ok, err = try_count(total, "full")

if not ok:
    # Try binary search again
    from_line = next(i for i, l in enumerate(select_lines) if l.strip().upper().startswith("FROM"))
    print(f"\nBinary search (FROM at sel-line {from_line+1}):")
    lo = from_line
    hi = from_line + 60  # first 60 JOINs
    while hi - lo > 2:
        mid = (lo + hi) // 2
        ok2, err2 = try_count(mid, f"lines-{mid}")
        if ok2:
            lo = mid
        else:
            hi = mid
    abs_lo = sel_start + lo + 1
    abs_hi = sel_start + hi + 1
    print(f"\nBug between sel-lines {lo}-{hi} (abs lines {abs_lo}-{abs_hi})")
    print("Context:")
    for i in range(lo, min(hi+3, len(select_lines))):
        print(f"  sel-{i+1}/abs-{sel_start+i+1}: {repr(select_lines[i])}")
