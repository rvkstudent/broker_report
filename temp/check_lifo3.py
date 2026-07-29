"""Проверить LIFO после перепарсинга."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit, get_open_trades

print("=== LIFO P&L (Сбер) ===")
p = get_trade_profit(broker='sber')
for r in p:
    print(f"  {r['security_code']:>10}: buy={r['buy_qty']:>5} sell={r['sell_qty']:>5} net={r['net_profit']:>8.2f}")
total_net = sum(r['net_profit'] for r in p)
print(f"  Всего net: {total_net:.2f}")

print("\n=== Открытые позиции (Сбер) ===")
o = get_open_trades(broker='sber')
for r in o:
    print(f"  {r['security_code']:>10}: qty={r['qty']:>4} price={r['avg_price']:>8.2f}")

# Проверяем SBER и MTSS отдельно
print("\n=== Детально SBER ===")
for r in p:
    if 'SBER' in r['security_code']:
        print(f"  {r['security_code']}: buy={r['buy_qty']} sell={r['sell_qty']} gross={r['gross_profit']} net={r['net_profit']}")

print("\n=== Детально MTSS ===")
for r in p:
    if 'MTS' in r['security_code']:
        print(f"  {r['security_code']}: buy={r['buy_qty']} sell={r['sell_qty']} gross={r['gross_profit']} net={r['net_profit']}")
