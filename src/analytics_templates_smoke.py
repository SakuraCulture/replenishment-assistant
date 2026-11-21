"""Native analytics acceptance on isolated data. No platform or browser calls."""
import json, os, sqlite3, sys, time, traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtGui import QFont,QFontDatabase
from PySide6.QtWidgets import QApplication,QLabel,QMessageBox,QTableView
from PySide6.QtTest import QTest


def facts(path):
    with closing(sqlite3.connect(path)) as db:
        return {n:db.execute('SELECT * FROM "'+n+'"').fetchall() for n, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1];existing=mode=='existing-detail'
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    if existing:
        marker=json.loads((out/'snapshot.json').read_text('utf-8'))
        assert marker['read_only_source'] and marker['synthetic'] is False and not marker['browser_configuration_copied']
        assert Path(marker['source_path']).resolve()!=win.work_repository.path.resolve()
        assert Path(os.environ['LOCALAPPDATA']).resolve()==out/'localappdata'
        assert not (out/'synthetic.json').exists()
        assert not any((out/'settings'/name).exists() for name in ('aoxiang/config','jst_catalog_sync/browsers'))
        expected=json.loads((out/'expected-rows.json').read_text('utf-8'))
        days=expected['query_days'];order_count=expected['warehouse_orders']
        assert days==marker['query_days']
    else:
        marker=json.loads((out/'synthetic.json').read_text('utf-8'))
        assert marker['synthetic'] and not marker['live_platform']
        fixture=json.loads((out/'fixture.json').read_text('utf-8'))
        days=[fixture['day']]*2;order_count=fixture['expected_orders']
    checks=[];shots=[];files=[];state={'error':None,'completed':0};observation={}
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
            observation.update(completion_title=modal.windowTitle(),completion_text='\n'.join(w.text() for w in modal.findChildren(QLabel)))
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
           'pid':os.getpid(),'window_title':win.windowTitle(),'frozen_exe':bool(getattr(sys,'frozen',False)),
           'version':diagnostic(),'synthetic_input':not existing,'live_platform':False,'dialogs_not_stubbed':True,**observation}
        (out/(mode+'-report.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),'utf-8',newline='\n')
        timer.stop();win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(1 if error else 0))
    def execute():
        try:
            import openpyxl
            before=None if existing else facts(win.work_repository.path)
            win.show_section(3);w=win.analytics
            if hasattr(w.parentWidget(),'setCurrentWidget'):w.parentWidget().setCurrentWidget(w)
            w.start.setText(days[0]);w.end.setText(days[1]);started=time.monotonic()
            if mode == 'allocation-revisions':
                w.lifecycle_range.setCurrentIndex(w.lifecycle_range.findData(''))
            QTest.mouseClick(w.query_btn,Qt.MouseButton.LeftButton)
            wait(lambda:w.worker is None and w.report is not None,timeout=240)
            observation.update(query_seconds=time.monotonic()-started,query_days=days,warehouse_orders=len(w.report['heads']))
            assert len(w.report['heads'])==order_count
            assert [(w.export_kind.itemText(i),w.export_kind.itemData(i)) for i in range(w.export_kind.count())]==[('订单经营分析','analysis'),('订单经营细则分析','analysis_detail')]
            checks.append('Actual analytics menu contains the two requested names and retains original query range')
            capture(w,'menu')
            def export(index,selected=False):
                w.export_kind.setCurrentIndex(index)
                observation['export_kind']=w.export_kind.currentText();started=time.monotonic()
                previous=set((out/'exports').glob('*.xlsx'));completed=state['completed']
                QTest.mouseClick(w.selected_btn if selected else w.export_btn,Qt.MouseButton.LeftButton)
                wait(lambda:w.worker is None and state['completed']>completed,timeout=240)
                observation['export_seconds']=time.monotonic()-started
                created=set((out/'exports').glob('*.xlsx'))-previous;assert len(created)==1
                path=created.pop();assert path.name.startswith(w.export_kind.currentText()+'_');files.append(str(path));return path
            if mode == 'allocation-revisions':
                from openpyxl import load_workbook
                for case in fixture['cutoffs']:
                    if case['as_of']:
                        w.as_of.setText(case['as_of'])
                        QTest.mouseClick(w.query_btn, Qt.MouseButton.LeftButton)
                        wait(lambda: w.worker is None and w.report is not None
                             and w.report['query'].get('as_of') == case['as_of'])
                    path = export(w.export_kind.findData('analysis_detail'))
                    with closing(load_workbook(path, read_only=True, data_only=True)) as book:
                        sheet = book['SKU完整明细']
                        sheet.reset_dimensions()
                        values = {(row[0],row[2]):row[9:13] for row in sheet.iter_rows(min_row=8,values_only=True)}
                        for number, sku, cells in case['rows']:
                            assert values[number,sku] == tuple(cells), (case,number,sku,values[number,sku])
                    checks.append('实际EXE细则导出逐单逐SKU核对四列 '+(case['as_of'] or '当前'))
                observation['allocation_cases'] = fixture['cutoffs']
            elif existing:
                assert w.report['data_stamp']==expected['data_stamp']
                assert len(w.report['rows'])==expected['total']
                assert w.report.get('_projection')=='overview' and w.details is None
                assert 'coverage' not in w.report and 'history' not in w.report
                assert w.report['metrics']['excluded']==len(expected['excluded'])
                overview=w.report
                from order_analytics import digest
                baseline=json.loads((out/'overview-baseline.json').read_text('utf-8'))
                fields=('metrics','groups','stores','warehouses','orders','payments','identifiers')
                assert digest({k:overview[k] for k in fields})==baseline['summary_digest']
                observation['overview_parity']=True
                started=time.monotonic()
                for key,tab in (('store',1),('warehouse',2),('order',3)):
                    table=w.tables[key];index=table.model().index(0,0);table.scrollTo(index)
                    assert index.isValid() and table.visualRect(index).isValid()
                    position=table.visualRect(index).center()
                    QTest.mouseClick(table.viewport(),Qt.MouseButton.LeftButton,pos=position)
                    QTest.mouseDClick(table.viewport(),Qt.MouseButton.LeftButton,pos=position)
                    wait(lambda:w.tabs.currentIndex()==tab and w.worker is None,timeout=240)
                assert w.report is overview and w.details and w.details[1]
                order=w.drill['order']
                assert {row['order'] for row in w.details[1]}=={order}
                assert w.models['detail'].rowCount()>0
                observation.update(drill_orders=1,drill_rows=len(w.details[1]),drill_seconds=time.monotonic()-started)
                checks.append('Native drill loads only its selected warehouse order and keeps the original overview')
                capture(w,'drill');w.back()
                export(w.export_kind.findData('analysis_detail'))
                assert f"纳入{len(expected['accepted'])}条" in observation['completion_text']
                checks.append('Exported the existing order snapshot through the actual analytics page')
            elif mode=='exercise':
                summary=export(0);detail=export(1)
                book=openpyxl.load_workbook(summary,read_only=True,data_only=True)
                try:
                    assert book.sheetnames==['SKU种类','商品货值','商品数量']
                    for sheet in book:
                        sheet.reset_dimensions();headers=list(next(sheet.iter_rows(min_row=16,max_row=16,values_only=True)))
                        assert len(headers)==22 and headers[-2:]==['聚水潭内部订单号','翱象采购单号']
                        body=list(sheet.iter_rows(min_row=17,values_only=True));assert len(body)==6
                        assert all(v not in (None,'','n.a.') for r in body for v in r[6:20])
                        assert '基数为0时无法计算比率' in sheet['A14'].value
                        assert any(r[1]==fixture['head']['number'] and r[-2:]==(fixture['jst'],fixture['po']) for r in body)
                    checks.append('Actual current-range summary export has 3 template sheets, ALL groups and exact linked identifiers')
                finally:book.close()
                book=openpyxl.load_workbook(detail,read_only=True,data_only=True)
                try:
                    assert book.sheetnames==['SKU完整明细'];sheet=book.active;sheet.reset_dimensions()
                    rows=list(sheet.iter_rows(min_row=8,values_only=True));assert len(rows)==fixture['expected_rows']
                    row=next(r for r in rows if r[0]==fixture['head']['number'])
                    assert len(row)==47 and row[2]=='0001' and row[33]=='0000000123456'
                    assert row[36:44]==(4,60,0,148,24,24,'2026-11-14 12:00:00',240),row
                    headers=next(sheet.iter_rows(min_row=7,max_row=7,values_only=True))
                    assert headers[9:13]==('分配时采用的仓可用库存','本单分配前余额','实际分配量','缺口原因')
                    assert headers[42:44]==('门店补货建议更新时间','签发参考仓库存')
                    if 'expected_allocation' in fixture:
                        assert row[9:13]==tuple(fixture['expected_allocation'])
                    assert row[-2:]==(fixture['jst'],fixture['po'])
                    assert isinstance(row[15],(int,float)) and abs(row[21]-4/24)<1e-12
                    assert all(v not in (None,'','n.a.') for r in rows for v in r[5:30])
                    assert '未留存表示缺少原字段' in sheet['A5'].value
                    checks.append('Actual detail exports retained inventory, saved allocation and readable formula caches')
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
            if not existing:
                assert facts(win.work_repository.path)==before
                checks.append('Query and actual exports do not modify original signed/lifecycle business data')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(200,execute)
