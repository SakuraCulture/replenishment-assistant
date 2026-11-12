"""Marked synthetic-only native migration/exit acceptance; real prepare/apply."""
import json,os,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton,QFileDialog,QDialog

def start_smoke(app,win,args):
    out=Path(args[0]).resolve();case=args[1];marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if marker.get('synthetic') is not True or marker.get('live_platform') is not False or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':raise ValueError('Only marked isolated migration data allowed')
    def emit(event,**values):
        with (out/(case+'-events.jsonl')).open('a',encoding='utf-8') as f:f.write(json.dumps({'event':event,**values},ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
    from application_runtime import current
    runtime=current();emit('setup',case=case,root=str(win.repository.folder))
    if case.startswith('hold'):(out/'hold-ready').write_text(str(os.getpid()),'utf-8')
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    win.browser_status.paused=True;win.browser_status.timer.stop();win.browser_status.initial.stop()
    class Client:
        running=True;error=None
        def __init__(self):self.calls=0;self.fail=case=='background_fail'
        def close(self,timeout=.5):
            self.calls+=1;emit('synthetic_shutdown_call',attempt=self.calls);time.sleep(.12)
            if self.fail:raise RuntimeError('BACKGROUND-ORIGINAL-EXIT-ERROR')
            if self.calls==1:return False
            self.running=False;return True
    client=None
    if case in ('async','background_fail','early_loop_exit','caught_background'):
        client=Client();win.catalog_sessions.session.client=client;win.catalog_sessions.session.ready=True
    if case=='shutdown_exception':
        def fail_destroyed():raise RuntimeError('ORIGINAL-DESTRUCTOR-ERROR')
        win.destroyed.connect(fail_destroyed)
    if case=='apply_failure':
        import order_migration as m
        original_copy=m.shutil.copytree
        def fail_copy(source,destination,*a,**kw):
            if Path(source).resolve()==out/'settings':raise OSError('ORIGINAL-APPLY-COPY-ERROR')
            return original_copy(source,destination,*a,**kw)
        m.shutil.copytree=fail_copy
    if case=='interrupt':
        original_rename=Path.rename
        def interrupt(source,destination):
            value=original_rename(source,destination)
            if source.resolve()==out/'settings':emit('interrupted_after_first_move');os._exit(73)
            return value
        Path.rename=interrupt
    started=time.monotonic();chosen=[False];finished=[False];early=[False];messages=[]
    def quit_normally():
        if finished[0]:return
        finished[0]=True;win._discard_close=True;win.close()
    def tick():
        try:
            box=QApplication.activeModalWidget()
            if isinstance(box,QMessageBox):
                title=box.windowTitle();messages.append(title);emit('real_message',title=title,text=box.text(),phase=runtime.phase)
                target=box.button(QMessageBox.StandardButton.No if case=='cancel' and title=='核对完整恢复范围' else QMessageBox.StandardButton.Yes) or box.button(QMessageBox.StandardButton.Ok)
                if target:target.click()
            elif box and box.windowTitle()=='完整订单迁移与参数导出':
                if not chosen[0]:
                    chosen[0]=True;button=next(b for b in box.findChildren(QPushButton) if b.text()=='校验并导入完整订单迁移包…');QTimer.singleShot(0,box,button.click)
                elif case=='cancel':
                    from migration_status import latest
                    value=latest(out/'settings')
                    if value and value['state']=='CANCELLED':box.reject();QTimer.singleShot(0,win,quit_normally)
            if case.startswith('reopen') and time.monotonic()-started>.5 and QApplication.activeModalWidget() is None:quit_normally()
            if case.startswith('hold') and (out/'hold-stop').exists():quit_normally()
            if case=='background_fail':
                from migration_status import latest
                value=latest(out/'settings')
                if value and value['state']=='FAILED' and not finished[0]:
                    emit('shutdown_failure_retained',window_enabled=win.isEnabled(),request_cleared=win._order_import_request is None);client.fail=False;quit_normally()
            if case=='early_loop_exit' and runtime.phase=='WAITING_EXIT' and win.catalog_sessions.worker and not early[0]:early[0]=True;emit('event_loop_interrupted');app.exit(7)
            if case=='caught_background' and runtime.phase=='WAITING_EXIT' and not early[0]:
                from aoxiang_ui_common import Call
                early[0]=True
                def health_failure(_):raise RuntimeError('HANDLED-CLOSING-HEALTH-ERROR')
                task=Call(health_failure,win);task.failed.connect(lambda text:emit('caught_health_failure',message=text));task.finished.connect(task.deleteLater);task.start();win._smoke_health=task
            if time.monotonic()-started>80:emit('watchdog_safe_close');quit_normally()
        except BaseException:emit('harness_exception',traceback=traceback.format_exc());quit_normally()
    timer=QTimer(app);timer.setInterval(20);timer.timeout.connect(tick);timer.start();app._migration_test_timer=timer
    if not case.startswith(('reopen','hold')):
        def begin():
            from unittest.mock import patch
            import order_migration_ui
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(out/'orders.zip'),'Zip')):order_migration_ui.open_migration(win,win)
            emit('dialog_returned',pending=bool(getattr(win,'_order_import_request',None)))
        QTimer.singleShot(200,win,begin)
