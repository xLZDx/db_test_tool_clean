import sqlite3
from app.config import DATA_DIR
p = DATA_DIR / 'app.db'
print('DB:', p)
try:
    conn = sqlite3.connect(str(p))
    cur = conn.cursor()
    for row in cur.execute('SELECT id,name,db_type,host,username FROM datasources ORDER BY id'):
        print(row)
    conn.close()
except Exception as e:
    print('ERROR', e)
