"""Что показывает дашборд сейчас."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit, get_repo_total

print("=== ДАШБОРД: все брокеры ===")
p = get_trade_profit()
total_gross = sum(r['gross_profit'] for r in p)
total_fees = sum(r['total_fees'] for r in p)
total_net = sum(r['net_profit'] for r in p)
repo = get_repo_total()
repo_total = repo['total'] if repo else 0
for r in p:
    print(f"  {r['security_code']:>12}: net={r['net_profit']:>10.2f}")
print(f"  Итого трейды: gross={total_gross:>.2f} fees={total_fees:>.2f} net={total_net:>.2f}")
print(f"  РЕПО: {repo_total:>.2f}")
print(f"  ИТОГО: {total_net - repo_total:>.2f}")

print("\n=== ДАШБОРД: только Сбер ===")
p = get_trade_profit(broker='sber')
total_gross = sum(r['gross_profit'] for r in p)
total_fees = sum(r['total_fees'] for r in p)
total_net = sum(r['net_profit'] for r in p)
repo = get_repo_total(broker='sber')
repo_total = repo['total'] if repo else 0
for r in p:
    print(f"  {r['security_code']:>12}: net={r['net_profit']:>10.2f}")
print(f"  Итого трейды: net={total_net:>.2f}")
print(f"  РЕПО: {repo_total:>.2f}")
print(f"  ИТОГО: {total_net - repo_total:>.2f}")

# Что в дашборде по всем данным
print("\n=== ДАШБОРД: все данные (как на главной) ===")
p = get_trade_profit()
total_gross = sum(r['gross_profit'] for r in p)
total_fees = sum(r['total_fees'] for r in p)
total_net = sum(r['net_profit'] for r in p)
repo = get_repo_total()
repo_total = repo['total'] if repo else 0
# Фильтр по дате/брокеру не применяем - это чисто все данные
print(f"  Всего инструментов: {len(p)}")
print(f"  Трейды net: {total_net:.2f}")
print(f"  РЕПО: {repo_total:.2f}")
print(f"  ФИНАЛ: {total_net - repo_total:.2f}")
