"""Проверить REPO данные."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))
c.row_factory = sqlite3.Row

print("=== Все REPO записи ===")
rows = c.execute("""
    SELECT r.id, r.report_id, rp.filename, r.security_code, r.quantity, r.amount_part1,
           r.repo_interest, r.broker_fee, r.exchange_fee, r.date_part1
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    ORDER BY r.report_id, r.id
""").fetchall()
print(f"Всего REPO записей: {len(rows)}")
for r in rows:
    d = dict(r)
    total = (d['repo_interest'] or 0) + (d['broker_fee'] or 0) + (d['exchange_fee'] or 0)
    print(f"  #{d['id']} (rpt={d['report_id']}): {d['filename'][:40]:>40} {d['security_code']:>10} qty={d['quantity']:>5} interest={d['repo_interest']:>8.2f} fees={d['broker_fee']:>8.2f}+{d['exchange_fee']:>8.2f} total={total:>8.2f}")

print("\n=== REPO по отчётам (суммы) ===")
rows = c.execute("""
    SELECT r.report_id, rp.filename,
           COUNT(*) as cnt,
           ROUND(SUM(r.repo_interest), 2) as total_interest,
           ROUND(SUM(r.broker_fee), 2) as total_bfee,
           ROUND(SUM(r.exchange_fee), 2) as total_efee,
           ROUND(SUM(COALESCE(r.repo_interest,0) + COALESCE(r.broker_fee,0) + COALESCE(r.exchange_fee,0)), 2) as grand_total
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    GROUP BY r.report_id
    ORDER BY r.report_id
""").fetchall()
for r in rows:
    print(f"  rpt={r['report_id']}: {r['filename'][:45]:>45} cnt={r['cnt']:>3} interest={r['total_interest']:>10.2f} fees={r['total_bfee']:>8.2f}+{r['total_efee']:>8.2f} = {r['grand_total']:>10.2f}")

# get_repo_total
print("\n=== get_repo_total() результат ===")
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_repo_total
for broker in (None, 'sber'):
    r = get_repo_total(broker=broker)
    print(f"  broker={broker}: {r}")

# Проверим дубликаты REPO - одинаковые сделки в разных отчётах
print("\n=== Поиск дубликатов REPO ===")
rows = c.execute("""
    SELECT r.security_code, r.quantity, r.date_part1, r.repo_interest, r.broker_fee,
           GROUP_CONCAT(r.report_id) as rpt_ids,
           COUNT(*) as cnt
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    WHERE rp.contract LIKE '%424F02N%'
    GROUP BY r.security_code, r.quantity, r.date_part1, r.repo_interest, r.broker_fee
    HAVING cnt > 1
    ORDER BY r.date_part1
""").fetchall()
if rows:
    print(f"Найдено дубликатов: {len(rows)}")
    for r in rows:
        print(f"  {r['security_code']:>10} qty={r['quantity']:>5} date={r['date_part1']} interest={r['repo_interest']:>8.2f} (reports={r['rpt_ids']})")
else:
    print("Дубликатов REPO не найдено")

c.close()
