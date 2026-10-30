"""Single-process alternating UI timing, including paint; no competing QA subprocesses."""
import sys,os,json,time,threading,subprocess,statistics,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/full_workflow_073'

def run(kind,iteration,series):
    os.environ['QT_QPA_PLATFORM']='offscreen';source=QA/'baseline' if kind=='old' else ROOT;sys.path[:0]=[str(source/'src'),str(ROOT/'qa/export_071/qa-deps')]
    import psutil
    from PySide6.QtWidgets import QApplication,QWidget,QStackedWidget,QVBoxLayout
    from PySide6.QtCore import QTimer,QCoreApplication,QEvent,Qt
    import ui_results,history_read,new_product_ui,new_product_focus as focus,result_semantics as sem
    app=QApplication([]);app.setStyle('Fusion');stop=threading.Event();peaks=[];proc=psutil.Process();active=[None];last=[time.perf_counter()];gaps={};report={'kind':kind,'iteration':iteration,'stores':[]}
    def sample():
        while not stop.wait(.05):
            try:peaks.append(sum(p.memory_info().rss for p in [proc]+proc.children(recursive=True) if p.is_running()))
            except psutil.Error:pass
    monitor=threading.Thread(target=sample,daemon=True);monitor.start();timer=QTimer();timer.setInterval(25)
    def beat():
        now=time.perf_counter()
        if active[0]:gaps.setdefault(active[0],[]).append(now-last[0])
        last[0]=now
    timer.timeout.connect(beat);timer.start()
    class Host(QWidget,ui_results.ResultsView):
        def __init__(self,r):
            super().__init__();self.result=r;self.loaded={'stores':{r['store']:r['store_name']}};self.pages=QStackedWidget();layout=QVBoxLayout(self);layout.addWidget(self.pages);self.make_results();self.resize(1300,800);self.show()
        def selected_store(self):return self.result['store']
    def settle(w):
        while w.summary_task is not None or w.summary_pending is not None:app.processEvents();time.sleep(.002)
    def measure(key,fn,w=None):
        app.processEvents();active[0]=key;last[0]=time.perf_counter();start=last[0];value=fn();block=time.perf_counter()-start;app.processEvents()
        if w:settle(w)
        elapsed=time.perf_counter()-start;active[0]=None
        return value,{'ready_seconds':elapsed,'call_seconds':block,'max_ui_gap_seconds':max(gaps.get(key,[0]))}
    def exercise():
        try:
            repo=history_read.repository(ROOT/'qa/archive_072/snapshot.sqlite3')
            for h in repo.headers():
                r=repo.get(h['day'],h['store'])['payload']['result'];r={**r,'rows':[d for d in r['rows'] if not sem.exclusion_reason(d)]};r['summary']=sem.summarize(r['rows']);w=Host(r);values={}
                _,values['open_goods']=measure(h['store']+'/open_goods',lambda:w.set_rows(r['rows'],True),w)
                _,values['all_goods']=measure(h['store']+'/all_goods',lambda:w.mode.setCurrentText('全部商品'),w)
                def query(q):w.search.setText(q);w.filter_changed()
                _,values['search']=measure(h['store']+'/search',lambda:query('奶'),w)
                _,values['clear_search']=measure(h['store']+'/clear_search',lambda:query(''),w)
                _,values['sort_month']=measure(h['store']+'/sort_month',lambda:w.table.sortByColumn(5,Qt.SortOrder.DescendingOrder),w)
                _,values['goods_detail']=measure(h['store']+'/goods_detail',lambda:w.show_detail(w.proxy.index(0,0)),w)
                def new_dialog():d=new_product_ui.NewProductDialog(w,r,r,focus.empty('2026-10'));d.show();d.repaint();return d
                d,values['open_new']=measure(h['store']+'/open_new',new_dialog)
                def search_new(q):d.search.setText(q);d.refresh();d.repaint()
                _,values['new_search']=measure(h['store']+'/new_search',lambda:search_new('奶'))
                _,values['new_clear']=measure(h['store']+'/new_clear',lambda:search_new(''))
                report['stores'].append({'store':h['store'],'eligible_rows':len(r['rows']),'new_candidates':len(d.rows),'actions':values})
                d.close();d.deleteLater();w.stop_filtered_summary();w.close();w.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
            report.update(peak_tree_mib=max(peaks)/2**20,sample_ms=50,timer_ms=25,scope='same eligible rows from original five-store snapshot; includes UI creation/paint/filter/sort and awaited summary; fixture DB read is excluded from action and heartbeat timing; peak RSS includes fixtures',limitations='Offscreen Qt, not EXE; OS filesystem cache not purged; no CPU/RAM saturation claim')
            folder=QA/series;folder.mkdir(exist_ok=True);path=folder/f'{kind}-{iteration}.json';assert not path.exists();path.write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report),flush=True);app.quit()
        except Exception:
            import traceback;traceback.print_exc();app.exit(1)
    QTimer.singleShot(0,exercise);code=app.exec();stop.set();monitor.join();sys.exit(code)

def main():
    p=argparse.ArgumentParser();p.add_argument('--kind');p.add_argument('--iteration',type=int);p.add_argument('--series',default='ui_bench_final');a=p.parse_args()
    if a.kind:return run(a.kind,a.iteration,a.series)
    folder=QA/a.series;assert not folder.exists()
    for i,kinds in enumerate([['old','new'],['new','old'],['old','new']],1):
        for k in kinds:subprocess.run([sys.executable,__file__,'--kind',k,'--iteration',str(i),'--series',a.series],check=True)
    runs=[json.loads(p.read_text()) for p in folder.glob('*-*.json')];table={}
    for kind in ['old','new']:
        rs=[r for r in runs if r['kind']==kind];table[kind]={}
        for store in rs[0]['stores']:
            table[kind][store['store']]={}
            for action in store['actions']:
                table[kind][store['store']][action]={}
                for metric in store['actions'][action]:
                    xs=[next(s for s in r['stores'] if s['store']==store['store'])['actions'][action][metric] for r in rs];table[kind][store['store']][action][metric]={'median':statistics.median(xs),'min':min(xs),'max':max(xs)}
        xs=[r['peak_tree_mib'] for r in rs];table[kind]['peak_tree_mib']={'median':statistics.median(xs),'min':min(xs),'max':max(xs)}
    (folder/'report.json').write_text(json.dumps(table,indent=2),encoding='utf-8')
if __name__=='__main__':main()
