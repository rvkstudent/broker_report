"""Детально посмотреть значения налоговой сводки."""
import pandas as pd, os

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    df = pd.read_excel(fp, header=None)
    print(f"\n=== {fn} ===")
    
    # Смотрим строки 10-50 с содержимым
    for r in range(10, min(55, df.shape[0])):
        vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:50]}" for cc in range(min(30, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
        if vals:
            print(f"  R{r:>3}: {' | '.join(vals)}")
