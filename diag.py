"""Diagnose orphan dispensation lines: compare export coverage per store."""
import sys, glob
import pandas as pd
sys.path.insert(0, '.')
from tta_etl import read_export, classify_export
from pathlib import Path

folder = Path(sys.argv[1] if len(sys.argv) > 1 else 'history/2025-12')
disp, pos = {}, {}

for p in sorted(folder.glob('*.xls*')):
    kind = classify_export(p)
    if kind == 'dispensations':
        d = read_export(p, 'dispensations')
        loc = str(d['Location'].dropna().iloc[0]).strip()
        d['ts'] = pd.to_datetime(d['ReceiptDate'])
        disp[loc] = d
    elif kind == 'pos_register':
        r = read_export(p, 'pos_register')
        r['ts'] = pd.to_datetime(r['PosDate'])
        pos[p.name] = r

print(f"\n{'STORE':<38} {'DISPENSATIONS':<32} {'lines':>8}")
print('-' * 82)
for loc, d in disp.items():
    print(f"{loc:<38} {str(d.ts.min())[:16]} -> {str(d.ts.max())[:16]}  {len(d):>8,}")

print(f"\n{'POS FILE':<38} {'RANGE':<32} {'rows':>8}")
print('-' * 82)
for name, r in pos.items():
    print(f"{name[:37]:<38} {str(r.ts.min())[:16]} -> {str(r.ts.max())[:16]}  {len(r):>8,}")

print("\nORPHAN ANALYSIS")
print('-' * 82)
all_pos = set()
for r in pos.values():
    all_pos |= set(r['PosId'])

for loc, d in disp.items():
    orph = d[~d['ReceiptNo'].isin(all_pos)]
    if not len(orph):
        print(f"{loc:<38} no orphans")
        continue
    by_day = orph.groupby(orph.ts.dt.date).size().sort_values(ascending=False)
    print(f"{loc:<38} {len(orph):,} orphan lines on {len(by_day)} day(s)")
    print(f"{'':38} worst days: " +
          ', '.join(f'{k} ({v})' for k, v in by_day.head(5).items()))
