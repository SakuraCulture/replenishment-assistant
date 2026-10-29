"""Opt-in packaged checks for ordering, failure, cancel, close, retry; private settings."""
import copy,functools,json,multiprocessing,pickle,sys,time,traceback,zlib
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox
import file_intake,intake_probe
from version_info import diagnostic

def start_smoke(app,win,args):
    qa,out=map(Path,args[:2]);inp=qa/'inputs';out.mkdir(parents=True,exist_ok=True);app.setQuitOnLastWindowClosed(False)
    state={'step':0,'start':time.monotonic(),'checks':[],'mode':None,'error':None};timer=QTimer(app);original=file_intake.checked_files
    def expected(name):return pickle.loads(zlib.decompress((qa/'oracle'/(name+'.pklz')).read_bytes()))
    def equal(loaded,name):
        a=copy.deepcopy(loaded);b=expected(name);a.pop('loaded_at',None);b.pop('loaded_at',None);assert a==b,name
        # Dict equality alone would miss insertion order; explicitly check ordered business containers.
        for key in ['stores','hashes']:assert list(a[key])==list(b[key]),key
        assert list(a['intake']['stores'])==list(b['intake']['stores'])
    def snap(name):assert win.grab().save(str(out/(name+'.png')))
    def finish(error=None):
        timer.stop();file_intake.checked_files=original
        assert not multiprocessing.active_children()
        meta=diagnostic();r={'passed':error is None,'error':error,'version':meta['version'],'source_commit':meta['source_commit'],'checks':state['checks']}
        (out/'intake_ui_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(1 if error else 0)
    def attach():
        base=win.failed
        def failed(message):
            state['error']=message
            QTimer.singleShot(120,lambda:QApplication.activeModalWidget().accept() if QApplication.activeModalWidget() else None)
            base(message)
        win.failed=failed
    attach()
    def mode(name):
        state['mode']=name
        if name:
            trace=out/name;trace.mkdir(exist_ok=True);task=functools.partial(intake_probe.probe_file,folder=str(trace),mode=name)
            file_intake.checked_files=lambda paths,progress,cancel:original(paths,progress,cancel,task=task)
        else:file_intake.checked_files=original
    def load(files):
        win.master_legacy.setChecked(True);win.sales.paths=[];win.sales.refresh();win.sales.add_paths([str(inp/n) for n in files]);win.products.set_path(str(inp/'edge_master.xlsx'));QTest.mouseClick(win.load_btn,Qt.MouseButton.LeftButton)
    def tick():
        nonlocal win
        try:
            assert time.monotonic()-state['start']<180,'timeout'
            step=state['step']
            if step==0:
                mode('reverse');load(['edge.xlsx','conflict.xlsx','order.xlsx']);state['step']=1
            elif step==1 and win.worker is None:
                assert not state['error'],state['error'];equal(win.loaded,'overlap_blocked');snap('01_原序检查与冲突')
                a=json.loads((out/'reverse/0_finish.json').read_text());b=json.loads((out/'reverse/1_finish.json').read_text());assert b['time']<a['time'];assert not a['qt_instance'] and not b['qt_instance']
                state['checks'].append('later file completes first in running application; reports, stores, headers, conflict candidates, records and source rows equal fixed serial baseline; children have no QApplication')
                state['old']=win.loaded;mode('cancel');load(['edge.xlsx','conflict.xlsx','order.xlsx']);w=win.worker;win.start_load();assert win.worker is w;QTimer.singleShot(250,win.cancel_work);state['step']=2
            elif step==2 and win.worker is None:
                assert win.loaded is state['old'] and win.input_dirty;assert not multiprocessing.active_children();assert not (out/'cancel/2_start.json').exists();snap('02_取消后结果失效')
                state['checks'].append('cancel stops new submissions, waits/reaps readers, discards completed payloads, blocks overlapping recheck and stale updates')
                mode(None);load(['broken.xlsx','edge.xlsx','duplicate_header.xlsx','empty.xlsx','numeric_store.xlsx']);state['step']=3
            elif step==3 and win.worker is None:
                assert not state['error'];equal(win.loaded,'mixed_error_loaded');snap('03_有效与失败文件')
                state['checks'].append('retry after cancel succeeds; mixed invalid/valid file reports and normalized rows equal serial baseline')
                load(['broken.xlsx','empty.xlsx','duplicate_header.xlsx','numeric_store.xlsx']);state['step']=4
            elif step==4 and win.worker is None:
                assert state['error']==expected('all_error')['error'][1];assert win.input_dirty
                state['checks'].append('all-failed check shows unchanged error and keeps prior results invalid');state['error']=None
                mode('close');load(['edge.xlsx','conflict.xlsx','order.xlsx']);QTimer.singleShot(250,win.close);state['step']=5
            elif step==5:
                box=QApplication.activeModalWidget()
                if isinstance(box,QMessageBox):
                    assert box.defaultButton().text()=='取消';QTest.mouseClick(next(b for b in box.buttons() if b.text()=='放弃本次变更'),Qt.MouseButton.LeftButton);return
                if win.worker is None and not win.isVisible():
                    assert not multiprocessing.active_children();assert not (out/'close/2_start.json').exists()
                    state['checks'].append('close during reading cancels/reaps children before closing and retains original unsaved-work confirmation')
                    from workbench import Workbench
                    win=Workbench();win.show();attach();mode(None);load(['edge.xlsx']);state['step']=6
            elif step==6 and win.worker is None:
                assert not state['error'];equal(win.loaded,'single_loaded');assert not multiprocessing.active_children();snap('04_重新打开单文件检查')
                state['checks'].append('fresh window after close can check again; single-file path has no child process');finish()
        except Exception:
            error=traceback.format_exc()
            if win.worker:
                win.cancel_work();timer.stop()
                def drained():
                    if win.worker:QTimer.singleShot(100,drained)
                    else:finish(error)
                QTimer.singleShot(100,drained)
            else:finish(error)
    timer.timeout.connect(tick);timer.start(80)
