"""Исследовать структуру 2026 файла - найти заголовки разделов."""
import pandas as pd, os

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
df = pd.read_excel(fp, header=None)

print(f"Shape: {df.shape}")

# Смотрим первые 50 строк полностью
print("\n=== Первые 50 строк (все непустые колонки) ===")
for r in range(min(50, df.shape[0])):
    vals = [str(df.iloc[r, cc]).strip()[:60] for cc in range(min(30, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
    if vals:
        print(f"  R{r}: {' | '.join(vals)}")

# Ищем заголовки разделов
print("\n=== Поиск заголовков разделов (ключевые слова) ===")
keywords = ['Дата продажи', 'Дата покупки', 'Цена', 'Сумма', 'КОНТРОЛЬ', 'Финансовый результат', 'Итого']
for r in range(df.shape[0]):
    for c in range(min(30, df.shape[1])):
        v = str(df.iloc[r, c]).strip()
        for kw in keywords:
            if kw.lower() in v.lower():
                vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:50]}" for cc in range(min(30, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
                print(f"  [{kw}] R{r}: {' | '.join(vals)}")
                break
