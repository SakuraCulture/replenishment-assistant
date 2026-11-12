"""Opt-in main-workbench automatic monthly first-fill acceptance."""
import json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    if not fixture.get('synthetic') or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Requires explicit isolated fixture')
    checks=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(predicate):
        deadline=time.monotonic()+45
        while not predicate():
            if time.monotonic()>deadline:raise TimeoutError('Isolated monthly calculation did not finish; process retained')
            app.processEvents();time.sleep(.01)
    def run():
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton
        from version_info import diagnostic
        import result_semantics as sem,new_products,normal_exports,normal_orders
        import openpyxl
        from new_product_ui import NewProductDialog
        report={};messages=[];timer=QTimer(win)
        def inform(parent,title,text,*a,**kw):messages.append((title,text));return QMessageBox.StandardButton.Ok
        try:
            require(win.work_repository.path.resolve()==out/'settings/workbench.sqlite3','只使用标记的隔离本机库')
            win.loaded_ok(fixture['loaded']);win.new_focus=fixture['monthly'];win.refresh_focus();win.exception.setChecked(False)
            widgets=win.store_widgets['S1'];widgets['mode'].setCurrentIndex(1)
            require(win.selected_settings('S1')['monthly_first_fill']['focus']['revision']==fixture['monthly']['revision'],'界面任务设置冻结当月关注清单')
            def calculate(mode=1,amount='0'):
                widgets['mode'].setCurrentIndex(mode);widgets['amount'].setText(amount)
                with patch.object(QMessageBox,'warning',side_effect=inform),patch.object(QMessageBox,'information',side_effect=inform):
                    win.start_calculation(['S1']);wait(lambda:win.worker is None)
                require(win.states['S1']['state']=='已完成','真实工作台计算按钮及子进程完成：'+str(mode)+'/'+amount)
                return win.results['S1']
            result=calculate();row=result['rows'][0]
            require(sem.order_quantity(row)==18 and row['AS']==0,'无需手工选品，原模型零建议转为本月首铺执行量18')
            require(row['_meta']['first_fill']['source_field']=='新店新品首铺数量','实际原表长列名被读取并可追溯')
            require(result['new_products']['automatic'] and 'S1' not in win.new_bases,'自动首铺使用紧凑证据，未保留第二份整店结果')
            require('自动首铺' in sem.explanation(row) and '人工选择' not in sem.explanation(row),'导出理由不把自动首铺写成人工选择')
            win.show_section(1);win.navigate(3);app.processEvents();win.grab().save(str(out/'01-monthly-auto-results.png'))
            order={'number':'BH-QA-S1-ZC','store':'S1','day':fixture['day'],'digest':normal_orders.fingerprint(result),'revision':1,'current_revision':1,'payload':{'result':result,'new_focus':fixture['monthly']}}
            exported=out/'exports/monthly-confirmation.xlsx';normal_exports.export(order,exported);book=openpyxl.load_workbook(exported,data_only=True)
            try:require(book['本月新品']['Q2'].value==18,'包内签发导出器的本月新品Q列为18，未手动选品')
            finally:book.close()
            result=calculate(2,'24');require(sem.order_quantity(result['rows'][0])==12,'自动首铺仍受预算24约束，安排12个库存单位')
            result=calculate(2,'0');require(sem.order_quantity(result['rows'][0])==0 and '预算' in sem.explanation(result['rows'][0]),'预算0时最终仍为0，并明确显示预算原因')
            result=calculate();win.current_store='S1';errors=[]
            def drive():
                dialog=QApplication.activeModalWidget()
                try:
                    if isinstance(dialog,NewProductDialog):
                        require(dialog.widgets[0][0].isChecked() and dialog.widgets[0][1].value()==18,'重开新品维护窗口带出已自动安排量18')
                        dialog.grab().save(str(out/'02-monthly-maintenance.png'));dialog.widgets[0][1].setValue(12);dialog.preview_btn.click()
                    elif dialog and dialog.objectName()=='newBudgetPreview':
                        next(b for b in dialog.findChildren(QPushButton) if b.text()=='应用到本店草稿').click()
                except Exception:
                    errors.append(traceback.format_exc());timer.stop()
                    if dialog:dialog.reject()
            timer.setInterval(30);timer.timeout.connect(drive);timer.start();win.choose_new_products(True)
            wait(lambda:win.worker is None);timer.stop()
            if errors:raise AssertionError(errors[0])
            require(sem.order_quantity(win.results['S1']['rows'][0])==12,'真实预览与应用后，人工改量12覆盖自动首铺量')
            require(not win.results['S1']['new_products'].get('automatic'),'人工结果保留为明确选择，未被自动规则再次覆盖')
            require(not messages,'计算过程无未处理报错弹窗')
            report={'passed':True,'checks':checks,'synthetic':True,'live_platform':False,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic()}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'synthetic':True,'live_platform':False,'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            timer.stop();(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n');win._discard_close=True;win.close()
    QTimer.singleShot(200,run)
