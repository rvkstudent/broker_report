import pandas as pd
import warnings
warnings.filterwarnings('ignore')

fp = 'c:\\Users\\Roman\\YandexDisk\\ProjectSQL\\Broker_Report\\reports\\report963464ae-b003-a5d1-c98b-2f4abc8d75ed.xlsx'
df = pd.read_excel(fp, header=None)

out = []

# 1. Find all section headers
out.append("=== SECTION HEADERS ===")
for r in range(len(df)):
    row = df.iloc[r]
    for c in [0, 1, 9, 15]:
        if c >= len(df.columns): continue
        v = str(row[c]).strip() if pd.notna(row[c]) else ''
        if v and len(v) > 5 and len(v) < 150:
            if v[0].isdigit() and '. ' in v[:5]:
                out.append(f'R{r}C{c}: {v[:150]}')

# 2. Find trade-related sections - look at column 1 for identifiers
out.append("\n=== TRADE/REPO SECTIONS (C1 starts with 'Внебирж' or 'Бирж') ===")
for r in range(len(df)):
    v = str(df.iloc[r, 1]).strip() if pd.notna(df.iloc[r, 1]) else ''
    if v.startswith(('Внебирж', 'Бирж', 'Спец')) or 'РЕПО' in v:
        vals = {}
        for c in range(min(30, len(df.columns))):
            x = df.iloc[r, c]
            if pd.notna(x):
                vals[c] = str(x).replace('\n',' ')[:40]
        out.append(f'R{r}: {vals}')

# 3. Show some rows around trades section
out.append("\n=== ROWS 150-250 (looking for column headers) ===")
for r in range(150, min(250, len(df))):
    vals = {}
    for c in range(min(40, len(df.columns))):
        v = df.iloc[r, c]
        if pd.notna(v):
            vals[c] = str(v).replace('\n',' ')[:50]
    if vals:
        out.append(f'R{r}: {vals}')

with open('c:\\Users\\Roman\\YandexDisk\\ProjectSQL\\Broker_Report\\xlsx_full_analysis.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))

print('Written to xlsx_full_analysis.txt', flush=True)
