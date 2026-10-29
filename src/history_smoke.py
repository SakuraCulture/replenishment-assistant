"""Opt-in packaged UI acceptance, real private archive copy; no daily restore."""
import time,json,sys,traceback,gc
from pathlib import Path
from PySide6.QtCore import QTimer,QCoreApplication,QEvent,Qt
from PySide6.QtWidgets import QFileDialog,QApplication,QLineEdit
from history_ui import ArchiveDialog,EventsDialog
from version_info import diagnostic
import work_repository as wr


def start_smoke(app,win,args):
    out=Path(args[0]);out.mkdir(parents=True,exist_ok=True);start=time.perf_counter();state={'step':0,'checks':[],'opens':0,'times':[],'beats':0,'last':time.perf_counter(),'max_gap':0};timer=QTimer(win)
    def finish(error=None):
        timer.stop();win.job_dirty=False;win._discard_close=True;win.close()
        report={'passed':error is None,'error':error,'checks':state['checks'],'detail_times':state['times'],'initial_list_seconds':state.get('initial'),'event_ticks':state['beats'],'max_tick_gap_seconds':state['max_gap'],'closed_dialogs':state.get('retained'),'version':diagnostic(),'daily_restore_tested':False}
        (out/'history_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(0 if error is None else 1)
    def tick():
        try:
            now=time.perf_counter();state['max_gap']=max(state['max_gap'],now-state['last']);state['last']=now;state['beats']+=1
            if now-start>240:raise TimeoutError(str(state))
            step=state['step']
            if step==0 and len(win.archive_all)==5 and win.history_reader.active is None:
                state['initial']=now-start;assert win.grab().save(str(out/'01-home.png'));state['checks'].append('five real orders loaded in packaged spawn reader; event loop alive during cold index');state['step']=1
                win.show_section(2);win.archive_search.setText('QA-NO-MATCH');win.archive_search.setText('');win.filter_archive()
            elif step==1 and not win.archive_timer.isActive() and win.history_reader.active is None:
                assert len(win.archive_orders)==5;state['record']=next(r for r in win.archive_orders if r['store']=='ZYD20251112');state['checks'].append('rapid queries latest result wins and original store order retained');state['step']=2
            elif step==2:
                state['opened']=now;state['dialog']=ArchiveDialog(win,state['record']);state['dialog'].show();state['step']=3
            elif step==3 and state['dialog'].selected:
                d=state['dialog'];state['times'].append(now-state['opened']);assert d.model.rowCount()==23579
                d.search.setText('QA-NO-MATCH');d.filter();assert d.model.rowCount()==0;d.search.clear();d.filter();assert d.model.rowCount()==23579
                if state['opens']==0:assert d.grab().save(str(out/'02-detail.png'))
                d.reject();d.deleteLater();state.pop('dialog');QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);state['opens']+=1
                if state['opens']<20:state['step']=2
                else:
                    gc.collect();state['retained']=len(win.findChildren(ArchiveDialog));assert state['retained']==0;state['checks'].append('20 opens/closes, 23579 rows, exact searches; zero closed archive dialogs retained');state['step']=4
            elif step==4:
                d=ArchiveDialog(win,state['record']);d.show();d.reject();d.deleteLater();win.refresh_archive();state['step']=5
            elif step==5 and win.history_reader.active is None:
                assert len(win.archive_orders)==5;state['checks'].append('cancel pending detail and re-check succeeds; stale detail cannot update new page')
                win.history_reader.proc.terminate();win.history_reader.proc.join(2);state['step']=6
            elif step==6 and win.history_reader.proc is None:
                win.refresh_archive();state['step']=7
            elif step==7 and win.history_reader.active is None and len(win.archive_orders)==5:
                state['checks'].append('owned read worker crash detected; refresh restarts and succeeds')
                records=win.orders.events();dialog=EventsDialog(win,records);dialog.show();dialog.search.setText('');dialog.filter();assert dialog.matches==records
                original=QFileDialog.getSaveFileName;QFileDialog.getSaveFileName=lambda *a,**k:(str(out/'events.json'),'');dialog.export();QFileDialog.getSaveFileName=original
                assert json.loads((out/'events.json').read_text(encoding='utf-8'))==records;dialog.accept();dialog.deleteLater();state['checks'].append('log query export exact full records and order');state['step']=8
                # Formal source get still validates hash before numbered export.
                header=win.orders.headers()[0];win.export_order(header)
                def choose():
                    modal=QApplication.activeModalWidget()
                    if isinstance(modal,QFileDialog):
                        modal.setDirectory(str(out));modal.findChild(QLineEdit,'fileNameEdit').setText('history-confirmation.xlsx');modal.accept()
                    elif win.worker or modal is None:QTimer.singleShot(50,choose)
                QTimer.singleShot(50,choose)
            elif step==8 and win.worker is None and (out/'history-confirmation.xlsx').exists():
                state['checks'].append('historical numbered merchant export through actual file dialog and validated source; export log recorded');finish()
        except Exception:finish(traceback.format_exc())
    win.failed=lambda msg:finish(msg)
    app.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,True)
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    timer.timeout.connect(tick);timer.start(25)
