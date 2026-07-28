"""Проверить отчёты на упоминание переноса убытков, сальдирования."""
import pandas as pd, os

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    df = pd.read_excel(fp, header=None)
    print(f"\n=== {fn} ===")
    
    keywords = ['убыток', 'сальдир', 'перенос', 'прошлых', 'переплат', 
                'зачет', 'к зачету', 'налог к возмещ']
    
    for r in range(df.shape[0]):
        for c in range(min(50, df.shape[1])):
            v = str(df.iloc[r, c]).lower().strip()
            for kw in keywords:
                if kw in v:
                    vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:60]}" for cc in range(min(50, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
                    print(f"  [{kw}] R{r:>3}: {' | '.join(vals)}")
                    break
