"""Fixed-source strict business comparison; only writes under an explicit QA root."""
import argparse,hashlib,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE='c125e4b4d06e61131c351e62be2bef0fabf2163b'
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def save(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def freeze(q):
    q.mkdir(parents=True,exist_ok=True)
    if (q/'baseline.json').exists():raise FileExistsError('Baseline already frozen')
    subprocess.run(['git','archive','--format=zip','-o',str(q/'source.zip'),BASE],cwd=ROOT,check=True)
    with zipfile.ZipFile(q/'source.zip') as z:z.extractall(q/'baseline')
    shutil.copytree(ROOT/'qa/governance_032/inputs',q/'inputs')
    protected={}
    for d in [q/'inputs',ROOT/'交付_0.33.1_rc2']:
        for p in d.rglob('*'):
            if p.is_file():protected[str(p)]=sha(p)
    for name in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']:
        protected[str(ROOT/name)]=sha(ROOT/name)
    user=Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings'
    for name in ['workbench.sqlite3','workbench.db','work.sqlite3','work.sqlite']:
        if (user/name).is_file():protected[str(user/name)]=sha(user/name)
    save(q/'baseline.json',{'baseline':BASE,'protected':protected,'source_zip_sha256':sha(q/'source.zip')})
def capture(q,name,source):
    subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(q),'--source',str(source),'--output',str(q/name)],check=True,cwd=ROOT,env={**os.environ,'PYTHONHASHSEED':'0','PYTHONIOENCODING':'utf-8'})
def compare(q,after_name='after'):
    from parallel_qa import read,clean,diff,WHITELIST
    from structure_oracle import excel_snapshot,source_hashes
    before,after=q/'before',q/after_name;a=json.loads((before/'capture.json').read_text('utf-8'));b=json.loads((after/'capture.json').read_text('utf-8'))
    if a['cases']!=b['cases'] or b['source_hashes']!=source_hashes(ROOT):raise ValueError('Source or scenario set changed')
    records=[{'case':n,'differences':diff(clean(read(before/(n+'.pklz'))),clean(read(after/(n+'.pklz'))))} for n in a['cases']]
    records += [{'case':f.name,'differences':diff(excel_snapshot(f),excel_snapshot(after/f.name))} for f in before.glob('*.xlsx')]
    baseline=json.loads((q/'baseline.json').read_text('utf-8'));changed=[p for p,h in baseline['protected'].items() if not Path(p).exists() or sha(p)!=h]
    core={n:{'before':sha(q/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in ['model.py','allocation.py','one_box_exception.py','default_config.json','default_must_sell.json','normal_orders.py','normal_exports.py','order_lifecycle.py']}
    external={str(ROOT/n) for n in ['README.md','docs/项目意图与实施进度.md','docs/plans/业务使用体验优化与验收清单_20261104.md']}
    documents=[{'path':p,'before':baseline['protected'][p],'after':sha(p)} for p in changed if p in external]
    business_changes=[p for p in changed if p not in external]
    report={'baseline':BASE,'cases':records,'difference_count':sum(len(x['differences']) for x in records),'protected_changed':business_changes,'external_document_changes':documents,'core':core,'whitelist':WHITELIST,'comparison':'strict values, Python types, nested field/key order and row order; Excel sheet/cell/types/styles/formats/order'}
    report['passed']=not report['difference_count'] and not business_changes and all(x['before']==x['after'] for x in core.values())
    save(q/'equivalence.json',report);print(json.dumps({k:v for k,v in report.items() if k not in ('cases','core','whitelist')},ensure_ascii=False))
    if not report['passed']:raise SystemExit(1)
def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','before','after','compare']);p.add_argument('--qa',type=Path,default=ROOT/'qa/v0340');p.add_argument('--after',default='after');a=p.parse_args();q=a.qa.resolve()
    if a.mode=='freeze':freeze(q)
    elif a.mode=='compare':compare(q,a.after)
    else:capture(q,a.mode,q/'baseline' if a.mode=='before' else ROOT)
if __name__=='__main__':main()
