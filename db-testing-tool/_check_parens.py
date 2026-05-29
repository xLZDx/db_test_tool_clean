"""Find unbalanced parentheses in the fixed SQL."""
with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.splitlines()
sel_start = next(i for i, l in enumerate(lines) if l.strip().upper().startswith("SELECT"))

# Check paren balance in the SELECT subquery
select_sql = "\n".join(lines[sel_start:])
print(f"SELECT SQL: {len(select_sql)} chars")

# Count parens (outside string literals and comments)
depth = 0
in_single = False
in_block = False
paren_issues = []
i = 0
text = select_sql
n = len(text)

while i < n:
    ch = text[i]
    nxt = text[i+1] if i+1 < n else ""

    if in_block:
        if ch == '*' and nxt == '/':
            in_block = False
            i += 2
        else:
            i += 1
        continue

    if in_single:
        if ch == "'" and nxt == "'":
            i += 2  # escaped quote
        elif ch == "'":
            in_single = False
            i += 1
        else:
            i += 1
        continue

    if ch == '/' and nxt == '*':
        in_block = True
        i += 2
        continue

    if ch == "'":
        in_single = True
        i += 1
        continue

    if ch == '(':
        depth += 1
        i += 1
        continue

    if ch == ')':
        depth -= 1
        if depth < 0:
            # Find which line
            line_num = text[:i].count('\n') + sel_start + 1
            context = text[max(0,i-40):i+40].replace('\n',' ')
            paren_issues.append((line_num, depth, f"EXTRA ')' at abs pos {i}: ...{context}..."))
            depth = 0  # Reset to avoid cascade errors
        i += 1
        continue

    i += 1

print(f"Final paren depth: {depth} (should be 0)")
if depth > 0:
    print(f"UNCLOSED {depth} open parens!")
if paren_issues:
    print("Extra close parens:")
    for ln, d, ctx in paren_issues:
        print(f"  Line {ln}: {ctx}")

# Also check the FULL SQL (INSERT + SELECT)
print("\n--- Full SQL paren check ---")
full_sql = sql
depth2 = 0
in_single2 = False
in_block2 = False
i2 = 0
n2 = len(full_sql)
issues2 = []

while i2 < n2:
    ch = full_sql[i2]
    nxt = full_sql[i2+1] if i2+1 < n2 else ""

    if in_block2:
        if ch == '*' and nxt == '/':
            in_block2 = False
            i2 += 2
        else:
            i2 += 1
        continue

    if in_single2:
        if ch == "'" and nxt == "'":
            i2 += 2
        elif ch == "'":
            in_single2 = False
            i2 += 1
        else:
            i2 += 1
        continue

    if ch == '/' and nxt == '*':
        in_block2 = True
        i2 += 2
        continue

    if ch == "'":
        in_single2 = True
        i2 += 1
        continue

    if ch == '(':
        depth2 += 1
        i2 += 1
        continue

    if ch == ')':
        depth2 -= 1
        if depth2 < 0:
            line_num2 = full_sql[:i2].count('\n') + 1
            ctx2 = full_sql[max(0,i2-40):i2+40].replace('\n', ' ')
            issues2.append((line_num2, f"EXTRA ')' at line {line_num2}: ...{ctx2}..."))
            depth2 = 0
        i2 += 1
        continue

    i2 += 1

print(f"Final paren depth: {depth2}")
if issues2:
    print("Extra close parens in full SQL:")
    for ln, ctx in issues2:
        print(f"  {ctx}")
