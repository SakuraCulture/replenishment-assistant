"""Marked synthetic native-application acceptance. No platform or browser calls."""
import json, os, sqlite3, sys, time, traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import QApplication,QMessageBox,QTableView
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert marker['synthetic'] and not marker['live_platform']
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    fixture=json.loads((out/'fixture.json').read_text('utf-8'));checks=[];shots=[];files=[];state={'error':None,'completed':0}
    font=Path('C:/Windows/Fonts/msyh.ttc')
    if font.is_file():QFontDatabase.addApplicationFont(str(font));app.setFont(QFont('Microsoft YaHei',9))
    win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    if hasattr(win,'auto_check'):win.auto_check.stop()
    def capture(widget,name):
        name=mode+'-'+name+'.png';assert widget.grab().save(str(out/name));shots.append(name)
    def modal_handler():
        modal=QApplication.activeModalWidget()
        if modal is None:return
        if modal.windowTitle()=='分析报表已导出':
            tables=modal.findChildren(QTableView)
            if not tables or tables[0].model().rowCount()!=1:state['error']='Export result did not contain one saved file'
            capture(modal,'saved-'+str(state['completed']));state['completed']+=1;modal.accept()
        elif isinstance(modal,QMessageBox):
            state['error']=modal.windowTitle()+': '+modal.text();modal.accept()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(modal_handler);timer.start()
    def wait(predicate,timeout=60):
        start=time.monotonic()
        while not predicate() and not state['error'] and time.monotonic()-start<timeout:QTest.qWait(20)
        assert not state['error'],state['error'];assert predicate(),'Actual UI export timed out'
    def finish(error=None):
        from version_info import diagnostic
        r={'passed':error is None,'error':error,'mode':mode,'checks':checks,'screenshots':shots,'files':files,
           'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'synthetic_input':True,'live_platform':False,
           'dialogs_not_stubbed':True}
        (out/(mode+'-report.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        timer.stop();win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(1 if error else 0))
    def execute():
        try:
            import openpyxl
            before=facts(win.work_repository.path)
            win.show_section(3);w=win.analytics
            if hasattr(w.parentWidget(),'setCurrentWidget'):w.parentWidget().setCurrentWidget(w)
            w.start.setText(fixture['day']);w.end.setText(fixture['day']);w.query()
            wait(lambda:w.worker is None and w.report is not None)
            assert len(w.report['heads'])==fixture['expected_orders']
            assert [(w.export_kind.itemText(i),w.export_kind.itemData(i)) for i in range(w.export_kind.count())]==[('订单经营分析','analysis'),('订单经营细则分析','analysis_detail')]
            checks.append('Actual analytics menu contains the two requested names and retains original query range')
            capture(w,'menu')
            def export(index,selected=False):
                w.export_kind.setCurrentIndex(index)
                previous=set((out/'exports').glob('*.xlsx'));completed=state['completed']
                QTest.mouseClick(w.selected_btn if selected else w.export_btn,Qt.MouseButton.LeftButton)
                wait(lambda:w.worker is None and state['completed']>completed)
                created=set((out/'exports').glob('*.xlsx'))-previous;assert len(created)==1
                path=created.pop();assert path.name.startswith(w.export_kind.currentText()+'_');files.append(str(path));return path
            if mode=='exercise':
                summary=export(0);detail=export(1)
                book=openpyxl.load_workbook(summary,read_only=True,data_only=True)
                try:
                    assert book.sheetnames==['SKU种类','商品货值','商品数量']
                    for sheet in book:
                        sheet.reset_dimensions();headers=list(next(sheet.iter_rows(min_row=16,max_row=16,values_only=True)))
                        assert len(headers)==22 and headers[-2:]==['聚水潭内部订单号','翱象采购单号']
                        body=list(sheet.iter_rows(min_row=17,values_only=True));assert len(body)==6
                        assert any(r[1]==fixture['head']['number'] and r[-2:]==(fixture['jst'],fixture['po']) for r in body)
                    checks.append('Actual current-range summary export has 3 template sheets, ALL groups and exact linked identifiers')
                finally:book.close()
                book=openpyxl.load_workbook(detail,read_only=True,data_only=True)
                try:
                    assert book.sheetnames==['SKU完整明细'];sheet=book.active;sheet.reset_dimensions()
                    rows=list(sheet.iter_rows(min_row=8,values_only=True));assert len(rows)==fixture['expected_rows']
                    row=next(r for r in rows if r[0]==fixture['head']['number'])
                    assert len(row)==43 and row[2]=='0001' and row[29]=='0000000123456'
                    assert row[32:40]==(4,60,0,148,24,24,'2026-11-14 12:00:00',240),row
                    assert row[-2:]==(fixture['jst'],fixture['po'])
                    assert isinstance(row[11],(int,float)) and abs(row[17]-4/24)<1e-12
                    checks.append('Actual single-sheet detail exports retained inventory and constraints with readable formula caches')
                finally:book.close()
                w.selected={fixture['head']['number']};w.controls();selected=export(1,True)
                book=openpyxl.load_workbook(selected,read_only=True,data_only=True)
                try:
                    book.active.reset_dimensions();rows=list(book.active.iter_rows(min_row=8,values_only=True))
                    assert rows and all(r[0]==fixture['head']['number'] for r in rows)
                finally:book.close()
                checks.append('Actual selected-order button exports only the selected warehouse order')
                (out/'saved-exports.json').write_text(json.dumps({'summary':str(summary),'detail':str(detail),'selected':str(selected)},ensure_ascii=False,indent=2),'utf-8',newline='\n')
            else:
                for name,path in json.loads((out/'saved-exports.json').read_text('utf-8')).items():
                    book=openpyxl.load_workbook(path,read_only=True,data_only=True);assert book.sheetnames;book.close()
                checks.append('Actual EXE reopen reads every earlier generated workbook')
                w.selected={fixture['head']['number']};w.controls();export(0,True)
                checks.append('Actual EXE reopen can still export selected-order summary')
            assert facts(win.work_repository.path)==before
            checks.append('Query and actual exports do not modify original signed/lifecycle business data')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(200,execute)
