"""Показать группировку инструментов."""
import sqlite3, os
c = sqlite3.connect(os.path.join('app', 'broker.db'))
c.row_factory = sqlite3.Row

rows = c.execute("""
    SELECT instrument_name, year, ROUND(SUM(income),2) as total
    FROM nalog
    WHERE instrument_name IS NOT NULL AND instrument_name != ''
    GROUP BY instrument_name, year
    ORDER BY instrument_name, year
""").fetchall()

print("=== Все инструменты по годам ===")
for r in rows:
    print(f"  {r['instrument_name']:>30}  {r['year']}: {r['total']:>10.2f}")

print("\n=== Группировка ===")
for r in rows:
    grp = 'ОФЗ' if r['instrument_name'].startswith('ОФЗ') else 'Прочие'
    print(f"  [{grp}] {r['instrument_name']:>30}  {r['year']}: {r['total']:>10.2f}")

# Итоги по группам
print("\n=== Итоги по группам ===")
grp_data = c.execute("""
    SELECT 
        CASE WHEN instrument_name LIKE 'ОФЗ%' THEN 'ОФЗ' ELSE 'Прочие' END as grp,
        year,
        COUNT(*) as deals,
        ROUND(SUM(income),2) as total_income,
        ROUND(SUM(CASE WHEN income > 0 THEN income ELSE 0 END),2) as profit,
        ROUND(SUM(CASE WHEN income < 0 THEN income ELSE 0 END),2) as loss
    FROM nalog
    WHERE instrument_name IS NOT NULL AND instrument_name != ''
    GROUP BY grp, year
    ORDER BY grp, year
""").fetchall()
for r in grp_data:
    print(f"  [{r['grp']}] {r['year']}: deals={r['deals']}, income={r['total_income']:>10.2f}, profit={r['profit']:>10.2f}, loss={r['loss']:>10.2f}")

c.close()
