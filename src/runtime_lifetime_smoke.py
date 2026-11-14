"""Opt-in synthetic acceptance of repeated work I/O and Qt object recycling."""
import hashlib,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QEventLoop
from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QMessageBox,QPushButton
from PySide6.QtTest import QTest


def fingerprint(results):
    from normal_orders import fingerprint as business_fingerprint
    return {code:business_fingerprint(result) for code,result in results.items()}


def churn(round_number,progress,cancel):
    """Normal worker allocations can naturally trigger Python's automatic GC."""
    for n in range(12000):
        value=[round_number,n];value.append(value)
        if n%2000==0:
            if cancel.is_set():
                from runtime_support import Cancelled
                raise Cancelled('Synthetic pressure cancelled')
            progress('合成后台读取压力 '+str(n))
    return round_number


def start_smoke(app,win,args):
    import batch_service,work_repository as wr
    from application_runtime import current
    from version_info import diagnostic
    from runtime_support import Cancelled
    out=Path(args[0]).resolve();mode=args[1]
    spec=json.loads((out/'synthetic.json').read_text('utf-8'))
    assert spec['synthetic'] is True and spec['live_platform'] is False
    assert Path(os.environ['REPLENISHMENT_DATA_DIR']).resolve()==out/'settings'
    assert win.work_repository.path.resolve()==out/'settings/workbench.sqlite3'
    fixture=json.loads((out/'fixture.json').read_text('utf-8'))
    checks=[];messages=[];shots=[];durations=[];heartbeat=[];state={'error':None,'cancel_prepare':False,'cancel_clicked':False}
    began=time.monotonic();last_beat=[began];platform_calls=[]
    # Startup is already excluded by the smoke entry flag. These extra tripwires
    # fail closed if any subsequent scene unexpectedly starts a real component.
    import aoxiang_client,catalog_sync_client
    ax_start=aoxiang_client.ProcessClient.start;jst_start=catalog_sync_client.Client.start
    def deny_platform(*a,**kw):
        platform_calls.append('component_start');raise AssertionError('Synthetic acceptance forbids live platform access')
    aoxiang_client.ProcessClient.start=deny_platform;catalog_sync_client.Client.start=deny_platform
    win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    def automate():
        now=time.monotonic();heartbeat.append(now-last_beat[0]);last_beat[0]=now
        modal=QApplication.activeModalWidget()
        if modal is None:return
        try:
            if state['cancel_prepare'] and getattr(modal,'operation',None)=='prepare_save' and not state['cancel_clicked']:
                state['cancel_clicked']=True;QTest.mouseClick(modal.cancel_btn,Qt.MouseButton.LeftButton);return
            if modal.objectName()=='dailyOverwrite':
                button=next(b for b in modal.findChildren(QPushButton) if b.text()=='覆盖当天记录')
                if not state.get('overwrite_screenshot'):
                    name=mode+'-real-overwrite-confirmation.png';assert modal.grab().save(str(out/name));shots.append(name)
                    state['overwrite_screenshot']=name
                QTest.mouseClick(button,Qt.MouseButton.LeftButton)
            elif isinstance(modal,QMessageBox):
                message={'title':modal.windowTitle(),'text':modal.text()};messages.append(message)
                state['error']='Unexpected native message: '+message['title']+': '+message['text']
                ok=modal.button(QMessageBox.StandardButton.Ok)
                if ok:QTest.mouseClick(ok,Qt.MouseButton.LeftButton)
                else:modal.reject()
        except Exception:
            state['error']=traceback.format_exc();modal.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(automate);timer.start()
    def wait(predicate,seconds=45):
        loop=QEventLoop();watch=QTimer();start=time.monotonic();watch.setInterval(15)
        watch.timeout.connect(lambda:loop.quit() if predicate() or state['error'] or time.monotonic()-start>seconds else None)
        watch.start();loop.exec();watch.stop()
        if state['error']:raise AssertionError(state['error'])
        if not predicate():raise AssertionError('Synthetic UI wait timeout: '+str(state))
    def settle(milliseconds=30):
        loop=QEventLoop();QTimer.singleShot(milliseconds,loop.quit);loop.exec()
        if state['error']:raise AssertionError(state['error'])
    def shot(name):
        filename=mode+'-'+name+'.png';assert win.grab().save(str(out/filename));shots.append(filename)
    def transient(round_number):
        # These are the application's real table-reading controllers and real
        # parent-owned Qt widgets, not mocked controls or bare Python stand-ins.
        from business_ui import grid
        dialog=QDialog(win);dialog.setWindowTitle('合成表单回收核对 '+str(round_number));layout=QVBoxLayout(dialog)
        table,model=grid(dialog,['商品编码','商品名称','数量'],[[str(i),'合成商品 '+str(i),i] for i in range(24)])
        layout.addWidget(table);dialog.show();settle(10);model.filter('合成');dialog.close();dialog.deleteLater()
    def collector_report():
        guard=getattr(current(),'gui_gc',None)
        assert guard is not None,'GUI collector was not installed'
        assert guard.off_thread_collections==0,guard.off_thread_collections
        return {k:getattr(guard,k) for k in ('off_thread_collections','collections','maximum_seconds') if hasattr(guard,k)}
    def finish(error=None):
        timer.stop();aoxiang_client.ProcessClient.start=ax_start;catalog_sync_client.Client.start=jst_start
        guard=getattr(current(),'gui_gc',None)
        gc_info={k:getattr(guard,k) for k in ('off_thread_collections','collections','maximum_seconds') if guard is not None and hasattr(guard,k)}
        report={'passed':error is None,'error':error,'mode':mode,'frozen_exe':bool(getattr(sys,'frozen',False)),
                'version':diagnostic(),'checks':checks,'screenshots':shots,'native_messages':messages,
                'platform_calls':platform_calls,'live_platform':False,'dialogs_not_stubbed':True,
                'round_seconds':durations,'gui_gc':gc_info,'elapsed_seconds':time.monotonic()-began,
                'heartbeat_max_seconds':max(heartbeat,default=0),'default_concurrency':fixture['default_concurrency']}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),'utf-8')
        win.job_dirty=False;win._discard_close=True;win.close()
        # Normal application shutdown drains workers and disposes QApplication.
        # Do not call app.exit here; it would bypass the window close protocol.
    def execute():
        try:
            assert win.concurrency.value()==fixture['default_concurrency']==2
            checks.append('Default calculation concurrency remains two')
            if mode=='exercise':
                win.show_section(1);win.navigate(0);win.sales.add_paths([str(out/'inputs/sales.xlsx')]);win.auto_check.stop()
                QTest.mouseClick(win.load_btn,Qt.MouseButton.LeftButton)
                wait(lambda:win.worker is None and win.loaded is not None)
                assert list(win.loaded['stores'])==fixture['stores']
                for code in fixture['stores']:
                    widget=win.store_widgets[code];widget['mode'].setCurrentIndex(1)
                    widget['primary'].setCurrentIndex(widget['primary'].findData('河北仓'))
                    widget['secondary'].setCurrentIndex(widget['secondary'].findData('山东仓'))
                win.navigate(1);assert win.calculate_btn.isEnabled();shot('prepared-two-stores')
                expected=batch_service.run(win.loaded,{code:win.selected_settings(code) for code in fixture['stores']},win.profile,1)
                expected_signature=fingerprint(expected)
                QTest.mouseClick(win.calculate_btn,Qt.MouseButton.LeftButton)
                wait(lambda:win.worker is None and len(win.results)==len(fixture['stores']))
                assert all(st['state']=='已完成' for st in win.states.values()),win.states
                if fingerprint(win.results)!=expected_signature:
                    (out/'expected-calculation.json').write_text(wr.dumps(expected),'utf-8')
                    (out/'actual-calculation.json').write_text(wr.dumps(win.results),'utf-8')
                assert fingerprint(win.results)==expected_signature
                assert list(win.states)==fixture['stores']
                checks.append('Actual concurrent UI calculation equals serial actual entry point in business values and store order')
                assert win.save_current()
                (out/'expected-results.json').write_text(json.dumps(expected_signature,ensure_ascii=False,indent=2),'utf-8')
                from ui_work_io import Session,request
                before=win.work_repository.get_job(win.job_day)
                state['cancel_prepare']=True
                try:
                    with Session(win.work_repository.path) as session:request(win,session,'prepare_save',win.snapshot())
                except Cancelled:pass
                else:raise AssertionError('Prepare cancellation did not reach its actual cancellation path')
                finally:state['cancel_prepare']=False
                assert state['cancel_clicked']
                assert win.work_repository.get_job(win.job_day)['revision']==before['revision']
                checks.append('Actual prepare-save cancellation preserves original daily revision')
                from business_ui import run
                for round_number in range(fixture['rounds']):
                    started=time.monotonic()
                    win.reset_job();win.resume_today()
                    assert fingerprint(win.results)==expected_signature
                    assert list(win.states)==fixture['stores']
                    for section in (1,0,1):win.show_section(section);settle(5)
                    for page in (0,1,3):win.navigate(page);settle(5)
                    win.open_overview(0,7);settle(10);win.navigate(3)
                    transient(round_number)
                    assert run(win,churn,[round_number],'合成后台读取压力')==round_number
                    # The selected store changes the saved UI state only. It
                    # must not change completed calculation rows or their order.
                    widget=win.store_widgets[fixture['stores'][-1]]['check'];widget.setChecked(not widget.isChecked())
                    assert win.save_current()
                    record=win.work_repository.get_job(win.job_day)
                    assert fingerprint(record['payload']['results'])==expected_signature
                    assert list(record['payload']['states'])==fixture['stores']
                    collector_report();durations.append(time.monotonic()-started)
                    (out/'round-progress.json').write_text(json.dumps({'completed':round_number+1,'total':fixture['rounds']},ensure_ascii=False),'utf-8')
                checks.append(str(fixture['rounds'])+' real restore/rebuild/page-switch/native-confirm/save cycles preserve completed results and order')
                collector_report();checks.append('Natural allocation pressure causes no off-GUI-thread garbage collection')
                checks.append('Actual overwrite confirmation is clicked; QMessageBox and progress dialogs are not stubbed')
                shot('twenty-rounds-complete')
            elif mode=='reopen':
                expected_signature=json.loads((out/'expected-results.json').read_text('utf-8'))
                win.resume_today()
                assert fingerprint(win.results)==expected_signature
                assert list(win.states)==fixture['stores']
                assert all(st['state']=='已完成' for st in win.states.values())
                before=win.work_repository.get_job(win.job_day)['revision'];assert win.save_current()
                assert win.work_repository.get_job(win.job_day)['revision']==before
                collector_report();shot('reopened-saved-results')
                checks.append('Actual second launch restores both stores, complete results, exact order and unchanged saved revision')
            else:raise ValueError(mode)
            assert not platform_calls;checks.append('No platform component was started or called')
            finish()
        except Exception:finish(traceback.format_exc())
    QTimer.singleShot(150,execute)
