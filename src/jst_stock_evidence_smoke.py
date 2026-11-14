"""Opt-in stock-evidence acceptance; real buttons, workers, SQLite and XLSX."""
import json,sqlite3,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QEventLoop
from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QMessageBox
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {name:db.execute('SELECT * FROM "'+name+'"').fetchall() for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def workbook(path,stock,status):
    import openpyxl
    book=openpyxl.load_workbook(path,read_only=True,data_only=True)
    try:
        summaries=list(book['资料批次'].values);summary=[dict(zip(summaries[0],r)) for r in summaries[1:]]
        assert summary and all(r['状态']==status for r in summary),summary
        fields=[]
        for sheet in book:
            if not sheet.title.startswith('采用字段_'):continue
            rows=iter(sheet.values);headers=next(rows);fields.extend(dict(zip(headers,r)) for r in rows)
        sku=next(r for r in fields if str(r['商品编码'])=='0001')
        assert float(sku['可用数'])==stock,sku
        assert all('未留存' in str(r.get('平台导出时间')) for r in summary),summary
        return {'sheets':book.sheetnames,'sources':summary,'sku':sku}
    finally:book.close()


def start_smoke(app,win,args):
    import work_repository as wr,export_locations
    from version_info import diagnostic
    out=Path(args[0]);mode=args[1];fixture=wr.loads((out/'fixture.json').read_text('utf-8'))
    path=win.work_repository.path;assert path.resolve()==(out/'settings/workbench.sqlite3').resolve()
    win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    checks=[];messages=[];shots=[];dialogs=[];state={'error':None,'target':None};begun=time.monotonic()
    original_save=export_locations.save_file
    def destination(*a,**kw):return (str(state['target']),'Excel (*.xlsx)') if state['target'] else ('','')
    export_locations.save_file=destination
    def automate():
        modal=QApplication.activeModalWidget()
        if isinstance(modal,QMessageBox):
            messages.append({'title':modal.windowTitle(),'text':modal.text()})
            if modal.windowTitle()!='请先预览库存分配':state['error']=modal.windowTitle()+': '+modal.text()
            ok=modal.button(QMessageBox.StandardButton.Ok)
            if ok:QTest.mouseClick(ok,Qt.MouseButton.LeftButton)
            else:modal.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(automate);timer.start()
    def wait(predicate,timeout=40):
        loop=QEventLoop();watch=QTimer();watch.setInterval(15);start=time.monotonic()
        watch.timeout.connect(lambda:loop.quit() if predicate() or state['error'] or time.monotonic()-start>timeout else None)
        watch.start();loop.exec();watch.stop()
        if state['error']:raise AssertionError(state['error'])
        if not predicate():raise AssertionError('Native UI wait timeout')
    def shot(dialog,name):
        name=mode+'-'+name+'.png';dialog.grab().save(str(out/name));shots.append(name)
    def click_export(button,name,stock,status):
        target=out/'exports'/(mode+'-'+name+'.xlsx');state['target']=target
        QTest.mouseClick(button,Qt.MouseButton.LeftButton)
        wait(target.is_file);data=workbook(target,stock,status);state['target']=None;return data
    def result_page():
        from jst_results_ui import ResultsPage
        dialog=QDialog(win);dialog.setProperty('jstResults',True);dialog.setWindowTitle('实际采用仓资料导出验收');dialog.resize(1260,860)
        layout=QVBoxLayout(dialog);page=ResultsPage(win,[fixture['head']],dialog);layout.addWidget(page);dialog.show();dialogs.append((dialog,page))
        wait(lambda:page.data is not None and not page.reading);page.timer.stop();return dialog,page
    def finish(error=None):
        report={'passed':error is None,'error':error,'mode':mode,'checks':checks,'screenshots':shots,'messages':messages,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'database':str(path),
                'live_platform':False,'file_destination_adapter':True,'dialogs_not_stubbed':True,'elapsed_seconds':time.monotonic()-begun}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
        export_locations.save_file=original_save;timer.stop()
        for dialog,page in reversed(dialogs):
            if page is not None:page.shutdown()
            dialog.close()
        win._large_io_dialog=None;win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if error is None else 1))
    def execute():
        try:
            from jst_ui import GenerationDialog
            before=facts(path);dialog,page=result_page()
            page.chosen={__import__('jst_results').key(fixture['head'])};page.render();page.controls()
            assert page.evidence_btn.isEnabled() and '勾选' in page.evidence_btn.text()
            if mode=='exercise':
                # File chooser cancellation is an input adapter only. The real
                # click must not change any persisted business record.
                state['target']=None;QTest.mouseClick(page.evidence_btn,Qt.MouseButton.LeftButton)
                assert facts(path)==before;checks.append('Cancel destination leaves all business records unchanged')
            chosen=click_export(page.evidence_btn,'chosen-generated',100,'已冻结')
            assert str(chosen['sku']['可用数'])!='900';checks.append('Actual selected-order button exports frozen 100, not current default 900')
            shot(dialog,'selected-generation')
            if mode=='reopen':
                assert facts(path)==before;checks.append('Actual second launch retains exact generation and exports without business writes')
                finish();return
            page.batch_id=fixture['batch_id'];page.controls()
            assert page.evidence_btn.text()=='导出本批采用仓资料'
            click_export(page.evidence_btn,'saved-batch',100,'已冻结');checks.append('Batch-view button exports exact original batch stock 100')
            generator=GenerationDialog(win,win,[fixture['head']],fixture['batch_id']);generator.show();dialogs.append((generator,None))
            wait(lambda:bool(generator.catalogs));generator.plan=None
            click_export(generator.evidence_btn,'old-batch-dialog',100,'已冻结');shot(generator,'old-batch-dialog')
            generator.close();checks.append('Existing generation dialog exports its frozen batch without re-preview or refresh')
            preview=GenerationDialog(win,win,[fixture['second']]);preview.show();dialogs.append((preview,None))
            wait(lambda:bool(preview.catalogs))
            QTest.mouseClick(preview.evidence_btn,Qt.MouseButton.LeftButton)
            assert any(m['title']=='请先预览库存分配' for m in messages);checks.append('Unpreviewed new batch uses real explanatory dialog and never exports defaults as adopted')
            # QA input adapts the prepared, unconfirmed plan; the exporter itself,
            # SQLite source, background worker and saved XLSX are unmodified.
            preview.catalogs=fixture['preview']['payload']['new_batch']['stocks'];preview.describe()
            preview.plan=fixture['preview']
            click_export(preview.evidence_btn,'unconfirmed-preview',500,'未确认预览');shot(preview,'unconfirmed-preview')
            checks.append('Actual preview button exports chosen stock 500 explicitly labelled unconfirmed preview')
            assert facts(path)==before;checks.append('All evidence exports preserve every business table and do not allocate or reserve inventory')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(100,execute)
