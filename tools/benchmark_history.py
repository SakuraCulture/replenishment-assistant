"""Isolated UI benchmark: AB/BA/AB, source methods, same real database and dependencies."""
import os,sys,json,time,gc,shutil,threading,statistics,subprocess,argparse,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/archive_072'

def run(kind,iteration):
    folder=QA/'bench-loop'/f'{kind}-{iteration}';folder.mkdir(parents=True,exist_ok=False);shutil.copy2(QA/'snapshot.sqlite3',folder/'workbench.sqlite3')
    os.environ['REPLENISHMENT_DATA_DIR']=str(folder);os.environ['QT_QPA_PLATFORM']='offscreen';sys.path[:0]=[str(ROOT/'src' if kind=='new' else QA/'baseline/src'),str(ROOT/'qa/export_071/qa-deps')]
    import psutil
    from PySide6.QtWidgets import QApplication,QDialog,QLineEdit,QTableWidget
    from PySide6.QtCore import QTimer,QCoreApplication,QEvent
    import shiboken6
    from application_window import ApplicationWindow
    app=QApplication([]);app.setStyle('Fusion');measures={};peaks=[];stop=threading.Event();proc=psutil.Process()
    def sample():
        while not stop.wait(.05):
            try:peaks.append(sum(p.memory_info().rss for p in [proc]+proc.children(recursive=True) if p.is_running()))
            except psutil.Error:pass
    thread=threading.Thread(target=sample,daemon=True);thread.start()
    def wait(pred):
        start=time.perf_counter()
        while not pred():
            app.processEvents();time.sleep(.005)
            if time.perf_counter()-start>180:raise TimeoutError('UI wait')
    def measure(name,fn):
        gc.collect();print('START '+name,flush=True);start=time.perf_counter();value=fn();measures[name]=time.perf_counter()-start;print('END '+name+' '+str(measures[name]),flush=True);return value
    def exercise():
        started=time.perf_counter();w=measure('constructor',ApplicationWindow);w.show()
        if kind=='new':wait(lambda:len(w.archive_all)==5 and w.history_reader.active is None)
        measures['cold_full_list']=time.perf_counter()-started
        def refresh():
            w.refresh_archive()
            if kind=='new':wait(lambda:w.history_reader.active is None)
        measure('warm_list',refresh)
        def search(q):
            w.archive_search.setText(q)
            if kind=='new':w.archive_timer.stop();w.query_archive();wait(lambda:w.history_reader.active is None)
        measure('archive_search',lambda:search('QA-no-such-SKU'));search('')
        record=next(r for r in w.archive_orders if r['store']=='ZYD20251112')
        original_exec=QDialog.exec
        def auto(dialog):
            if not dialog.windowTitle().endswith('历史签发快照'):return original_exec(dialog)
            if kind=='new':wait(lambda:dialog.selected is not None)
            measures['detail_ready']=time.perf_counter()-open_start
            field=dialog.findChild(QLineEdit)
            def filt():
                field.setText('QA-no-such-SKU')
                if kind=='new':dialog.filter()
            measure('detail_search',filt);QTimer.singleShot(10,dialog.reject);return original_exec(dialog)
        QDialog.exec=auto;open_start=time.perf_counter();w.open_archive(record);QDialog.exec=original_exec
        if kind=='new':wait(lambda:w.history_reader.active is None)
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
        order=w.orders.get(record['day'],record['store']);r=order['payload']['result'];w.result=r;w.current_store=r['store'];w.loaded={'stores':{r['store']:r['store_name']}}
        def summary_wait():
            if kind=='new':wait(lambda:w.summary_task is None and w.summary_pending is None)
        def setrows():w.set_rows(r['rows'],True);summary_wait()
        measure('work_set_rows',setrows)
        def allrows():w.mode.setCurrentText('全部商品');summary_wait()
        measure('work_all',allrows)
        def worksearch(q):
            w.search.setText(q)
            if kind=='new':w.filter_changed()
            summary_wait()
        measure('work_no_match',lambda:worksearch('QA-no-such-SKU'));measure('work_clear',lambda:worksearch(''))
        visible=[d['E'] for d in w.visible_rows()];visible_hash=hashlib.sha256(json.dumps(visible).encode()).hexdigest()
        rss_close=proc.memory_info().rss
        w.job_dirty=False;w._discard_close=True;w.close();app.processEvents();stop.set();thread.join();report={'kind':kind,'iteration':iteration,'seconds':measures,'sampled_peak_tree_mib':max(peaks)/1024**2,'rss_end_mib':rss_close/1024**2,'sample_ms':50,'visible_order_sha256':visible_hash}
        (folder/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True);w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.quit()
    def safe_exercise():
        try:exercise()
        except Exception:
            import traceback
            traceback.print_exc();stop.set();app.exit(1)
    QTimer.singleShot(0,safe_exercise);code=app.exec();stop.set();thread.join();sys.exit(code)

def main():
    p=argparse.ArgumentParser();p.add_argument('--kind');p.add_argument('--iteration',type=int);p.add_argument('--collect',action='store_true');a=p.parse_args()
    if a.kind:return run(a.kind,a.iteration)
    if not a.collect:
        for i,kinds in enumerate([['old','new'],['new','old'],['old','new']],start=1):
            for kind in kinds:subprocess.run([sys.executable,__file__,'--kind',kind,'--iteration',str(i)],check=True)
    records=[json.loads(p.read_text()) for p in (QA/'bench-loop').glob('*/report.json') if p.parent.name not in ('new-0','new-1')];summary={}
    for metric in records[0]['seconds']:
        summary[metric]={k:{'median':statistics.median(v),'min':min(v),'max':max(v)} for k in ['old','new'] for v in [[r['seconds'][metric] for r in records if r['kind']==k]]}
    summary['peak_tree_mib']={k:{'median':statistics.median(v),'min':min(v),'max':max(v)} for k in ['old','new'] for v in [[r['sampled_peak_tree_mib'] for r in records if r['kind']==k]]}
    (QA/'bench-loop/report.json').write_text(json.dumps({'rounds':3,'order':'AB/BA/AB sequential plus candidate rerun new-4 replacing new-1 after metadata query-order correction; inside QApplication event loop; earlier harness evidence retained separately','metrics':summary,'runs':records,'limits':'Actual Qt methods offscreen, source build; warm OS file cache not forcibly flushed; cold derived index per run, includes spawn/IPC; peak sum RSS process tree sampled 50ms includes shared pages counted in each process. No full daily restore.'},indent=2))
if __name__=='__main__':
    import multiprocessing;multiprocessing.freeze_support();main()
