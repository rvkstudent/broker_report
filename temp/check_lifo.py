"""Проверить, что показывает LIFO-движок."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit, get_open_trades

print("=== LIFO P&L (все отчёты, все брокеры) ===")
profit = get_trade_profit()
for r in profit:
    print(f"  {r['sec_code']:>12} {r['sec_name']:>25}: P&L={r['pnl']:>10.2f} fees={r['fees']:>8.2f}")

print("\n=== Сбер только ===")
sber_profit = get_trade_profit(broker='sber')
for r in sber_profit:
    print(f"  {r['sec_code']:>12} {r['sec_name']:>25}: P&L={r['pnl']:>10.2f} fees={r['fees']:>8.2f}")

print("\n=== Открытые позиции ===")
op = get_open_trades()
for r in op:
    print(f"  {r['sec_code']:>12} {r['sec_name']:>25}: qty={r['qty']:>5} price={r['avg_price']:>8.2f}")
