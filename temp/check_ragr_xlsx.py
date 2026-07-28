"""Проверить Русагро в исходном Excel 2026."""
import pandas as pd, os

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
df = pd.read_excel(fp, header=None)

print(f"Shape: {df.shape}")

# Ищем Русагро
print("\n=== Поиск Русагро в файле ===")
for r in range(df.shape[0]):
    for c in range(min(50, df.shape[1])):
        v = str(df.iloc[r, c]).strip()
        if 'Русагро' in v or 'RAGR' in v:
            vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:40]}" for cc in range(min(50, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
            print(f"  row {r}: {' | '.join(vals)}")
            # Показать 3 строки до и после
            for dr in range(-3, 4):
                rr = r + dr
                if rr >= 0 and rr < df.shape[0] and dr != 0:
                    vv = [f"C{cc}={str(df.iloc[rr, cc]).strip()[:40]}" for cc in range(min(50, df.shape[1])) if not pd.isna(df.iloc[rr, cc])]
                    if vv:
                        print(f"    [{dr}] row {rr}: {' | '.join(vv)}")
            break
