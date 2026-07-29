"""Проверить перекрытие REPO между отчётами 4 и 61."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))

# REPO в отчёте 4
print("=== REPO rpt=4 (01-23.07) ===")
r4 = c.execute("""
    SELECT security_code, quantity, date_part1, repo_interest, broker_fee, deal_number
    FROM repo WHERE report_id=4
""").fetchall()
for r in r4:
    print(f"  {r[0]:>10} qty={r[1]:>5} date={r[2]} interest={r[3]:>7.2f} fee={r[4]:>6.2f} deal={r[5]}")

# Те же сделки в отчёте 61?
print("\n=== Поиск тех же deal_number в rpt=61 ===")
deal_nums = [r[5] for r in r4 if r[5]]
for dn in deal_nums:
    found = c.execute("SELECT id, report_id, repo_interest, broker_fee FROM repo WHERE deal_number=? AND report_id=61", (dn,)).fetchall()
    if found:
        print(f"  deal={dn} НАЙДЕН в rpt=61:")
        for f in found:
            print(f"    id={f[0]} interest={f[2]} fee={f[3]}")
    else:
        print(f"  deal={dn} НЕ НАЙДЕН в rpt=61")

# Сверим суммы
print("\n=== Сумма interest в rpt=4 (по периодам) ===")
c2 = c.execute("SELECT SUM(repo_interest), SUM(broker_fee) FROM repo WHERE report_id=4").fetchone()
print(f"  interest={c2[0]:.2f}, fees={c2[1]:.2f}")
c2 = c.execute("SELECT SUM(repo_interest), SUM(broker_fee) FROM repo WHERE report_id=61").fetchone()
print(f"  rpt=61: interest={c2[0]:.2f}, fees={c2[1]:.2f}")

# Узнаём есть ли перекрытие в суммах
print("\n=== Диапазон дат REPO в rpt=61 ===")
r = c.execute("SELECT MIN(date_part1), MAX(date_part1) FROM repo WHERE report_id=61").fetchone()
print(f"  от {r[0]} до {r[1]}")
r = c.execute("SELECT MIN(date_part1), MAX(date_part1) FROM repo WHERE report_id=4").fetchone()
print(f"  rpt=4: от {r[0]} до {r[1]}")

c.close()
