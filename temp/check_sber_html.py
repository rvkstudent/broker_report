"""Проверить SBER/MTS в сыром HTML отчёте Сбера."""
from bs4 import BeautifulSoup
import os

# Самый полный отчёт Сбера
fp = os.path.join('reports', '424F02N_15042026_27072026.HTML')
if not os.path.exists(fp):
    fp = os.path.join('reports', '424F02N_01072026_23072026.HTML')

with open(fp, 'r', encoding='utf-8') as f:
    html = f.read()

soup = BeautifulSoup(html, 'lxml')

# Ищем все таблицы
tables = soup.find_all('table')
print(f"Всего таблиц: {len(tables)}")

# Ищем MTS, SBER, MTSS во всём HTML
for kw in ['MTS', 'SBER', 'MTSS', 'МТС', 'Сбербанк']:
    found = html.find(kw)
    if found >= 0:
        # Показать контекст
        start = max(0, found - 100)
        end = min(len(html), found + 200)
        ctx = html[start:end].replace('\n', ' ').replace('\r', '')
        print(f"\n=== Найдено '{kw}' (pos={found}) ===")
        print(f"  ...{ctx}...")
