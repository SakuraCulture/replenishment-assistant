"""Reproducible strict comparison and isolated EXE acceptance for maintenance."""
import argparse,json,os,subprocess,sys,time
from pathlib import Path
import parallel_qa as qa

def compare(a):
    root=a.qa.resolve();before=root/'before';after=root/'after'
    baseline=json.loads((root/'baseline.json').read_text('utf-8'))
    old=json.loads((before/'capture.json').read_text('utf-8'));new=json.loads((after/'capture.json').read_text('utf-8'))
    if old['cases']!=new['cases']:raise AssertionError('Scenario order changed')
    cases=[{'case':name,'differences':qa.diff(qa.clean(qa.read(before/(name+'.pklz'))),qa.clean(qa.read(after/(name+'.pklz'))))} for name in old['cases']]
    if sorted(p.name for p in before.glob('*.xlsx'))!=sorted(p.name for p in after.glob('*.xlsx')):raise AssertionError('Export file set changed')
    for file in before.glob('*.xlsx'):cases.append({'case':file.name,'differences':qa.diff(qa.excel_snapshot(file),qa.excel_snapshot(after/file.name))})
    if new['source_hashes']!=qa.source_hashes(qa.ROOT):raise AssertionError('Source changed after capture')
    # Existing frozen fixture hashes predate this change; do not regenerate expectations.
    fixture=qa.ROOT/'qa/aoxiang_integration_20261101'
    prior=json.loads((fixture/'baseline.json').read_text('utf-8'))['input_sha256']
    inputs={str(fixture/p):h for p,h in prior.items()}
    protected={**baseline['protected'],**inputs}
    changed=[p for p,h in protected.items() if qa.sha(Path(p))!=h]
    core={n:{'before':qa.sha(root/'before_source/src'/n),'after':qa.sha(qa.ROOT/'src'/n)} for n in ('model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json')}
    report={'baseline_commit':baseline['baseline'],'strict_values_types_order':True,'whitelist':qa.WHITELIST,'additional_ignored_fields':[],
            'cases':cases,'differences':sum(len(c['differences']) for c in cases),'core':core,'input_sha256':inputs,'changed_protected':changed}
    report['passed']=not report['differences'] and not changed and all(v['before']==v['after'] for v in core.values())
    (root/'equivalence.json').write_bytes(json.dumps(report,ensure_ascii=False,indent=2).encode())
    print(json.dumps({k:report[k] for k in ('passed','differences','changed_protected')}))
    if not report['passed']:raise AssertionError('Strict comparison failed')

def exe(a):
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    env=os.environ.copy();env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PYTHONUTF8']='1';env.pop('QT_QPA_PLATFORM',None)
    start=time.perf_counter()
    with (out/'process.txt').open('w',encoding='utf-8') as log:
        result=subprocess.run([str(a.exe.resolve()),'--maintenance-smoke',str(a.fixture.resolve()),str(out)],env=env,stdout=log,stderr=log,timeout=180)
    data=json.loads((out/'maintenance-report.json').read_text('utf-8'));data.update(process_exit=result.returncode,exe_sha256=qa.sha(a.exe),seconds=time.perf_counter()-start)
    (out/'maintenance-report.json').write_bytes(json.dumps(data,ensure_ascii=False,indent=2).encode())
    print(json.dumps({'passed':data['passed'],'frozen_exe':data.get('frozen_exe'),'seconds':data['seconds']}))
    if result.returncode or not data['passed'] or not data.get('frozen_exe'):raise AssertionError('Actual EXE acceptance failed')

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    c=sub.add_parser('compare');c.add_argument('--qa',type=Path,required=True)
    e=sub.add_parser('exe');e.add_argument('--exe',type=Path,required=True);e.add_argument('--output',type=Path,required=True);e.add_argument('--fixture',type=Path,required=True)
    a=p.parse_args();globals()[a.mode](a)
