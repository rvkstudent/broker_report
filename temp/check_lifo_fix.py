"""Проверить LIFO после исправления — Сбер+ВТБ должно равняться Оба."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_trade_profit

DATE_FROM = '01.05.2026'

def show(title, data):
    print(f'\n=== {title} ===')
    for r in data:
        print(f"  {r['security_code']:>10}: buy={r['buy_qty']:>5} sell={r['sell_qty']:>5} gross={r['gross_profit']:>10.2f} fees={r['total_fees']:>8.2f} net={r['net_profit']:>10.2f}")
    total = sum(r['net_profit'] for r in data)
    print(f'  Итого net: {total:,.2f}')
    return total

t1 = show('СБЕР', get_trade_profit(broker='sber', date_from=DATE_FROM))
t2 = show('ВТБ', get_trade_profit(broker='vtb', date_from=DATE_FROM))
t3 = show('ОБА', get_trade_profit(broker='all', date_from=DATE_FROM))

print(f'\nСбер + ВТБ = {t1 + t2:,.2f}')
print(f'Оба        = {t3:,.2f}')
print(f'Совпадают: {abs((t1 + t2) - t3) < 0.01}')
