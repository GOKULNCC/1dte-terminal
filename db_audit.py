import sqlite3
conn = sqlite3.connect('trading.db')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
print('Tables:', tables)
print()
for t in sorted(tables):
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info({t})').fetchall()]
    cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'  {t} ({cnt} rows): {cols}')
conn.close()
