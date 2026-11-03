"""Opt-in real Qt/EXE acceptance. Explicit isolated synthetic database only."""
import copy,json,os,sys,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QMessageBox,QInputDialog
from PySide6.QtTest import QTest

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();checks=[];ticks=[];messages=[];dialogs=[]
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    timer=QTimer(win);timer.setInterval(25)
    def tick():
        ticks.append(time.monotonic());box=app.activeModalWidget()
        if isinstance(box,QInputDialog):box.accept();return
        if isinstance(box,QMessageBox):
            messages.append({'title':box.windowTitle(),'text':box.text()})
            yes=box.button(QMessageBox.StandardButton.Yes)
            if yes and box.windowTitle()=='确认生成聚水潭订单':yes.click()
            else:box.reject()
    timer.timeout.connect(tick)
    def wait(fn):
        deadline=time.monotonic()+45
        while not fn() and time.monotonic()<deadline:QTest.qWait(20)
        if not fn():raise AssertionError('等待界面任务超时')
    def require(ok,message):
        if not ok:raise AssertionError(message)
    def execute():
        timer.start()
        try:
            import jst_batches as batches,jst_orders as orders,jst_results as results,order_lifecycle as lc,warehouse_catalog as wc,work_repository as wr,export_settings,export_locations
            from jst_ui import GenerationDialog
            from jst_results_smoke import cells
            from lifecycle_home_ui import StageDialog
            path=win.work_repository.path
            require(path.resolve()==out/'settings/workbench.sqlite3','拒绝使用非隔离资料目录')
            folder=out/'exports';folder.mkdir(exist_ok=True);export_settings.save(export_locations.config_path(win),str(folder))
            heads=[h for h in lc.current_targets(path) if h['warehouse']=='河北仓'];require(len(heads)==2,'需要两店合成样本')
            d=GenerationDialog(win,win,list(reversed(heads)));dialogs.append(d);d.setModal(True);d.show();wait(lambda:bool(d.catalogs))
            import openpyxl
            source=out/'本批合成仓资料.xlsx';book=openpyxl.Workbook();sheet=book.active;fields=wc.FIELDS+['商品标签'];sheet.append(fields)
            item=wc.Repository(path.parent).catalogs()['河北仓']['items']['0001'];sheet.append([item.get(k) for k in fields]);book.save(source);book.close()
            with closing(sqlite3.connect(path)) as db:before=orders.state(db,'河北仓')
            d.warehouse.setCurrentText('河北仓');d.refresh_stock(str(source))
            with closing(sqlite3.connect(path)) as db:require(orders.state(db,'河北仓')==before,'准备资料提前改变全局库存')
            checks.append('实际导入槽读取合成Excel供本批选择；预览确认前不写全局库存')
            QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            require([p['summary']['quantity'] for p in d.plan['payload']['plans']]==['40','60'],'预览数量或默认顺序变化')
            original=batches.execute
            def slower(path,plan,accept_issues=False,progress=lambda _:None,cancel=None):
                def report(v):
                    progress(v)
                    if getattr(v,'business_event',None):time.sleep(.25)
                return original(path,plan,accept_issues,report,cancel)
            batches.execute=slower
            QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:len(d.saved)==1)
            QTest.mouseClick(d.stop_btn,Qt.MouseButton.LeftButton);wait(lambda:d.worker is None)
            batches.execute=original
            require(d.receipt and d.receipt['cancelled'] and len(d.saved)==1,'实际停止没有保留第一单并停止第二单')
            bid=d.batch_id;require(batches.listing(path)[0]['status']=='paused','停止未持久化')
            d.grab().save(str(out/'01-暂停批次.png'));d.close()
            checks.append('实际预览/确认/停止：按回传顺序预览40/60；第一单保存后停止，原批暂停且40件保留')
            # Updating future inventory cannot alter this batch or old files.
            c=wc.Repository(path.parent).catalogs()['河北仓'];new=copy.deepcopy(c);new['items']['0001']['可用数']=1000;new['revision']=wc.revision(new['items'])
            orders.commit_refresh(path,orders.prepare_refresh(path,'河北仓',catalog=new))
            d=GenerationDialog(win,win,heads,bid);dialogs.append(d);d.setModal(True);d.show();wait(lambda:bool(d.catalogs))
            require(not d.update_btn.isEnabled(),'继续原批仍可更换库存')
            QTest.mouseClick(d.preview_btn,Qt.MouseButton.LeftButton)
            require(len(d.plan['payload']['plans'])==1 and d.plan['payload']['plans'][0]['summary']['quantity']=='60','恢复读取了新库存')
            QTest.mouseClick(d.generate_btn,Qt.MouseButton.LeftButton);wait(lambda:d.worker is None and d.receipt is not None)
            require(not d.receipt['errors'] and len(d.saved)==1,'恢复生成失败');d.close()
            checks.append('关闭并重新进入原批，库存全局改为1000后仍补第二店60；原批资料控件禁用')
            win.followup.search.setText('第二店');win.followup.warehouse_filter.setCurrentText('河北仓')
            stage=StageDialog(win.followup,'jst');dialogs.append(stage);stage.setModal(True);stage.show();p=stage.panel
            wait(lambda:p.data is not None);require(p.search.text()=='第二店' and p.warehouse.currentText()=='河北仓','入口没有继承筛选')
            require(len(p.visible_records())==1 and p.all_btn.text()=='导出当前筛选（1份）','导出范围不明确')
            def export(btn):
                previous=getattr(p,'receipt',None);QTest.mouseClick(btn,Qt.MouseButton.LeftButton)
                wait(lambda:p.worker is None and getattr(p,'receipt',None) is not previous);return p.receipt
            first=export(p.all_btn);require(len(first['files'])==1 and not first['remaining'],'筛选导出失败')
            content=cells(first['files'][0]['path']);again=export(p.all_btn);require(cells(again['files'][0]['path'])==content,'重复导出变化')
            p.search.clear();require(p.all_btn.text()=='导出当前筛选（2份）','清空筛选份数不正确');both=export(p.all_btn)
            require(len(both['files'])==2,'全范围导出份数不符')
            p.search.setText('没有这个店');require(not p.all_btn.isEnabled() and not p.visible_records(),'空筛选仍导出隐藏订单')
            p.search.clear();QTest.mouseClick(p.batch_btn,Qt.MouseButton.LeftButton);wait(lambda:not p.reading)
            require(p.batch_id==bid,'实际批次选择没有绑定原批')
            p.grab().save(str(out/'02-批次结果.png'));require(not p.progress.isVisible(),'加载完成仍显示进度100%')
            checks.append('实际结果页spawn读取、继承门店/仓筛选；1份重复导出严格一致；清空筛选2份；空筛选禁用导出；读取完成收起进度')
            stage.close()
            # Real page close while export worker is active; retain saved facts.
            stage=StageDialog(win.followup,'jst');dialogs.append(stage);stage.setModal(True);stage.show();p=stage.panel;p.search.clear();wait(lambda:p.data is not None)
            original_export=results.export
            def slow_export(*a,**k):time.sleep(.2);return original_export(*a,**k)
            results.export=slow_export;QTest.mouseClick(p.all_btn,Qt.MouseButton.LeftButton);stage.reject();wait(lambda:p.worker is None and not stage.isVisible());results.export=original_export
            checks.append('导出运行时关闭页面：安全取消剩余文件，等待线程结束再关闭；已有生成单仍可再次导出')
            from version_info import diagnostic
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'messages':messages,'heartbeat_ticks':len(ticks),'max_tick_gap':max((b-a for a,b in zip(ticks,ticks[1:])),default=0),**diagnostic()}
        except Exception:report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'messages':messages,'error':traceback.format_exc()}
        finally:
            timer.stop()
            for d in reversed(dialogs):
                if hasattr(d,'panel'):d.panel.shutdown()
                if not getattr(d,'worker',None) and not getattr(d,'export_worker',None):d.close()
        (out/'batch-exe-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        win._discard_close=True;app.exit(0 if report['passed'] else 1)
    QTimer.singleShot(100,execute)
