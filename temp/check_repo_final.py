"""Финальная проверка REPO."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_repo_total, get_trade_profit

# Сбер
p = get_trade_profit(broker='sber')
total_net = sum(r['net_profit'] for r in p)
repo = get_repo_total(broker='sber')
print(f"Сбер:")
print(f"  Трейды net: {total_net:.2f}")
print(f"  REPO: {repo}")
print(f"  Итого: {total_net - repo['total']:.2f}")

# Проверка: считаем сумму процентов из БД
import sqlite3
c = sqlite3.connect(os.path.join('app', 'broker.db'))
# Sber repo interest total
r = c.execute("""
    SELECT ROUND(SUM(COALESCE(repo_interest,0)),2),
           ROUND(SUM(COALESCE(broker_fee,0)),2),
           ROUND(SUM(COALESCE(exchange_fee,0)),2)
    FROM repo WHERE report_id IN (4,61)
""").fetchone()
print(f"\nИз БД (rpt=4+61):")
print(f"  interest={r[0]}, broker_fee={r[1]}, exchange_fee={r[2]}, total={r[0]+r[1]+r[2]}")

# Сверим с HTML Итого
print(f"\nИз HTML ИТОГО:")
print(f"  interest=6373.08, broker_fees=358.10, exchange_fees=0.00, total=6731.18")
print(f"\n  Совпадает: {abs(r[0] - 6373.08) < 0.1 and abs(r[1] - 358.10) < 0.1}")

c.close()
