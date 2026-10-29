"""Real full daily snapshot read/restore probe; samples RSS from an external supervisor."""
import os,sys,json,time,hashlib,argparse,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'

def child(kind,out):
    os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    sys.path.insert(0,str((QA/'baseline' if kind=='old' else ROOT)/'src'))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from workbench import Workbench
    import work_repository as wr,sqlite3
    app=QApplication([]);current=[None];last=[time.perf_counter()];gaps={};report={'kind':kind,'passed':False,'source':'qa/archive_072/snapshot.sqlite3'}
    timer=QTimer();timer.setInterval(25)
    def beat():
        now=time.perf_counter()
        if current[0]:gaps.setdefault(current[0],[]).append(now-last[0])
        last[0]=now
    timer.timeout.connect(beat);timer.start()
    def phase(name):
        app.processEvents();(out/'phase.txt').write_text(name,encoding='ascii');current[0]=name;last[0]=time.perf_counter();return last[0]
    def measure_end(name,start):
        app.processEvents();report[name]={'seconds':time.perf_counter()-start,'max_ui_gap_seconds':max(gaps.get(name,[0]))};current[0]=None
    def exercise():
        try:
            path=ROOT/'qa/archive_072/snapshot.sqlite3'
            class ReadOnly(wr.Repository):
                def connect(self):return sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
            repo=ReadOnly.__new__(ReadOnly);repo.path=path;head=repo.list_jobs()[0];w=Workbench();w.show()
            start=phase('read')
            if kind=='new':
                from ui_work_io import read_job
                record=read_job(w,path,head['day'])
            else:record=repo.get_job(head['day'])
            measure_end('read',start);phase('validate');current[0]=None
            p=record['payload'];digest=hashlib.sha256(wr.dumps(p).encode()).hexdigest();assert digest==record['digest'],(digest,record['digest'])
            start=phase('restore')
            if kind=='new':w.restore_job(record,owned=True)
            else:w.restore_job(record)
            measure_end('restore',start);phase('validate');current[0]=None
            assert set(w.states)==set(p['states']) and all(w.results[s]==p['results'][s] for s in w.results)
            report.update(passed=True,full_payload_digest=digest,stores=len(p['states']),rows=sum(len(r['rows']) for r in p['results'].values()),restore_states={s:v['state'] for s,v in w.states.items()},storage='original schema, SQLite, zlib and canonical JSON; no migration')
            # Small independent fixture for later input/calc pressure checks.
            if kind=='new':
                fixture={k:p.get(k) for k in ['catalogs','profile','store_presets','overrides','default_by_store','store_ui','new_focus']}
                (out/'frozen_settings.json').write_text(wr.dumps(fixture),encoding='utf-8')
            w.job_dirty=False;w._discard_close=True;w.close();app.quit()
        except Exception:
            import traceback;report['error']=traceback.format_exc();app.exit(1)
        (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    QTimer.singleShot(0,exercise);sys.exit(app.exec())

def main():
    p=argparse.ArgumentParser();p.add_argument('--kind',choices=['old','new'],required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--child',action='store_true');a=p.parse_args();out=a.output.resolve()
    if a.child:return child(a.kind,out)
    out.mkdir(parents=True,exist_ok=False);sys.path.append(str(ROOT/'qa/export_071/qa-deps'));import psutil
    with (out/'run.log').open('w',encoding='utf-8') as log:
        run=subprocess.Popen([sys.executable,__file__,'--child','--kind',a.kind,'--output',str(out)],stdout=log,stderr=log);proc=psutil.Process(run.pid);peaks={};children=set();start=time.monotonic()
        while run.poll() is None:
            if time.monotonic()-start>600:
                for c in proc.children(recursive=True):c.kill()
                proc.kill();raise TimeoutError('isolated full daily probe')
            try:
                phase=(out/'phase.txt').read_text() if (out/'phase.txt').exists() else 'startup';tree=proc.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree)
                memory=sum(p.memory_info().rss for p in [proc]+tree);peaks[phase]=max(peaks.get(phase,0),memory)
            except (psutil.Error,OSError):pass
            time.sleep(.05)
    result=json.loads((out/'result.json').read_text(encoding='utf-8'));result.update(exit_code=run.returncode,peak_tree_mib={k:v/2**20 for k,v in peaks.items()},memory_method='external supervisor sums parent and owned child RSS every 50ms; validation peak separate; shared pages counted per process',timer_ms=25)
    result['remaining_children']=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():result['remaining_children'].append(pid)
        except psutil.Error:pass
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False));sys.exit(0 if run.returncode==0 and result['passed'] and not result['remaining_children'] else 1)
if __name__=='__main__':main()
