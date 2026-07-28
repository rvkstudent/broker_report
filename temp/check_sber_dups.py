"""Проверить сделки Сбера 2026 на дубликаты."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))
c.row_factory = sqlite3.Row

# Смотрим какие отчёты Сбера загружены
print("=== Отчёты в БД ===")
rows = c.execute("SELECT id, filename, period_start, period_end FROM report ORDER BY id").fetchall()
for r in rows:
    print(f"  {r['id']}: {r['filename']} ({r['period_start']} - {r['period_end']})")

# Сделки по Сберу за 2026
print("\n=== Сделки Сбер 2026 (первые 30) ===")
rows = c.execute("""
    SELECT t.id, t.security_name, t.security_code, t.side, t.quantity, t.price, t.trade_date, t.report_id
    FROM trade t
    JOIN report r ON r.id = t.report_id
    WHERE r.contract LIKE '%424F02N%'
    ORDER BY t.trade_date, t.id
    LIMIT 50
""").fetchall()
for r in rows:
    print(f"  #{r['id']}: {r['security_name']:>20} {r['security_code']:>10} {r['side']:>4} qty={r['quantity']:>5} price={r['price']:>8.2f} {r['trade_date']} (rpt={r['report_id']})")

# Проверка дубликатов
print("\n=== Поиск дубликатов (одинаковые сделки в двух отчётах) ===")
rows = c.execute("""
    SELECT t.security_code, t.side, t.quantity, t.price, t.trade_date, 
           GROUP_CONCAT(t.report_id) as rpt_ids,
           COUNT(*) as cnt
    FROM trade t
    JOIN report r ON r.id = t.report_id
    WHERE r.contract LIKE '%424F02N%'
    GROUP BY t.security_code, t.side, t.quantity, t.price, t.trade_date
    HAVING cnt > 1
    ORDER BY cnt DESC, t.trade_date
    LIMIT 20
""").fetchall()
if rows:
    print(f"Найдено {len(rows)} дубликатов:")
    for r in rows:
        print(f"  {r['sec_code']:>10} {r['side']:>4} qty={r['quantity']:>5} price={r['price']:>8.2f} {r['trade_date']} (reports={r['rpt_ids']}, cnt={r['cnt']})")
else:
    print("Дубликатов не найдено")

# Итоговый P&L по инструментам
print("\n=== P&L по инструментам (трейды) ===")
rows = c.execute("""
    SELECT t.security_code, t.security_name,
           SUM(CASE WHEN t.side='B' THEN t.quantity ELSE 0 END) as bought,
           SUM(CASE WHEN t.side='S' THEN t.quantity ELSE 0 END) as sold,
           ROUND(SUM(CASE WHEN t.side='S' THEN t.quantity*t.price ELSE -t.quantity*t.price END), 2) as pnl,
           ROUND(SUM(t.broker_fee + t.exchange_fee), 2) as fees
    FROM trade t
    JOIN report r ON r.id = t.report_id
    WHERE r.contract LIKE '%424F02N%'
    GROUP BY t.security_code
    ORDER BY pnl DESC
""").fetchall()
for r in rows:
    print(f"  {r['security_code']:>10} {r['security_name']:>25}: B={r['bought']:>5} S={r['sold']:>5} P&L={r['pnl']:>10.2f} fees={r['fees']:>8.2f}")

c.close()
