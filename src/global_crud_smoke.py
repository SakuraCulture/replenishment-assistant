"""Actual windows and workers against the opt-in driver's synthetic fixture."""
import json,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QApplication,QPushButton,QComboBox,QDialogButtonBox

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();phase=args[1];checks=[];dialogs=[]
    def require(value,message):
        if not value:raise AssertionError(message)
        checks.append(message)
    def wait(fn):
        loop=QEventLoop();timer=QTimer();start=time.monotonic()
        timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>40 else None);timer.start(20);loop.exec();timer.stop();require(fn(),'后台作业安全结束')
    def drive(call,handler):
        errors=[];timer=QTimer();timer.setInterval(25)
        def step():
            try:handler(QApplication.activeModalWidget())
            except Exception:
                errors.append(traceback.format_exc());timer.stop();d=QApplication.activeModalWidget()
                if d:d.reject()
        timer.timeout.connect(step);timer.start()
        try:call()
        finally:timer.stop()
        require(not errors,'实际窗口交互完成：'+str(errors))
    def run():
        try:
            import order_lifecycle as lc,payment_shop_names_ui as names,settings_maintenance_ui as maintenance
            import stockout_views as views
            from stockout_repository import Repository
            from version_info import diagnostic
            from lifecycle_batch_ui import BatchImport
            from lifecycle_ui import Document
            from payment_matching import readonly
            data=json.loads((out/'fixture.json').read_text('utf-8'));path=Path(data['path']);head=data['head']
            require(win.work_repository.path.resolve()==path.resolve(),'仅打开新建合成资料目录')
            win.jst_generator.stopped=True;wait(lambda:win.jst_generator.worker is None)
            if phase=='exercise':
                editor=names.ShopNames(win,win);dialogs.append(editor);editor.show()
                require('NEWONLY' in editor.codes,'实际名称维护窗口显示仅存在于目录的新门店');editor.close();dialogs.remove(editor);editor.deleteLater()
                def import_file(file,stage):
                    d=BatchImport(win,win,stage);dialogs.append(d);d.show();d.add_paths([str(file)])
                    require(d.read_ready and bool(d.documents),'实际导入读取成功：'+stage)
                    d.preview_btn.click();require(d.pages.currentIndex()>0,'实际导入进入核对页：'+stage)
                    d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.save.click();require(bool(d.receipt),'实际确认保存成功：'+stage)
                    receipt=d.receipt;d.close();dialogs.remove(d);d.deleteLater();return receipt
                import_file(out/'原回传.xlsx','return');import_file(out/'原回传.xlsx','operations')
                with closing(readonly(path)) as db:prior=lc.current(db,head,'return');ops=lc.current(db,head,'operations')
                saved=import_file(out/'附加验算列.xlsx','return');require(all(not r['changed'] and r['version']==1 for r in saved),'新增验算列和尾部行不新增业务版本')
                with closing(readonly(path)) as db:require(lc.current(db,head,'return')==prior and lc.current(db,head,'operations')==ops,'实际保存保持原版本及运营依据')
                d=Document(win,win,head,'return');dialogs.append(d);d.show();require(d.files.count()==2,'实际版本窗口同时显示两份原件');d.grab().save(str(out/'原版本与新增原件.png'));d.close();dialogs.remove(d);d.deleteLater()
                def prepare_payment(file,scope):
                    d=BatchImport(win,win,'payment');dialogs.append(d);d.show();d.add_paths([str(file)])
                    index=d.source_company.findData(scope);require(index>=0,'实际界面可选择合成付款来源企业')
                    d.source_company.setCurrentIndex(index);d.preview_btn.click();require(d.pages.currentIndex()>0,'同名店付款在来源企业范围内正确进入核对')
                    d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);return d
                a=prepare_payment(out/'企业甲付款.xlsx','100');b=prepare_payment(out/'企业乙付款.xlsx','200')
                b.review.save.click();require(bool(b.receipt),'企业乙的同名店付款已实际保存')
                a.review.save.click();require(bool(a.receipt),'企业甲旧预览不因另一企业同名店保存而误冲突')
                for d in (a,b):d.close();dialogs.remove(d);d.deleteLater()
                state=[0]
                def history_step(d):
                    if d is None or d.windowTitle()!='默认设置变更记录':return
                    combo=d.findChild(QComboBox);buttons={b.text() or b.accessibleName():b for b in d.findChildren(QPushButton)}
                    if state[0]==0:
                        require(combo.count()==50,'实际历史窗口首次仅加载50条');state[0]=1;buttons['下一页'].click()
                    elif state[0]==1:
                        require(0<combo.count()<50,'实际历史下一页可达且未重复全量加载');state[0]=2;buttons['上一页'].click()
                    else:
                        require(combo.count()==50,'实际历史返回上一页正常');d.grab().save(str(out/'参数历史分页.png'));buttons['关闭'].click()
                drive(lambda:maintenance.open_history(win,win),history_step)
                from unittest.mock import patch
                import export_locations,openpyxl
                analytics=win.analytics
                analytics.start.setText(head['day']);analytics.end.setText(head['day'])
                analytics.query_btn.click();wait(lambda:analytics.worker is None)
                require(analytics.report is not None,'真实经营分析查询完成')
                analytics.tabs.setCurrentWidget(analytics.workflow);analytics.workflow.grain.setCurrentIndex(2);analytics.workflow.render()
                export_button=next(b for b in analytics.workflow.findChildren(QPushButton) if '导出' in (b.text() or b.accessibleName()))
                target=out/'趋势页实际导出.xlsx';notices=[]
                def export_notice(d):
                    if d is not None and d.windowTitle()=='分析报表已导出':
                        notices.append(True)
                        close=next(b for b in d.findChildren(QPushButton) if '关闭' in (b.text() or b.accessibleName()));close.click()
                def export_wait():
                    with patch.object(export_locations,'save_file',return_value=(str(target),'Excel (*.xlsx)')):export_button.click()
                    wait(lambda:analytics.worker is None)
                drive(export_wait,export_notice)
                require(target.is_file() and bool(notices) and analytics.export_kind.count()==2,'实际趋势按钮导出成功，主菜单仍为两种模板')
                book=openpyxl.load_workbook(target,read_only=True);require(bool(book.sheetnames),'实际趋势导出工作簿可打开');book.close()
            hub=win.stockout_hub;hub.enter_kind('store');entries=[hub,*hub.children.values()]
            wait(lambda:not any(e.worker or e.queued for e in entries));child=win.stockout
            repo=Repository(path.parent)
            if phase=='exercise':
                def edit_view(name):
                    def answer(d):
                        if d is not None and d.windowTitle()=='自定义切面组合':
                            d.name.setText(name);d.findChild(QDialogButtonBox).button(QDialogButtonBox.StandardButton.Ok).click()
                    drive(lambda:child.page.manage_views_btn.click(),answer)
                win.worker=object()
                try:
                    edit_view('队列保存验收');child.page.refresh()
                    require(child.queued and child.queued[0]=='save_views' and len(child._requests)>=2,'实际按钮保存排队后，查询未覆盖保存')
                    require(hub.page.stop.isEnabled(),'实际停止按钮可取消排队请求')
                    hub.page.grab().save(str(out/'保存与查询依次排队.png'))
                finally:win.worker=None
                wait(lambda:not any(e.worker or e.queued for e in entries))
                require(views.load(repo)['combinations'][0]['name']=='队列保存验收','真实工作进程已保存排队中的视图')
                win.worker=object()
                try:
                    child.page.adopt.click()
                    require(child.queued and child.queued[0]=='choices' and child.worker is None,'真实选择门店保留平台占用门禁，未启动配置读取')
                    hub.page.stop.click();require(child.queued is None,'可取消尚未执行的配置读取')
                    child.page.refresh();wait(lambda:child.worker is None and child.queued is None)
                    require(not child.page.last_error if hasattr(child.page,'last_error') else True,'主流程忙碌时真实本地查询可完成')
                    edit_view('不应保存的取消项');require(child.queued is not None,'取消验收已排队')
                    hub.page.stop.click();require(child.queued is None and '未保存' in child.message,'实际停止明确取消尚未执行的保存')
                finally:win.worker=None
            else:
                require(views.load(repo)['combinations'][0]['name']=='队列保存验收','退出重开后已保存视图保留，取消项未保存')
                docs=lc.history(path,head,'return');require(len(docs)==1 and len(docs[0]['attachments'])==2,'重开后业务版本和新增原件均保留')
                win.grab().save(str(out/'退出重开.png'))
            require(not win.browser_startup.started,'未启动浏览器或调用真实平台')
            result={'passed':True,'phase':phase,'checks':checks,'live_platform':False,**diagnostic()}
        except Exception:
            win.worker=None
            for i,d in enumerate(dialogs):d.grab().save(str(out/f'失败窗口-{i}.png'))
            result={'passed':False,'phase':phase,'checks':checks,'error':traceback.format_exc(),'live_platform':False}
        (out/f'report-{phase}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        for d in dialogs:d.hide();d.deleteLater()
        if result['passed']:win.close()
        else:app.exit(1)
    QTimer.singleShot(350,run)
