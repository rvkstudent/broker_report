"""Трассировка парсера - почему MTSS/SBER не попадают."""
from bs4 import BeautifulSoup
import os

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()
soup = BeautifulSoup(html, 'lxml')

# Имитация логики парсера
for p_tag in soup.find_all(['p', 'p1']):
    if 'Сделки купли/продажи ценных бумаг' in p_tag.get_text():
        table = p_tag.find_next('table')
        break

rows = table.find_all('tr')
print(f"Всего строк: {len(rows)}")

parsed = 0
skipped_total = 0
for i, row in enumerate(rows):
    cells = row.find_all('td')
    if not cells:
        skipped_total += 1
        continue
        
    txt = row.get_text(strip=True)
    
    # Площадка
    if 'Площадка:' in txt:
        skipped_total += 1
        continue
    
    # Заголовки
    first_text = cells[0].get_text(strip=True)
    if first_text in ('1', 'Дата заключения', '№ п/п'):
        skipped_total += 1
        continue
    if 'row-number' in (cells[0].get('class') or []):
        skipped_total += 1
        continue
    if 'Итого' in txt:
        skipped_total += 1
        continue
    
    if len(cells) < 10:
        skipped_total += 1
        if any(kw in txt for kw in ['MTSS', 'SBER', 'SBMM', 'МТС', 'Сбербанк']):
            print(f"  Row {i}: SKIP len<10 ({len(cells)} cells) - {[c.get_text(strip=True)[:15] for c in cells[:6]]}")
        continue
    
    try:
        trade_date = cells[0].get_text(strip=True)
        sec_name = cells[3].get_text(strip=True)
        sec_code = cells[4].get_text(strip=True)
        side = cells[6].get_text(strip=True)
    except:
        skipped_total += 1
        continue
    
    if not trade_date or not sec_name or not side:
        skipped_total += 1
        continue
    if side not in ('Покупка', 'Продажа'):
        skipped_total += 1
        continue
    
    parsed += 1
    if any(kw in txt for kw in ['MTSS', 'SBER', 'SBMM', 'МТС', 'Сбербанк']):
        print(f"  Row {i}: PARSED - {sec_code} {side} qty={cells[7].get_text(strip=True)}")

print(f"\nПропущено: {skipped_total}, Распарсено: {parsed}")

# Проверим строки с MTSS/SBER/SBMM
print("\n=== Все строки с MTSS/SBER/SBMM ===")
for i, row in enumerate(rows):
    txt = row.get_text(strip=True)
    if any(kw in txt for kw in ['MTSS', 'SBER', 'SBMM', 'МТС', 'Сбербанк']):
        cells = row.find_all('td')
        print(f"  Row {i}: {len(cells)} cells, first='{cells[0].get_text(strip=True)[:20]}', text='{txt[:120]}'")
