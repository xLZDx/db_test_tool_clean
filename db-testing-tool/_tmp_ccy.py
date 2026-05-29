import requests
r = requests.post('http://127.0.0.1:8550/api/datasources/3/query',
    json={'sql': "SELECT COLUMN_NAME FROM ALL_TAB_COLUMNS WHERE OWNER='CCAL_REPL_OWNER' AND TABLE_NAME='TXN' AND COLUMN_NAME LIKE '%CCY%' ORDER BY COLUMN_ID", 'row_limit': 20},
    timeout=30)
print("CCY-related TXN columns:")
for row in r.json().get('rows', []):
    print(" ", row['COLUMN_NAME'])
