"""Проверить: какие строки в 2026 файле парсер загружает (проходит его фильтр)."""
import pandas as pd, os, re
from datetime import datetime

def parse_float(s):
    if s is None: return 0.0
    if isinstance(s, (int, float)): return float(s)
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(',', '.')
    s = s.lstrip('+')
    try: return float(s)
    except ValueError: return 0.0

def _fmt_date(val):
    if val is None or (pd and pd.isna(val)): return ''
    if isinstance(val, datetime): return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'): return val.strftime('%d.%m.%Y')
    s = str(val).strip()[:10]
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m: return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    return s

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
df = pd.read_excel(fp, header=None)

current_name = ''
current_code = ''
passed = 0
failed = 0

print("=== Строки, ПРОШЕДШИЕ фильтр парсера ===")
for r in range(df.shape[0]):
    cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''

    if cell1.startswith('Наименование:'):
        parts = cell1.replace('Наименование:', '').strip().split(',')
        current_name = parts[0].strip() if parts else ''
        current_code = parts[1].strip() if len(parts) > 1 else ''
        continue

    # Пропускаем заголовки
    if not cell1 or cell1.startswith('Наименование') or cell1.startswith('Финансовый') or cell1.startswith('^'):
        continue

    income = parse_float(df.iloc[r, 1])
    
    deal_date = _fmt_date(df.iloc[r, 3]) if not pd.isna(df.iloc[r, 3]) else _fmt_date(df.iloc[r, 1])
    deal_number = str(df.iloc[r, 6]).strip() if not pd.isna(df.iloc[r, 6]) else ''
    price = parse_float(df.iloc[r, 14]) if not pd.isna(df.iloc[r, 14]) else 0
    qty = parse_float(df.iloc[r, 18]) if not pd.isna(df.iloc[r, 18]) else 0
    amount = parse_float(df.iloc[r, 22]) if not pd.isna(df.iloc[r, 22]) else 0

    if not deal_date and amount == 0:
        failed += 1
        continue
    
    passed += 1
    
    # Определяем тип строки
    side = str(df.iloc[r, 3]).strip() if not pd.isna(df.iloc[r, 3]) else ''
    if side in ('Продажа', 'Покупка', 'Дата продажи'):
        continue
    
    # Проверка на REPO
    is_repo = deal_number.startswith('SER') if deal_number else False
    
    print(f"R{r:>3} {'[REPO]' if is_repo else '[NORM]'}: name=[{current_name:>20}] income={income:>8.2f} date={deal_date} deal={deal_number:>30} price={price:>8.2f} qty={qty:>5} amt={amount:>10.2f}")

print(f"\nПропущено: {failed}, Прошло: {passed}")
