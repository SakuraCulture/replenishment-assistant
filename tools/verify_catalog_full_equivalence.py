"""Strict ordered fixed-commit comparison for the dual-account live verification."""
import argparse,json,sys,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,clean,diff,WHITELIST
from structure_oracle import excel_snapshot,source_hashes
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def main():
 p=argparse.ArgumentParser();p.add_argument('--qa',type=Path,required=True);p.add_argument('--before',default='before-fixed');p.add_argument('--after',default='after-final');a=p.parse_args();q=a.qa.resolve();old=q/a.before;new=q/a.after
 x=json.loads((old/'capture.json').read_text('utf-8'));y=json.loads((new/'capture.json').read_text('utf-8'))
 if x['cases']!=y['cases'] or x['PYTHONHASHSEED']!='0' or y['PYTHONHASHSEED']!='0':raise ValueError('Capture settings mismatch')
 if y['source_hashes']!=source_hashes(ROOT):raise ValueError('Source changed after capture')
 cases=[{'case':n,'differences':diff(clean(read(old/(n+'.pklz'))),clean(read(new/(n+'.pklz'))))} for n in x['cases']]
 cases += [{'case':f.name,'differences':diff(excel_snapshot(f),excel_snapshot(new/f.name))} for f in old.glob('*.xlsx')]
 frozen=json.loads((q/'baseline.json').read_text('utf-8'))
 changed=[s for s,h in frozen['protected'].items() if not Path(s).is_file() or sha(s)!=h]
 ax=[s for s,h in frozen['aoxiang_source'].items() if sha(ROOT/s)!=h]
 core={n:{'before':sha(q/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in ['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']}
 changes=sum(len(c['differences']) for c in cases)
 report={'passed':not changes and not changed and not ax and all(v['before']==v['after'] for v in core.values()),'baseline':frozen['baseline'],'cases':cases,'differences':changes,'whitelist':WHITELIST,'strict_record_and_dictionary_order':True,'core':core,'aoxiang_source_changed':ax,'changed_protected':changed,'protected_count':len(frozen['protected'])}
 (q/'equivalence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps({'passed':report['passed'],'cases':len(cases),'differences':changes,'protected':len(frozen['protected'])}))
 if not report['passed']:raise SystemExit(1)
if __name__=='__main__':main()
