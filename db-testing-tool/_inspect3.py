import json, re

with open(r"C:\GIT_Repo\db-testing-tool\data\insert_fixed.sql") as f:
    sql = f.read()

lines = sql.split("\n")
print(f"Total lines: {len(lines)}, chars: {len(sql)}")

# Show lines around fix 2 area (comment)
print("\n=== Lines 840-852 (comment fix area) ===")
for i, l in enumerate(lines[839:852], 840):
    print(f"{i:4}: {repr(l)}")

# Find ALL lines with ";" 
print("\n=== All semicolons ===")
for i, l in enumerate(lines):
    if ";" in l:
        print(f"{i+1:4}: {repr(l)}")

# Find all lines with "(" not part of a function call pattern
# (check for unmatched parens / inline comments)
print("\n=== Lines with bare '(' likely inline comments ===")
# A parenthetical comment would be a ( followed by UPPERCASE word
bad_parens = [(i+1, l) for i, l in enumerate(lines)
              if re.search(r'\s+\((?:THIS|USING|ADDED|NOTE|FIXME|TODO)', l, re.IGNORECASE)]
for ln, l in bad_parens:
    print(f"{ln:4}: {repr(l[:120])}")
