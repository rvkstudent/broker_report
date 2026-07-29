"""Проверить REPO в HTML отчёте Сбера."""
from bs4 import BeautifulSoup
import os, re

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

# Ищем сводку по РЕПО
for tag in soup.find_all(['p', 'p1', 'h3', 'h4']):
    txt = tag.get_text()
    if 'РЕПО' in txt or 'Сделки РЕПО' in txt:
        print(f"\n=== Текст: {txt[:200]} ===")
        next_table = tag.find_next('table')
        if next_table:
            rows = next_table.find_all('tr')
            print(f"Строк в таблице: {len(rows)}")
            for row in rows:
                cells = row.find_all('td')
                texts = [c.get_text(strip=True) for c in cells]
                print(f"  {texts}")

# Ищем движение денег
print("\n\n=== Движение денежных средств ===")
for tag in soup.find_all(['p', 'p1']):
    if 'Движение денежных средств' in tag.get_text():
        table = tag.find_next('table')
        if table:
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all('td')
                texts = [c.get_text(strip=True) for c in cells]
                if any('РЕПО' in t or 'процент' in t for t in texts):
                    print(f"  {texts}")
