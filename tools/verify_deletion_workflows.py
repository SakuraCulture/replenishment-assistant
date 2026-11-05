"""Fixed-source business comparison and isolated actual EXE deletion acceptance."""
import argparse,hashlib,json,os,subprocess,sys,time,zipfile
from pathlib import Path
import parallel_qa as q
ROOT=Path(__file__).resolve().parents[1]

def save(path,data):path.write_text(json.dumps(data,ensure_ascii=False,indent=2),'utf-8')
def compare(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse',a.baseline+'^{commit}'],cwd=ROOT,text=True).strip()
    archive=out/'baseline.zip';subprocess.run(['git','archive','-o',str(archive),commit],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(out/'baseline')
    inputs=a.fixture.resolve()/'inputs'
    protected={str(p):q.sha(p) for p in inputs.rglob('*') if p.is_file()}
    for p in a.protect:protected[str(p.resolve())]=q.sha(p)
    save(out/'frozen.json',{'baseline_commit':commit,'source_archive_sha256':q.sha(archive),'protected':protected})
    env={**os.environ,'PYTHONHASHSEED':'0','PYTHONIOENCODING':'utf-8','QT_QPA_PLATFORM':'offscreen'}
    for name,source in [('before',out/'baseline'),('after',ROOT)]:
        with (out/(name+'.txt')).open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(a.fixture.resolve()),'--source',str(source),'--output',str(out/name)],cwd=ROOT,env=env,stdout=log,stderr=log,check=True)
    before=out/'before';after=out/'after';old=json.loads((before/'capture.json').read_text('utf-8'));new=json.loads((after/'capture.json').read_text('utf-8'))
    if old['cases']!=new['cases']:raise AssertionError('Scenario set/order changed')
    cases=[{'case':n,'differences':q.diff(q.clean(q.read(before/(n+'.pklz'))),q.clean(q.read(after/(n+'.pklz'))))} for n in old['cases']]
    files=[p.name for p in before.glob('*.xlsx')]
    if files!=[p.name for p in after.glob('*.xlsx')]:raise AssertionError('Excel set/order changed')
    cases += [{'case':n,'differences':q.diff(q.excel_snapshot(before/n),q.excel_snapshot(after/n))} for n in files]
    names=('model.py','allocation.py','one_box_exception.py','engine.py','default_config.json','default_must_sell.json','normal_exports.py','jst_orders.py')
    core={n:{'before':q.sha(out/'baseline/src'/n),'after':q.sha(ROOT/'src'/n)} for n in names}
    changed=[p for p,h in protected.items() if q.sha(p)!=h]
    report={'baseline_commit':commit,'cases':cases,'differences':sum(len(c['differences']) for c in cases),'core':core,'changed_protected':changed,'whitelist':q.WHITELIST,'strict_types_values_and_insertion_order':True,'additional_ignored_fields':[]}
    report['passed']=not changed and not report['differences'] and all(v['before']==v['after'] for v in core.values())
    save(out/'equivalence.json',report);print(json.dumps({'passed':report['passed'],'cases':len(cases),'differences':report['differences']}))
    if not report['passed']:raise AssertionError('Comparison failed')

def exe(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONIOENCODING':'utf-8'};env.pop('QT_QPA_PLATFORM',None)
    start=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as log:
        result=subprocess.run([str(a.exe.resolve()),'--deletion-smoke',str(a.fixture.resolve()),str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,timeout=300)
    report=json.loads((out/'deletion-report.json').read_text('utf-8'))
    report.update(process_exit=result.returncode,exe_sha256=q.sha(a.exe),seconds=time.perf_counter()-start)
    save(out/'deletion-report.json',report);print(json.dumps({'passed':report['passed'],'frozen_exe':report.get('frozen_exe'),'seconds':report['seconds']}))
    if result.returncode or not report['passed'] or not report.get('frozen_exe'):raise AssertionError('Actual EXE acceptance failed')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);s=p.add_subparsers(dest='mode',required=True)
    c=s.add_parser('compare');c.add_argument('--baseline',required=True);c.add_argument('--protect',type=Path,nargs='*',default=[])
    e=s.add_parser('exe');e.add_argument('--exe',type=Path,required=True)
    for command in (c,e):command.add_argument('--output',type=Path,required=True);command.add_argument('--fixture',type=Path,required=True)
    a=p.parse_args();globals()[a.mode](a)
