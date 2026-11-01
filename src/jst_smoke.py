"""Opt-in actual Qt/EXE acceptance; caller supplies an isolated synthetic database."""
import json,time,traceback,sqlite3
from pathlib import Path
from contextlib import closing
from PySide6.QtCore import QTimer,QEventLoop,Qt,QMimeData,QUrl,QPointF
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication,QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]);phase=args[1];checks=[];state={};ticks=[];last=[time.perf_counter()]
    heartbeat=QTimer(win);heartbeat.setInterval(50)
    def tick():
        now=time.perf_counter();ticks.append(now-last[0]);last[0]=now
    heartbeat.timeout.connect(tick);heartbeat.start()
    def automate():
        from lifecycle_ui import Review
        d=QApplication.activeModalWidget()
        if isinstance(d,QMessageBox):
            if d.windowTitle() in ('确认生成聚水潭订单','确认已在聚水潭下单'):
                b=d.button(QMessageBox.StandardButton.Yes)
                if b:b.click()
                else:state['error']='No confirm button';d.reject()
            else:state['error']=d.windowTitle()+': '+d.text();d.reject()
        elif isinstance(d,Review):
            d.complete.setChecked(True);d.extra_ack.setChecked(True);d.save.click()
    driver=QTimer(win);driver.setInterval(25);driver.timeout.connect(automate);driver.start()
    def wait(fn,seconds=90):
        loop=QEventLoop();timer=QTimer();start=time.monotonic();timer.timeout.connect(lambda:loop.quit() if fn() or time.monotonic()-start>seconds else None);timer.start(15);loop.exec();timer.stop()
        if state.get('error'):raise AssertionError(state['error'])
        if not fn():raise AssertionError('UI timeout')
    def execute():
        start=time.perf_counter();dialog=None
        try:
            import jst_ui,jst_orders as j,order_lifecycle as lc,jst_exports as ex,work_repository as wr
            path=win.work_repository.path
            if str(path.resolve())!=str((out/'settings/workbench.sqlite3').resolve()):raise AssertionError('Isolation path mismatch')
            win._discard_close=True;win.show_section(3);win.followup.query();wait(lambda:len(win.followup.records)==4)
            heads=lc.current_targets(path)
            if phase=='exercise':
                from lifecycle_home_ui import StageDialog
                overview=(win.followup.start.text(),win.followup.end.text(),win.followup.search.text())
                for stage in ('return','jst','payment','receipt'):
                    focused=StageDialog(win.followup,stage);focused.show();wait(lambda:hasattr(focused.panel,'count') and focused.panel.table.isEnabled())
                    if stage=='jst' and focused.panel.count!=4:raise AssertionError('focused generation scope')
                    focused.grab().save(str(out/('stage-'+stage+'.png')));focused.reject();focused.deleteLater()
                if overview!=(win.followup.start.text(),win.followup.end.text(),win.followup.search.text()):raise AssertionError('overview filters changed')
                checks.append('四个独立环节工作页、主动作与筛选、返回保留总览条件')
                if sum(r['next']=='jst_generate' for r in win.followup.records)!=4:raise AssertionError('daily stage')
                dialog=jst_ui.GenerationDialog(win,win,heads);dialog.show();wait(lambda:len(dialog.catalogs)==2)
                dialog.preview();dialog.grab().save(str(out/'preview.png'))
                if len(dialog.plan['payload']['plans'])!=4:raise AssertionError('preview scope')
                dialog.generate();wait(lambda:dialog.worker is None and dialog.export_worker is None and not dialog.export_queue and dialog.receipt is not None)
                r=dialog.receipt
                if len(r['generated'])!=4 or r['errors']:raise AssertionError(r)
                checks.append('实际窗口预览、四店仓单后台生成、逐单导出与三列文件')
                dialog.completed.item(0).setSelected(True);gid=dialog.completed.item(0).data(Qt.ItemDataRole.UserRole);p=j.get(path,gid)
                dialog.lock_saved()
                with closing(sqlite3.connect(path)) as db:
                    if j.locked(db,p['head'])!=gid:raise AssertionError('lock did not persist')
                checks.append('已生成单选择、确认下单锁定')
                wh=p['head']['warehouse'];dialog.warehouse.setCurrentText(wh)
                mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(out/(wh+'-新库存.xlsx')))])
                drop=QDropEvent(QPointF(20,20),Qt.DropAction.CopyAction,mime,Qt.MouseButton.LeftButton,Qt.KeyboardModifier.NoModifier);dialog.dropEvent(drop)
                with closing(sqlite3.connect(path)) as db:
                    b=j.baseline(db,wh)
                    if gid not in b['included']:raise AssertionError('locked order not included')
                    sku=p['rows'][0]['sku'];remaining=j.reservations(db,wh,set(b['included'])).get(sku,0)
                    if remaining!=24:raise AssertionError(('unlocked remaining',remaining))
                checks.append('实际拖拽更新仓资料、已锁不重复扣、未锁预留保留')
                dialog.preview()
                if dialog.plan['payload']['plans'] or len(dialog.plan['payload']['skipped'])!=4:raise AssertionError('repeated generation not skipped')
                dialog.grab().save(str(out/'generated-and-locked.png'));dialog.reject();dialog.deleteLater();dialog=None
                jst_ui.payment(win,win,p['head']);v=lc.snapshot(path,p['head'])
                if not v['present']['payment'] or v['next']!='receipt':raise AssertionError('payment review failed')
                checks.append('实际付款核对表单、确认后进入待收货')
                # A read task cancelled before starting must leave no generation; a subsequent preview still works.
                from business_ui import Task
                task=Task(win,j.prepare,(path,heads),'取消预览验收');task.worker.cancel.set();task.worker.start();task.exec();task.worker.wait()
                if task.error is None:raise AssertionError('cancel did not propagate')
                task.deleteLater();j.prepare(path,heads);checks.append('后台取消、完成后重试')
                save={'gid':gid,'head':p['head'],'files':[str(x) for x in (out/'exports').glob('*.xlsx')]};(out/'saved.json').write_text(json.dumps(save,ensure_ascii=False),encoding='utf-8')
            elif phase=='close_retry':
                # Synthetic delay makes a real window close deterministic while a transaction is preparing.
                original=j.put_generation
                def slow(db,p):time.sleep(.12);return original(db,p)
                j.put_generation=slow
                dialog=jst_ui.GenerationDialog(win,win,heads);dialog.show();wait(lambda:len(dialog.catalogs)==2);dialog.preview()
                dialog.generate();QTimer.singleShot(25,dialog.close)
                wait(lambda:dialog.worker is None and dialog.export_worker is None and not dialog.isVisible())
                j.put_generation=original
                with closing(sqlite3.connect(path)) as db:
                    if db.execute('SELECT count(*) FROM jst_active').fetchone()[0]>1:raise AssertionError('close did not stop remaining tasks')
                dialog.deleteLater();dialog=jst_ui.GenerationDialog(win,win,heads);dialog.show();wait(lambda:len(dialog.catalogs)==2);dialog.preview();dialog.generate()
                wait(lambda:dialog.worker is None and dialog.export_worker is None and dialog.receipt is not None)
                with closing(sqlite3.connect(path)) as db:
                    if db.execute('SELECT count(*) FROM jst_active').fetchone()[0]!=4:raise AssertionError('retry not complete')
                dialog.reject();dialog.deleteLater();dialog=None
                checks.append('生成准备中实际关闭窗口、安全停止、再次打开生成；使用QA延迟确保触发')
            else:
                saved=json.loads((out/'saved.json').read_text(encoding='utf-8'));gid=saved['gid']
                with closing(sqlite3.connect(path)) as db:
                    if not j.locked(db,saved['head']):raise AssertionError('lost lock after restart')
                if lc.snapshot(path,saved['head'])['next']!='receipt':raise AssertionError('lost payment')
                r=ex.export(path,[gid],out/'exports')
                if len(r['files'])!=1 or r['failed']:raise AssertionError('reexport failed')
                checks.extend(['重开、日批次与锁单和付款恢复','原生成重复导出同名覆盖'])
            win.followup.query();wait(lambda:win.followup.table.isEnabled() and len(win.followup.records)==4);win.grab().save(str(out/('daily-'+phase+'.png')))
            log=jst_ui.Logs(win,win,wr.today(),wr.today());log.show();wait(lambda:hasattr(log,'records'))
            if not log.records:raise AssertionError('missing logs')
            log.show_detail(log.model.index(0,0));log.grab().save(str(out/('logs-'+phase+'.png')));log.reject();log.deleteLater();checks.append('业务日志分页、详情表单、spawn后台查询')
            result={'passed':True,'phase':phase,'checks':checks,'elapsed':time.perf_counter()-start,'ui_heartbeat_max':max(ticks,default=0),'source_commit':__import__('version_info').diagnostic().get('source_commit')}
        except Exception:
            result={'passed':False,'phase':phase,'checks':checks,'error':traceback.format_exc()}
            if dialog and not dialog.worker and not dialog.export_worker:dialog.reject();dialog.deleteLater()
        (out/('ui-'+phase+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');driver.stop();heartbeat.stop();win._discard_close=True;win.close();QTimer.singleShot(100,lambda:app.exit(0 if result['passed'] else 1))
    QTimer.singleShot(100,execute)
