"""Query Oracle for AVY_FACT_SIDE column sizes."""
import requests, json

r = requests.post('http://127.0.0.1:8550/api/datasources/3/query',
    json={
        'sql': ("SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, NULLABLE "
                "FROM ALL_TAB_COLUMNS "
                "WHERE OWNER = 'IKOROSTELEV' AND TABLE_NAME = 'AVY_FACT_SIDE' "
                "ORDER BY COLUMN_ID"),
        'row_limit': 500
    }, timeout=30)
print('status:', r.status_code)
d = r.json()
rows = d.get('rows', [])
print(f'total cols: {len(rows)}')

# Show VARCHAR2 with small lengths (<= 50)
small = [(row['COLUMN_NAME'], row['DATA_TYPE'], row['DATA_LENGTH'])
         for row in rows if row.get('DATA_TYPE') == 'VARCHAR2' and (row.get('DATA_LENGTH') or 9999) <= 50]
print(f'\nVARCHAR2 cols with length <= 50 ({len(small)}):')
for col, dt, sz in sorted(small, key=lambda x: x[2]):
    print(f'  {col}: {dt}({sz})')

# Save all as JSON for builder to use
col_sizes = {row['COLUMN_NAME']: {'dtype': row['DATA_TYPE'], 'length': row['DATA_LENGTH']}
             for row in rows}
with open('data/avyfactside_col_sizes.json', 'w') as f:
    json.dump(col_sizes, f, indent=2)
print('\nSaved to data/avyfactside_col_sizes.json')
