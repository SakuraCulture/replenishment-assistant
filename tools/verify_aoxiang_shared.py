"""Fresh fixed-prechange business comparison; no browser, exports or daily-user writes.

python tools/verify_aoxiang_shared.py --baseline a5036cd --fixture qa/aoxiang_integration_20261101 --output qa/NEW
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from parallel_qa import read, clean, diff, WHITELIST
from structure_oracle import excel_snapshot

ROOT=Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',required=True);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    baseline=git('rev-parse',a.baseline+'^{commit}').decode().strip()
    seed=json.loads((a.fixture/'baseline.json').read_text(encoding='utf-8'))
    protected={}
    for key,base in [('input_sha256',a.fixture),('core_sha256',ROOT),('protected',Path())]:
        for name,expected in seed[key].items():
            file=(base/name).resolve()
            if sha(file)!=expected:raise ValueError('Protected baseline differs: '+str(file))
            protected[str(file)]=expected
    old_delivery=json.loads((ROOT/'releases/0.24.0/ax_rc4/delivery-files.json').read_text(encoding='utf-8'))
    for name,record in old_delivery.items():
        file=ROOT/'交付_0.24.0_ax_rc4/补货助手'/name
        if sha(file)!=record['sha256']:raise ValueError('Old delivery differs: '+name)
        protected[str(file)]=record['sha256']
    for file in (ROOT/'交付_0.24.0_ax_rc4').glob('*.zip'):protected[str(file)]=sha(file)
    archive=out/'baseline-source.zip'
    subprocess.run(['git','archive','--format=zip','-o',str(archive),baseline],cwd=ROOT,check=True)
    with zipfile.ZipFile(archive) as z:z.extractall(out/'baseline-source')
    source_changes=[]
    for file in (out/'baseline-source/src').glob('*'):
        if file.is_file() and sha(file)!=sha(ROOT/'src'/file.name):
            source_changes.append(file.name)
            if not file.name.startswith('aoxiang_'):raise ValueError('Non-AX source changed: '+file.name)
    manifest=dict(baseline_commit=baseline,baseline_archive_sha256=sha(archive),source_changes=source_changes,protected=protected)
    (out/'baseline.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    timings={}
    for name,source in [('before',out/'baseline-source'),('after',ROOT)]:
        env=dict(os.environ,PYTHONUTF8='1',PYTHONHASHSEED='0',QT_QPA_PLATFORM='offscreen',REPLENISHMENT_DATA_DIR=str(out/(name+'-settings')))
        start=time.perf_counter()
        with (out/(name+'.txt')).open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(a.fixture.resolve()),'--source',str(source),'--output',str(out/name)],cwd=ROOT,env=env,stdout=log,stderr=log,check=True)
        timings[name]=time.perf_counter()-start
    cases=json.loads((out/'before/capture.json').read_text(encoding='utf-8'))['cases']
    if cases!=json.loads((out/'after/capture.json').read_text(encoding='utf-8'))['cases']:raise ValueError('Scenario order changed')
    comparisons=[]
    for case in cases:
        changes=diff(clean(read(out/'before'/(case+'.pklz'))),clean(read(out/'after'/(case+'.pklz'))))
        comparisons.append(dict(case=case,differences=changes))
    for file in (out/'before').glob('*.xlsx'):
        comparisons.append(dict(case=file.name,differences=diff(excel_snapshot(file),excel_snapshot(out/'after'/file.name))))
    changed=[file for file,digest in protected.items() if sha(file)!=digest]
    total=sum(len(c['differences']) for c in comparisons)
    report=dict(passed=not total and not changed,baseline_commit=baseline,baseline_archive_sha256=sha(archive),
        comparisons=comparisons,total_differences=total,strict_order_and_types=True,dynamic_whitelist=WHITELIST,
        protected_files=len(protected),changed_protected=changed,source_changes=source_changes,capture_seconds=timings,
        performance_note='Capture durations are regression runtimes, not Edge or platform speed measurements.')
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({k:report[k] for k in ['passed','total_differences','protected_files','changed_protected']},ensure_ascii=False))
    if not report['passed']:raise SystemExit(1)

if __name__=='__main__':main()
