"""Native UI acceptance restricted to freshly marked, isolated data locations."""
import atexit,json,os,re,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QMessageBox,QPushButton

def context(args):
    out=Path(args[0]).resolve();case=args[1]
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    local=Path(marker.get('localappdata','')).resolve()
    if marker.get('synthetic') is not True or marker.get('live_platform') is not False or Path(os.environ.get('LOCALAPPDATA','')).resolve()!=local or Path(marker.get('source','')).absolute()!=local/'YoutuanTools/补货助手' or local.parent!=Path(os.environ['TEMP']).resolve() or not re.fullmatch('replenishment-location-[a-f0-9]{16}',local.name) or os.environ.get('REPLENISHMENT_DATA_DIR'):
        raise ValueError('Only marked isolated location data allowed')
    def emit(event,**values):
        with (out/(case+'-events.jsonl')).open('a',encoding='utf-8') as file:
            file.write(json.dumps({'event':event,'pid':os.getpid(),**values},ensure_ascii=False)+'\n');file.flush();os.fsync(file.fileno())
    return out,case,emit

def install_startup(app,args):
    out,case,emit=context(args)
    # Every repository is already restricted to a fresh private LOCALAPPDATA.
    # Scope the process census too, so isolated acceptance cannot interfere
    # with, or demand closing, the user's unrelated running business window.
    import application_processes
    records=application_processes.records
    def isolated_records():
        return [row for row in records() if str(out) in row['argv'] or '--data-location-progress' in row['argv'] or not row['argv']]
    application_processes.records=isolated_records
    if case=='busy-second':return
    atexit.register(lambda:(out/(case+'-exited')).write_text(str(os.getpid()),'utf-8'))
    from application_runtime import current
    def messages():
        box=QApplication.activeModalWidget()
        if not isinstance(box,QMessageBox):return
        title=box.windowTitle();runtime=current()
        if case=='login_pause' and title=='确认搬迁全部本机资料':
            # Keep the timestamp on the native object: activeModalWidget() can
            # return a fresh Python wrapper on a later timer callback.
            since=box.property('qa_wait_from')
            if since is None:since=time.monotonic();box.setProperty('qa_wait_from',since)
            if time.monotonic()-since<.6:return
        emit('real_message',title=title,text=box.text(),phase=runtime.phase)
        cancel=case=='cancel' and title=='确认搬迁全部本机资料'
        button=box.button(QMessageBox.StandardButton.No if cancel else QMessageBox.StandardButton.Yes) or box.button(QMessageBox.StandardButton.Ok)
        if case=='unsaved_cancel' and title=='有未保存的作业变更':button=next(b for b in box.buttons() if b.text()=='取消')
        if button:button.click()
    timer=QTimer(app);timer.setInterval(25);timer.timeout.connect(messages);timer.start();app._location_message_timer=timer

def start_smoke(app,win,args):
    out,case,emit=context(args)
    import data_location as location,data_relocation as move
    from application_runtime import current
    runtime=current()
    expected=Path(json.loads((out/'synthetic.json').read_text('utf-8'))['source'])/'settings'
    configured=location.config()
    if configured:expected=Path(configured['active_base'])/'settings'
    if Path(win.repository.folder).resolve()!=expected.resolve():
        raise AssertionError('Synthetic root mismatch; acceptance must not operate on this window')
    emit('opened',root=str(win.repository.folder))
    original_copy=move._copy
    def require_no_qt(*values):
        assert QApplication.instance() is None,'Qt must be destroyed before copying storage'
        emit('copy_after_qt_destroyed',instance_guard_retained=runtime.primary.lock is not None)
        return original_copy(*values)
    move._copy=require_no_qt
    if case in ('busy-second','reopen-offline'):
        raise AssertionError('A second or missing-drive launch reached the business window')
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop();win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    win.browser_status.paused=True;win.browser_status.timer.stop();win.browser_status.initial.stop()
    class Client:
        running=True;error=None
        def __init__(self):self.calls=0;self.fail=case=='background_fail'
        def close(self,timeout=.5):
            self.calls+=1;emit('background_close',attempt=self.calls);time.sleep(.12)
            if self.fail:raise RuntimeError('LOCATION-BACKGROUND-FIRST-ERROR')
            if self.calls==1:return False
            self.running=False;return True
    client=None
    if case=='cwd_in_source':os.chdir(expected)
    if case=='qt_handle':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];kernel.CreateFileW.restype=wintypes.HANDLE
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.CreateFileW(str(expected.parent),1,3,None,3,0x02000000,None)
        assert handle!=ctypes.c_void_p(-1).value
        app.destroyed.connect(lambda:(kernel.CloseHandle(handle),emit('qt_directory_released')))
    if case=='login_pause':
        win.browser_startup.started=True;win.browser_startup.waiting={'山东仓'};win.browser_startup.retry_until=time.monotonic()+30
        def unexpected(*args,**kwargs):emit('unexpected_auto_connection');raise AssertionError('Maintenance started an automatic connection')
        win.browser_startup.retry=unexpected
    if case=='unsaved_cancel':win.job_dirty=True
    if case in ('async','background_fail'):
        client=Client();win.catalog_sessions.session.client=client;win.catalog_sessions.session.ready=True
    if case=='shutdown_exception':
        def fail_destroyed():raise RuntimeError('LOCATION-DESTRUCTOR-FIRST-ERROR')
        win.destroyed.connect(fail_destroyed)
    if case=='viewer_failure':
        import data_location_progress
        def fail_viewer(*args,**kwargs):raise OSError('LOCATION-VIEWER-START-FIRST-ERROR')
        data_location_progress.launch=fail_viewer
    if case=='large_file':
        (Path(expected).parent/'large-test.bin').write_bytes(b'x'*(12*1024**2))
        original_progress_tick=move.Progress.tick
        def slow_progress(owner,amount=0,**kwargs):
            if owner.phase=='COPYING' and amount>0:time.sleep(.08)
            return original_progress_tick(owner,amount,**kwargs)
        move.Progress.tick=slow_progress
    if case in ('apply_failure','busy'):
        original=move._copy
        def copy(*values):
            if case=='apply_failure':raise OSError('LOCATION-COPY-FIRST-ERROR')
            (out/'copy-ready').touch();deadline=time.monotonic()+60
            while not (out/'copy-release').exists():
                if time.monotonic()>deadline:raise TimeoutError('Synthetic copy hold expired')
                time.sleep(.05)
            return original(*values)
        move._copy=copy
    if case=='cleanup_failure':
        def fail_archive(*values):raise OSError('LOCATION-ARCHIVE-FIRST-ERROR')
        move.verify_archive=fail_archive
    if case=='interrupt_before':
        rename=Path.rename
        def stop(path,destination):
            result=rename(path,destination)
            value=move.status()
            if value and str(path)==value['source']:emit('intentional_interruption',phase='SOURCE_RENAMED');os._exit(73)
            return result
        Path.rename=stop
    if case=='interrupt_after':
        atomic=move.atomic_json
        def stop(path,value):
            result=atomic(path,value)
            if path==location.config_file():emit('intentional_interruption',phase='CONFIG_COMMITTED');os._exit(73)
            return result
        move.atomic_json=stop
    chosen=[False];finished=[False];started=time.monotonic()
    def quit_normally():
        if finished[0]:return
        finished[0]=True;win._discard_close=True;win.close()
    def tick():
        try:
            box=QApplication.activeModalWidget()
            if box and box.objectName()=='dataLocationDialog':
                if not chosen[0]:
                    chosen[0]=True;box.target.setText(json.loads((out/'synthetic.json').read_text('utf-8'))['target'])
                    box.grab().save(str(out/'data-location-dialog.png'))
                    QTimer.singleShot(0,box,box.move.click)
                elif case in ('cancel','unsaved_cancel','viewer_failure'):
                    value=move.status()
                    if value and value['state']=='CANCELLED':box.reject();QTimer.singleShot(0,win,quit_normally)
            if case.startswith('reopen') and time.monotonic()-started>.7 and QApplication.activeModalWidget() is None:
                value=move.status();emit('reopened',root=str(win.repository.folder),state=value['state'],phase=value['phase'])
                (out/(case+'-ready')).write_text(str(win.repository.folder),'utf-8');quit_normally()
            if case=='background_fail':
                value=move.status()
                if value and value['state']=='FAILED' and not finished[0]:
                    emit('shutdown_failure_retained',enabled=win.isEnabled(),cleared=getattr(win,'_data_location_request',None) is None)
                    client.fail=False;quit_normally()
            if time.monotonic()-started>90:emit('watchdog_safe_close');quit_normally()
        except BaseException:emit('harness_exception',traceback=traceback.format_exc());quit_normally()
    timer=QTimer(app);timer.setInterval(25);timer.timeout.connect(tick);timer.start();app._location_test_timer=timer
    if not case.startswith('reopen'):
        def begin():
            from data_location_ui import show
            if case=='login_pause':win.browser_startup.timer.setInterval(25);win.browser_startup.timer.start()
            show(win);emit('dialog_returned',pending=bool(getattr(win,'_data_location_request',None)))
        QTimer.singleShot(200,win,begin)
