"""Проверить структуру HTML вокруг сделок."""
from bs4 import BeautifulSoup
import os, re

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

# Ищем "Сделки купли/продажи"
for tag in ['p', 'p1', 'div', 'span', 'h1', 'h2', 'h3', 'h4']:
    found = soup.find_all(tag)
    for el in found:
        if 'Сделки купли/продажи' in el.get_text():
            print(f"Найдено в <{tag}>: {el.get_text()[:100]}")
            next_table = el.find_next('table')
            if next_table:
                rows = next_table.find_all('tr')
                print(f"  Строк в таблице: {len(rows)}")
                for i, row in enumerate(rows[:5]):
                    cells = row.find_all('td')
                    texts = [c.get_text(strip=True) for c in cells]
                    print(f"  Row {i}: {texts[:8]}")
                print(f"  ...")
                for i, row in enumerate(rows[-5:], len(rows)-5):
                    cells = row.find_all('td')
                    texts = [c.get_text(strip=True) for c in cells]
                    print(f"  Row {i}: {texts[:8]}")
            break
    if 'Сделки купли/продажи' in el.get_text():
        break

# Fallback: search table with headers
print("\n=== Fallback поиск таблицы ===")
for table in soup.find_all('table'):
    rows = table.find_all('tr')
    for row in rows:
        cells = row.find_all('td')
        texts = [c.get_text(strip=True) for c in cells]
        if 'Дата заключения' in texts and 'Код ЦБ' in texts:
            print(f"Найдена таблица с заголовками: {texts}")
            print(f"Строк в таблице: {len(rows)}")
            break
