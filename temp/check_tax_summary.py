"""Проверить налоговую сводку в файлах отчётов."""
import pandas as pd, os, re

for fn in sorted(os.listdir('reports/nalog')):
    if not fn.endswith('.xlsx'): continue
    fp = os.path.join('reports/nalog', fn)
    df = pd.read_excel(fp, header=None)
    print(f"\n=== {fn} (shape={df.shape}) ===")
    
    # Ищем строки с налогами
    for r in range(min(50, df.shape[0])):
        for c in range(min(20, df.shape[1])):
            v = str(df.iloc[r, c]).strip()
            if any(kw in v for kw in ['Налог', 'налог', 'налогооблагаемая', 'Уплаченный', 'Начисленный', 'к уплате', 'ИТОГО общая']):
                vals = [f"C{cc}={str(df.iloc[r, cc]).strip()[:40]}" for cc in range(min(20, df.shape[1])) if not pd.isna(df.iloc[r, cc])]
                print(f"  R{r:>3}: {' | '.join(vals)}")
                break
