"""Ищем сводку РЕПО в HTML отчёте."""
from bs4 import BeautifulSoup
import os

fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

# Ищем все строки с Итого в таблице РЕПО
for p_tag in soup.find_all('p'):
    if 'Сделки РЕПО' in p_tag.get_text():
        table = p_tag.find_next('table')
        break

rows = table.find_all('tr')
for row in rows:
    cells = row.find_all('td')
    texts = [c.get_text(strip=True) for c in cells]
    if 'Итого' in row.get_text(strip=True):
        print(f"ИТОГО: {texts}")

# Также проверим движение денег
for p_tag in soup.find_all('p'):
    if 'Движение денежных средств' in p_tag.get_text():
        table = p_tag.find_next('table')
        break

print("\n=== Движение денег (строки с РЕПО/процент) ===")
rows = table.find_all('tr')
for row in rows:
    cells = row.find_all('td')
    texts = [c.get_text(strip=True) for c in cells]
    if any('РЕПО' in t or 'процент' in t.lower() or 'Итого' in t for t in texts):
        print(f"  {texts}")
