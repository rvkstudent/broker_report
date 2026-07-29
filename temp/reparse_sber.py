"""Очистить данные отчёта 61 и перепарсить."""
import sqlite3, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)

# Сколько сделок до
cnt = c.execute("SELECT COUNT(*) FROM trade WHERE report_id=61").fetchone()[0]
print(f"Сделок в report 61 до: {cnt}")

# Удаляем
c.execute("DELETE FROM trade WHERE report_id=61")
c.execute("DELETE FROM repo WHERE report_id=61")
c.execute("DELETE FROM cash_flow WHERE report_id=61")
c.commit()
print("Данные отчёта 61 удалены")

cnt2 = c.execute("SELECT COUNT(*) FROM trade WHERE report_id=61").fetchone()[0]
print(f"Сделок в report 61 после: {cnt2}")
c.close()

# Перепарсим
from app.parser import parse_report
fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
print(f"\nПерепарсиваем {fp}...")
rid = parse_report(fp)
print(f"Новый/старый id: {rid}")

# Проверка
c = sqlite3.connect(db)
cnt = c.execute("SELECT COUNT(*) FROM trade WHERE report_id=?", (rid,)).fetchone()[0]
print(f"Сделок после перепарсинга: {cnt}")
uniq = c.execute("SELECT DISTINCT security_code FROM trade WHERE report_id=?", (rid,)).fetchall()
print(f"Коды: {sorted([x[0] for x in uniq])}")

# MTSS/SBER
for code in ('MTSS', 'SBER', 'SBMM'):
    cnt = c.execute("SELECT COUNT(*) FROM trade WHERE report_id=? AND security_code=?", (rid, code)).fetchone()[0]
    print(f"  {code}: {cnt} сделок")
c.close()
