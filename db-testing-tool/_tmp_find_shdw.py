import json

with open(r'c:\GIT_Repo\db-testing-tool\data\local_kb\schema_kb_ds_2.json', 'r') as f:
    kb = json.load(f)

pdm = kb['pdm']
for s in pdm['schemas']:
    schema_name = s.get('schema', '')
    tables = s.get('tables', [])
    if isinstance(tables, list):
        for t in tables:
            tname = t.get('table', '') if isinstance(t, dict) else ''
            if 'SHDW_TXN_TP' in tname.upper():
                print(f"Found: {schema_name}.{tname}")
    elif isinstance(tables, dict):
        for tname in tables:
            if 'SHDW_TXN_TP' in tname.upper():
                print(f"Found: {schema_name}.{tname}")

# Also check DRD for what schema it says
import openpyxl
wb = openpyxl.load_workbook(r'C:\Users\ikorostelev\Downloads\DRD_Activity_Fact.xlsx', read_only=True, data_only=True)
ws = wb['Table-View']
for row in ws.iter_rows(min_row=12, values_only=True):
    if row and row[1] and 'SHDW_TXN_TP' in str(row[1]).upper():
        print(f"DRD row: physical={row[1]}, source_schema={row[24]}, source_table={row[25]}")
