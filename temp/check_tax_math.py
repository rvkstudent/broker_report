"""Проверка налогов: база × 13% = начислено?"""
import sqlite3, os
db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row
rows = c.execute('SELECT * FROM nalog_tax_summary ORDER BY year').fetchall()
for r in rows:
    d = dict(r)
    calc_13 = d['broker_taxable'] * 0.13
    diff = abs(calc_13 - d['broker_tax_calc'])
    ok = '✓' if diff < 1 else '✗'
    print(f"{d['year']}:")
    print(f"  нал.база={d['broker_taxable']:>10.2f}")
    print(f"  ×13%     ={calc_13:>10.2f}")
    print(f"  начислено={d['broker_tax_calc']:>10.2f}  {ok}")
    print(f"  уплачено ={d['broker_tax_paid']:>10.2f}")
    print(f"  к уплате ={d['broker_tax_due']:>10.2f}")
    if diff >= 1:
        print(f"  >>> РАСХОЖДЕНИЕ: {diff:.2f}")
c.close()
