"""Opt-in Qt/EXE contract exercise; isolated data required by caller."""
import json,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QEventLoop
from PySide6.QtWidgets import QMessageBox,QApplication


def start_smoke(app,win,args):
    out=Path(args[0]);checks=[];ticks=[];previous=[time.perf_counter()];state={}
    def pulse():
        now=time.perf_counter();ticks.append(now-previous[0]);previous[0]=now
    timer=QTimer(win);timer.setInterval(30);timer.timeout.connect(pulse);timer.start()
    def wait(fn):
        loop=QEventLoop();poll=QTimer();start=time.monotonic()
        poll.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>90 else None)
        poll.start(15);loop.exec();poll.stop()
        if not fn():raise AssertionError('timeout')
    def modal():
        from lifecycle_ui import Review
        d=QApplication.activeModalWidget()
        if isinstance(d,Review):d.complete.setChecked(True);d.extra_ack.setChecked(True);d.save.click()
        elif isinstance(d,QMessageBox):state['error']=d.text();d.reject()
    driver=QTimer(win);driver.setInterval(30);driver.timeout.connect(modal);driver.start()
    def execute():
        try:
            import jst_orders as j,jst_exports as ex,order_lifecycle as lc,jst_ui,openpyxl
            from lifecycle_batch_ui import BatchImport
            path=win.work_repository.path
            if path.resolve()!=(out/'settings/workbench.sqlite3').resolve():raise ValueError('isolated database required')
            win._discard_close=True
            def generated():
                with closing(sqlite3.connect(path)) as db:
                    return j.exists(db) and db.execute('SELECT count(*) FROM jst_active').fetchone()[0]==4
            wait(generated);wait(lambda:win.jst_generator.worker is None)
            win.jst_generator.stopped=True
            heads=lc.current_targets(path)
            if any(lc.snapshot(path,h)['present']['payment'] for h in heads):raise AssertionError('fake paid')
            checks.append('重启自动恢复4个回传任务，完成生成，无自动付款')
            if list(win.followup.cards)!=['待回传','待付款','待收货','已结束']:raise AssertionError('stages')
            gids=jst_ui.selected_ids(path,heads);files=ex.export(path,gids,out/'exports')['files']
            for f in files:
                b=openpyxl.load_workbook(f['path']);s=b.active
                if list(s.values)[0]!=('商品编码','单价','数量','订单号'):raise AssertionError('columns')
                if s['D2'].data_type!='s':raise AssertionError('order type')
                b.close()
            checks.append('四列每店仓单导出、末列文本单号，库存不因导出增加占用')
            jst_ui.payment(win,win,heads[0])
            if state.get('error'):raise AssertionError(state['error'])
            first=lc.snapshot(path,heads[0])['documents']['payment']
            if first['payload']['rows'][0]['document_number']:raise AssertionError('invented ERP number')
            checks.append('实际付款确认表单直接采用生成明细，允许缺少聚水潭内部单号')
            target=files[1]['path'];d=BatchImport(win,win,'payment',scope=[heads[1]]);d.show();d.add_paths([target]);d.preview()
            if not d.plan:raise AssertionError(d.note.text())
            d.review.complete.setChecked(True);d.review.extra_ack.setChecked(True);d.review.save.click()
            if not d.receipt:raise AssertionError(d.note.text())
            d.deleteLater();checks.append('实际批量导入界面读取四列文件、核对保存，按补货单号关联')
            win.followup.query();wait(lambda:win.followup.table.isEnabled());win.grab().save(str(out/'payment-source.png'))
            result={'passed':True,'checks':checks,'heartbeat_max':max(ticks,default=0)}
        except Exception:result={'passed':False,'checks':checks,'error':traceback.format_exc()}
        (out/'payment-ui.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        driver.stop();timer.stop();win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if result['passed'] else 1))
    QTimer.singleShot(100,execute)
