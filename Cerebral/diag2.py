"""Deep diagnosis of orphan dispensation lines on a specific day."""
import sys
import pandas as pd
sys.path.insert(0, '.')
from tta_etl import read_export, classify_export, map_channel
from pathlib import Path

folder = Path(sys.argv[1] if len(sys.argv) > 1 else 'history/2025-12')

disp, pos = {}, []
for p in sorted(folder.glob('*.xls*')):
    k = classify_export(p)
    if k == 'dispensations':
        d = read_export(p, 'dispensations')
        d['ts'] = pd.to_datetime(d['ReceiptDate'])
        disp[str(d['Location'].dropna().iloc[0]).strip()] = d
    elif k == 'pos_register':
        r = read_export(p, 'pos_register')
        r['ts'] = pd.to_datetime(r['PosDate'])
        pos.append(r)

allpos = pd.concat(pos, ignore_index=True).drop_duplicates(subset=['PosId'])
pos_ids = set(allpos['PosId'])
print(f"\nPOS pool: {len(allpos):,} unique transactions, "
      f"PosId {allpos.PosId.min():,} .. {allpos.PosId.max():,}\n")

for loc, d in disp.items():
    orph = d[~d['ReceiptNo'].isin(pos_ids)]
    if not len(orph):
        continue
    day = orph.groupby(orph.ts.dt.date).size().idxmax()
    o = orph[orph.ts.dt.date == day]
    dd = d[d.ts.dt.date == day]
    pp = allpos[allpos.ts.dt.date == day]

    print('=' * 74)
    print(f"{loc}   worst day: {day}")
    print('-' * 74)
    print(f"  dispensation lines that day : {len(dd):>7,}  "
          f"({dd.ReceiptNo.nunique():,} receipts)")
    print(f"  POS transactions that day   : {len(pp):>7,}")
    print(f"  orphan lines that day       : {len(o):>7,}  "
          f"({o.ReceiptNo.nunique():,} receipts)")
    print()
    print(f"  orphan ReceiptNo range : {o.ReceiptNo.min():,} .. {o.ReceiptNo.max():,}")
    if len(pp):
        print(f"  POS PosId range that day: {pp.PosId.min():,} .. {pp.PosId.max():,}")
        print(f"  POS registers that day  : {sorted(pp.Register.astype(str).unique())}")
    matched = dd[dd.ReceiptNo.isin(pos_ids)]
    if len(matched):
        print(f"  matched ReceiptNo range : {matched.ReceiptNo.min():,} .. "
              f"{matched.ReceiptNo.max():,}")
    print()
    print("  sample orphan receipts:")
    s = o.drop_duplicates('ReceiptNo').head(5)
    for _, row in s.iterrows():
        print(f"    {row.ReceiptNo}  {str(row.ts)[:19]}  "
              f"{str(row.Pharmacist)[:22]:<22} {str(row.Product)[:34]}")
    near = allpos[(allpos.PosId > o.ReceiptNo.min() - 40) &
                  (allpos.PosId < o.ReceiptNo.min() + 40)]
    print(f"\n  POS transactions with nearby IDs: {len(near)}")
    if len(near):
        for _, row in near.head(4).iterrows():
            print(f"    {row.PosId}  {str(row.ts)[:19]}  {row.Register}")
    print()
