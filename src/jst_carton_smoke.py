"""Opt-in real Qt allocation/export/reopen checks; marked synthetic data only."""
import json, os, sqlite3, sys, time, traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] is True and marker['live_platform'] is False
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    assert mode in ('exercise','reopen')
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    import jst_orders as orders,order_lifecycle as lc,export_locations,jst_results as results
    from jst_ui import GenerationDialog
    from version_info import diagnostic
    from stockout_task import Bridge
    from unittest.mock import patch
    def forbidden(*a,**k):raise AssertionError('Synthetic acceptance forbids platform requests')
    fence=patch.object(Bridge,'request',forbidden);fence.start()
    initial_directory=export_locations.initial_directory
    export_locations.initial_directory=lambda *a,**k:str(out/'exports')
    checks=[];messages=[];dialogs=[];state={'error':None};start=time.monotonic()
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def automate():
        modal=QApplication.activeModalWidget()
        if not isinstance(modal,QMessageBox):return
        messages.append({'title':modal.windowTitle(),'text':modal.text()})
        if modal.windowTitle()=='确认生成聚水潭订单':target=modal.button(QMessageBox.StandardButton.Yes)
        else:
            state['error']=modal.windowTitle()+': '+modal.text();target=modal.button(QMessageBox.StandardButton.Ok)
        if target:QTest.mouseClick(target,Qt.MouseButton.LeftButton)
        else:modal.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(automate);timer.start()
    def wait(predicate):
        deadline=time.monotonic()+60
        while not predicate() and not state['error'] and time.monotonic()<deadline:QTest.qWait(15)
        assert not state['error'],state['error']
        assert predicate(),'Native UI wait timeout'
    def verify_saved():
        with closing(sqlite3.connect(win.work_repository.path)) as db:
            gids=[r[0] for r in db.execute('SELECT gid FROM jst_generations ORDER BY rowid')]
        plans=[orders.get(win.work_repository.path,gid) for gid in gids]
        require(len(plans)==2,'Two warehouses each retain their own generation')
        actual={(p['head']['warehouse'],r['sku']):r for p in plans for r in p['rows']}
        for wh,sku,demand,step,stock,expected,status in marker['cases']:
            if not demand:
                require((wh,sku) not in actual,'Zero demand does not create an allocation row');continue
            row=actual[(wh,sku)]
            require(row['quantity']==str(expected),wh+' '+sku+' saved quantity '+str(expected))
            require(row['demand']==str(demand),wh+' '+sku+' preserves original returned quantity')
            require(row['missing']==str(max(demand-expected,0)),wh+' '+sku+' has no negative shortage')
            require(row['after']==str(stock-expected),wh+' '+sku+' subtracts actual allocation only')
        require(all(p['rule']=='jst-stock-v3' for p in plans),'Saved generations identify ceiling rule v3')
        for h in marker['heads']:
            require(lc.snapshot(win.work_repository.path,h)['documents']['return']['digest']==marker['return_digests'][h['warehouse']],h['warehouse']+' original return remains unchanged')
        data=results.read(win.work_repository.path,out/'results.sqlite3',marker['heads'][0]['day'])
        require(data['metrics']['reduced']>=0 and data['metrics']['increased']>0,'Result history separates shortages and carton increases')
        return plans
    def finish(error=None):
        report={'passed':error is None,'error':error,'mode':mode,'checks':checks,'messages':messages,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'synthetic':True,
                'platform_calls':0,'dialogs_not_stubbed':True,'elapsed_seconds':time.monotonic()-start}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
        timer.stop();fence.stop();export_locations.initial_directory=initial_directory
        for d in reversed(dialogs):d.close()
        win._large_io_dialog=None;win._discard_close=True;win.close()
        QTimer.singleShot(100,lambda:app.exit(0 if error is None else 1))
    def execute():
        try:
            if mode=='reopen':verify_saved();finish();return
            d=GenerationDialog(win,win,marker['heads']);dialogs.append(d);d.show();wait(lambda:len(d.catalogs)==2)
            QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            require(d.plan is not None,'Actual preview button uses selected warehouse catalogs')
            (out/'preview.json').write_text(json.dumps({'summary':d.review.total.text(),'plan':d.plan},ensure_ascii=False,indent=2,default=str),'utf-8')
            d.review.grab().save(str(out/'ceiling-preview.png'))
            require('箱规增配' in d.review.total.text(),'Preview states carton increases explicitly')
            require(all(float(r['missing'])>=0 for p in d.plan['payload']['plans'] for r in p['rows']),'Preview has no negative shortages')
            d.review.open_store(d.review.store_model.index(0,0))
            require(d.review.model.columnCount()==18,'SKU review includes separate increase quantity and amount')
            d.review.grab().save(str(out/'ceiling-preview.png'))
            QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton)
            wait(lambda:d.worker is None and d.receipt is not None)
            require(not d.receipt['errors'] and len(d.saved)==2,'Real confirmation and worker commit both warehouse orders')
            plans=verify_saved()
            QTest.mouseClick(d.select_completed_btn,Qt.MouseButton.LeftButton)
            QTest.mouseClick(d.export_saved_btn,Qt.MouseButton.LeftButton)
            wait(lambda:d.export_worker is None and not d.export_queue)
            import openpyxl
            files=list((out/'exports').glob('*.xlsx'));require(len(files)==2,'Actual export button writes two warehouse files')
            by_number={p['head']['number']:p for p in plans}
            for file in files:
                book=openpyxl.load_workbook(file,read_only=True,data_only=True)
                try:rows=list(book.active.values)
                finally:book.close()
                p=by_number[rows[1][3]]
                require([(r[0],r[1],r[2]) for r in rows[1:]]==[(r['sku'],float(r['price']),float(r['quantity'])) for r in p['rows'] if float(r['quantity'])>0],'Export quantity, price and row order equal frozen allocation: '+p['head']['warehouse'])
            d.grab().save(str(out/'ceiling-generated.png'));finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(100,execute)
