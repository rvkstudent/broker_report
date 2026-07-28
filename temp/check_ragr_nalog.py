"""Проверить данные Русагро в налоговых отчётах."""
import sqlite3, os, sys, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print("=== Русагро в nalog ===")
rows = c.execute("""
    SELECT year, COUNT(*) as deals, 
           ROUND(SUM(income), 2) as total_income,
           ROUND(SUM(amount), 2) as total_amount,
           ROUND(AVG(price), 2) as avg_price,
           ROUND(AVG(quantity), 2) as avg_qty
    FROM nalog 
    WHERE instrument_name LIKE '%Русагро%' OR instrument_code LIKE '%RAGR%'
    GROUP BY year
    ORDER BY year
""").fetchall()
for r in rows:
    print(f"  {r['year']}: deals={r['deals']}, income={r['total_income']}, amount={r['total_amount']}, avg_price={r['avg_price']}, avg_qty={r['avg_qty']}")

# Детальные записи по Русагро
print(f"\n=== Детально Русагро 2025 ===")
rows = c.execute("""
    SELECT deal_date, deal_number, price, quantity, amount, income
    FROM nalog 
    WHERE (instrument_name LIKE '%Русагро%' OR instrument_code LIKE '%RAGR%') AND year='2025'
    ORDER BY deal_date
""").fetchall()
for r in rows:
    print(f"  {r['deal_date']} deal={r['deal_number']:<20} price={r['price']:<10} qty={r['quantity']:>6} amt={r['amount']:>10.2f} income={r['income']:>10.2f}")

c.close()
