"""Проверить итоговую секцию (депозитарий + итоги)."""
import pandas as pd, os

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    df = pd.read_excel(fp, header=None)
    print(f"\n=== {fn} ===")
    
    # Ищем "Итоговая информация" и вокруг
    started = False
    for r in range(28, min(60, df.shape[0])):
        vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:50]}" for cc in range(min(80, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
        if vals:
            if 'Итоговая' in str(vals) or 'ИТОГО' in str(vals) or 'Депозитари' in str(vals):
                started = True
            if started:
                print(f"  R{r:>3}: {' | '.join(vals)}")
                if 'налогооблагаемый доход' in str(vals).lower():
                    started = False
