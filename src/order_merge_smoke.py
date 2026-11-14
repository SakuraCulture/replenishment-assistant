"""Explicit synthetic-only native merge preview, safe exit and reopen probe.

The verifier creates every input. Production prepare/inspect/merge/apply remain
real. Only file selection and explicitly named failure points are injected.
"""
import json,os,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton,QFileDialog,QDialog,QTableWidget


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1]
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if marker.get('synthetic') is not True or marker.get('live_platform') is not False or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings':
        raise ValueError('Only verifier-created isolated merge data allowed')
    from application_runtime import current
    runtime=current();started=time.monotonic();state={'chosen':False,'preview':False,'finished':False,'tracking':False,'failed':False}
    events=out/(mode+'-events.jsonl')
    def emit(event,**values):
        with events.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'event':event,'mode':mode,'phase':runtime.phase,**values},ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
    emit('setup',root=str(win.repository.folder),frozen_exe=bool(getattr(__import__('sys'),'frozen',False)))
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    win.browser_status.paused=True;win.browser_status.timer.stop();win.browser_status.initial.stop()
    class ClosingClient:
        running=True;error=None
        def __init__(self):self.calls=0;self.fail=mode=='background_fail'
        def close(self,timeout=.5):
            self.calls+=1;emit('synthetic_shutdown_call',attempt=self.calls);time.sleep(.12)
            if self.fail:raise RuntimeError('SYNTHETIC-MERGE-BACKGROUND-EXIT-ERROR')
            if self.calls==1:return False
            self.running=False;return True
    client=None
    if mode in ('async','background_fail'):
        client=ClosingClient();win.catalog_sessions.session.client=client;win.catalog_sessions.session.ready=True
    if mode=='apply_failure':
        import order_merge_migration as wrapper
        original_copy=wrapper.shutil.copytree
        def fail_copy(source,destination,*a,**kw):
            if Path(source).resolve()==out/'settings':raise OSError('SYNTHETIC-MERGE-APPLY-COPY-ERROR')
            return original_copy(source,destination,*a,**kw)
        wrapper.shutil.copytree=fail_copy
    if mode in ('interrupt-before','interrupt-after'):
        original_rename=Path.rename
        def interrupt(source,destination):
            source_absolute=source.absolute();destination_absolute=Path(destination).absolute()
            value=original_rename(source,destination)
            if mode=='interrupt-before' and source_absolute==out/'settings':
                emit('interrupted_before_new_root_install');os._exit(73)
            if mode=='interrupt-after' and source_absolute.name.startswith('.settings.order-new-') and destination_absolute==out/'settings':
                emit('interrupted_after_new_root_install');os._exit(74)
            return value
        Path.rename=interrupt
    def shot(widget,name):
        path=out/(mode+'-'+name+'.png')
        if not widget.grab().save(str(path)):raise AssertionError('Screenshot was not saved: '+str(path))
        emit('screenshot',file=path.name)
    def quit_normally():
        if state['finished']:return
        state['finished']=True;win._discard_close=True
        win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
        win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
        win.close()
    def fail(error):
        if state['failed']:return
        state['failed']=True;emit('harness_exception',error=str(error),traceback=traceback.format_exc())
        modal=QApplication.activeModalWidget()
        if isinstance(modal,QDialog):modal.reject()
        QTimer.singleShot(0,win,quit_normally)
    def tick():
        try:
            box=QApplication.activeModalWidget()
            if isinstance(box,QMessageBox):
                emit('real_message',title=box.windowTitle(),text=box.text())
                shot(box,'message-'+str(len(list(out.glob(mode+'-message-*.png')))))
                target=box.button(QMessageBox.StandardButton.Ok) or box.button(QMessageBox.StandardButton.Yes) or box.button(QMessageBox.StandardButton.Close)
                if target:target.click()
            elif isinstance(box,QDialog) and box.windowTitle()=='核对订单合并范围' and not state['preview']:
                state['preview']=True
                table=box.findChild(QTableWidget,'orderMergePreview')
                if table is None:raise AssertionError('Real merge preview table missing')
                rows=[[table.item(i,j).text() if table.item(i,j) else '' for j in range(table.columnCount())] for i in range(table.rowCount())]
                confirm=box.findChild(QPushButton,'confirmOrderMerge');cancel=box.findChild(QPushButton,'cancelOrderMerge')
                if confirm is None or cancel is None:raise AssertionError('Real merge preview buttons missing')
                observed={'added':sum(r[4]=='新增' for r in rows),'duplicate':sum(r[4]=='完全重复，跳过' for r in rows),'conflict':sum(r[4]=='有差异，保留本机' for r in rows),'deleted':sum(r[4]=='本机已删除，不恢复' for r in rows)}
                shot(box,'preview')
                expected=marker['repeat_counts'] if mode in ('repeat','reexport') else marker['initial_counts']
                if observed!=expected:raise AssertionError('Merge preview counts differ: '+repr((observed,expected,rows)))
                if confirm.isEnabled()!=(observed['added']>0):raise AssertionError('Confirm availability does not match additions')
                conflict=next((i for i,r in enumerate(rows) if r[4]=='有差异，保留本机'),None)
                if conflict is not None:table.selectRow(conflict)
                shot(box,'preview-selected-conflict')
                emit('preview_verified',counts=observed,rows=rows,confirm_enabled=confirm.isEnabled())
                if mode in ('cancel','repeat','reexport','source-deleted'):QTimer.singleShot(0,box,cancel.click)
                else:QTimer.singleShot(0,box,confirm.click)
            elif isinstance(box,QDialog) and box.windowTitle()=='完整订单迁移与参数导出':
                if not state['chosen']:
                    state['chosen']=True
                    button=next(b for b in box.findChildren(QPushButton) if b.text()=='合并导入订单（保留本机）…')
                    QTimer.singleShot(0,box,button.click)
                elif state['preview'] and mode in ('cancel','repeat','reexport','source-deleted'):
                    if getattr(win,'_order_import_request',None) is not None or not win.isEnabled():raise AssertionError('Cancelled/no-additions preview requested an exit')
                    emit('no_handoff',enabled=win.isEnabled(),pending=False)
                    box.reject();QTimer.singleShot(0,win,quit_normally)
            if mode=='background_fail':
                from migration_status import latest
                value=latest(out/'settings')
                if value and value['state']=='FAILED' and not state['finished']:
                    if 'SYNTHETIC-MERGE-BACKGROUND-EXIT-ERROR' not in value.get('error',{}).get('message',''):raise AssertionError('Original background error lost')
                    emit('shutdown_failure_retained',window_enabled=win.isEnabled(),request_cleared=win._order_import_request is None)
                    client.fail=False;quit_normally()
            if mode.startswith('reopen') and time.monotonic()-started>.6 and box is None:
                if not state['tracking']:
                    state['tracking']=True;win.show_section(3);win.followup.clear_filters()
                expected=marker['reopen_parent_count']*2
                if len(win.followup.records)==expected:
                    shot(win,'tracking');emit('tracking_verified',warehouse_orders=expected);quit_normally()
            if time.monotonic()-started>90:
                emit('watchdog_safe_close');fail(TimeoutError('Synthetic native merge scenario timed out'))
        except BaseException as exc:fail(exc)
    timer=QTimer(app);timer.setInterval(25);timer.timeout.connect(tick);timer.start();app._order_merge_test_timer=timer
    if not mode.startswith('reopen'):
        def begin():
            from unittest.mock import patch
            import order_migration_ui
            package=out/('reexport.zip' if mode=='reexport' else 'orders.zip')
            with patch.object(QFileDialog,'getOpenFileName',return_value=(str(package),'Zip')):order_migration_ui.open_migration(win,win)
            emit('dialog_returned',pending=bool(getattr(win,'_order_import_request',None)))
        QTimer.singleShot(220,win,begin)
