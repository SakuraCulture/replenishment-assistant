"""Opt-in Qt acceptance on a newly created synthetic database, no platform."""
import json, sqlite3, sys, time, traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer, QEventLoop
from PySide6.QtWidgets import QDialog
from version_info import diagnostic


def start_smoke(app,win,args):
    out=Path(args[0]); checks=[]; detail=None
    def require(ok,title):
        if not ok:raise AssertionError(title)
        checks.append(title)
    def wait(fn):
        loop=QEventLoop(); timer=QTimer(); start=time.monotonic()
        timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>60 else None); timer.start(20); loop.exec(); timer.stop()
        if not fn():raise AssertionError('隔离界面等待超时')
    def execute():
        nonlocal detail
        try:
            require('payment_references' not in sys.modules,'应用启动未加载单号维护模块')
            path=win.work_repository.path
            require(path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'仅使用本轮隔离数据库')
            wait(lambda:win.jst_generator.worker is None); win.jst_generator.stopped=True
            import payment_references as service
            from payment_references_ui import ReferenceDialog
            from lifecycle_ui import LifecycleDialog
            f=json.loads((out/'reference-fixture.json').read_text('utf-8')); head=f['head']
            def facts():
                with closing(sqlite3.connect(path)) as db:
                    return {t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('normal_orders','normal_revisions','lifecycle_versions','lifecycle_files','jst_generations','jst_active','jst_reservations','jst_locks')}
            before=facts(); detail=LifecycleDialog(win,win,head); detail.show()
            require(detail.payment_reference_btn.isEnabled(),'已付款订单详情显示维护入口')
            failures=[]; timer=QTimer(); timer.setInterval(30)
            def edit_from_entry():
                d=app.activeModalWidget()
                if not isinstance(d,ReferenceDialog):return
                timer.stop()
                try:
                    require(not d.number.text(),'直接采用生成单的订单原内部号为空')
                    require(not d.findChildren(__import__('PySide6.QtWidgets',fromlist=['QComboBox']).QComboBox),'直接填写单号，无企业或订单选择步骤')
                    d.number.setText('00994021108001'); app.processEvents(); d.grab().save(str(out/'填写内部订单号.png')); d.save_btn.click()
                    require(d.result()==QDialog.DialogCode.Accepted and bool(d.receipt),'点击保存完成单号登记并关闭')
                except Exception:
                    failures.append(traceback.format_exc()); d.reject()
            timer.timeout.connect(edit_from_entry); timer.start(); detail.payment_reference_btn.click(); timer.stop()
            if failures:raise AssertionError(failures[0])
            require(service.read(path,head)['number']=='00994021108001','重新读取保留前导零和完整内部号')
            require('00994021108001' in detail.note.text(),'订单详情显示保存结果')
            require(facts()==before,'补号后付款收货版本、金额数量、签发和库存分配逐表完全相同')
            d=ReferenceDialog(detail,win,head); d.show(); require(d.number.text()=='00994021108001','重开入口自动带出已登记号码')
            d.number.setText('00994021108002'); d.save_btn.click(); d.deleteLater()
            current=service.read(path,head); require(current['history'][0][1:]==['00994021108001','00994021108002'],'修改旧号保留前后记录')
            d=ReferenceDialog(detail,win,head); d.show(); d.number.setText('CANCELLED'); d.reject(); d.deleteLater()
            require(service.read(path,head)['number']=='00994021108002','关闭或取消不保存输入')
            d=ReferenceDialog(detail,win,head); d.show(); d.number.clear(); d.save_btn.click()
            require(d.isVisible() and '请填写' in d.note.text(),'空号码明确提示并留在编辑页'); d.reject(); d.deleteLater()
            d=ReferenceDialog(detail,win,head); d.show(); d.save_btn.click(); d.deleteLater()
            require(len(service.read(path,head)['history'])==2,'相同号码重复保存不新增记录')
            d=ReferenceDialog(detail,win,head); d.show(); app.processEvents(); d.grab().save(str(out/'已保存内部订单号.png'))
            timer=QTimer(); timer.setInterval(30)
            def history_shot():
                view=app.activeModalWidget()
                if view and view.windowTitle()=='内部订单号修改记录':
                    timer.stop(); view.grab().save(str(out/'号码修改记录.png')); view.accept()
            timer.timeout.connect(history_shot); timer.start(); d.history_btn.click(); timer.stop(); d.reject(); d.deleteLater()
            require((out/'号码修改记录.png').exists(),'界面可以查看号码修改记录')
            require(facts()==before,'更正、取消、重复保存后付款和收货依据仍完全不变')
            other=LifecycleDialog(win,win,f['unpaid']); require(not other.payment_reference_btn.isEnabled(),'未确认付款的订单不开放维护入口'); other.deleteLater()
            result={'passed':True,'checks':checks,'live_platform':False,'live_database_written':False,**diagnostic()}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc(),'live_platform':False,**diagnostic()}
        (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        if detail:detail.close();detail.deleteLater()
        app.exit(0 if result['passed'] else 1)
    QTimer.singleShot(300,execute)
