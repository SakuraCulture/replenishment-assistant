"""Native repair acceptance, gated to a marked, isolated synthetic directory."""
import copy,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QFileDialog,QMessageBox,QPushButton


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();phase=args[1]
    if not (out/'synthetic.json').is_file() or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Marked synthetic settings required')
    if not json.loads((out/'synthetic.json').read_text('utf-8')).get('synthetic'):raise ValueError('Synthetic marker required')
    from version_info import diagnostic
    report={'passed':False,'phase':phase,'frozen_exe':bool(getattr(sys,'frozen',False)),'checks':[],
            'version':diagnostic(),'synthetic':True,'live_platform':False}
    def save():
        (out/(phase+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    def require(value,message):
        if not value:raise AssertionError(message)
        report['checks'].append(message)
    def wait(predicate,seconds=15):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:app.processEvents();QTest.qWait(20)
        if not predicate():raise TimeoutError('Isolated UI timeout')
    def finish():
        save();win._discard_close=True;win.job_dirty=False;win.close()
    def run():
        for name in ('jst_generator','aoxiang_workflows','browser_status'):
            owner=getattr(win,name,None)
            if owner and hasattr(owner,'timer'):owner.timer.stop()
        win.jst_generator.stopped=True
        try:
            from catalog_session_ui import manager
            session=manager(win).session;lease=session.acquire();lease.start();require(lease.close(),'释放窗口操作权并保留真实组件连接')
            if phase=='reopen':
                data=session.client.request('sync.status');require([b['id'] for b in data['batches']]==['SOURCE-TASK'],'重开只显示迁入来源任务，未混入目标旧任务')
                item=data['batches'][0]['items'][0];require(item['native']['nativeTaskRef']=='ORIGINAL-SOURCE-TASK' and item['native']['submissions']==1,'平台原任务号与一次提交事实保留')
                require(item['migration']['needs_binding'] and data['rebind_required']['山东仓'],'迁入任务暂停，等待本机明确核验')
                require(not data['accounts'][0]['enabled'] and 'target-local-profile' in data['accounts'][0]['binding']['actualProfilePath'],'本机浏览器配置保留，自动同步暂停')
                require(win.work_repository.stores()['A']['preset']['config']['parameters']['minimum_reference_units']==7,'迁入来源门店参数，未混合旧目标参数')
                require((out/'settings/订单迁入完成.json').is_file(),'应用退出链路已实际完成整体迁入')
                report['passed']=True;finish();return

            import order_analytics as analytics,order_analytics_exports as exports
            from order_analytics_ui import AnalyticsWidget
            from work_repository import today
            frozen=analytics.collect(win.work_repository.path,{'from':today(),'to':today()})
            require(frozen['metrics']['stages']['signed']['value'] is None,'全部缺价货值保留未知')
            require(frozen['metrics']['stages']['receipt']['skus'] is None,'未取得实收资料的SKU数保留未知')
            zero=copy.deepcopy(frozen['rows'])
            for r in zero:r['valid']['signed']=True;r['quantities']['signed']=0
            require(analytics.aggregate(zero)['stages']['signed']['value']==0,'已确认零数量仍显示零货值')
            screen=AnalyticsWidget(win);screen.show_report(frozen)
            require('未确认' in screen.funnel.item(0,2).text(),'真实Qt五段表显示未确认')
            screen.grab().save(str(out/'unknown-values.png'));screen.deleteLater()
            exports.export(win.work_repository.path,frozen,out/'native-analytics.xlsx')
            import openpyxl
            from funnel_dimension_export import OMITTED_HEADERS
            book=openpyxl.load_workbook(out/'native-analytics.xlsx',read_only=True,data_only=True)
            try:require(all(OMITTED_HEADERS.isdisjoint(next(s.values,())) for s in book),'EXE原生导出不包含四个冗余字段')
            finally:book.close()

            import store_parameter_ui as sui,store_parameter_import as spi,parameter_snapshot
            from parameter_workspace_smoke import fixture
            from import_ui import ImportDialog
            from parameter_scene import ParameterScene
            win.loaded_ok(fixture());current=copy.deepcopy(win.config_for('A'));scene=ParameterScene(win)
            old_open=QFileDialog.getOpenFileNames
            ticks=QTimer();ticks.setInterval(20);handled=set();errors=[]
            def advance():
                dialog=QApplication.activeModalWidget()
                if not dialog or id(dialog) in handled:return
                if isinstance(dialog,ImportDialog):handled.add(id(dialog));dialog.set_paths([str(out/'changed-parameters.xlsx')]);dialog.go.click()
                elif isinstance(dialog,sui.Preview):
                    handled.add(id(dialog));dialog.scope.setCurrentIndex(dialog.scope.findData('defaults'))
                    if not dialog.go.isEnabled():errors.append(dialog.error.text());dialog.reject()
                    else:dialog.grab().save(str(out/'complete-parameters.png'));dialog.go.click()
                elif isinstance(dialog,QMessageBox):
                    handled.add(id(dialog))
                    if dialog.icon()==QMessageBox.Icon.Warning:errors.append(dialog.text())
                    dialog.accept()
            ticks.timeout.connect(advance);ticks.start()
            try:
                button=next(b for b in scene.findChildren(QPushButton) if b.text()=='导入完整门店参数');button.click()
            finally:ticks.stop();QFileDialog.getOpenFileNames=old_open;scene.deleteLater()
            require(not errors,'实际完整参数按钮、文件读取、差异预览及保存完成：'+';'.join(errors))
            require(win.work_repository.stores()['A']['preset']['config']['parameters']['minimum_reference_units']==3,'默认参数实际保存为3')
            require(win.config_for('A')==current,'仅保存默认不偷偷改变本次参数')
            parameter_snapshot.export(win.work_repository,out/'native-all-parameters.xlsx')
            data=spi.read([out/'native-all-parameters.xlsx']);plan=spi.plan(data,win.work_repository.stores())
            require(not plan['errors'] and not plan['changed'],'EXE完整参数导出可原样回导且无虚假变化')
            require(bool(data['notices']),'映射和辅助页不写回的提示保留')

            import order_migration_ui as migration_ui
            component=session.client;messages=[];old_info=QMessageBox.information;old_warning=QMessageBox.warning
            old_question=QMessageBox.question;old_save=QFileDialog.getSaveFileName;old_load=QFileDialog.getOpenFileName
            def information(parent,title,text,*rest):
                messages.append({'title':title,'text':text})
                if title=='订单已迁入':
                    report['checks'].append('已确认迁入计划跨过真实异步安全退出，并在app主入口完成替换');report['passed']=True;report['messages']=messages;save()
                return QMessageBox.StandardButton.Ok
            def warning(parent,title,text,*rest):
                report['error']=title+': '+text;report['passed']=False;save();return QMessageBox.StandardButton.Ok
            QMessageBox.information=information;QMessageBox.warning=warning
            QMessageBox.question=lambda *a:QMessageBox.StandardButton.Yes
            QFileDialog.getSaveFileName=lambda *a:(str(out/'live-component-export.zip'),'Zip (*.zip)')
            QFileDialog.getOpenFileName=lambda *a:(str(out/'source-orders.zip'),'Zip (*.zip)')
            def drive(text,close_after=False):
                timer=QTimer();timer.setInterval(25);clicked=[]
                def click():
                    for d in QApplication.topLevelWidgets():
                        if isinstance(d,QDialog) and d.windowTitle()=='完整订单迁移与参数导出' and not clicked:
                            button=next(b for b in d.findChildren(QPushButton) if b.text()==text);clicked.append(True);button.click();timer.stop()
                            if close_after:d.reject()
                            return
                timer.timeout.connect(click);timer.start()
                try:migration_ui.open_migration(win,win)
                finally:timer.stop()
                require(bool(clicked),'实际点击'+text)
            drive('导出完整订单迁移包…',True)
            require((out/'live-component-export.zip').is_file() and component.running,'保留组件写锁时通过原服务快照导出完整包')
            drive('校验并导入完整订单迁移包…')
            require(bool(getattr(win,'_order_import_request',None)),'等待安全退出时迁入请求仍被保留')
            require(not (out/'settings/订单迁入完成.json').exists(),'窗口尚未关闭时不提前替换资料')
            save()
            # Keep the information interceptor for app.py's post-event-loop
            # restore. It records success only after the real apply returns.
        except Exception:
            report['error']=traceback.format_exc();report['passed']=False;finish()
    QTimer.singleShot(250,run)
