"""Opt-in real Qt/EXE summary export acceptance, isolated fixtures only."""
import hashlib,json,sqlite3,sys,time,traceback
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog
import openpyxl,export_locations
import lifecycle_comparison_ui as ui
from version_info import diagnostic

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();checks=[];screens=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn):
        end=time.monotonic()+30
        while not fn() and time.monotonic()<end:
            loop=QEventLoop();QTimer.singleShot(15,loop.quit);loop.exec()
        require(fn(),'Background query completed')
    def facts():
        with closing(sqlite3.connect(win.work_repository.path)) as db:return {t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('normal_orders','normal_revisions','lifecycle_versions','lifecycle_events')}
    def export(widget,level,choice,cancel=False):
        target=out/(level+'-'+choice+'-'+str(len(checks))+('-cancel' if cancel else '')+'.xlsx');seen=[];errors=[];timer=QTimer();timer.setInterval(50);start=time.monotonic();save=export_locations.save_file
        export_locations.save_file=lambda *a,**k:(str(target),'Excel (*.xlsx)')
        def drive():
            d=QApplication.activeModalWidget()
            if d is None:return
            if time.monotonic()-start>30:errors.append('dialog timeout');d.reject();return
            if isinstance(d,ui.Scope) and id(d) not in seen:
                seen.append(id(d));d.scope.setCurrentIndex(d.scope.findData(choice));require(d.scope.currentData()==choice,'Exact selected/filtered scope offered')
                name='scope-'+level+'-'+choice+'.png';d.grab().save(str(out/name));screens.append(name)
                d.reject() if cancel else d.accept()
            elif d.windowTitle()=='对比报表已导出':d.accept()
        timer.timeout.connect(drive);timer.start()
        try:(widget.order_compare_btn if level=='order' else widget.store_compare_btn).click()
        finally:timer.stop();export_locations.save_file=save
        if errors:raise AssertionError(errors)
        require(bool(seen),'Actual export button opens scope dialog')
        require(target.exists()!=cancel,'Cancel leaves no file; confirmed export creates workbook')
        return target
    def execute():
        ticks=[];beat=QTimer();beat.setInterval(25);beat.timeout.connect(lambda:ticks.append(time.monotonic()));beat.start();report={}
        try:
            win.jst_generator.timer.stop();win.jst_generator.stopped=True;win.aoxiang_workflows.timer.stop();win.show_section(3);w=win.followup
            w.start.clear();w.end.clear();w.query();wait(lambda:w.table.isEnabled() and getattr(w,'count',0)==4 and getattr(w,'applied_request',None))
            require(w.order_compare_btn.isEnabled() and w.store_compare_btn.isEnabled(),'Both report actions available without page-by-page selection')
            before=facts();export(w,'order','filtered',True);path=export(w,'order','filtered')
            b=openpyxl.load_workbook(path);require(b.sheetnames==['订单级对比','差异商品明细','实际付款金额','统计口径'],'Actual order workbook structure');require(b['订单级对比'].max_row==5,'All four queried warehouse orders exported');require(b['实际付款金额'].max_row==2,'Only imported actual payment appears');b.close()
            w.table.item(0,0).setCheckState(Qt.CheckState.Checked);path=export(w,'store','selected')
            b=openpyxl.load_workbook(path);require(b.sheetnames[:3]==['门店级对比','门店分仓对比','订单级对比'],'Store summary retains warehouse and order drilldown');require(b['订单级对比'].max_row==2 and b['门店级对比'].max_row==2,'Store report uses selected one order, not all same-store history');b.close()
            w.warehouse_filter.setCurrentText('山东仓');wait(lambda:w.table.isEnabled() and w.count==2 and not w.checked)
            path=export(w,'order','filtered');b=openpyxl.load_workbook(path);require([r[3] for r in list(b['订单级对比'].values)[1:]]==['山东仓','山东仓'],'Full filtered export respects warehouse and clears prior selection');b.close()
            name='tracking-actions.png';w.grab().save(str(out/name));screens.append(name)
            require(facts()==before,'Signed and lifecycle business tables unchanged');require(len(ticks)>5,'Qt responds during query/export')
            report={'passed':True,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':checks,'heartbeat_ticks':len(ticks),'screenshots':screens,**diagnostic()}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),**diagnostic()}
        finally:
            beat.stop()
            from history_tasks import HistoryReader
            for reader in win.findChildren(HistoryReader):reader.shutdown()
            win.aoxiang_workflows.shutdown();win.stop_filtered_summary();(out/'comparison-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');app.exit(0 if report.get('passed') else 1)
    QTimer.singleShot(200,execute)
