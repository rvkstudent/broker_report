"""Дамп всех строк файла 2026 с содержимым ключевых колонок."""
import pandas as pd, os

fp = os.path.join('reports', 'nalog', 'ВТБ 2026.xlsx')
df = pd.read_excel(fp, header=None)

# Дамп всех строк с 286 по 400 (зона Русагро)
print("=== Дамп строк 286-400 (ключевые колонки) ===")
for r in range(286, min(400, df.shape[0])):
    c1 = str(df.iloc[r, 1]).strip()[:50] if not pd.isna(df.iloc[r, 1]) else ''
    c3 = str(df.iloc[r, 3]).strip()[:20] if not pd.isna(df.iloc[r, 3]) else ''
    c6 = str(df.iloc[r, 6]).strip()[:30] if not pd.isna(df.iloc[r, 6]) else ''
    c14 = str(df.iloc[r, 14]).strip()[:15] if not pd.isna(df.iloc[r, 14]) else ''
    c18 = str(df.iloc[r, 18]).strip()[:10] if not pd.isna(df.iloc[r, 18]) else ''
    c22 = str(df.iloc[r, 22]).strip()[:15] if not pd.isna(df.iloc[r, 22]) else ''
    
    # Для РЕПО строк
    c0 = str(df.iloc[r, 0]).strip()[:30] if not pd.isna(df.iloc[r, 0]) else ''
    c5 = str(df.iloc[r, 5]).strip()[:30] if not pd.isna(df.iloc[r, 5]) else ''
    c9 = str(df.iloc[r, 9]).strip()[:30] if not pd.isna(df.iloc[r, 9]) else ''
    c15 = str(df.iloc[r, 15]).strip()[:15] if not pd.isna(df.iloc[r, 15]) else ''
    c30 = str(df.iloc[r, 30]).strip()[:15] if not pd.isna(df.iloc[r, 30]) else ''
    c34 = str(df.iloc[r, 34]).strip()[:10] if not pd.isna(df.iloc[r, 34]) else ''
    c42 = str(df.iloc[r, 42]).strip()[:15] if not pd.isna(df.iloc[r, 42]) else ''
    
    if c1 or c3 or c6 or c0:
        print(f"R{r:>3}: C1=[{c1:>30}] C3=[{c3:>15}] C6=[{c6:>25}] C14=[{c14:>10}] C18=[{c18:>5}] C22=[{c22:>10}] || REPO: C0=[{c0}] C5=[{c5}] C9=[{c9}] C15=[{c15}] C30=[{c30}] C34=[{c34}] C42=[{c42}]")
