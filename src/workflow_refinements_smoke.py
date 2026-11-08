"""Isolated native allocation, locking feedback and whole-batch import review."""
import json,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QEventLoop,QModelIndex,Qt
from PySide6.QtWidgets import QMessageBox
from PySide6.QtTest import QTest
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]);checks=[];geometry=[];state={};opened=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn):
        loop=QEventLoop();poll=QTimer();start=time.monotonic()
        poll.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>90 else None)
        poll.start(20);loop.exec();poll.stop()
        if not fn():raise AssertionError('隔离作业等待超时')
    def modal():
        d=app.activeModalWidget()
        if isinstance(d,QMessageBox):
            if d.icon()==QMessageBox.Icon.Question:d.button(QMessageBox.StandardButton.Yes).click()
            else:
                if d.windowTitle()!='请选择已生成单':state['error']=d.text()+' '+d.informativeText()
                d.accept()
    driver=QTimer(win);driver.setInterval(30);driver.timeout.connect(modal)
    def execute():
        result={}
        try:
            import jst_orders as orders,jst_exports,order_lifecycle as lc
            from jst_ui import GenerationDialog
            from lifecycle_batch_ui import BatchImport
            path=win.work_repository.path
            require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'只操作新目录的隔离数据库')
            wait(lambda:win.jst_generator.worker is None);win.jst_generator.stopped=True
            with closing(sqlite3.connect(path)) as db:gids=[r[0] for r in db.execute('SELECT gid FROM jst_active')]
            require(len(gids)==4,'两店四仓单夹具已生成')
            orders.cancel_generated(path,gids,'隔离验收重新检查生成界面')
            heads=list(reversed(lc.current_targets(path)));driver.start()
            d=GenerationDialog(win,win,heads);opened.append(d);d.show();wait(lambda:len(d.catalogs)==2)
            d.preview_btn.click();require(d.plan is not None,'实际新批预览成功')
            expected=[p['head']['number'] for p in d.plan['payload']['plans']]
            numbers=lambda:[d.order_list.item(i).data(Qt.ItemDataRole.UserRole)['number'] for i in range(d.order_list.count())]
            require(numbers()==expected and d.order_mode.currentIndex()==0,'回传时间预览结果与上方可见执行顺序一致')
            require(d.order_list.model().moveRows(QModelIndex(),0,1,QModelIndex(),d.order_list.count()),'真实Qt列表完成移动')
            manual=numbers();require(d.order_mode.currentIndex()==1 and d.plan is None,'拖动自动采用人工顺序并使旧预览失效，无需再勾选')
            d.preview_btn.click();require([p['head']['number'] for p in d.plan['payload']['plans']]==manual,'人工预览沿用上方顺序')
            d.restore_order_btn.click();require(numbers()==expected and d.order_mode.currentIndex()==0,'恢复回传顺序并重新预览，不保存生成')
            d.accept_issues.setChecked(True);d.generate_btn.click();wait(lambda:d.worker is None)
            if state.get('error') or d.completed.count()!=4:raise AssertionError(f'生成未完成：{state.get("error")}；{d.completed.count()}单；{d.status.text()}；{d.receipt}')
            require(True,'实际确认生成四单并安全完成提交')
            require(not d.lock_saved_btn.isEnabled() and '已选择0单' in d.completed_note.text(),'完成后无选择时锁单按钮禁用并显示0单')
            d.lock_saved();require(not state.get('error'),'无选择直接调用锁单会显示操作指引')
            d.completed.item(0).setSelected(True);gid=d.gids()[0];d.lock_saved_btn.click()
            with closing(sqlite3.connect(path)) as db:locked=[r[0] for r in db.execute('SELECT gid FROM jst_locks')]
            require(locked==[gid],'实际锁单仅作用于明确选择的一单')
            d.select_completed_btn.click();require(len(d.gids())==4 and '4单' in d.lock_saved_btn.text(),'全选已生成单及锁单范围数量可见')
            require(not d.order_mode.isEnabled() and not d.order_list.isEnabled(),'保存后原批顺序冻结，不能重新拖动改序')
            d.grab().save(str(out/'generation.png'));gids=list(d.saved);d.reject()
            require(all(not lc.snapshot(path,h)['present']['payment'] for h in heads),'生成及锁单不自动确认付款')
            files=jst_exports.export(path,gids,out/'synthetic-payment-files')['files']
            b=BatchImport(win,win,'payment',scope=heads);opened.append(b);b.show();b.add_paths([f['path'] for f in files]);b.preview()
            require(b.review is not None,'实际批量文件导入进入新核对布局')
            r=b.review;root=r.scope_tree.topLevelItem(0);r.scope_tree.setCurrentItem(root.child(0).child(0))
            require(r.model.rowCount()==1 and len(b.plan['plans'])==4 and '本批全部 4 份' in r.view_scope.text(),'查看一单时仍明确保存整批四单，未改确认范围')
            require(not r.save.isEnabled(),'未确认完整读取范围不能保存')
            for width,height in [(1366,900),(1280,800)]:
                b.resize(width,height);QTest.qWait(80)
                g={'window':[b.width(),b.height()],'review_height':r.height(),'table_height':r.table.height(),'viewport_height':r.table.viewport().height()};geometry.append(g)
                require(g['table_height']>=r.height()*.5 and g['viewport_height']>=200,f'原生窗口{width}x{height}商品明细占核对区至少一半且可读')
                b.grab().save(str(out/f'import-{width}.png'))
            for tab in range(r.review_tabs.count()):r.review_tabs.setCurrentIndex(tab);app.processEvents()
            r.review_tabs.setCurrentIndex(0);r.complete.setChecked(True);r.extra_ack.setChecked(True);r.save.click()
            require(not state.get('error') and len(b.receipt)==4,'左侧仅查看一单后确认仍原子保存本批四单')
            require(all(lc.snapshot(path,h)['present']['payment'] for h in heads),'四张付款单均已正确关联各自仓单')
            result={'passed':True,**diagnostic(),'checks':checks,'geometry':geometry,'synthetic':True,'live_platform':False}
        except Exception:result={'passed':False,**diagnostic(),'checks':checks,'geometry':geometry,'error':traceback.format_exc()}
        finally:
            driver.stop()
            for d in reversed(opened):d.reject();d.deleteLater()
            (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if result['passed'] else 1))
    QTimer.singleShot(200,execute)
