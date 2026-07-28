"""Поиск MTS и SBER среди сделок."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))

print("=== Все коды с SBER/MTS ===")
rows = c.execute("SELECT DISTINCT security_code, security_name FROM trade WHERE security_code LIKE '%MTS%' OR security_code LIKE '%SBER%' OR security_code LIKE '%MTSS%'").fetchall()
for r in rows:
    print(f"  {r[0]:>20} = {r[1]}")

# Сравнение с LIFO profit
print("\n=== В profit (все брокеры) ===")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit
profit = get_trade_profit()
profit_codes = set(r['security_code'] for r in profit)
for r in profit:
    if 'SBER' in r['security_code'] or 'MTS' in r['security_code'] or 'MTSS' in r['security_code']:
        print(f"  {r['security_code']:>20}: net={r['net_profit']:.2f}")

# Чистые сделки Сбера (последние)
print("\n=== Все сделки SBER в БД ===")
rows = c.execute("""
    SELECT security_code, security_name, COUNT(*) as cnt
    FROM trade
    WHERE security_code LIKE '%SBER%' OR security_name LIKE '%Сбер%'
    GROUP BY security_code
""").fetchall()
for r in rows:
    print(f"  {r[0]:>20} {r[1]}: {r[2]} сделок")

c.close()
