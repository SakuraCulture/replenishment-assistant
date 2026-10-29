"""Alternating, isolated first-fill UI benchmark. Does not touch daily user data."""
import os,sys,json,time,threading,subprocess,statistics,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'

def run(kind,iteration):
    os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,str((QA/'baseline' if kind=='old' else ROOT)/'src'))
    sys.path.append(str(ROOT/'qa/export_071/qa-deps'))
    import psutil
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer,QCoreApplication,QEvent
    import new_product_ui as ui,history_read,new_product_focus as focus,result_semantics as sem
    app=QApplication([]);app.setStyle('Fusion');proc=psutil.Process();peaks=[];stop=threading.Event();values=[];gaps=[];last=[time.perf_counter()]
    def sample():
        while not stop.wait(.05):
            try:peaks.append(sum(p.memory_info().rss for p in [proc]+proc.children(recursive=True) if p.is_running()))
            except psutil.Error:pass
    monitor=threading.Thread(target=sample,daemon=True);monitor.start()
    timer=QTimer();timer.setInterval(25)
    def beat():now=time.perf_counter();gaps.append(now-last[0]);last[0]=now
    timer.timeout.connect(beat);timer.start()
    def measure(fn):
        app.processEvents();last[0]=time.perf_counter();start=last[0];v=fn();elapsed=time.perf_counter()-start;app.processEvents();return v,elapsed
    def exercise():
        try:
            repo=history_read.repository(ROOT/'qa/archive_072/snapshot.sqlite3')
            for h in repo.headers():
                result=repo.get(h['day'],h['store'])['payload']['result']
                # Same eligible rows in both versions: isolates virtualization benefit from exclusion delta.
                result={**result,'rows':[r for r in result['rows'] if not sem.exclusion_reason(r)]}
                def create():d=ui.NewProductDialog(None,result,result,focus.empty('2026-10'));d.show();d.repaint();return d
                d,create_seconds=measure(create)
                def filt(q):d.search.setText(q);d.refresh();d.repaint()
                _,search_seconds=measure(lambda:filt('奶'));_,clear_seconds=measure(lambda:filt(''))
                _,all_seconds=measure(lambda:d.scope.setCurrentText('全部新品'))
                values.append({'store':h['store'],'rows':len(d.rows),'create_seconds':create_seconds,'search_seconds':search_seconds,'clear_seconds':clear_seconds,'all_seconds':all_seconds})
                d.close();d.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            stop.set();monitor.join();report={'kind':kind,'iteration':iteration,'stores':values,'peak_tree_mib':max(peaks)/2**20,'sample_ms':50,'timer_ms':25,'max_ui_gap_seconds':max(gaps),'scope':'same eligible actual archived rows, constructor/show/paint plus filters; excludes archive read; OS cache not purged'}
            folder=QA/'new_product_bench';folder.mkdir(exist_ok=True);(folder/f'{kind}-{iteration}.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report),flush=True);app.quit()
        except Exception:
            import traceback;traceback.print_exc();stop.set();app.exit(1)
    QTimer.singleShot(0,exercise);code=app.exec();stop.set();monitor.join();sys.exit(code)

def main():
    p=argparse.ArgumentParser();p.add_argument('--kind');p.add_argument('--iteration',type=int);a=p.parse_args()
    if a.kind:return run(a.kind,a.iteration)
    for i,kinds in enumerate([['old','new'],['new','old'],['old','new']],1):
        for kind in kinds:subprocess.run([sys.executable,__file__,'--kind',kind,'--iteration',str(i)],check=True)
    reports=[json.loads(p.read_text()) for p in (QA/'new_product_bench').glob('*-*.json')]
    table={}
    for kind in ['old','new']:
        runs=[r for r in reports if r['kind']==kind];table[kind]={}
        for store in runs[0]['stores']:
            table[kind][store['store']]={}
            for key in ['create_seconds','search_seconds','clear_seconds','all_seconds']:
                xs=[next(s[key] for s in r['stores'] if s['store']==store['store']) for r in runs];table[kind][store['store']][key]={'median':statistics.median(xs),'min':min(xs),'max':max(xs)}
        xs=[r['peak_tree_mib'] for r in runs];table[kind]['peak_tree_mib']={'median':statistics.median(xs),'min':min(xs),'max':max(xs)}
    (QA/'new_product_bench/report.json').write_text(json.dumps(table,indent=2),encoding='utf-8');print(json.dumps(table))
if __name__=='__main__':main()
