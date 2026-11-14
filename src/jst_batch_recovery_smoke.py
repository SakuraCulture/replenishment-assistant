"""Opt-in native recovery/commit/export/reopen acceptance on marked synthetic data."""
import json,os,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QEventLoop
from PySide6.QtWidgets import QApplication,QMessageBox
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def start_smoke(app,win,args):
    import jst_batches as batches,jst_orders as orders,export_locations
    from jst_ui import GenerationDialog
    from version_info import diagnostic
    out=Path(args[0]).resolve();mode=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] and not marker['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    fixture=json.loads((out/'fixture.json').read_text('utf-8'));path=win.work_repository.path
    assert path.resolve()==out/'settings/workbench.sqlite3'
    win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    checks=[];messages=[];shots=[];dialogs=[];begun=time.monotonic();state={'action':None,'error':None}
    original_directory=export_locations.initial_directory
    export_locations.initial_directory=lambda *a,**kw:str(out/'exports')
    def capture(widget,name):
        name=mode+'-'+name+'.png';assert widget.grab().save(str(out/name));shots.append(name)
    def automate():
        modal=QApplication.activeModalWidget()
        if modal is None:return
        if modal.objectName()=='jstBatchRecovery' and state['action']:
            action=state['action'];state['action']=None;capture(modal,'recovery-'+action)
            target={'cancel':None,'restart':modal.restart,'resume':modal.resume}[action]
            if target is None:modal.reject()
            else:QTest.mouseClick(target,Qt.MouseButton.LeftButton)
        elif isinstance(modal,QMessageBox):
            messages.append({'title':modal.windowTitle(),'text':modal.text()})
            if modal.windowTitle()=='确认生成聚水潭订单':target=modal.button(QMessageBox.StandardButton.Yes)
            else:state['error']=modal.windowTitle()+': '+modal.text();target=modal.button(QMessageBox.StandardButton.Ok)
            if target:QTest.mouseClick(target,Qt.MouseButton.LeftButton)
            else:modal.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(automate);timer.start()
    def wait(predicate,timeout=35):
        loop=QEventLoop();watch=QTimer();watch.setInterval(15);start=time.monotonic()
        watch.timeout.connect(lambda:loop.quit() if predicate() or state['error'] or time.monotonic()-start>timeout else None)
        watch.start();loop.exec();watch.stop()
        assert not state['error'],state['error'];assert predicate(),'Native UI wait timeout'
    def generator(bid=None):
        heads=[fixture['head']]
        if bid:
            with closing(sqlite3.connect(path)) as db:members=batches.context(db,bid,load_stock=False)['members']
            heads=[m['head'] for m in members if m['status'] in ('pending','failed')] or [m['head'] for m in members]
        d=GenerationDialog(win,win,heads,bid);dialogs.append(d);d.show();wait(lambda:bool(d.catalogs))
        return d
    def context():
        with closing(sqlite3.connect(path)) as db:return batches.context(db,fixture['batch_id'])
    def commit_and_export(d,expected):
        QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton)
        wait(lambda:d.worker is None and d.receipt is not None)
        assert not d.receipt['errors'] and len(d.saved)==1,d.receipt
        gid=d.saved[0];assert orders.get(path,gid)['summary']['quantity']==str(expected)
        QTest.mouseClick(d.select_completed_btn,Qt.MouseButton.LeftButton)
        assert d.export_saved_btn.isEnabled();QTest.mouseClick(d.export_saved_btn,Qt.MouseButton.LeftButton)
        wait(lambda:d.export_worker is None and not d.export_queue)
        found=False
        import openpyxl
        for f in (out/'exports').glob('*.xlsx'):
            b=openpyxl.load_workbook(f,read_only=True,data_only=True)
            try:
                rows=list(b.active.values)
                if len(rows)==2 and rows[1][2]==expected:found=True
            finally:b.close()
        assert found,'Expected XLSX quantity not exported';return gid
    def finish(error=None):
        report={'passed':error is None,'error':error,'mode':mode,'checks':checks,'messages':messages,'screenshots':shots,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'live_platform':False,'synthetic_input':True,
                'dialogs_not_stubbed':True,'file_destination_adapter':True,'elapsed_seconds':time.monotonic()-begun}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
        timer.stop();export_locations.initial_directory=original_directory
        for d in reversed(dialogs):d.close()
        win._large_io_dialog=None;win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if error is None else 1))
    def execute():
        try:
            if mode=='reopen':
                with closing(sqlite3.connect(path)) as db:
                    gids=[r[0] for r in db.execute('SELECT gid FROM jst_generations ORDER BY rowid')]
                assert [orders.get(path,g)['summary']['quantity'] for g in gids]==['40','80']
                assert not batches.unfinished(path,[fixture['head'],fixture['other']])
                checks.append('Actual EXE reopen retains both generations with no pending-claim resurrection')
                before=facts(path);d=generator(fixture['batch_id'])
                assert context()['members'][0]['status']=='cancelled' and context()['members'][1]['status']=='done'
                assert facts(path)==before;capture(d,'reopened-original-batch');checks.append('Reopened original batch retains cancelled selected item and completed other item')
                finish();return
            before=facts(path);d=generator();d.refresh_stock(fixture['fresh_catalog'])
            state['action']='cancel';QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            assert facts(path)==before and d.plan is None and not d.generate_btn.isEnabled()
            checks.append('Real recovery cancel preserves every business table');d.close()
            d=generator();d.refresh_stock(fixture['fresh_catalog']);state['action']='resume'
            QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            assert d.batch_id==fixture['batch_id'] and [p['rows'][0]['quantity'] for p in d.plan['payload']['plans']]==['40','60']
            assert all(p['rows'][0]['stock']=='100' for p in d.plan['payload']['plans']) and facts(path)==before
            capture(d,'resume-frozen-100');checks.append('Real resume previews original stock 100 and original remaining order without committing');d.close()
            d=generator();d.refresh_stock(fixture['fresh_catalog']);state['action']='restart'
            QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            assert d.batch_id is None and d.plan['payload']['plans'][0]['rows'][0]['stock']=='700'
            assert d.catalogs['河北仓']['catalog']['items']['0001']['商品状态']=='备用'
            assert not d.plan['payload']['plans'][0]['rows'][0]['issue'] and d.plan['payload']['plans'][0]['rows'][0]['quantity']=='40'
            checks.append('Actual EXE admits a standby SKU with valid stock and preserves its demand quantity')
            assert [m['status'] for m in context()['members']]==['cancelled','pending']
            with closing(sqlite3.connect(path)) as db:assert db.execute('SELECT count(*) FROM jst_generations').fetchone()[0]==0
            capture(d,'fresh-preview-700');checks.append('Real selected restart releases only selected pending claim and previews fresh 700 before generation')
            commit_and_export(d,40);capture(d,'fresh-generated-exported');d.close()
            checks.append('Actual generation confirmation, worker, SQLite commit and export create selected order exactly once with quantity 40')
            assert context()['members'][1]['status']=='pending'
            d=generator(fixture['batch_id']);QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            assert d.plan['payload']['plans'][0]['rows'][0]['stock']=='100'
            commit_and_export(d,80);capture(d,'other-original-batch-complete');d.close()
            assert [m['status'] for m in context()['members']]==['cancelled','done']
            checks.append('Unselected original order continues against original stock 100, exports 80, and retains its own batch history')
            assert not state['error'];finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(100,execute)
