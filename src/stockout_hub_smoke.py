"""Opt-in source/frozen acceptance, marked synthetic files and no live RPC."""
import os,json,time,traceback,sys
from pathlib import Path
from PySide6.QtCore import QTimer,QDate
from PySide6.QtTest import QTest


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings'
    if Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings or not json.loads((out/'synthetic.json').read_text('utf-8')).get('synthetic'):raise ValueError('Requires marked isolated settings')
    def run():
        checks=[];diagnostics=[];root=win.stockout_hub;beats=[]
        timer=QTimer(win);timer.setInterval(20);timer.timeout.connect(lambda:beats.append(time.monotonic()));timer.start()
        def require(ok,text):
            if not ok:raise AssertionError(text)
            checks.append(text)
        def idle():return all(not e.worker and not e.queued for e in [root,*root.children.values()])
        def wait():
            end=time.monotonic()+90
            while not idle() and time.monotonic()<end:QTest.qWait(10)
            require(idle(),'工作进程安全结束并释放')
        def request(entry,op,values):
            results=[];entry.start(op,values,results.append);task=entry.worker;wait()
            if task and task.diagnostic:diagnostics.append(task.diagnostic)
            require(bool(results),'实际子进程完成 '+op+'：'+entry.message)
            return results[0]
        try:
            from unittest.mock import patch
            from stockout_task import Bridge
            from stockout_common import file_hash,today
            from stockout_hub_service import Journal,preview,create
            from stockout_views import defaults
            from warehouse_stockout_views import defaults as wd
            import openpyxl
            def forbidden(*a,**kw):raise AssertionError('真实平台调用禁止')
            with patch.object(Bridge,'request',forbidden):
                require(root.nav.text()=='缺货率' and not win.stockout.nav.isVisible() and not win.warehouse_stockout.nav.isVisible(),'只有一个缺货率导航入口')
                require(root.page is None and win.stockout.page is None and win.warehouse_stockout.page is None,'未进入时两个分析页面均未创建')
                win.warehouse_stockout.enter();wait();require(win.stockout.page is None,'直接进入大仓不创建门店页面')
                for wh in ('山东仓','河北仓'):request(win.warehouse_stockout,'ingest',{'path':str(out/'inputs'/(wh+'.xlsx')),'warehouse':wh})
                page=win.warehouse_stockout.page;day=QDate.fromString(today(),'yyyy-MM-dd');page.start.setDate(day);page.end.setDate(day);page.query();wait()
                require('50.00%' in page.total.text(),'大仓固定排除考核品后缺货率50%');win.grab().save(str(out/'warehouse-hub.png'))
                root.page.tabs.setCurrentIndex(0);wait();require(win.warehouse_stockout.page is None,'切换到门店释放大仓页面')
                fixture=json.loads((out/'fixture.json').read_text('utf-8'))
                store=request(win.stockout,'ingest',fixture)
                page=win.stockout.page;page.start.setDate(day);page.end.setDate(day);page.refresh();wait()
                require('50.00%' in page.total.text(),'门店默认范围固定排除考核品后缺货率50%');require(page.view.count()==2,'考核品不再是可取消的排除开关')
                require(len(root.page.entry.children)==2 and root.page.mode.count()==3,'保留两个子页及三种运行范围');win.grab().save(str(out/'store-hub.png'))
                journal=Journal(settings);row=create(journal,preview(settings,'store',[]));journal.change(row['id'],state='COMPLETE',store_run=store['id'],catalogs=store['catalogs'])
                request(root,'hub_reuse',{'run':row['id']});require(True,'使用原仓资料离线补算大仓')
                from PySide6.QtWidgets import QFileDialog,QDialog,QApplication
                target=out/'exports'/'combined.xlsx'
                def accept():
                    dialog=QApplication.activeModalWidget()
                    if isinstance(dialog,QDialog):
                        require(dialog.screen().availableGeometry().contains(dialog.frameGeometry()),'统一导出窗口位于可用屏幕内');dialog.grab().save(str(out/'hub-export.png'));dialog.accept()
                with patch.object(QFileDialog,'getSaveFileName',return_value=(str(target),'Excel (*.xlsx)')):
                    QTimer.singleShot(100,accept);root.page.export_btn.click();wait()
                require(target.is_file(),'真实统一导出按钮完成报表')
                book=openpyxl.load_workbook(target,read_only=True,data_only=True)
                try:
                    require(any(n.startswith('门店_') for n in book.sheetnames) and any(n.startswith('大仓_') for n in book.sheetnames),'两个指标分工作表导出')
                    require(book['大仓_范围汇总']['D2'].value==.5,'大仓导出比例保持数值类型且为50%')
                    require(book['门店_门店总览']['E2'].value==.5,'门店导出与前端50%一致')
                finally:book.close()
                require(all(not d['qt_loaded'] for d in diagnostics),'分析子进程不加载Qt');require(len(beats)>5,'分析期间主界面心跳继续')
                win.show_section(0);wait();require(root.page is None and all(c.page is None for c in root.children.values()),'离开缺货率释放两个页面')
            timer.stop();result={'passed':True,'checks':checks,'workers':diagnostics,'frozen_exe':bool(getattr(sys,'frozen',False)),'live_platform':False}
            from version_info import diagnostic
            result.update(diagnostic());(out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8');app.exit(0)
        except Exception as exc:
            timer.stop();(out/'report.json').write_text(json.dumps({'passed':False,'checks':checks,'error':str(exc),'traceback':traceback.format_exc()},ensure_ascii=False,indent=2),'utf-8');app.exit(1)
    QTimer.singleShot(100,run)
