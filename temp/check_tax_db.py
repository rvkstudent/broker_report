"""Проверить налоговую сводку в БД."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sqlite3

db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print("=== Налоговая сводка ===")
rows = c.execute("SELECT * FROM nalog_tax_summary ORDER BY year").fetchall()
for r in rows:
    d = dict(r)
    print(f"  {d['year']}:")
    print(f"    Доход брокер:     {d['broker_income']:>12.2f}")
    print(f"    Налогообл. база:  {d['broker_taxable']:>12.2f}")
    print(f"    Начислено налога: {d['broker_tax_calc']:>12.2f}")
    print(f"    Уплачено налога:  {d['broker_tax_paid']:>12.2f}")
    print(f"    Налог к уплате:   {d['broker_tax_due']:>12.2f}")
    print(f"    Дивиденды доход:  {d['depositary_income']:>12.2f}")
    print(f"    ИТОГО доход:      {d['total_income']:>12.2f}")
    print(f"    ИТОГО нал. база:  {d['total_taxable']:>12.2f}")

c.close()
