"""Explicit isolated EXE: empty-error UI, low-headroom export and cancellation."""
import ctypes,hashlib,json,math,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox,QTableView


def empty_failure(*args,progress,cancel):
    progress('读取签发与履约快照');raise MemoryError()


def start_smoke(app,win,args):
    from analytics_template_export import NOT_RETAINED, select_detail_rows
    out=Path(args[0]).resolve();mode=args[1];spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert spec['synthetic'] is True and spec['live_platform'] is False
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    checks=[];shots=[];files=[];state={'error':None,'expect_error':False,'seen_error':False,'cancel':False,'cancelled':False,'completed':0}
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def capture(widget,name):
        name=mode+'-'+name+'.png'
        if widget.grab().save(str(out/name)):shots.append(name)
    def modal():
        d=QApplication.activeModalWidget()
        try:
            if isinstance(d,QMessageBox):
                if state['expect_error'] and d.windowTitle()=='分析未完成':
                    require('MemoryError' in d.text() and '读取签发与履约快照' in d.text(),'真实错误窗口显示异常类型和失败阶段')
                    require('MemoryError' in d.detailedText() and 'app.log' in d.detailedText(),'技术详情及应用日志位置可复制')
                    capture(d,'memory-error-visible');state['seen_error']=True;d.accept()
                else:raise AssertionError(d.windowTitle()+': '+d.text())
            elif d is not None and d.windowTitle()=='分析报表已导出':
                tables=d.findChildren(QTableView)
                require(bool(tables) and tables[0].model().rowCount()==1,'真实导出结果窗口包含一个已完成文件')
                capture(d,'completed-'+str(state['completed']));state['completed']+=1;d.accept()
            w=getattr(win,'analytics',None)
            if w is not None and state['cancel'] and w.worker is not None and '复核分析摘要' in w.note.text():
                state['cancel']=False;state['cancelled']=True;capture(w,'cancel-verification');w.cancel_btn.click()
        except Exception:
            state['error']=traceback.format_exc()
            if d is not None:d.accept()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(modal);timer.start()
    def wait(condition,seconds=300):
        start=time.monotonic()
        while not condition() and not state['error'] and time.monotonic()-start<seconds:QTest.qWait(20)
        if state['error']:raise AssertionError(state['error'])
        require(condition(),'实际窗口后台操作安全完成')
    def file_hash(path):
        with path.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
    def execute():
        import order_analytics_exports as exports,export_locations,openpyxl
        from version_info import diagnostic
        from work_memory import snapshot
        from resource_pressure_probe import Probe
        report={'passed':False,'mode':mode,'actual_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),
                'checks':checks,'screenshots':shots,'files':files,'live_platform':False,'messagebox_stubs':False}
        original_export=exports.export;original_save=export_locations.save_file;probe=Probe('tight')
        class Own:
            pid=os.getpid()
            probe.kernel.GetCurrentProcess.restype=ctypes.c_void_p
            sentinel=probe.kernel.GetCurrentProcess()
            def is_alive(self):return True
        own=Own()
        try:
            before=file_hash(win.work_repository.path);win.show_section(3);w=win.analytics
            if hasattr(w.parentWidget(),'setCurrentWidget'):w.parentWidget().setCurrentWidget(w)
            w.start.setText(spec['day']);w.end.setText(spec['day']);w.query()
            wait(lambda:w.worker is None and w.report is not None)
            require(len(w.report['heads'])==spec['orders'] and len(w.report['rows'])==spec['rows'],'实际查询保留全部64单与64000行')
            require(win.concurrency.value()==2,'默认计算并发仍为2')
            target_dir=out/'exports';target_dir.mkdir(exist_ok=True)
            export_locations.save_file=lambda parent,title,name,*a,**k:(str(target_dir/name),'Excel (*.xlsx)')
            def export(index,selected=False):
                w.export_kind.setCurrentIndex(index);previous=set(target_dir.glob('*.xlsx'));completed=state['completed']
                (w.selected_btn if selected else w.export_btn).click();wait(lambda:w.worker is None and state['completed']>completed)
                created=set(target_dir.glob('*.xlsx'))-previous;require(len(created)==1,'目标目录只发布本次一个完整文件')
                path=created.pop();files.append(str(path));return path
            if mode=='exercise':
                state['expect_error']=True;exports.export=empty_failure;w.export_btn.click();wait(lambda:w.worker is None and state['seen_error'])
                exports.export=original_export;state['expect_error']=False
                require(not list(target_dir.glob('*.xlsx')),'异常路径未误报或发布文件')
                limit=math.ceil(snapshot()['private_bytes']/1024**2)+64
                probe.limit(own,limit,'analytics-root-extra64MiB')
                summary=export(0)
                require(snapshot().get('process_limit_bytes')==limit*1024**2,'实际EXE处于自身内存额度约束下')
                probe.limit(own,6000,'analytics-root-resources-restored')
                book=openpyxl.load_workbook(summary,read_only=True,data_only=True)
                try:
                    require(book.sheetnames==['SKU种类','商品货值','商品数量'],'三口径汇总模板保持')
                    for sheet in book:
                        values=list(sheet.iter_rows(min_row=17,values_only=True))
                        require(len([r for r in values if r[4]=='ALL'])==spec['orders'],sheet.title+'保留全部仓单ALL行')
                finally:book.close()
                state['cancel']=True;w.export_kind.setCurrentIndex(1);before_files=set(target_dir.glob('*.xlsx'));w.export_btn.click()
                wait(lambda:w.worker is None)
                require(state['cancelled'],'真实UI在摘要复核期间取消')
                require(set(target_dir.glob('*.xlsx'))==before_files and not list(target_dir.glob('.analytics-*')),'取消后无半文件或复核暂存残留')
                w.selected={h['number'] for h in w.report['heads'][:8]};w.controls()
                expected_rows=select_detail_rows(r for r in w.report['rows'] if r['order'] in w.selected)
                detail=export(1,True);book=openpyxl.load_workbook(detail,read_only=True,data_only=True)
                try:
                    sheet=book.active;sheet.reset_dimensions();actual=iter(sheet.iter_rows(min_row=7,values_only=True));count=0
                    day_column=next(actual).index('订单日期')
                    for expected in expected_rows:
                        row=next(actual);count+=1
                        require_row=(row[0]==expected['order'] and row[2]==expected['sku'] and row[day_column]==expected['day'])
                        if not require_row:raise AssertionError('Detail identity/order changed at '+str(count))
                        for col,stage in ((5,'model'),(6,'signed')):
                            q=expected['quantities'][stage] if expected['valid'][stage] else None
                            value=float(q) if q is not None else NOT_RETAINED
                            if row[col]!=value:raise AssertionError('Detail quantity changed at '+str(count))
                    require(count==len(expected_rows) and next(actual,None) is None,f'所选{count}行SKU、日期、数量及顺序逐行一致')
                finally:book.close()
                w.selected={w.report['heads'][0]['number']};w.controls();selected=export(0,True)
                book=openpyxl.load_workbook(selected,read_only=True,data_only=True)
                try:require(len([r for r in book['商品数量'].iter_rows(min_row=17,values_only=True) if r[4]=='ALL'])==1,'所选订单导出范围仍为单个仓单')
                finally:book.close()
                (out/'completed-files.json').write_text(json.dumps(files,ensure_ascii=False),'utf-8')
            elif mode=='full-detail':
                started=time.perf_counter();detail=export(1);report['full_detail_seconds']=time.perf_counter()-started
                book=openpyxl.load_workbook(detail,read_only=True,data_only=True)
                try:
                    sheet=book.active;sheet.reset_dimensions();actual=iter(sheet.iter_rows(min_row=7,values_only=True))
                    headers=next(actual);day_column=headers.index('订单日期');warehouse_column=headers.index('供货仓')
                    count=0
                    for count,expected in enumerate(select_detail_rows(w.report['rows']),1):
                        row=next(actual)
                        if (row[0],row[2],row[day_column],row[warehouse_column])!=(expected['order'],expected['sku'],expected['day'],expected['warehouse']):
                            raise AssertionError('整表逐行身份或顺序变化：'+str(count))
                    require(next(actual,None) is None,'细则导出范围内的SKU逐行核对，文件无额外行')
                    report['detail_rows']=count
                finally:book.close()
            else:
                old=json.loads((out/'completed-files.json').read_text('utf-8'))
                for path in old:
                    book=openpyxl.load_workbook(path,read_only=True);require(bool(book.sheetnames),'重开后原已导出文件可读');book.close()
                w.selected={w.report['heads'][0]['number']};w.controls();export(0,True)
            require(file_hash(win.work_repository.path)==before,'查询、失败、取消和导出均未修改正式业务库')
            require(not list(target_dir.glob('.analytics-*')),'所有私有分析暂存均已关闭移除')
            report.update(passed=True,resource_events=probe.events);capture(w,'final')
        except Exception:report['error']=traceback.format_exc()
        finally:
            exports.export=original_export;export_locations.save_file=original_save;timer.stop();probe.close()
            (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False;win._discard_close=True;win.close()
    QTimer.singleShot(350,execute)
