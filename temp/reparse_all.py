"""Перепарсить все налоговые файлы."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.parser_nalog import parse_nalog_report

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    print(f'Парсим {fn}...')
    parse_nalog_report(fp)

# Проверка
import sqlite3
db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
print("\n=== Налоговая сводка ===")
rows = c.execute("SELECT * FROM nalog_tax_summary ORDER BY year").fetchall()
for r in rows:
    d = dict(r)
    print(f"\n  {d['year']}:")
    for k,v in d.items():
        if k not in ('year','source_file'):
            print(f"    {k}: {v}")
c.close()
