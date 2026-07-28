"""Перепарсить 2026 с исправленным парсером."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.parser_nalog import parse_nalog_report

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
print(f"Парсим {fp}...")
year = parse_nalog_report(fp)
print(f"Год: {year}")

# Проверка
import sqlite3
db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)

print("\n=== Все данные 2026 ===")
rows = c.execute("""
    SELECT year, COUNT(*) as deals, ROUND(SUM(income), 2) as total_income
    FROM nalog WHERE year='2026'
    GROUP BY year
""").fetchall()
for r in rows:
    print(f"  {r[0]}: deals={r[1]}, total_income={r[2]}")

print("\n=== 2026 по инструментам ===")
rows = c.execute("""
    SELECT instrument_name, COUNT(*) as deals, ROUND(SUM(income), 2) as total_income,
           ROUND(AVG(price), 2) as avg_price
    FROM nalog WHERE year='2026'
    GROUP BY instrument_name ORDER BY total_income
""").fetchall()
for r in rows:
    print(f"  {r[0]:>30}: deals={r[1]:>4}, income={r[2]:>10.2f}, avg_price={r[3]:>8.2f}")

print("\n=== Русагро 2026 ===")
rows = c.execute("""
    SELECT COUNT(*) as deals, ROUND(SUM(income), 2) as total,
           ROUND(AVG(price), 2) as avg_price, ROUND(AVG(quantity), 1) as avg_qty,
           ROUND(SUM(amount), 2) as total_amount
    FROM nalog WHERE year='2026' AND instrument_name LIKE '%Русагро%'
""").fetchall()
for r in rows:
    print(f"  deals={r[0]}, total_income={r[1]}, avg_price={r[2]}, avg_qty={r[3]}, total_amount={r[4]}")

c.close()
