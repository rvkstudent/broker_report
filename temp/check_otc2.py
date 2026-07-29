"""Проверить MTSS/SBER в БД и парсере."""
import sqlite3, os

db = os.path.join('app', 'broker.db')
c = sqlite3.connect(db)
for code in ('MTSS', 'SBER', 'SBMM'):
    cnt = c.execute("SELECT COUNT(*) FROM trade WHERE security_code=?", (code,)).fetchone()[0]
    print(f"{code}: {cnt} сделок в БД")
c.close()

# Теперь проверим парсинг OTC-строк
print("\n=== Проверка OTC-строк в парсере ===")
from bs4 import BeautifulSoup

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()
soup = BeautifulSoup(html, 'lxml')

for p_tag in soup.find_all('p'):
    if 'Сделки купли/продажи' in p_tag.get_text():
        table = p_tag.find_next('table')
        break

rows = table.find_all('tr')
in_otc = False
for i, row in enumerate(rows):
    cells = row.find_all('td')
    txt = row.get_text(strip=True)
    
    if 'Площадка: Внебиржевой' in txt:
        in_otc = True
        print(f"\nOTC начинается с row {i}")
        continue
    if 'Площадка:' in txt and in_otc:
        break  # следующая площадка
    
    if in_otc and cells and len(cells) >= 2:
        # Проверяем является ли строка данными
        first = cells[0].get_text(strip=True)
        if first and first not in ('Итого', '') and not first.startswith('№'):
            # Проверка: проходит ли через парсер?
            txt_all = row.get_text(strip=True)
            has_date = len(first) == 10 and first[2] == '.' and first[5] == '.'
            has_side = any('Покупка' in c.get_text() or 'Продажа' in c.get_text() for c in cells)
            
            if has_date and has_side:
                print(f"  OTC Row {i}: {len(cells)} cells - {[c.get_text(strip=True)[:20] for c in cells[:8]]}")
                print(f"    -> len<10={len(cells) < 10}, пропускается парсером!")
