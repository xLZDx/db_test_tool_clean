import json
from pathlib import Path

LIVE_COLS_PATH = Path(r"c:\GIT_Repo\db-testing-tool\reports\avyfactside_live_columns.json")
OUTPUT_DIR = Path(r"c:\GIT_Repo\db-testing-tool\reports")

with open(LIVE_COLS_PATH, "r", encoding="utf-8-sig") as f:
    data = json.load(f)

col_names = []
col_values = []
for row in data:
    name = row["COLUMN_NAME"]
    dtype = row["DATA_TYPE"]
    nullable = row.get("NULLABLE", "Y")
    col_names.append(name)
    
    if nullable == "N":  # NOT NULL - provide defaults
        if dtype == "NUMBER":
            col_values.append("0")
        elif dtype == "DATE":
            col_values.append("SYSDATE")
        elif "TIMESTAMP" in dtype:
            col_values.append("SYSTIMESTAMP")
        elif dtype in ("VARCHAR2", "CHAR"):
            length = row.get("DATA_LENGTH", 100)
            if length and int(length) < 4:
                col_values.append("'X'")
            else:
                col_values.append("'TEST'")
        else:
            col_values.append("NULL")
    else:
        col_values.append("NULL")

lines = ["INSERT INTO IKOROSTELEV.AVY_FACT_SIDE ("]
lines.append("    " + ",\n    ".join(col_names))
lines.append(") SELECT")
lines.append("    " + ",\n    ".join(col_values))
lines.append("FROM DUAL")

insert_path = OUTPUT_DIR / "avyfactside_insert_ikorostelev.sql"
with open(insert_path, "w") as f:
    f.write("\n".join(lines) + ";\n")

not_null_count = sum(1 for r in data if r.get("NULLABLE") == "N")
print(f"Generated INSERT with {len(col_names)} columns ({not_null_count} NOT NULL with defaults)")
