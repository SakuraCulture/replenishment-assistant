"""Synthetic-only native duplicate resolution, real worker save and reopen QA."""
import hashlib,json,os,sys,time,traceback
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox


def start_smoke(app,win,args):
    out=Path(args[0]).resolve();mode=args[1]
    marker=json.loads((out/'synthetic.json').read_text('utf-8'))
    if (mode not in ('exercise','reopen') or marker.get('synthetic') is not True
            or marker.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=out/'settings'
            or win.work_repository.path.resolve()!=out/'settings/workbench.sqlite3'):
        raise ValueError('Only verifier-created isolated duplicate acceptance data allowed')
    from lifecycle_duplicate_ui import DuplicateResolution
    from lifecycle_batch_ui import BatchImport
    from business_ui import run
    import order_lifecycle as service
    import lifecycle_batch as batch
    import lifecycle_duplicates as duplicates
    from version_info import diagnostic
    state={'action':None,'seen':set(),'finished':False,'automation_error':None}
    checks=[];shots=[];messages=[];windows=[];started=time.monotonic()
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
    def clean():
        if state['automation_error']:raise AssertionError(state['automation_error'])
    def action(value):state.update(action=value,seen=set(),automation_error=None)
    def finish(error=None):
        if state['finished']:return
        state['finished']=True;timer.stop()
        report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'messages':messages,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'mode':mode,
                'live_platform':False,'database':str(win.work_repository.path),'dialogs_not_stubbed':True}
        (out/(mode+'-report.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        for window in reversed(windows):window.close();window.deleteLater()
        win.job_dirty=False;win._discard_close=True;win.close()
        QTimer.singleShot(100,lambda:app.exit(1 if error else 0))
    def automate():
        modal=QApplication.activeModalWidget()
        if not modal:return
        key=id(modal)
        if key in state['seen']:return
        try:
            if isinstance(modal,DuplicateResolution):
                if state['action'] not in ('cancel','positive','zero','repeat'):raise AssertionError('Unexpected duplicate selection window')
                state['seen'].add(key)
                if len(modal.conflicts)!=10:raise AssertionError('Expected ten independent conflicts')
                if modal.apply_btn.isEnabled() or any(combo.currentData() is not None for combo in modal.choices):raise AssertionError('Duplicate choice had an unsafe default')
                modal.showMaximized()
                def choose():
                    try:
                        shot(modal,state['action']+'-ten-conflicts')
                        if state['action']=='cancel':modal.reject();return
                        quantity='7' if state['action']=='positive' else '0'
                        for index,(conflict,combo) in enumerate(zip(modal.conflicts,modal.choices)):
                            row=next(row for row in conflict['rows'] if row['quantity']==quantity)
                            selected=duplicates.candidate_id(row)
                            combo.setCurrentIndex(next(n for n in range(combo.count()) if combo.itemData(n)==selected))
                            if index==0 and modal.apply_btn.isEnabled():raise AssertionError('One selection unlocked ten unresolved SKUs')
                        if not modal.apply_btn.isEnabled():raise AssertionError('Explicit choices failed to unlock preview')
                        shot(modal,state['action']+'-chosen');emit('native_choices',quantity=quantity,sku_count=10)
                        modal.apply_btn.click()
                    except BaseException:
                        state['automation_error']=traceback.format_exc();modal.reject()
                QTimer.singleShot(70,modal,choose)
            elif isinstance(modal,QMessageBox):
                state['seen'].add(key)
                value={'title':modal.windowTitle(),'text':modal.text(),'detail':modal.detailedText()};messages.append(value)
                if state['action']!='error-message':raise AssertionError('Unexpected real QMessageBox: '+str(value))
                if '河东冲突_10款.xlsx' not in value['detail'] or '定陶正常.xlsx' in value['detail']:raise AssertionError('Error attached the currently selected unrelated source')
                if '第2行' not in value['detail'] or '数量=7' not in value['detail']:raise AssertionError('Real worker error lost source rows/quantities')
                shot(modal,'real-conflict-message');emit('real_message',**value)
                ok=modal.button(QMessageBox.StandardButton.Ok)
                if ok is None:raise AssertionError('Real error message has no OK button')
                ok.click()
            elif isinstance(modal,QDialog) and modal.windowTitle()=='重复商品采用记录':
                state['seen'].add(key);shot(modal,'saved-audit');modal.accept()
        except BaseException:
            state['automation_error']=traceback.format_exc()
            if isinstance(modal,QDialog):modal.reject()
    timer=QTimer(app);timer.setInterval(25);timer.timeout.connect(automate);timer.start();app._lifecycle_duplicate_test_timer=timer
    def exercise():
        try:
            heads=service.current_targets(win.work_repository.path)
            if len(heads)!=2:raise AssertionError('Synthetic fixture must expose two warehouse orders')
            hb=next(h for h in heads if h['warehouse']=='河北仓');sd=next(h for h in heads if h['warehouse']=='山东仓')
            files=[out/'定陶正常.xlsx',out/'河东冲突_10款.xlsx']
            def digest(file):return hashlib.sha256(Path(file).read_bytes()).hexdigest()
            def verify_saved():
                documents=service.history(win.work_repository.path,hb,'return')
                if [d['version'] for d in documents]!=[2,1]:raise AssertionError('Repeat import added a version or expected history disappeared')
                for document,quantity in zip(documents,('0','7')):
                    raw=document['payload']['raw_rows']
                    if len(raw)!=10 or any(r['quantity']!=quantity for r in raw):raise AssertionError('Saved whole-row quantity differs')
                    if any(len(r['duplicate_resolution']['candidates'])!=2 for r in raw):raise AssertionError('Rejected source row audit missing')
                    source=document['payload']['files'][0];target=out/f"{mode}-download-v{document['version']}.xlsx"
                    service.attachment(win.work_repository.path,hb,'return',document['version'],source['sha'],target)
                    if digest(target)!=marker['source_hashes']['河东冲突_10款.xlsx']:raise AssertionError('Retained Excel original differs after resolution')
                other=service.history(win.work_repository.path,sd,'return')
                if len(other)!=1 or other[0]['payload']['raw_rows'][0]['quantity']!='3':raise AssertionError('Other warehouse order changed')
                from lifecycle_ui import Document
                detail=Document(win,win,hb,'return');windows.append(detail);detail.show();app.processEvents()
                if not detail.duplicate_audit.isVisible():raise AssertionError('Saved audit entry missing')
                action('audit');detail.duplicate_audit.click();clean();shot(detail,'saved-version-detail')
                checks.append('reopened exact versions 2/1, quantities 0/7, stable original Excel and independent other warehouse')
            if mode=='reopen':
                verify_saved();checks.append('second actual process retained chosen and rejected source audit');finish();return
            def workspace():
                dialog=BatchImport(win,win,'return',heads);windows.append(dialog);dialog.show();dialog.add_paths([str(p) for p in files])
                if not dialog.read_ready or dialog.issues:raise AssertionError('Native input recognition failed: '+dialog.note.text())
                if len(dialog.documents)!=2:raise AssertionError('Source order grouping differs')
                return dialog
            cancel=workspace();action('cancel');cancel.preview_btn.click();clean()
            if cancel.review or cancel.busy or any(service.history(win.work_repository.path,h,'return') for h in heads):raise AssertionError('Cancel saved or left UI locked')
            checks.append('native cancel preserved files and saved no lifecycle documents');cancel.close()
            for name,quantity in (('positive','7'),('zero','0'),('repeat','0')):
                dialog=workspace();action(name);dialog.preview_btn.click();clean()
                if dialog.review is None:raise AssertionError('Native preview missing: '+dialog.note.text())
                plan=next(p for p in dialog.plan['plans'] if p['head']['warehouse']=='河北仓')
                if any(r['quantity']!=quantity for r in plan['payload']['raw_rows']):raise AssertionError('Choice changed or quantities summed')
                if len(dialog.documents[1]['scan']['rows'])!=20:raise AssertionError('UI overwrote original scanned rows')
                if dialog.review.save.isEnabled():raise AssertionError('Final save confirmation defaulted on')
                shot(dialog,name+'-review');dialog.review.complete.setChecked(True);dialog.review.extra_ack.setChecked(True)
                emit('before_commit',case=name);dialog.review.save.click();emit('after_commit',case=name);clean()
                if not dialog.receipt:raise AssertionError('Actual worker commit did not return a receipt')
                receipt=next(r for r in dialog.receipt if r['number']==hb['number'])
                if name=='repeat' and (receipt['version']!=2 or receipt['changed']):raise AssertionError('Repeat source generated another revision')
                checks.append('real native '+name+' selection and worker commit, exact quantity '+quantity)
            error=workspace();error.table.setCurrentCell(0,1);error.capture()
            try:run(error,batch.prepare,(win.work_repository.path,error.documents,heads),'核查真实冲突提示')
            except RuntimeError as exc:
                action('error-message');error.problem('尚未进入核对页',str(exc));clean()
            else:raise AssertionError('Unresolved conflicting rows unexpectedly passed')
            checks.append('real error dialog identified conflicting file and row numbers independently of current selection')
            verify_saved()
            for file in files:
                if digest(file)!=marker['source_hashes'][file.name]:raise AssertionError('Input workbook bytes changed')
            finish()
        except BaseException:finish(traceback.format_exc())
    QTimer.singleShot(200,win,exercise)
