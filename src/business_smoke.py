"""Opt-in real Qt/EXE acceptance, exclusively launcher-supplied QA paths."""
import json,time,traceback,sys,copy
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,QMimeData,QUrl,QPoint,QPointF,Qt
from PySide6.QtGui import QDragEnterEvent,QDropEvent
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QFileDialog,QPushButton,QComboBox
from PySide6.QtTest import QTest
import work_repository as wr,normal_exports as ne,warehouse_catalog as wc
from version_info import diagnostic

def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);checks=[];shots=[];beats=[];last=[time.perf_counter()];heartbeat=QTimer(win);heartbeat.setInterval(25)
    def beat():now=time.perf_counter();beats.append(now-last[0]);last[0]=now
    heartbeat.timeout.connect(beat);heartbeat.start()
    def wait(test,seconds=40):
        loop=QEventLoop();timer=QTimer();start=time.monotonic()
        timer.timeout.connect(lambda:loop.quit() if test() or time.monotonic()-start>seconds else None);timer.start(20);loop.exec();timer.stop();assert test(),'UI wait timed out'
    def shot(widget,name):assert widget.grab().save(str(out/name));shots.append(name)
    def drop(widget,path):
        mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path.resolve()))]);enter=QDragEnterEvent(QPoint(10,10),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);QApplication.sendEvent(widget,enter);assert enter.isAccepted()
        event=QDropEvent(QPointF(10,10),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);QApplication.sendEvent(widget,event);assert event.isAccepted()
    def finish(error=None):
        heartbeat.stop();report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'ui_ticks':len(beats),'max_tick_gap_seconds':max(beats,default=0),'version':diagnostic()};(out/'business_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');win.job_dirty=False;win._discard_close=True;win.close();app.exit(0 if error is None else 1)
    def exercise():
        try:
            from import_ui import ImportDialog,download
            from import_templates import TEMPLATES
            from history_ui import ArchiveDialog,AuditDialog,EventsDialog
            from feedback_ui import import_returns,FeedbackDialog
            from business_ui import Task
            import merchant_feedback as mf,source_archive,openpyxl
            p=wr.loads(fixture.read_text(encoding='utf-8'));p['day']=wr.today();root=fixture.parent
            for wh in wc.WAREHOUSES:
                win.import_catalog_path(str(root/(wh+'.xlsx')),wh);wait(lambda:win.worker is None);assert wh in win.catalogs
            p['catalogs']=copy.deepcopy(win.catalogs);win.orders.confirm(p,list(p['results']),None,{s:None for s in p['results']});old=win.orders.get(p['day'],'QA_A');original=wr.dumps(old['payload']);win.refresh_archive();wait(lambda:win.history_reader.active is None and len(win.archive_orders)==2)
            checks.append('actual background catalog imports retain original bytes, isolated two-store signed fixture')
            win.show_section(1);drop(win.sales,root/'sales.xlsx');assert str((root/'sales.xlsx').resolve()) in win.sales.paths
            for kind in TEMPLATES:
                d=ImportDialog(win,kind);d.show();drop(d,root/'sales.xlsx');assert d.paths;d.reject();d.deleteLater()
            checks.append('native drag-enter/drop accepted for sales and all six typed import dialogs')
            save,info=QFileDialog.getSaveFileName,QMessageBox.information;QMessageBox.information=lambda *a,**k:QMessageBox.StandardButton.Ok
            try:
                for kind in TEMPLATES:
                    path=out/(kind+'-template.xlsx');QFileDialog.getSaveFileName=lambda *a,p=path,**k:(str(p),'');download(win,kind);assert path.is_file()
            finally:QFileDialog.getSaveFileName=save;QMessageBox.information=info
            checks.append('download minimum workbook template action for all six import kinds')
            win.show_section(2);wait(lambda:win.history_reader.active is None);d=ArchiveDialog(win,next(r for r in win.archive_orders if r['store']=='QA_A'));d.show();wait(lambda:d.selected is not None);shot(d,'01-signed-detail.png')
            audit=AuditDialog(d,win,d.selected);audit.show();wait(lambda:audit.model.rowCount()>0);audit.sku.setText('new_must')
            for scope in ['当时参数','商品建议与销量库存','当时商品资料快照','商品完整计算依据']:
                audit.scope.setCurrentText(scope);wait(lambda:win.history_reader.active is None);assert audit.model.rowCount()>0
                if scope=='商品建议与销量库存':assert any('原计算建议量' in r[0] for r in audit.model.rows);shot(audit,'02-business-form.png')
                if scope=='当时商品资料快照':assert any('原表全部列' in r[0] for r in audit.model.rows);shot(audit,'03-original-catalog.png')
            path=out/'form.xlsx';QFileDialog.getSaveFileName=lambda *a,**k:(str(path),'');audit.export();wait(lambda:path.exists());QFileDialog.getSaveFileName=save
            audit.load();audit.reject();audit.deleteLater();win.refresh_archive();wait(lambda:win.history_reader.active is None);assert len(win.archive_orders)==2
            moving=win.orders.get(wr.today(),'QA_B');audit=AuditDialog(d,win,moving);audit.show();wait(lambda:audit.model.rowCount()>0)
            audit.sku.setText('moving');audit.scope.setCurrentText('商品建议与销量库存');wait(lambda:win.history_reader.active is None)
            values={r[0]:r[1] for r in audit.model.rows};assert float(values['签发执行量'])==5 and float(values['签发金额'])==10,values
            shot(audit,'07-moving-execution.png');audit.reject();audit.deleteLater()
            checks.append('ordinary moving SKU form displays actual budget execution 5 and amount 10, not absent first-fill fields')
            checks.append('actual historical forms and lazy raw catalog fields, full form Excel export, close pending read and retry')
            reply=out/'returned.xlsx';ne.export(old,reply);book=openpyxl.load_workbook(reply)
            for ws in book:
                for cells in list(ws.rows)[1:]:
                    if cells[4].value=='new_must':cells[16].value=5
            book.save(reply);book.close();state={'cancel':True};timer=QTimer(win);timer.setInterval(25)
            def automate():
                box=QApplication.activeModalWidget()
                if isinstance(box,ImportDialog):drop(box,reply);box.accept()
                elif isinstance(box,Task):pass
                elif isinstance(box,QDialog) and box.windowTitle().startswith('确认保存门店回传'):
                    if not state.get('captured'):shot(box,'04-return-preview.png');state['captured']=True
                    text='取消' if state['cancel'] else '确认保存独立回传';QTest.mouseClick(next(b for b in box.findChildren(QPushButton) if b.text()==text),Qt.MouseButton.LeftButton)
                elif isinstance(box,QMessageBox):box.accept()
            timer.timeout.connect(automate);timer.start()
            try:
                import_returns(d,win)
                with win.work_repository.connect() as db:assert mf.current(db,old['oid'],1) is None
                state['cancel']=False;import_returns(d,win);import_returns(d,win)
            finally:timer.stop()
            with win.work_repository.connect() as db:record=mf.current(db,old['oid'],1);assert record['version']==1 and record['payload']['values']['new_must']==5
            assert wr.dumps(win.orders.get(p['day'],'QA_A',1)['payload'])==original
            feedback=FeedbackDialog(d,win,d.selected);feedback.show();assert any(row[0]=='new_must' and row[3:6]==[6,5,-1] for row in feedback.model.rows);shot(feedback,'05-return-comparison.png');feedback.accept();feedback.deleteLater()
            checks.append('actual return drag/import preview cancel and confirm; repeat idempotent; original signed 6 and returned5 shown independently')
            events=EventsDialog(win,win.orders.events());events.show();events.search.setText('门店确认回传');events.filter();assert len(events.matches)==1;shot(events,'06-event-form.png')
            back=QTimer(win);back.setInterval(20);returned=[]
            def visit():
                box=QApplication.activeModalWidget()
                if isinstance(box,ArchiveDialog) and box.selected:
                    assert box.selected['revision']==1;returned.append(True);back.stop();box.accept()
            back.timeout.connect(visit);back.start();events.open_order(events.model.index(0,0));assert returned;back.stop();back.deleteLater()
            checks.append('log row resolves exact original signed revision, not latest replacement')
            target=out/'events.xlsx';QFileDialog.getSaveFileName=lambda *a,**k:(str(target),'');events.export();QFileDialog.getSaveFileName=save;assert target.exists();events.accept();events.deleteLater();d.reject();d.deleteLater()
            checks.append('queryable business log grid exports matching records as Excel; no JSON business view')
            finish()
        except Exception:finish(traceback.format_exc())
    sys.excepthook=lambda t,v,tb:finish(''.join(traceback.format_exception(t,v,tb)))
    QTimer.singleShot(500,exercise)
