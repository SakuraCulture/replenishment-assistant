"""Isolated real Qt buttons, cancellation, selected and filtered payment review."""
import json,time,traceback,sqlite3
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QMessageBox
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]);checks=[];state={'action':'cancel','reviews':[]};report={}
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(fn):
        loop=QEventLoop();poll=QTimer();started=time.monotonic()
        poll.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-started>90 else None)
        poll.start(20);loop.exec();poll.stop()
        if not fn():raise AssertionError('等待隔离任务或查询超时')
    def modal():
        from lifecycle_ui import Review
        d=app.activeModalWidget()
        if isinstance(d,Review):
            state['reviews'].append([r[0] for r in d.overview_model.rows])
            if d.overview_model.headers[-3:]!=['门店','供货仓','付款金额（元）']:state['error']='付款范围缺少门店、仓或金额'
            if d.save.isEnabled():state['error']='未勾选付款确认即可保存'
            d.grab().save(str(out/('review-'+state['action']+'.png')))
            if state['action']=='cancel':d.reject()
            else:d.complete.setChecked(True);d.extra_ack.setChecked(True);d.save.click()
        elif isinstance(d,QMessageBox):state['error']=d.text()+' '+d.informativeText();d.reject()
    driver=QTimer(win);driver.setInterval(30);driver.timeout.connect(modal)
    def execute():
        nonlocal report
        page=None;dialog=None
        try:
            import jst_orders as orders,order_lifecycle as lc,jst_results as results
            from lifecycle_home_ui import StageDialog
            path=win.work_repository.path
            require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'仅使用本轮隔离数据库')
            wait(lambda:win.jst_generator.worker is None)
            win.jst_generator.stopped=True
            with closing(sqlite3.connect(path)) as db:
                require(db.execute('SELECT count(*) FROM jst_active').fetchone()[0]==4,'两店四仓单均已有生成结果')
            heads=lc.current_targets(path)
            def paid():return {results.key(h) for h in heads if lc.snapshot(path,h)['present']['payment']}
            def stock():
                with closing(sqlite3.connect(path)) as db:
                    return {t:db.execute('SELECT * FROM '+t).fetchall() for t in ('jst_active','jst_reservations','jst_locks','jst_warehouses')}
            before=stock();require(not paid(),'生成不自动确认付款')
            dialog=StageDialog(win.followup,'jst');dialog.resize(1366,950);dialog.show();page=dialog.panel
            wait(lambda:page.data is not None and not page.reading)
            page.timer.stop();page.grab().save(str(out/'results.png'))
            require(len(page.records)==4 and page.payment_all_btn.isEnabled() and not page.payment_selected_btn.isEnabled(),'当前筛选四单可直接核对，未勾选时勾选入口禁用')
            driver.start();page.payment_all_btn.click()
            require(not state.get('error') and not paid() and page.payment_receipt is None,'取消四单付款核对不写入')
            wait(lambda:not page.reading)
            first=page.table.topLevelItem(0).child(0);first.setCheckState(0,Qt.CheckState.Checked)
            wanted={tuple(first.data(0,Qt.ItemDataRole.UserRole))};state['action']='selected'
            page.payment_selected_btn.click()
            if state.get('error'):raise AssertionError(state['error'])
            require(paid()==wanted and len(page.payment_receipt)==1,'实际勾选按钮只保存一张仓单，未选订单不变')
            wait(lambda:not page.reading)
            page.warehouse.setCurrentText('山东仓');wanted|={results.key(r['head']) for r in page.visible_records()}
            state['action']='filtered';page.payment_all_btn.click()
            if state.get('error'):raise AssertionError(state['error'])
            require(paid()==wanted and len(page.payment_receipt)==2,'当前筛选按钮只保存山东仓两单，另一仓未选单不变')
            wait(lambda:not page.reading)
            page.warehouse.setCurrentIndex(0);state['action']='all';page.payment_all_btn.click()
            require(not state.get('error') and len(paid())==4,'直接完成四单付款，不退出结果页、不导入Excel')
            wait(lambda:not page.reading)
            state['action']='repeat';page.payment_all_btn.click()
            require(not state.get('error') and all(not r['changed'] for r in page.payment_receipt),'相同生成明细再次确认不新增重复付款版本')
            require(stock()==before,'付款确认不改库存占用、生成单、锁单和仓资料')
            require(not list((out/'exports').glob('*.xlsx')),'整个直达付款流程无需中间Excel导出')
            require([len(r) for r in state['reviews']]==[4,1,2,4,4],'五次核对范围准确，包含取消和重复确认')
            page.grab().save(str(out/'completed.png'))
            report={'passed':True,**diagnostic(),'checks':checks,'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,**diagnostic(),'checks':checks,'error':traceback.format_exc()}
        finally:
            driver.stop()
            if dialog:dialog.reject();dialog.deleteLater()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(200,execute)
