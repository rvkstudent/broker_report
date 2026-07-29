"""Найти значения налогов в широких колонках."""
import pandas as pd, os

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    df = pd.read_excel(fp, header=None)
    print(f"\n=== {fn} ===")
    
    # Строки 14-28 где налоги, все колонки
    for r in range(13, 29):
        vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:50]}" for cc in range(min(80, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
        if vals:
            print(f"  R{r:>3}: {' | '.join(vals)}")
