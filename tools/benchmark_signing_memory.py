"""Alternating old/new signing with complete wide synthetic daily inputs.

No production paths/platforms. Child PID is reported before allocation. A
Windows Job Object limits only this isolated process, not the user's session.
The same generator and fixed clock/UUIDs preserve exact comparable snapshots.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import threading
import time
import traceback


def counters(pid=None):
    class Memory(c.Structure):
        _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(n,c.c_size_t) for n in
            ('peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_private','private')]
    kernel=c.WinDLL('kernel32',use_last_error=True);psapi=c.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    psapi.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.POINTER(Memory),w.DWORD]
    h=kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1010,False,pid)
    if not h:return {}
    try:
        v=Memory();v.cb=c.sizeof(v)
        if not psapi.GetProcessMemoryInfo(h,c.byref(v),v.cb):return {}
        return {name+'_bytes':getattr(v,name) for name in ('working','peak_working','private','peak_private')}
    finally:
        if pid is not None:kernel.CloseHandle(h)


def memory_limit(mib):
    if not mib:return None
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
            ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(c.Structure):_fields_=[(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    k=c.WinDLL('kernel32',use_last_error=True)
    k.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];k.CreateJobObjectW.restype=w.HANDLE
    k.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    k.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
    k.GetCurrentProcess.restype=w.HANDLE
    handle=k.CreateJobObjectW(None,None);info=Extended();info.basic.flags=0x100;info.process_memory=mib*1024**2
    if not handle or not k.SetInformationJobObject(handle,9,c.byref(info),c.sizeof(info)) or not k.AssignProcessToJobObject(handle,k.GetCurrentProcess()):
        raise c.WinError(c.get_last_error())
    return handle


def fixture(rows=20000,files=17,extra=1000):
    # Load only the data generator; all runtime imports still resolve to the
    # selected frozen old/new source, never to the other business implementation.
    import importlib.util
    spec=importlib.util.spec_from_file_location('synthetic_signing_fixture',Path(__file__).resolve().parents[1]/'src/signing_memory_fixture.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.fixture(rows,files,extra)


def child(a):
    out=a.output;out.mkdir(parents=True,exist_ok=False)
    sys.path[:0]=[str(a.source/'src')]
    handle=memory_limit(a.limit_mib)
    (out/'worker-pid.txt').write_text(str(os.getpid()))
    report={'passed':False,'pid':os.getpid(),'source':str(a.source),'files':a.files,'rows_per_file':a.rows,'limit_mib':a.limit_mib,'stages':[]}
    report['source_hashes']={p.name:hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in sorted((a.source/'src').glob('*.py'))}
    report['fixture_generator_sha256']=hashlib.file_digest((Path(__file__).resolve().parents[1]/'src/signing_memory_fixture.py').open('rb'),'sha256').hexdigest()
    (out/'phase.txt').write_text('fixture')
    try:
        import work_repository as wr,normal_orders as normal,normal_delivery as delivery
        from uuid import UUID
        import uuid
        # Identical ids/timestamps on both sides, without patching business rules.
        wr.now=lambda:'2026-11-16T12:00:00+08:00'
        serial=iter(range(1,1000000));uuid.uuid4=lambda:UUID(int=next(serial))
        start=time.perf_counter();payload,codes=fixture(a.rows,a.files,a.extra)
        report['fixture_seconds']=time.perf_counter()-start;report['fixture_memory']=counters()
        work=wr.Repository(out/'settings');repo=normal.Repository(work)
        if (a.source/'src/encoding_store.py').is_file():
            import encoding_store
            close=encoding_store.Store.close
            def observed_close(store):
                report['encoding_stats']=store.stats()
                return close(store)
            encoding_store.Store.close=observed_close
        if a.profile:
            import cProfile
            profiler=cProfile.Profile();profiler.enable()
        begins=[];sql=[]
        def progress(message):
            text=str(message)
            (out/'stage.txt').write_text(text,encoding='utf-8')
            if hasattr(message,'active'):begins.append(message.active)
            if len(report['stages'])<300 and (not report['stages'] or time.perf_counter()-report['stages'][-1]['at']>2 or hasattr(message,'active')):
                report['stages'].append({'at':time.perf_counter(),'stage':text,**counters()})
        (out/'phase.txt').write_text('confirm');start=time.perf_counter();cpu=time.process_time()
        heads=repo.confirm(payload,codes,None,{s:None for s in codes},headers_only=True,progress=progress)
        report['confirm_seconds']=time.perf_counter()-start;report['confirm_cpu_seconds']=time.process_time()-cpu;report['confirm_memory']=counters()
        if a.profile:profiler.disable();profiler.dump_stats(out/'profile.pstats')
        assert begins==[True,False],begins
        assert [h['store'] for h in heads]==codes
        # Verify all 340k ordered rows, quantities and amounts from immutable child
        # snapshots, one store at a time, without retaining a second complete day.
        (out/'phase.txt').write_text('verify');start=time.perf_counter();checks=[]
        import warehouse_execution as execution
        for head in heads:
            assert len(head['warehouse_heads'])==1
            child_head=head['warehouse_heads'][0];record=execution.get_order(repo,child_head)
            rows=record['payload']['result']['rows'];expected=payload['results'][head['store']]
            assert rows==expected['rows'] and record['payload']['result']['summary']==expected['summary']
            h=hashlib.sha256()
            for row in rows:h.update(wr.dumps([row['E'],row['AS'],row['BV'],row['AU']]).encode())
            checks.append({'store':head['store'],'digest':head['digest'],'child_digest':child_head['snapshot_digest'],
                'sequence_quantity_amount_sha256':h.hexdigest(),'summary':expected['summary'],'rows':len(rows)})
            del record,rows
        with work.connect() as db:
            row=db.execute('SELECT digest,summary,payload FROM jobs').fetchone()
            manifest=__import__('storage_chunks').manifest(row[2])
            assert db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]==a.files
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        report['work_digest']=row[0];report['work_size']=manifest['size'];report['orders']=checks
        report['verification_seconds']=time.perf_counter()-start
        assert 'SYN%03d'%(a.files+1) in wr.loads(row[1])['stores'] if a.extra else True
        if a.export:
            output=out/'export';output.mkdir();start=time.perf_counter();(out/'phase.txt').write_text('export')
            for head in heads:
                record=execution.get_order(repo,head['warehouse_heads'][0],view='export')
                __import__('normal_exports').export(record,output/('synthetic-'+head['store']+'.xlsx'),warehouse=head['warehouse_heads'][0]['warehouse'])
                del record
            report['export_seconds']=time.perf_counter()-start
            assert len(list(output.glob('*.xlsx')))==a.files
        report['temporary_remaining']=[str(p) for p in (work.path.parent/'.encoding-cache').glob('*')]
        assert not report['temporary_remaining'];report['database_bytes']=work.path.stat().st_size;report['passed']=True
    except BaseException as exc:
        report['exception']=type(exc).__name__;report['error']=str(exc);report['traceback']=traceback.format_exc()
        report['failure_phase']=(out/'phase.txt').read_text()
        report['commit_signals']=locals().get('begins',[])
        try:
            import sqlite3
            with sqlite3.connect(out/'settings/workbench.sqlite3') as db:
                report['failure_db_heads']=db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]
                report['failure_db_jobs']=db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        except Exception:pass
    finally:
        report['final_memory']=counters();(out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if report['passed'] else 1


def run(a,source,out):
    command=[sys.executable,__file__,'--child','--source',str(source),'--output',str(out),
             '--rows',str(a.rows),'--files',str(a.files),'--extra',str(a.extra),'--limit-mib',str(a.limit_mib)]
    if a.export:command.append('--export')
    if a.profile:command.append('--profile')
    with out.with_suffix('.log').open('wb') as log:
        process=subprocess.Popen(command,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        samples=[];began=time.monotonic();pid=None
        while process.poll() is None:
            if (out/'worker-pid.txt').is_file():pid=int((out/'worker-pid.txt').read_text())
            phase=(out/'phase.txt').read_text() if (out/'phase.txt').is_file() else 'startup'
            data=counters(pid) if pid else {}
            if data:samples.append({'at':time.monotonic()-began,'phase':phase,**data})
            if time.monotonic()-began>5400:raise TimeoutError('Owned benchmark exceeded 90 minutes')
            time.sleep(.1)
    result=json.loads((out/'result.json').read_text(encoding='utf-8')) if (out/'result.json').is_file() else {'passed':False,'error':'child exited without result'}
    result['returncode']=process.returncode;result['samples_peak']={key:max((s.get(key,0) for s in samples),default=0) for key in ('working_bytes','private_bytes')}
    result['phase_peaks']={phase:{key:max((s.get(key,0) for s in samples if s['phase']==phase),default=0) for key in ('working_bytes','private_bytes')} for phase in {s['phase'] for s in samples}}
    (out/'samples.json').write_text(json.dumps(samples))
    (out/'result-sampled.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'run':str(out),'passed':result['passed'],'confirm_seconds':result.get('confirm_seconds'),
        'peak':result['samples_peak'],'error':result.get('exception')},ensure_ascii=False),flush=True)
    return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--child',action='store_true');p.add_argument('--source',type=Path,default=Path.cwd())
    p.add_argument('--baseline',type=Path);p.add_argument('--output',type=Path,required=True);p.add_argument('--rows',type=int,default=20000)
    p.add_argument('--files',type=int,default=17);p.add_argument('--extra',type=int,default=1000);p.add_argument('--runs',type=int,default=3)
    p.add_argument('--limit-mib',type=int,default=2400);p.add_argument('--export',action='store_true');p.add_argument('--profile',action='store_true');a=p.parse_args()
    a.source=a.source.resolve();a.output=a.output.resolve()
    if a.child:return child(a)
    a.output.mkdir(exist_ok=False);results={'old':[],'new':[]}
    for n in range(a.runs):
        for kind in (('old','new') if n%2==0 else ('new','old')):
            result=run(a,a.baseline if kind=='old' else a.source,a.output/f'{kind}-{n+1}')
            results[kind].append(result)
            (a.output/'comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    success=all(r['passed'] for records in results.values() for r in records)
    if success:
        reference=results['old'][0]
        for records in results.values():
            for record in records:
                assert record['orders']==reference['orders'];assert record['work_digest']==reference['work_digest']
        results['median']={kind:{key:statistics.median(r[key] for r in records) for key in ('confirm_seconds','confirm_cpu_seconds','verification_seconds')} for kind,records in results.items() if kind in ('old','new')}
    results['passed']=success
    (a.output/'comparison.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if success else 1


if __name__=='__main__':sys.exit(main())
