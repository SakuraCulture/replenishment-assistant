"""Serial AB/BA/AB comparison on one immutable synthetic database; no user data."""
import argparse, ctypes, hashlib, json, os, platform, statistics, subprocess, sys, threading, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def peak_mb():
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(n,ctypes.c_size_t) for n in
            ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
    k=ctypes.WinDLL('kernel32'); p=ctypes.WinDLL('psapi'); k.GetCurrentProcess.restype=wintypes.HANDLE
    p.GetProcessMemoryInfo.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
    c=Counters(); c.cb=ctypes.sizeof(c)
    if not p.GetProcessMemoryInfo(k.GetCurrentProcess(),ctypes.byref(c),c.cb):raise ctypes.WinError()
    return c.PeakWorkingSetSize/1024**2

def worker(a):
    sys.path.insert(0,str(a.source/'src'))
    import history_read as hr, daily_batch, jst_batches
    out=a.out; out.mkdir(parents=True,exist_ok=False); cancel=threading.Event(); timings={}; hashes={}
    def timed(name,fn):
        start=time.perf_counter(); value=fn(); timings[name]=time.perf_counter()-start
        hashes[name]=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=False,default=str).encode()).hexdigest()
        return value
    for cold in ('cold','warm'):
        timed('archive_'+cold,lambda:hr.execute(a.database,out/'archive.sqlite3',{'kind':'list'},cancel,lambda _:None))
    for cold in ('cold','warm'):
        timed('tracking_'+cold,lambda:daily_batch.read(a.database,out/'lifecycle.sqlite3',{'all_matched':True},cancel))
    # Same existing service work on both sides. Baseline has no home widget.
    for cold in ('cold','warm'):
        def home():
            if (a.source/'src/business_flow.py').exists():
                import business_flow
                r=business_flow.read_home(a.database,out/'home.sqlite3',cancel)
                return {'total':r['total'],'groups':r['groups'],'latest_batch':r['latest_batch']}
            d=daily_batch.read(a.database,out/'home.sqlite3',{'all_matched':True},cancel)
            bs=jst_batches.listing(a.database,cancel=cancel)
            # The independent old-service oracle is intentionally exact, rather
            # than importing the candidate's projection into the baseline.
            groups={s:[] for s in ('return','payment','receipt','attention')}
            for row in d['records']:
                stage={'jst_payment':'payment','jst_generate':'payment'}.get(row.get('next'),row.get('next'))
                if stage in groups:groups[stage].append(row)
                changed=any(row.get('present',{}).get(s) and not row.get('health',{}).get(s,True) for s in ('return','payment','receipt'))
                if changed or row.get('legacy_incomplete') or row.get('old_revision') or row.get('warehouse')=='旧版未标仓' or (row.get('generation_task') or {}).get('status') in ('failed','blocked','needs_review'):groups['attention'].append(row)
            b=next((x for x in bs if x['status'] in ('active','paused') and sum(x['counts'].get(s,0) for s in ('pending','failed'))),None)
            return {'total':len(d['records']),'groups':groups,'latest_batch':b}
        timed('home_services_'+cold,home)
    value={'timings_seconds':timings,'ordered_results_sha256':hashes,'peak_worker_working_set_mb':peak_mb()}
    (out/'result.json').write_text(json.dumps(value,indent=2),'utf-8')

def main(a):
    from business_flow_fixture import prepare
    a.out.mkdir(parents=True,exist_ok=False)
    meta=prepare(a.out/'fixture',stores=60,products=200); db=Path(meta['database']); before=sha(db)
    runs=[]
    for i,order in enumerate([('baseline','candidate'),('candidate','baseline'),('baseline','candidate')]):
        for version in order:
            source=ROOT/'qa/v0360/baseline' if version=='baseline' else ROOT
            out=a.out/f'{i+1}-{version}'
            started=time.perf_counter()
            subprocess.run([sys.executable,__file__,'--worker','--source',str(source),'--database',str(db),'--out',str(out)],check=True,env={**os.environ,'PYTHONHASHSEED':'0'})
            r=json.loads((out/'result.json').read_text('utf-8'));r.update(version=version,round=i+1,process_total_seconds=time.perf_counter()-started);runs.append(r)
    if sha(db)!=before:raise AssertionError('Business database changed')
    if any(r['ordered_results_sha256']!=runs[0]['ordered_results_sha256'] for r in runs):raise AssertionError('Ordered service output differs')
    summary={}
    for field in [*runs[0]['timings_seconds'],'process_total_seconds','peak_worker_working_set_mb']:
        summary[field]={}
        for version in ('baseline','candidate'):
            values=[r['timings_seconds'].get(field,r.get(field)) for r in runs if r['version']==version]
            summary[field][version]={'median':statistics.median(values),'min':min(values),'max':max(values),'raw':values}
    report={'passed':True,'baseline':'35b33de022be33958ec57851f722130feb328d7a','python':sys.version,'platform':platform.platform(),
        'fixture':meta,'database_sha256':before,'order':'AB/BA/AB, sequential workers, PYTHONHASHSEED=0',
        'limits':['Synthetic 60 stores / 120 warehouse orders / 12000 signed rows; not production scale.',
        'Cold means a fresh application index. Windows filesystem cache was not flushed.',
        'PeakWorkingSetSize is Windows native peak for each service worker, includes imports; no GUI or other processes.',
        'Home has no old UI counterpart; baseline measures equivalent old services, not old home startup. New automatic home query adds background work.',
        'UI event-loop responsiveness is measured separately in the actual executable.'],
        'summary':summary,'runs':runs}
    (a.out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--worker',action='store_true');p.add_argument('--source',type=Path);p.add_argument('--database',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    (worker if a.worker else main)(a)
