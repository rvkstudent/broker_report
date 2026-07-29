"""REPO по отчётам Сбера."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))

print("=== REPO Сбер по отчётам ===")
rows = c.execute("""
    SELECT r.report_id, rp.filename,
           COUNT(*) as cnt,
           ROUND(SUM(COALESCE(r.repo_interest,0)),2) as interest,
           ROUND(SUM(COALESCE(r.broker_fee,0)),2) as bfee,
           ROUND(SUM(COALESCE(r.exchange_fee,0)),2) as efee,
           ROUND(SUM(COALESCE(r.repo_interest,0)+COALESCE(r.broker_fee,0)+COALESCE(r.exchange_fee,0)),2) as total
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    WHERE rp.contract LIKE '%424F02N%'
    GROUP BY r.report_id
    ORDER BY r.report_id
""").fetchall()
for r in rows:
    print(f"  rpt={r[0]:>3}: total={r[6]:>10.2f} (interest={r[3]:>8.2f} fees={r[4]:>6.2f}+{r[5]:>6.2f}) cnt={r[2]:>3} {r[1][:45]}")

# Всего по Сберу
print("\n=== Всего REPO Сбер ===")
r = c.execute("""
    SELECT ROUND(SUM(COALESCE(repo_interest,0)+COALESCE(broker_fee,0)+COALESCE(exchange_fee,0)),2)
    FROM repo r
    JOIN report rp ON rp.id = r.report_id
    WHERE rp.contract LIKE '%424F02N%'
""").fetchone()
print(f"  Всего: {r[0]}")

# Сверить с get_repo_total
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from app.db import get_repo_total
print(f"\n  get_repo_total(broker='sber'): {get_repo_total(broker='sber')}")
print(f"  get_repo_total(): {get_repo_total()}")

# Проверить дубликаты REPO в Сбере
print("\n=== Дубликаты REPO Сбер ===")
rows = c.execute("""
    SELECT security_code, quantity, date_part1, repo_interest, broker_fee,
           GROUP_CONCAT(report_id) as rpt_ids, COUNT(*) as cnt
    FROM repo
    WHERE report_id IN (SELECT id FROM report WHERE contract LIKE '%424F02N%')
    GROUP BY security_code, quantity, date_part1, repo_interest, broker_fee
    HAVING cnt > 1
    ORDER BY cnt DESC
    LIMIT 20
""").fetchall()
if rows:
    print(f"Найдено дубликатов: {len(rows)}")
    for r in rows:
        print(f"  {r[0]:>10} qty={r[1]:>5} date={r[2]} interest={r[3]:>8.2f} fee={r[4]:>8.2f} (reports={r[5]}, cnt={r[6]})")
else:
    print("Дубликатов нет")

c.close()
