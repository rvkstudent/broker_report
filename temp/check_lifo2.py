"""Проверить LIFO P&L."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit

print("=== LIFO P&L (Сбер) ===")
p = get_trade_profit(broker='sber')
for r in p:
    print(f"  {r['security_code']:>12} {r['security_name']:>25}: buy={r['buy_qty']:>5} sell={r['sell_qty']:>5} gross={r['gross_profit']:>10.2f} net={r['net_profit']:>10.2f}")

print(f"\nTotal gross: {sum(r['gross_profit'] for r in p):.2f}")
print(f"Total net: {sum(r['net_profit'] for r in p):.2f}")

print("\n=== LIFO P&L (все) ===")
p2 = get_trade_profit()
for r in p2:
    print(f"  {r['security_code']:>12} {r['security_name']:>25}: buy={r['buy_qty']:>5} sell={r['sell_qty']:>5} gross={r['gross_profit']:>10.2f} net={r['net_profit']:>10.2f}")
