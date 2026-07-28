"""Поиск MTS и SBER сделок."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))
c.row_factory = sqlite3.Row

# Ищем все сделки с MTS или SBER
print("=== Сделки MTS/SBER ===")
rows = c.execute("""
    SELECT t.id, t.security_code, t.security_name, t.side, t.quantity, t.price, t.trade_date, t.source, r.contract
    FROM trade t
    LEFT JOIN report r ON r.id = t.report_id
    WHERE t.security_code LIKE '%MTS%' OR t.security_code LIKE '%SBER%' 
       OR t.security_name LIKE '%МТС%' OR t.security_name LIKE '%Сбер%'
    ORDER BY t.trade_date
""").fetchall()
for r in rows:
    print(f"  #{r['id']}: {r['security_code']:>15} {r['security_name']:>25} {r['side']:>4} qty={r['quantity']:>5} price={r['price']:>8.2f} {r['trade_date']} src={r['source']}")

# Смотрим какие security_code бывают
print("\n=== Все security_code (уникальные) ===")
codes = c.execute("SELECT DISTINCT security_code FROM trade ORDER BY security_code").fetchall()
for r in codes:
    print(f"  {r['security_code']}")

# Проверка LIFO-матчинга - все ли инструменты попадают в profit
print("\n=== Что возвращает LIFO profit ===")
import sys, os as os2
sys.path.insert(0, os2.path.dirname(os2.path.dirname(__file__)))
from app.db import get_trade_profit
p = get_trade_profit()
codes_in_profit = set(r['security_code'] for r in p)
print(f"  Инструментов в profit: {len(codes_in_profit)}")
# Каких кодов нет
for r in (x['security_code'] for x in codes):
    if r not in codes_in_profit:
        print(f"  HET B PROFIT: {r}")

c.close()
