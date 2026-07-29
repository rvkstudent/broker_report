"""Удалить некорректные данные 2026 и перепарсить."""
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)

# Удаляем 2026 данные
r = c.execute("DELETE FROM nalog WHERE year='2026'")
print(f"Удалено записей 2026: {r.rowcount}")

c.commit()

# Проверка
print("\n=== Налоговые данные по годам ===")
rows = c.execute("""
    SELECT year, COUNT(*) as deals, ROUND(SUM(income), 2) as total
    FROM nalog GROUP BY year ORDER BY year
""").fetchall()
for r in rows:
    print(f"  {r[0]}: deals={r[1]}, total_income={r[2]}")

print("\n=== Русагро ===")
rows = c.execute("""
    SELECT year, COUNT(*) as deals, ROUND(SUM(income), 2) as total
    FROM nalog WHERE instrument_name LIKE '%Русагро%'
    GROUP BY year ORDER BY year
""").fetchall()
for r in rows:
    print(f"  {r[0]}: deals={r[1]}, total_income={r[2]}")

c.close()
print("\n✅ Перезапусти сервер для обновления страницы.")
