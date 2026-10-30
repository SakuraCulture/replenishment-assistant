"""Strict order/value/type/style comparison against frozen pre-fix commit."""
import json
from pathlib import Path
from parallel_qa import read,clean,diff,WHITELIST
from structure_oracle import excel_snapshot
q=Path(__file__).resolve().parents[1]/'qa/verified_092';old=q/'regression_old';new=q/'regression_pre_overrun'
names=json.loads((old/'capture.json').read_text())['cases']
assert names==json.loads((new/'capture.json').read_text())['cases']
cases=[{'case':name,'differences':diff(clean(read(old/(name+'.pklz'))),clean(read(new/(name+'.pklz'))))} for name in names]
for p in old.glob('*.xlsx'):cases.append({'case':p.name,'differences':diff(excel_snapshot(p),excel_snapshot(new/p.name))})
count=sum(len(x['differences']) for x in cases)
r={'passed':count==0,'baseline':'7dfc00b545b6dac1dd586212fff940e5346e0710','unexpected_differences':count,'cases':cases,'whitelist':WHITELIST,'hash_seed':0}
(q/'equivalence_pre_overrun_recheck.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':not count,'cases':len(cases),'differences':count}));assert not count
