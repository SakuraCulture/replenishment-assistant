"""Opt-in real Qt acceptance, synthetic database and no platform connection."""
import json,time,traceback
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QPushButton,QMessageBox,QFileDialog

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();phase=args[1] if len(args)>1 else 'exercise';checks=[];dialogs=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn):
        loop=QEventLoop();timer=QTimer();started=time.monotonic();timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-started>35 else None);timer.start(20);loop.exec();timer.stop()
        require(bool(fn()),'后台任务正常结束')
    def accept_names(call):
        timer=QTimer();timer.setInterval(25);seen=[]
        def answer():
            d=QApplication.activeModalWidget()
            if d is not None and d.objectName() in ('shopNamesReview','shopNamesImport'):
                if d.objectName()=='shopNamesImport':
                    states=[d.table.item(i,4).text() for i in range(d.table.rowCount())]
                    require(all(any(word in state for state in states) for word in ('新增','保持','待处理')),'真实回导页显示新增、保持和待处理行，不静默丢行')
                name='confirmShopNames' if d.objectName()=='shopNamesReview' else 'previewNamesImport'
                seen.append(d.objectName());d.findChild(QPushButton,name).click()
        timer.timeout.connect(answer);timer.start()
        try:call()
        finally:timer.stop()
        require('shopNamesReview' in seen,'真实变更预览与确认控件已执行')
    def execute():
        try:
            import payment_shop_names as names,payment_matching as pm,payment_shop_names_ui as ui,order_lifecycle as lc
            from lifecycle_batch_ui import BatchImport
            from version_info import diagnostic
            path=win.work_repository.path;require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'只打开本轮合成数据库')
            wait(lambda:win.jst_generator.worker is None);win.jst_generator.stopped=True
            fixture=json.loads((out/'shop-fixture.json').read_text('utf-8'));head=fixture['head']
            def payment(file,*,manual=False,save=True):
                d=BatchImport(win,win,'payment');dialogs.append(d);d.show();d.add_paths([str(file)])
                require(d.read_ready and len(d.documents)==1,'真实付款导入完整读取')
                if manual:
                    require(d.targets[0].currentData() is None and d.targets[0].count()==3,'两仓同名只列两张相关候选，不默认选单')
                    combo=d.targets[0];combo.setCurrentIndex(next(i for i in range(combo.count()) if combo.itemData(i) and combo.itemData(i)['number']==head['number']))
                else:require(d.targets[0].currentData()['number']==head['number'],'实际界面自动识别正确仓单')
                d.preview_btn.click();require(d.pages.currentIndex()>0,'真实付款进入核对页')
                d.grab().save(str(out/(phase+('-多仓点选.png' if manual else '-付款核对.png'))))
                if save:
                    d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.save.click();require(bool(d.receipt),'真实确认保存完成')
                else:d.reject();require(not d.receipt,'取消付款预览不保存关联')
                d.close();d.deleteLater();dialogs.remove(d)
                return d
            if phase=='exercise':
                require(not names.load(path)['records'],'初始没有名称配置')
                payment(out/'付款合成.xlsx')
                editor=ui.ShopNames(win,win);dialogs.append(editor);editor.show();require(editor.data is not None,'真实独立维护窗口可读取')
                i=editor.codes.index(head['store']);j=3 if head['warehouse']=='山东仓' else 4
                require(editor.table.item(i,2).text()==head['name'],'只有历史订单时仍显示应用门店名称')
                editor.table.item(i,j).setText('隔离独立仓店名');accept_names(editor.save_btn.click)
                require(editor.receipt and editor.receipt['changed']==1,'单店只修改一仓名称：'+editor.note.text())
                editor.grab().save(str(out/'独立名称维护.png'))
                exported=out/'专用名称回导.xlsx'
                with patch.object(QFileDialog,'getSaveFileName',return_value=(str(exported),'Excel (*.xlsx)')):editor.export(False)
                require(exported.is_file(),'实际导出专用模板成功，业务导出服务未替换')
                # Only file-picker selection is supplied by the isolated driver.
                import openpyxl
                w=openpyxl.load_workbook(exported);s=w.active
                for row in range(2,s.max_row+1):
                    if s.cell(row,1).value==head['store']:s.cell(row,4 if j==3 else 3,'隔离另一仓店名')
                s.append(['UNKNOWN','未知店','不应保存']);w.save(exported);w.close()
                accept_names(lambda:editor.import_path(exported))
                require(len(names.load(path)['records'])==2,'批量回导只保存有效选定行；未知店未创建')
                # Cancel a real name preview; no QMessageBox or save service stub.
                before=names.load(path)['records'];editor.table.item(i,j).setText('取消的名称')
                timer=QTimer();timer.setInterval(25)
                def reject_preview():
                    d=QApplication.activeModalWidget()
                    if d is not None and d.objectName()=='shopNamesReview':d.reject();timer.stop()
                timer.timeout.connect(reject_preview);timer.start();editor.save_btn.click();timer.stop()
                require(names.load(path)['records']==before,'取消名称预览保留原值')
                # Restore editing text before normal close; no unsaved-close prompt.
                editor.table.item(i,j).setText('隔离独立仓店名');editor.close();editor.deleteLater();dialogs.remove(editor)
                payment(out/'唯一名称差异付款.xlsx',save=False)
                payment(out/'唯一名称差异付款.xlsx')
                payment(out/'唯一名称差异付款.xlsx')
                # Set equal names by real editor controls and confirm once.
                editor=ui.ShopNames(win,win);dialogs.append(editor);editor.show();i=editor.codes.index(head['store'])
                for c in (3,4):editor.table.item(i,c).setText('隔离两仓同名')
                accept_names(editor.save_btn.click);editor.close();editor.deleteLater();dialogs.remove(editor)
                payment(out/'两仓同名付款.xlsx',manual=True)
                request=pm.analyze(path,__import__('lifecycle_batch').read([out/'两仓新单.xlsx'],'payment')['documents'][0],pm.build(path))
                require(not request['automatic'] and len(request['candidates'])==2,'下一张同名付款保留两仓歧义，未永久记成一仓')
            elif phase=='reopen':
                editor=ui.ShopNames(win,win);dialogs.append(editor);editor.show();values=names.load(path)['records']
                require(len(values)==2 and all(v['name']=='隔离两仓同名' for v in values.values()),'真实EXE退出重开后两仓名称完整保留')
                editor.grab().save(str(out/'退出重开名称仍保留.png'));editor.close();editor.deleteLater();dialogs.remove(editor)
                payment(out/'两仓同名付款.xlsx')
            else:raise ValueError('Unknown synthetic phase')
            require(not win.browser_startup.started,'验收未启动浏览器或真实平台')
            result={'passed':True,'phase':phase,'checks':checks,'live_platform':False,'file_picker_only_supplied':True,**diagnostic()}
        except Exception:
            from version_info import diagnostic
            for i,d in enumerate(dialogs):d.grab().save(str(out/(f'失败界面-{i}.png')))
            result={'passed':False,'phase':phase,'checks':checks,'error':traceback.format_exc(),'live_platform':False,**diagnostic()}
        (out/('report-'+phase+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        for d in dialogs:
            d.hide();d.deleteLater()
        if result['passed']:win.close()
        else:app.exit(1)
    QTimer.singleShot(350,execute)
