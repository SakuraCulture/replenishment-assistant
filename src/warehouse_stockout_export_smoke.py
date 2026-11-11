"""Opt-in native export acceptance. Marked isolated data only; no platform calls."""
import json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer,QDate


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings'
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings or not json.loads((out/'synthetic.json').read_text('utf-8')).get('synthetic'):raise ValueError('Requires marked isolated settings')
    checks=[];entry=win.warehouse_stockout
    def require(value,message):
        if not value:raise AssertionError(message)
        checks.append(message)
    def wait(predicate):
        deadline=time.monotonic()+40
        while not predicate():
            if time.monotonic()>deadline:raise TimeoutError('Isolated export did not finish; no forced termination')
            app.processEvents();time.sleep(.01)
    def idle():return entry.worker is None and entry.queued is None
    def run():
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication,QFileDialog,QMessageBox
        from version_info import diagnostic
        from stockout_common import file_hash
        import openpyxl
        fixture=json.loads((out/'fixture.json').read_text('utf-8'));dialogs=[];messages=[];targets=[];proposed=[];report={}
        def choose(*a,**kw):proposed.append(a[2]);return str(targets[-1]),'Excel (*.xlsx)'
        def inform(parent,title,message,*a,**kw):messages.append((title,message));return QMessageBox.StandardButton.Ok
        try:
            require('warehouse_stockout' not in sys.modules,'未进入板块时未加载大仓分析能力')
            entry.enter();wait(idle);page=entry.page
            from warehouse_stockout_ui import ExportDialog,checked
            day=QDate.fromString(fixture['day'],'yyyy-MM-dd');page.start.setDate(day);page.end.setDate(day)
            page.filters={'warehouse':['山东仓'],'supplier':['测试供货一']};page.search.setText('测试商品');page.query();wait(idle)
            require('601' in page.total.text() and '50.08%' in page.total.text(),'搜索及仓、供应商筛选得到601款和301款缺货')
            win.grab().save(str(out/'warehouse-page.png'))
            analysis=settings/'analysis/warehouse_stockout/warehouse_stockout.sqlite3';before=file_hash(analysis);business=file_hash(win.work_repository.path)
            def launch(filename,configure,reject=False,cancel=False):
                targets.append(out/'exports'/filename);errors=[]
                def drive():
                    dialog=QApplication.activeModalWidget()
                    try:
                        require(isinstance(dialog,ExportDialog),'实际点击按钮打开大仓报表选择窗口')
                        configure(dialog);app.processEvents();area=dialog.screen().availableGeometry();frame=dialog.frameGeometry()
                        require(area.contains(frame),'导出窗口完整位于当前屏幕可用区域')
                        dialog.grab().save(str(out/('export-'+str(len(targets))+'.png')));dialogs.append(dialog.windowTitle())
                        dialog.reject() if reject else dialog.accept()
                    except Exception as exc:
                        errors.append(exc)
                        if dialog:dialog.reject()
                QTimer.singleShot(100,drive);page.export_btn.click()
                if errors:raise errors[0]
                if not reject:
                    require(entry.worker is not None and entry.worker.operation=='export','真实子进程承接导出')
                    require(not page.export_btn.isEnabled() and not page.tabs.isEnabled(),'导出时禁用重复点击及切换查询')
                    page.query();page.view();page.refresh_metadata();require(not entry.worker.cancel.is_set(),'刷新和查询不会静默取消正在导出的报表')
                    if cancel:entry.pause('隔离验收主动取消导出')
                    wait(idle)
                return targets[-1]
            with patch.object(QFileDialog,'getSaveFileName',side_effect=choose),patch.object(QMessageBox,'information',side_effect=inform):
                def select_all(dialog):
                    require(len(checked(dialog.listing))==1,'默认只选当前组合，未默认勾选全部大报表');dialog.check_all(True)
                target=launch('all-cuts.xlsx',select_all);require(target.is_file(),'原生导出完整切面及商品明细')
                book=openpyxl.load_workbook(target,data_only=True)
                try:
                    require(book['范围汇总']['B2'].value==601 and book['范围汇总']['C2'].value==301,'汇总分母分子与前端一致')
                    require(abs(book['范围汇总']['D2'].value-301/601)<1e-12 and book['范围汇总']['D2'].number_format=='0.00%','百分比为数值并显示两位百分比')
                    rows=list(book['商品明细1'].values);require(len(rows)==602,'601条明细全部导出，超过页面200行')
                    require(rows[1][2]=='00000000' and book['商品明细1']['C2'].data_type=='s','商品编码前导零按文本保留')
                    require({'供应商编码','供应商名称'}<=set(next(book['按供应商1'].values)),'供应商切面同时保留编码和名称')
                    require({'商品编码','商品名称'}<=set(next(book['大仓商品1'].values)),'商品切面同时保留编码和名称')
                    require('按类目1' in book.sheetnames and '每日趋势1' in book.sheetnames,'导出包含独立类目与每日趋势切面')
                    require(book['商品明细1'].auto_filter.ref and book['商品明细1'].freeze_panes,'报表支持Excel筛选和冻结表头')
                finally:book.close()
                require(Path(proposed[0]).parent==out/'exports','保存位置沿用全局导出配置')
                page.filters={'warehouse':['山东仓']};page.search.clear();page.query();wait(idle);page.tabs.setCurrentIndex(2);wait(idle);page.mode.setCurrentIndex(page.mode.findData('excluded'));wait(idle)
                require(page.tables[2].rowCount()==3,'页面已剔除明细为3款')
                def detail_only(dialog):require(not checked(dialog.listing) and dialog.mode.currentData()=='excluded','商品页默认仅导出当前明细范围')
                target=launch('excluded-only.xlsx',detail_only);book=openpyxl.load_workbook(target,data_only=True)
                try:
                    require(len(list(book['商品明细1'].values))==4,'明细单独导出且只含已剔除3款')
                    require(book['范围汇总']['D2'].value is None and book['范围汇总']['F2'].value==1,'明细切换不改汇总口径，待核缺货率仍留空')
                finally:book.close()
                cancelled=launch('cancel-dialog.xlsx',lambda _:None,reject=True);require(not cancelled.exists(),'取消选择窗口不创建导出文件')
                cancelled=launch('cancel-worker.xlsx',lambda _:None,cancel=True);require(not cancelled.exists(),'主动暂停导出不发布残缺Excel')
            require(not list((out/'exports').glob('.warehouse-*.xlsx')),'取消后清理本次临时文件')
            require(file_hash(analysis)==before and file_hash(win.work_repository.path)==business,'导出未改动大仓快照或补货业务库')
            require(len(messages)==2,'两次成功导出均明确反馈文件位置')
            report={'passed':True,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if entry.worker:entry.pause();wait(idle)
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            win.close()
    QTimer.singleShot(200,run)
