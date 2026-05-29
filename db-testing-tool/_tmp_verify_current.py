"""Quick verification of current state before edits."""
import re
from pathlib import Path
from app.services.control_table_service import analyze_control_table, validate_insert_join_aliases

p = Path(r"C:\Users\ikorostelev\Downloads\DRD_Activity_Fact.xlsx")
b = p.read_bytes()
res = analyze_control_table(
    file_bytes=b, filename=p.name,
    target_schema='IKOROSTELEV', target_table='AVY_FACT_SIDE',
    source_datasource_id=2, target_datasource_id=2,
    control_schema='IKOROSTELEV', main_grain='', manual_sql='',
    selected_fields=None, sheet_name=None,
)
sql = res.get('generated_insert_sql') or ''
from_m = re.search(r'\bFROM\b([\s\S]+)', sql, re.I)
from_clause = from_m.group(1) if from_m else ''

bad_joins = re.findall(r'LEFT JOIN[^\n]+\n\s*ON 1 = 0', sql, re.I)
j_table = 'CCAL_REPL_OWNER.J ' in sql
tax_self = re.findall(r'TXN_SRC_TAX_CODE_LKUP_\d+\.(\w+).*?=.*?TXN_SRC_TAX_CODE_LKUP[_\d]*\.(\w+)', from_clause, re.I)
sel_m = re.search(r'SELECT(.*?)\bFROM\b', sql, re.S | re.I)
sel = sel_m.group(1) if sel_m else ''
all_aliases_used = set(a.upper() for a in re.findall(r'\b([A-Z_][A-Z0-9_]*)\.[A-Z_]', sel, re.I))
join_aliases = set(a.upper() for a in re.findall(r'\b(?:FROM|JOIN)\s+[\w\.\$]+\s+(\w+)', from_clause, re.I))
undefined = all_aliases_used - join_aliases - {'SYSDATE', 'SYSTIMESTAMP', 'DUAL'}

print(f"ON_1_0_JOINS: {len(bad_joins)}")
print(f"J_TABLE_BUG: {j_table}")
print(f"TAX_LKUP_SELF_JOIN: {tax_self}")
print(f"UNDEFINED_ALIASES: {undefined}")
