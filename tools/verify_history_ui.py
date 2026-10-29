"""Source Qt spawn/cancellation test; only a private copied database."""
import os,sys,json,shutil,time,multiprocessing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    folder=ROOT/'qa/archive_072/stage_b_ui';folder.mkdir(exist_ok=False);shutil.copy2(ROOT/'qa/archive_072/snapshot.sqlite3',folder/'workbench.sqlite3')
    os.environ['REPLENISHMENT_DATA_DIR']=str(folder);os.environ['QT_QPA_PLATFORM']='offscreen';sys.path.insert(0,str(ROOT/'src'))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from application_window import ApplicationWindow
    from history_ui import ArchiveDialog
    app=QApplication([]);started=time.perf_counter();w=ApplicationWindow();elapsed=time.perf_counter()-started;w.show();state={'step':0,'ticks':0,'start':time.monotonic(),'constructor':elapsed}
    def fail(exc):
        (folder/'failure.txt').write_text(str(exc));w.job_dirty=False;w._discard_close=True;w.close();app.exit(1)
    def tick():
        try:
            state['ticks']+=1
            if time.monotonic()-state['start']>180:raise TimeoutError(state)
            if state['step']==0 and len(w.archive_all)==5:
                assert elapsed<2;state['step']=1;w.archive_search.setText('QA-NO-MATCH');w.filter_archive();w.archive_search.setText('');w.filter_archive()
            elif state['step']==1 and not w.archive_timer.isActive() and w.history_reader.active is None:
                assert len(w.archive_orders)==5
                d=ArchiveDialog(w,w.archive_orders[-1]);state['d']=d;d.show();state['step']=2
            elif state['step']==2 and state['d'].selected:
                d=state['d'];assert d.model.rowCount()>10000;d.search.setText('QA-NO-MATCH');d.filter();assert d.model.rowCount()==0;d.reject();d.deleteLater();state.pop('d');state['step']=3
                # Close a cold request immediately; its callback must never land.
                d=ArchiveDialog(w,w.archive_orders[0]);d.show();d.reject();d.deleteLater();w.refresh_archive()
            elif state['step']==3 and w.history_reader.active is None:
                assert len(w.archive_orders)==5;pid=w.history_reader.proc.pid;w.job_dirty=False;w._discard_close=True;w.close();assert w.history_reader.proc is None
                report={'passed':True,'constructor_seconds':elapsed,'event_loop_ticks':state['ticks'],'checks':['cold index while event loop alive','latest query wins','async detail >10000 rows','search no reload','close pending dialog then refresh','owned spawn worker reaped'],'worker_pid':pid}
                (folder/'report.json').write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True);timer.stop();app.quit()
        except Exception as exc:
            import traceback
            fail(traceback.format_exc());timer.stop()
    timer=QTimer();timer.timeout.connect(tick);timer.start(20);return app.exec()
if __name__=='__main__':multiprocessing.freeze_support();sys.exit(main())
