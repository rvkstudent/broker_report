"""Посмотреть откуда берутся строки 498+ с пустым deal_number."""
import pandas as pd, os

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
df = pd.read_excel(fp, header=None)

# Смотрим строки 480-600, что там за секции
print("=== Строки 480-600 ===")
for r in range(480, min(600, df.shape[0])):
    vals = []
    for cc in range(min(50, df.shape[1])):
        v = df.iloc[r, cc]
        if not pd.isna(v):
            vals.append(f"C{cc}={str(v).strip()[:60]}")
    if vals:
        print(f"R{r:>3}: {' | '.join(vals)}")
