"""Проверить дублирование REPO/комиссий между таблицами."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))

# 1. Есть ли сделки с REPO в trade table (Sber)?
print("=== Сделки с 'РЕПО'/'repo' в trade ===")
cnt = c.execute("SELECT COUNT(*) FROM trade WHERE LOWER(security_name) LIKE '%репо%' OR LOWER(comment) LIKE '%репо%'").fetchone()[0]
print(f"  Найдено: {cnt}")

# 2. Есть ли пересечение deal_number между repo и trade
print("\n=== Пересечение deal_number repo vs trade ===")
cnt = c.execute("""
    SELECT COUNT(*) FROM (
        SELECT DISTINCT r.deal_number FROM repo r
        INTERSECT
        SELECT DISTINCT t.deal_number FROM trade t
    )
""").fetchone()[0]
print(f"  Совпадающих deal_number: {cnt}")

# 3. Есть ли РЕПО в cash_flow
print("\n=== РЕПО в cash_flow ===")
cnt = c.execute("SELECT COUNT(*) FROM cash_flow WHERE LOWER(description) LIKE '%репо%'").fetchone()[0]
print(f"  Найдено: {cnt}")
if cnt:
    rows = c.execute("SELECT description, credit, debit FROM cash_flow WHERE LOWER(description) LIKE '%репо%' LIMIT 10").fetchall()
    for r in rows:
        print(f"  {r[0]:>30} credit={r[1]:>10} debit={r[2]:>10}")

# 4. Комиссии в REPO vs комиссии в trade (Sber)
print("\n=== Суммы комиссий по таблицам (Сбер) ===")
r = c.execute("""
    SELECT 
        ROUND(SUM(COALESCE(broker_fee,0) + COALESCE(exchange_fee,0)), 2) as trade_fees
    FROM trade t
    JOIN report rp ON rp.id = t.report_id
    WHERE rp.contract LIKE '%424F02N%'
""").fetchone()
print(f"  Комиссии в trade (Сбер): {r[0]}")

r = c.execute("""
    SELECT 
        ROUND(SUM(COALESCE(broker_fee,0) + COALESCE(exchange_fee,0)), 2) as repo_fees
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    WHERE rp.contract LIKE '%424F02N%'
""").fetchone()
print(f"  Комиссии в repo (Сбер): {r[0]}")

# 5. Проверить LIFO profit fees - считаются ли они из trade только?
print("\n=== Анализ LIFO net_profit ===")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit
p = get_trade_profit(broker='sber')
total_fees = sum(r['total_fees'] for r in p)
total_net = sum(r['net_profit'] for r in p)
print(f"  LIFO total_fees (трейд комиссии): {total_fees:.2f}")
print(f"  LIFO net_profit: {total_net:.2f}")
# REPO total
r = c.execute("SELECT ROUND(SUM(COALESCE(repo_interest,0)+COALESCE(broker_fee,0)+COALESCE(exchange_fee,0)),2) FROM repo WHERE report_id IN (SELECT id FROM report WHERE contract LIKE '%424F02N%')").fetchone()
print(f"  REPO total: {r[0]}")

c.close()
