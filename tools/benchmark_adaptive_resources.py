"""Old/new signed 17x20k comparison under process-local resource limits.

The occupied scenario reserves 64 MiB only in its own worker, lowers only that
worker's Job Object limit, then releases it. It never fills the host's memory.
"""
import argparse
import ctypes as c
from ctypes import wintypes as w
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback
import statistics

import benchmark_signing_memory as helpers

MIB=1024**2


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str),encoding='utf-8')


def set_limit(handle,mib):
    class Basic(c.Structure):
        _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
            ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
    class IO(c.Structure):
        _fields_=[(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
    class Extended(c.Structure):
        _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
    info=Extended();info.basic.flags=0x100;info.process_memory=mib*MIB
    if not kernel.SetInformationJobObject(handle,9,c.byref(info),c.sizeof(info)):
        raise c.WinError(c.get_last_error())


def child(a):
    out=a.output;out.mkdir(exist_ok=False);sys.path[:0]=[str(a.source/'src')]
    handle=helpers.memory_limit(a.tight_mib if a.scenario=='tight' else 6000)
    report={'passed':False,'source':str(a.source),'scenario':a.scenario,'files':a.files,'rows_per_file':a.rows,
            'pressure_events':[],'resource_budgets':[],'stages':[],'synthetic_only':True,'live_platform':False}
    (out/'worker-pid.txt').write_text(str(os.getpid()))
    held=None;phase='fixture';sign_active=False;sign_started=sign_released=export_started=False;last_sample=0
    budgets=[]
    def occupy(active,where):
        nonlocal held
        if active:
            held=bytearray(64*MIB)
            set_limit(handle,a.occupied_mib)
        else:
            set_limit(handle,6000);held=None
        (out/'reserved-bytes.txt').write_text(str(64*MIB if active else 0))
        report['pressure_events'].append({'active':active,'where':where,'at':time.monotonic(),**helpers.counters(),
                                          'process_limit_mib':a.occupied_mib if active else 6000})
    def progress(message):
        nonlocal last_sample,sign_active,sign_started,sign_released,export_started
        text=str(message);now=time.monotonic()
        if a.scenario=='occupied':
            current=getattr(message,'current',0);total=getattr(message,'total',0)
            if phase=='confirm' and '编码内容块' in text:
                if current>=min(512,max(32,total//4)) and not sign_started:
                    occupy(True,'signing');sign_active=sign_started=True
                elif sign_active and current>=max(64,total//2):
                    occupy(False,'signing');sign_active=False;sign_released=True
            if phase=='export' and current==500 and not export_started:
                occupy(True,'export');export_started=True
        if now-last_sample>=1 or hasattr(message,'active'):
            report['stages'].append({'phase':phase,'text':text,'at':now,**helpers.counters()})
            (out/'stage.txt').write_text(text,encoding='utf-8');last_sample=now
        if hasattr(message,'active'):report.setdefault('commit_signals',[]).append(message.active)
        if '资源自适应' in text or '改为逐行' in text:
            report.setdefault('adaptive_messages',[]).append(text)
    def change_phase(name):
        nonlocal phase
        phase=name;(out/'phase.txt').write_text(name)
    try:
        import uuid
        from uuid import UUID
        import work_repository as wr,normal_orders as normal,warehouse_execution as execution,normal_exports
        try:
            import adaptive_resources
        except ImportError:pass
        else:
            init=adaptive_resources.Budget.__init__
            def observed(obj,*args,**kwargs):init(obj,*args,**kwargs);budgets.append(obj)
            adaptive_resources.Budget.__init__=observed
        wr.now=lambda:'2026-11-16T12:00:00+08:00';serial=iter(range(1,1000000));uuid.uuid4=lambda:UUID(int=next(serial))
        import work_memory
        report['system_before_fixture']=work_memory.snapshot()
        change_phase('fixture');start=time.perf_counter();payload,codes=helpers.fixture(a.rows,a.files,a.extra)
        report['fixture_seconds']=time.perf_counter()-start;report['fixture_memory']=helpers.counters()
        report['system_after_fixture']=work_memory.snapshot()
        work=wr.Repository(out/'settings');repo=normal.Repository(work)
        if (a.source/'src/encoding_store.py').is_file():
            import encoding_store
            close=encoding_store.Store.close
            def observed_close(store):report['encoding_stats']=store.stats();return close(store)
            encoding_store.Store.close=observed_close
        change_phase('confirm');start=time.perf_counter();cpu=time.process_time()
        heads=repo.confirm(payload,codes,None,{s:None for s in codes},headers_only=True,progress=progress)
        report.update(confirm_seconds=time.perf_counter()-start,confirm_cpu_seconds=time.process_time()-cpu,
                      confirm_memory=helpers.counters())
        assert report['commit_signals']==[True,False]
        assert [h['store'] for h in heads]==codes
        if held is not None:occupy(False,'signing-finish')
        change_phase('verify');checks=[]
        for head in heads:
            record=execution.get_order(repo,head['warehouse_heads'][0]);rows=record['payload']['result']['rows']
            expected=payload['results'][head['store']]
            assert rows==expected['rows'] and record['payload']['result']['summary']==expected['summary']
            digest=hashlib.sha256()
            for row in rows:digest.update(wr.dumps([row['E'],row['AS'],row['BV'],row['AU']]).encode())
            checks.append({'store':head['store'],'digest':head['digest'],'child_digest':head['warehouse_heads'][0]['snapshot_digest'],
                           'sequence_quantity_amount_sha256':digest.hexdigest(),'summary':expected['summary'],'rows':len(rows)})
            del record,rows
        with work.connect() as db:
            report['work_digest']=db.execute('SELECT digest FROM jobs').fetchone()[0]
            assert db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]==a.files
            assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        report['orders']=checks
        if not a.no_export:
            import openpyxl
            directory=out/'exports';directory.mkdir();change_phase('export');start=time.perf_counter();cpu=time.process_time();files=[]
            for head in heads:
                record=execution.get_order(repo,head['warehouse_heads'][0],view='export');path=directory/(head['store']+'.xlsx')
                normal_exports.export(record,path,warehouse=head['warehouse_heads'][0]['warehouse'],progress=progress)
                del record
                if held is not None:occupy(False,'export-finish')
                files.append(path)
            report.update(export_seconds=time.perf_counter()-start,export_cpu_seconds=time.process_time()-cpu,export_memory=helpers.counters())
            change_phase('export-verify');report['exports']=[]
            for path in files:
                book=openpyxl.load_workbook(path,read_only=True,data_only=False)
                digest=hashlib.sha256();count=0
                try:
                    for name in book.sheetnames:
                        digest.update(name.encode())
                        for values in book[name].values:
                            digest.update(wr.dumps(values).encode());count+=1
                finally:book.close()
                report['exports'].append({'store':path.stem,'cell_sequence_sha256':digest.hexdigest(),'rows_including_headers':count})
        assert not list((work.path.parent/'.encoding-cache').glob('*.tmp'))
        report['resource_budgets']=[b.stats() for b in budgets]
        report['passed']=True
    except BaseException as exc:
        report.update(error=str(exc),exception=type(exc).__name__,traceback=traceback.format_exc(),failure_phase=phase)
        try:
            import sqlite3
            with sqlite3.connect(out/'settings/workbench.sqlite3') as db:
                report['failure_orders']=db.execute('SELECT count(*) FROM normal_orders').fetchone()[0]
        except Exception:pass
    finally:
        held=None;report['final_memory']=helpers.counters();write(out/'result.json',report)
    return 0 if report['passed'] else 1


def run(a,source,directory,scenario):
    cmd=[sys.executable,__file__,'--child','--source',str(source),'--output',str(directory),'--scenario',scenario,
         '--rows',str(a.rows),'--files',str(a.files),'--extra',str(a.extra),'--tight-mib',str(a.tight_mib),'--occupied-mib',str(a.occupied_mib)]
    if a.no_export:cmd.append('--no-export')
    samples=[];start=time.monotonic();pid=None
    with directory.with_suffix('.log').open('wb') as log:
        p=subprocess.Popen(cmd,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        while p.poll() is None:
            try:
                if pid is None and (directory/'worker-pid.txt').exists():pid=int((directory/'worker-pid.txt').read_text())
                phase=(directory/'phase.txt').read_text() if (directory/'phase.txt').exists() else 'startup'
                reserved=int((directory/'reserved-bytes.txt').read_text()) if (directory/'reserved-bytes.txt').exists() else 0
                if pid:samples.append({'at':time.monotonic()-start,'phase':phase,'qa_reserved_bytes':reserved,**helpers.counters(pid)})
            except (ValueError,OSError):pass
            if time.monotonic()-start>3600:raise TimeoutError('Isolated benchmark exceeded an hour')
            time.sleep(.1)
    report=json.loads((directory/'result.json').read_text('utf-8')) if (directory/'result.json').exists() else {'passed':False,'error':'worker produced no result'}
    report['exit_code']=p.returncode
    report['phase_peaks']={phase:{key:max(s.get(key,0) for s in samples if s['phase']==phase) for key in ('private_bytes','working_bytes')} for phase in {s['phase'] for s in samples}}
    write(directory/'samples.json',samples);write(directory/'sampled-result.json',report)
    print(json.dumps({'run':directory.name,'passed':report['passed'],'confirm_seconds':report.get('confirm_seconds'),'export_seconds':report.get('export_seconds'),'error':report.get('exception')},ensure_ascii=False),flush=True)
    return report


def main():
    p=argparse.ArgumentParser();p.add_argument('--child',action='store_true');p.add_argument('--source',type=Path,default=Path.cwd());p.add_argument('--baseline',type=Path)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--rows',type=int,default=20000);p.add_argument('--files',type=int,default=17);p.add_argument('--extra',type=int,default=1000)
    p.add_argument('--scenario',choices=['ample','tight','occupied'],default='ample');p.add_argument('--scenarios',nargs='+',default=['ample','tight','occupied']);p.add_argument('--runs',type=int,default=2)
    p.add_argument('--tight-mib',type=int,default=3427);p.add_argument('--occupied-mib',type=int,default=3600);p.add_argument('--no-export',action='store_true');a=p.parse_args()
    a.source=a.source.resolve();a.output=a.output.resolve()
    if a.child:return child(a)
    a.output.mkdir(exist_ok=False);report={};reference=None
    for scenario in a.scenarios:
        report[scenario]={'old':[],'new':[]}
        for index in range(a.runs):
            for kind in (('old','new') if index%2==0 else ('new','old')):
                result=run(a,a.baseline if kind=='old' else a.source,a.output/f'{scenario}-{kind}-{index+1}',scenario)
                report[scenario][kind].append(result)
                if kind=='new' and not result['passed']:
                    write(a.output/'comparison.json',report)
                    return 1
                if result['passed']:
                    if reference is None:reference=result
                    assert result['orders']==reference['orders'] and result['work_digest']==reference['work_digest']
                    if not a.no_export:assert result['exports']==reference['exports']
                write(a.output/'comparison.json',report)
        report[scenario]['medians']={kind:{key:statistics.median(r[key] for r in records if r['passed']) for key in ('confirm_seconds','export_seconds') if any(key in r and r['passed'] for r in records)} for kind,records in report[scenario].items() if kind in ('old','new')}
    report['passed']=all(r['passed'] for v in report.values() if isinstance(v,dict) for r in v.get('new',[]))
    write(a.output/'comparison.json',report)
    return 0 if report['passed'] else 1


if __name__=='__main__':sys.exit(main())
