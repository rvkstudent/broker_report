"""Посмотреть точную структуру OTC-строк."""
from bs4 import BeautifulSoup
import os

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

# Найти таблицу сделок
for p_tag in soup.find_all('p'):
    if 'Сделки купли/продажи' in p_tag.get_text():
        table = p_tag.find_next('table')
        break

rows = table.find_all('tr')
for i, row in enumerate(rows):
    cells = row.find_all('td')
    txt = row.get_text(strip=True)
    
    if 'SBER' in txt or 'SBMM' in txt or 'MTSS' in txt or 'МТС' in txt:
        print(f"\nRow {i}: len(cells)={len(cells)}")
        for j, c in enumerate(cells):
            print(f"  cell[{j}]: class={c.get('class')} text='{c.get_text(strip=True)}' html='{str(c)[:80]}'")
        
    if 'Площадка: Внебиржевой' in txt:
        # Показать следующие строки
        print(f"\n=== Внебиржевой рынок (row {i}) ===")
        for j in range(i, min(i+10, len(rows))):
            r = rows[j]
            cs = r.find_all('td')
            txts = [c.get_text(strip=True) for c in cs]
            print(f"  Row {j}: ({len(cs)} cells) {txts}")
