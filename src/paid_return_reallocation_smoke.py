"""Opt-in native acceptance; refuses ordinary application settings."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import traceback
from contextlib import closing
from PySide6.QtCore import QTimer


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();settings=out/'settings'
    if (Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=settings
            or not json.loads((out/'synthetic.json').read_text('utf-8')).get('synthetic')):raise ValueError('Requires marked isolated settings')
    checks=[]
    def require(ok,text):
        if not ok:raise AssertionError(text)
        checks.append(text)
    def wait(predicate):
        deadline=time.monotonic()+40
        while not predicate():
            if time.monotonic()>deadline:raise TimeoutError('Isolated native task did not finish; no forced termination')
            app.processEvents();time.sleep(.01)
    def run():
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QMessageBox
        from version_info import diagnostic
        import jst_orders as orders
        import jst_batches as batches
        import jst_results as results
        import order_lifecycle as lifecycle
        from jst_results_ui import ResultsPage
        from jst_ui import GenerationDialog
        import openpyxl
        fixture=json.loads((out/'fixture.json').read_text('utf-8'));dialog=None;page=None;report={};errors=[]
        try:
            path=win.work_repository.path;heads=fixture['heads']
            require(Path(path).resolve()==(settings/fixture['db_name']).resolve(),'仅访问标记的隔离订单库')
            dialog=QDialog(win);dialog.setWindowTitle('付款后更正回传 · 隔离演练');layout=QVBoxLayout(dialog)
            page=ResultsPage(win,heads,dialog);layout.addWidget(page);dialog.resize(1320,820);dialog.show();wait(lambda:page.data is not None and not page.reading);page.timer.stop()
            require(page.batch_id is None and page.data['counts']['stale']==2,'日期总览准确显示同店两仓旧分配失效')
            require(page.data['counts']['ready']==0 and not page.all_btn.isEnabled(),'旧分配不可直接导出')
            require(all('旧付款保留' in r['reason'] for r in page.records),'两仓均说明新回传需重分配且旧付款保留')
            page.select_visible();require(len(page.chosen)==2 and page.reallocate_btn.isEnabled(),'勾选同店后可直接按原批重分配两张仓单')
            app.processEvents();page.layout().activate();app.processEvents()
            require(page.total.height()>=page.total.heightForWidth(page.total.width()),'结果页汇总各行完整可见')
            dialog.grab().save(str(out/'01-stale-results.png'))
            def drive_generator():
                d=QApplication.activeModalWidget()
                try:
                    require(isinstance(d,GenerationDialog),'真实按钮打开原批更正窗口')
                    require(d.batch_id==fixture['batch_id'] and d.correction_mode,'更正沿用原批身份与冻结库存')
                    d.preview_btn.click();require(d.plan is not None,'实际预览按钮完成两仓分配试算')
                    plans=d.plan['payload']['plans']
                    require(len(plans)==2 and {p['head']['warehouse']:p['summary']['quantity'] for p in plans}==fixture['expected'],'新回传分配量为河北30、山东15')
                    require(all(p.get('previous_payment') for p in plans),'预览冻结两张旧付款的版本与摘要')
                    require('旧付款事实保留' in d.summary.text() and '需重新核对付款' in d.summary.text(),'界面明确说明旧付款保留与后续步骤')
                    app.processEvents();d.layout().activate();app.processEvents()
                    require(d.summary.height()>=d.summary.heightForWidth(d.summary.width()),'付款更正说明完整可见，无文字挤压')
                    require(d.review.total.height()>=d.review.total.heightForWidth(d.review.total.width()),'分配汇总各行完整可见')
                    d.grab().save(str(out/'02-correction-preview.png'));d.auto_export.setChecked(True)
                    def confirm(parent,title,message):
                        require('旧付款保留' in message and '此处不修改平台订单' in message,'生成确认说明更正不改平台订单')
                        return True
                    with patch('jst_ui.confirm',side_effect=confirm):d.generate_btn.click()
                    wait(lambda:d.worker is None and d.export_worker is None and not d.export_queue)
                    require(d.receipt is not None and not d.receipt['errors'] and len(d.receipt['generated'])==2,'实际工作线程成功更正两张仓单')
                    require(all('已导出' in d.completed.item(i).text() for i in range(d.completed.count())),'实际工作线程自动导出两份新生成单')
                    d.grab().save(str(out/'03-corrected-and-exported.png'));d.reject()
                except Exception as exc:
                    errors.append(traceback.format_exc())
                    if d and getattr(d,'worker',None) is None and getattr(d,'export_worker',None) is None:d.reject()
            QTimer.singleShot(200,drive_generator);page.reallocate_btn.click()
            if errors:raise AssertionError(errors[0])
            wait(lambda:not page.reading)
            require(page.data['counts']['ready']==2 and page.data['counts']['stale']==0,'返回日期总览后两仓新分配均可导出')
            require(page.batch_id is None and len(page.chosen)==2,'原日期范围和两仓勾选保留')
            for head in heads:
                wh=head['warehouse'];old_gid=fixture['old_ids'][wh]
                with closing(sqlite3.connect(path)) as db:
                    require(lifecycle.current(db,head,'payment')==fixture['old_payments'][wh],wh+'旧付款事实未被删除或覆盖')
                    gid=orders.active(db,head)
                require(gid!=old_gid and orders.get(path,old_gid)==fixture['old_generations'][wh],wh+'旧生成单完整保留，新生成单独立留存')
                require(lifecycle.snapshot(path,head)['status']=='待付款',wh+'重新核对前保持待付款状态')
                lifecycle.commit(path,orders.payment_plan(path,gid))
                require(lifecycle.snapshot(path,head)['status']=='待收货',wh+'明确重新核对付款后恢复待收货')
            with closing(sqlite3.connect(path)) as db:
                require(orders.active(db,fixture['peer'])==fixture['peer_gid'],'未选门店活动生成单不变')
                require(batches.reservations(db,fixture['batch_id'],'河北仓')['0001']==90,'河北原库存仅占用所选30加未选60，无重复扣减')
            require(orders.get(path,fixture['peer_gid'])==fixture['peer_generation'],'未选门店完整冻结分配不变')
            files=list((out/'exports').glob('*.xlsx'));require(len(files)==2,'只导出勾选两仓的新文件')
            seen={}
            for file in files:
                book=openpyxl.load_workbook(file,read_only=True)
                try:
                    row=list(book.active.values)[1];seen[row[3]]=str(row[2]);require(row[1]==2,'导出单价保持原批价格')
                finally:book.close()
            require(seen=={h['number']:fixture['expected'][h['warehouse']] for h in heads},'Excel单号、仓单数量与更正预览一致')
            report={'passed':True,'checks':checks,'frozen_exe':bool(getattr(sys,'frozen',False)),**diagnostic(),'synthetic':True,'live_platform':False}
        except Exception:report={'passed':False,'checks':checks,'error':traceback.format_exc(),'frozen_exe':bool(getattr(sys,'frozen',False))}
        finally:
            if page:page.timer.stop();page.reader.shutdown()
            if dialog:dialog.close()
            (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            win._discard_close=True;win.close()
    QTimer.singleShot(200,run)
