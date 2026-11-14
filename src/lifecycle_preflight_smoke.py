"""Opt-in actual dialog acceptance, only on a marked isolated synthetic database."""
import hashlib, json, os, sqlite3, sys, threading, time, traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QEventLoop
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QTableView
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        names=[n for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n in names}


def start_smoke(app,win,args):
    from lifecycle_home_ui import LifecycleWidget
    from aoxiang_workflow_ui import QueueDialog
    from aoxiang_workflow_runner import Runner
    from version_info import diagnostic
    out=Path(args[0]).resolve(); mode=args[1]
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] and not marker['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    # Isolated font caches need an explicit CJK font for readable screenshots.
    from PySide6.QtGui import QFont, QFontDatabase
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font)); app.setFont(QFont('Microsoft YaHei',9))
    fixture=json.loads((out/'fixture.json').read_text('utf-8')); path=win.work_repository.path
    assert path.resolve()==out/'settings/workbench.sqlite3'
    manager=win.aoxiang_workflows; manager.timer.stop(); manager.stopping=True
    win.jst_generator.stopped=True; win.jst_generator.timer.stop()
    checks=[]; screenshots=[]; dialogs=[]; seen=[]; state={'error':None,'expected':None}; started=time.monotonic()
    def capture(widget,name):
        name=mode+'-'+name+'.png'; assert widget.grab().save(str(out/name)); screenshots.append(name)
    def automate():
        modal=QApplication.activeModalWidget()
        if modal is None or not state['expected']: return
        expected=state['expected']
        if expected=='excluded' and modal.windowTitle()=='未显示订单与原因':
            views=modal.findChildren(QTableView); assert views and views[0].model().rowCount()==1
            text=' '.join(str(views[0].model().index(0,c).data()) for c in range(views[0].model().columnCount()))
            seen.append(text); capture(modal,'excluded-reason'); state['expected']=None; modal.accept()
        elif expected=='reason' and isinstance(modal,QMessageBox):
            seen.append(modal.text()+'\n'+modal.informativeText()); capture(modal,'payment-reason-'+str(len(seen)))
            state['expected']=None; QTest.mouseClick(modal.button(QMessageBox.StandardButton.Ok),Qt.MouseButton.LeftButton)
    timer=QTimer(win); timer.setInterval(25); timer.timeout.connect(automate); timer.start()
    def wait(predicate,timeout=25):
        loop=QEventLoop(); watch=QTimer(); watch.setInterval(15); start=time.monotonic()
        watch.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-start>timeout else None)
        watch.start(); loop.exec(); watch.stop(); assert predicate(),'Native UI wait timed out'
    def finish(error=None):
        report={'passed':error is None,'error':error,'mode':mode,'checks':checks,'screenshots':screenshots,'dialog_text':seen,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'live_platform':False,
                'synthetic_input':True,'dialogs_not_stubbed':True,'elapsed_seconds':time.monotonic()-started}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8',newline='\n')
        timer.stop()
        for d in reversed(dialogs): d.close()
        win._large_io_dialog=None; win._discard_close=True; win.close(); QTimer.singleShot(100,lambda:app.exit(0 if error is None else 1))
    def execute():
        try:
            before=facts(path)
            page=LifecycleWidget(win,'operations'); dialogs.append(page)
            page.resize(1250,650); page.show(); page.status.setCurrentText('本环节全部'); page.query()
            wait(lambda:not page.bar.isVisible() and page.table.isEnabled() and page.count==12)
            assert len(page.records)==12 and len(page.excluded_records)==1
            assert '13' in page.note.text() and '12' in page.note.text() and page.excluded_btn.isEnabled()
            capture(page,'counts'); checks.append('Actual lifecycle page distinguishes 13 total / 12 filtered / 1 excluded')
            state['expected']='excluded'; QTest.mouseClick(page.excluded_btn,Qt.MouseButton.LeftButton)
            assert state['expected'] is None and any('S12' in s for s in seen)
            checks.append('Actual searchable excluded-order dialog names the order and its stage reason')
            page.close()
            if mode=='exercise':
                job=manager.store.prepare(fixture['heads'][:8])
                class NoPlatformClient:
                    def __init__(self): self.calls=[]
                    def request(self,action,**kw):
                        self.calls.append(action); raise AssertionError('Unexpected component request: '+action)
                client=NoPlatformClient()
                Runner(manager.store,client,out/'no-component',path,cancel=threading.Event(),delay=0).run(job['id'])
                job=manager.store.get(job['id'])
                assert job['state']=='COMPLETED_WITH_ERRORS' and all(i['state']=='REVIEW' for i in job['items'])
                assert not client.calls
                assert len({i['error']['code'] for i in job['items']})>=3
                (out/'job-id.json').write_text(json.dumps({'id':job['id']}),'utf-8',newline='\n')
                checks.append('Actual Runner rejects 8 local-invalid payments with 0 identity/template/purchase requests')
            else:
                job=manager.store.get(json.loads((out/'job-id.json').read_text('utf-8'))['id'])
                assert all(i['state']=='REVIEW' and not i.get('package') for i in job['items'])
                checks.append('Actual EXE reopen retains review states and original detailed causes')
            queue=QueueDialog(win,manager,job['id']); dialogs.append(queue); queue.show(); queue.refresh()
            assert queue.current and len(queue.current['items'])==8
            for n,expected in ((0,'付款'),(1,'核对'),(2,'V0 → V1')):
                queue.table.setCurrentCell(n,0); queue.detail_controls(); assert queue.reason_btn.isEnabled()
                state['expected']='reason'; QTest.mouseClick(queue.reason_btn,Qt.MouseButton.LeftButton)
                assert state['expected'] is None and expected in seen[-1],seen[-1]
            checks.append('Actual reason dialogs distinguish missing, pending and changed-basis payments')
            capture(queue,'queue'); assert facts(path)==before
            checks.append('Queries, preflight and review dialogs leave signed and lifecycle business tables unchanged')
            finish()
        except Exception: finish(traceback.format_exc())
    QTimer.singleShot(180,execute)
