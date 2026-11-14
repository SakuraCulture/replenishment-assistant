"""Opt-in native signing/deletion/reopen acceptance on verifier-created data."""
import copy,json,os,sys,time,traceback
from contextlib import closing
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox,QPushButton,QCheckBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1]
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if (mode not in ('exercise','reopen') or marker.get('synthetic') is not True
            or marker.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings'
            or win.work_repository.path.resolve()!=out/'settings/workbench.sqlite3'):
        raise ValueError('Only verifier-created isolated warehouse acceptance data allowed')
    import warehouse_execution as execution
    import order_identity,work_repository as wr
    from version_info import diagnostic
    state={'action':None,'seen':set(),'finished':False,'automation_error':None}
    checks=[];shots=[];messages=[];started=time.monotonic()
    win.auto_check.stop();win.jst_generator.stopped=True;win.jst_generator.timer.stop()
    win.aoxiang_workflows.stopping=True;win.aoxiang_workflows.timer.stop()
    win.browser_status.paused=True;win.browser_status.timer.stop();win.browser_status.initial.stop()
    events=out/(mode+'-events.jsonl')
    def emit(event,**values):
        with events.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps({'event':event,**values},ensure_ascii=False)+'\n');stream.flush();os.fsync(stream.fileno())
    def shot(widget,name):
        file=out/(mode+'-'+name+'.png')
        if not widget.grab().save(str(file)):raise AssertionError('Screenshot failed: '+str(file))
        shots.append(file.name)
    def choose(dialog,text):
        for button in dialog.findChildren(QPushButton):
            if button.text()==text:button.click();return
        raise AssertionError('Missing actual button: '+text)
    def action(value,scope=None):state.update(action=value,scope=scope or {},seen=set(),automation_error=None)
    def clean():
        if state['automation_error']:raise AssertionError(state['automation_error'])
    def wait(predicate,timeout=40):
        loop=QEventLoop();watch=QTimer();watch.setInterval(20);begin=time.monotonic()
        watch.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-begin>timeout else None)
        watch.start();loop.exec();watch.stop();clean()
        if not predicate():raise AssertionError('Timed out waiting for actual Qt/spawn result')
    def heads():
        with closing(win.work_repository.connect()) as db:return execution.heads(db)
    def child(store,warehouse):return next(h for h in heads() if h['store']==store and h['warehouse']==warehouse)
    def verify_excel(file,head):
        import openpyxl
        book=openpyxl.load_workbook(file,read_only=True)
        try:
            refs=order_identity.read(book)
            assert book[order_identity.SHEET].sheet_state=='veryHidden'
            assert refs and all(r['warehouse']==head['warehouse'] and r['store']==head['store'] and r['revision']==head['revision'] and r['digest']==head['digest'] and r['number']==head['number'] for r in refs),refs
        finally:book.close()
    def finish(error=None):
        if state['finished']:return
        state['finished']=True;timer.stop()
        report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'messages':messages,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'mode':mode,
                'live_platform':False,'database':str(win.work_repository.path),'dialogs_not_stubbed':True,
                'synthetic_input_adapter':mode=='exercise','elapsed_seconds':time.monotonic()-started}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        emit('finished',passed=error is None)
        win.job_dirty=False;win._discard_close=True;win.close()
        QTimer.singleShot(100,lambda:app.exit(1 if error else 0))
    def automate():
        dialog=QApplication.activeModalWidget()
        if dialog is None:return
        key=(id(dialog),dialog.objectName(),dialog.windowTitle())
        if key in state['seen']:return
        name=dialog.objectName();current=state['action']
        try:
            if name in ('warehouseExecutionSelection','merchantSelection'):
                state['seen'].add(key)
                if current=='cancel-selection':shot(dialog,'cancel-selection');dialog.reject();return
                for store,warehouse,item,enabled in dialog.items:
                    item.setCheckState(Qt.CheckState.Checked if enabled and warehouse in state['scope'].get(store,[]) else Qt.CheckState.Unchecked)
                shot(dialog,current+'-scope')
                if name=='merchantSelection':dialog.destination.setText(str(out/'exports'));dialog.ok.click()
                else:dialog.go.click()
            elif name=='normalConfirm':
                state['seen'].add(key);shot(dialog,current+'-confirmation');choose(dialog,'确认归档 '+str(len(state['scope']))+' 家店')
            elif name=='deliveryConfirm':
                state['seen'].add(key);shot(dialog,current+'-confirmation');choose(dialog,'确认签发并导出')
            elif dialog.windowTitle()=='签发与导出结果':
                state['seen'].add(key);shot(dialog,current+'-result');choose(dialog,'继续本次补货')
            elif dialog.windowTitle()=='彻底删除订单及关联数据':
                state['seen'].add(key)
                assert current=='delete-hb'
                labels=[v.text() for v in dialog.findChildren(__import__('PySide6.QtWidgets',fromlist=['QLabel']).QLabel)]
                assert any('本次保留' in text and '山东仓' in text for text in labels),labels
                boxes=dialog.findChildren(QCheckBox);assert len(boxes)==1
                boxes[0].setChecked(True);shot(dialog,'single-warehouse-delete-confirmation');choose(dialog,'永久删除上述订单')
            elif isinstance(dialog,QMessageBox):
                state['seen'].add(key);value={'action':current,'title':dialog.windowTitle(),'text':dialog.text(),'detail':dialog.detailedText()};messages.append(value);emit('native_message',**value)
                if current=='export-both' and dialog.windowTitle()=='导出已签发版本':dialog.button(QMessageBox.StandardButton.Yes).click();return
                if current=='delete-hb' and dialog.windowTitle()=='已彻底删除':
                    assert '未选仓保留' in dialog.text();shot(dialog,'single-warehouse-delete-success');dialog.button(QMessageBox.StandardButton.Ok).click();return
                raise AssertionError('Unexpected native message: '+str(value))
        except BaseException:
            state['automation_error']=traceback.format_exc();emit('automation_failed',error=state['automation_error'])
            if isinstance(dialog,QDialog):dialog.reject()
    timer=QTimer(app);timer.setInterval(25);timer.timeout.connect(automate);timer.start();app._warehouse_execution_test_timer=timer
    def exercise():
        try:
            payload=wr.loads((out/'payload.json').read_text('utf-8'));payload['day']=wr.today()
            win.restore_job({'day':payload['day'],'saved':'','payload':payload},owned=True)
            # Only source input is synthetic. Selection, prepare, confirmation,
            # spawned writer, SQLite, immutable reads, deletion and Excel are real.
            def synthetic_snapshot():
                value=copy.deepcopy(payload)
                value['execution_warehouses']=copy.deepcopy(win.execution_warehouses)
                value['warehouse_deletion_generations']=copy.deepcopy(win.warehouse_deletion_generations)
                return value
            win.snapshot=synthetic_snapshot;win.show_section(1);win.navigate(3);win.update_controls()
            original=copy.deepcopy(win.results)
            action('cancel-selection');win.issue_btn.click();clean();assert not heads()
            checks.append('Cancel actual execution selector leaves no signed warehouse')
            action('sign-hb',{'QA01':['河北仓']});win.issue_btn.click();clean()
            assert [(h['store'],h['warehouse']) for h in heads()]==[('QA01','河北仓')],heads()
            hb=copy.deepcopy(child('QA01','河北仓'));assert win.results==original
            checks.append('Actual confirm button signs HB only; full dual-warehouse result remains exact')
            action('add-sd',{'QA01':['山东仓']});win.merchant_btn.click();clean()
            sd=copy.deepcopy(child('QA01','山东仓'));assert child('QA01','河北仓')==hb
            assert win.results==original
            files=list((out/'exports').glob('*.xlsx'));assert len(files)==1
            verify_excel(files[0],sd)
            checks.append('Actual sign-and-export button supplements SD without changing HB source or calculation')
            action('sign-control',{'QA02':['河北仓','山东仓']});win.issue_btn.click();clean()
            control=[copy.deepcopy(h) for h in heads() if h['store']=='QA02'];assert len(control)==2
            action('export-both',{'QA01':['河北仓','山东仓']});win.merchant_btn.click();clean()
            files=list((out/'exports').glob('*.xlsx'));assert len(files)==2
            for file in files:verify_excel(file,hb if '河北仓' in file.name else sd)
            assert child('QA01','河北仓')==hb and child('QA01','山东仓')==sd
            checks.append('Actual re-export writes two hidden exact warehouse identities without resigning')
            action('delete-hb')
            import data_maintenance_ui
            data_maintenance_ui.remove(win,win,[hb]);clean()
            assert not any(h['store']=='QA01' and h['warehouse']=='河北仓' for h in heads())
            assert child('QA01','山东仓')==sd
            assert [h for h in heads() if h['store']=='QA02']==control
            assert {row['AU'] for row in win.results['QA01']['rows']}=={'山东仓'}
            checks.append('Actual delete preview and transaction delete HB only, retain SD/control store and project current rows')
            import normal_delivery
            surviving=execution.get_order(win.orders,sd)
            paths=normal_delivery.export_signed(win.orders,surviving,out/'survivor-export',['山东仓'])
            verify_excel(paths[0],sd)
            checks.append('Retained SD source remains exportable after sibling deletion')
            from order_lifecycle import current_targets
            targets=current_targets(win.work_repository.path)
            assert {(h['store'],h['warehouse']) for h in targets}=={('QA01','山东仓'),('QA02','河北仓'),('QA02','山东仓')},targets
            shot(win,'after-single-warehouse-delete')
            (out/'expected.json').write_text(json.dumps({'heads':heads(),'sd':sd,'control':control},ensure_ascii=False,indent=2),'utf-8')
            emit('exercise_complete',active_warehouse_count=len(heads()));finish()
        except BaseException:finish(traceback.format_exc())
    def reopen():
        try:
            expected=json.loads((out/'expected.json').read_text('utf-8'))
            assert heads()==expected['heads'],heads()
            from order_lifecycle import current_targets
            targets=current_targets(win.work_repository.path)
            assert {(h['store'],h['warehouse']) for h in targets}=={('QA01','山东仓'),('QA02','河北仓'),('QA02','山东仓')}
            assert child('QA01','山东仓')==expected['sd']
            order=execution.get_order(win.orders,expected['sd']);assert {r['AU'] for r in order['payload']['result']['rows']}=={'山东仓'}
            for file in (out/'exports').glob('*山东仓*.xlsx'):verify_excel(file,expected['sd'])
            win.show_section(3);win.followup.clear_filters();wait(lambda:len(win.followup.records)==3)
            shot(win,'reopened-three-retained-warehouse-orders')
            checks.extend(['Actual application reopen retains independent heads and deleted HB stays absent','Real lifecycle tracking reader lists surviving SD and both control-store warehouses','Pinned SD snapshot and exported very-hidden signature identity survive reopen'])
            finish()
        except BaseException:finish(traceback.format_exc())
    QTimer.singleShot(0,exercise if mode=='exercise' else reopen)
