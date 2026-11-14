"""Opt-in actual Qt/spawn signing acceptance; uses only tool-created synthetic data."""
import copy,json,sqlite3,sys,time,traceback
from contextlib import closing
from datetime import date,timedelta
from pathlib import Path
from PySide6.QtCore import QTimer,QEventLoop,Qt
from PySide6.QtWidgets import QApplication,QDialog,QCheckBox,QPushButton,QMessageBox
import work_repository as wr
from version_info import diagnostic


def start_smoke(app,win,args):
    fixture,out=map(Path,args[:2]);out=out.resolve();checks=[];shots=[];messages=[];state={'mode':None,'seen':set()}
    payload=wr.loads(fixture.read_text('utf-8'));codes=list(payload['results'])
    expected=20
    def shot(widget,name):
        path=out/name
        if not widget.grab().save(str(path)):raise AssertionError('Screenshot failed: '+name)
        shots.append(name)
    def choose(dialog,text):
        for button in dialog.findChildren(QPushButton):
            if button.text()==text:button.click();return
        raise AssertionError('No modal button: '+text)
    def automate():
        dialog=QApplication.activeModalWidget()
        if dialog is None:return
        identity=(id(dialog),dialog.objectName(),dialog.windowTitle())
        if identity in state['seen']:return
        name=dialog.objectName();mode=state['mode']
        if mode=='export' and dialog.windowTitle()=='签发与导出结果':
            state['seen'].add(identity);shot(dialog,'twenty-files-export-result.png');choose(dialog,'继续本次补货');return
        if name not in ('storeScopeSelection','normalConfirm','merchantSelection','deliveryConfirm') and not isinstance(dialog,QMessageBox):return
        state['seen'].add(identity)
        try:
            if name=='storeScopeSelection':
                if mode=='cancel-selection':shot(dialog,'cancel-store-selection.png');dialog.reject();return
                for item in dialog.findChildren(QCheckBox):
                    if item.isEnabled():item.setChecked(True)
                choose(dialog,'下一步：核对归档')
            elif name=='normalConfirm':
                if mode=='cancel-confirm':shot(dialog,'cancel-signature-confirmation.png');dialog.reject();return
                shot(dialog,'signature-confirmation.png');choose(dialog,'确认归档 '+str(len(codes))+' 家店')
            elif name=='merchantSelection':
                dialog.destination.setText(str(out/'exports'));dialog.select_all(True)
                shot(dialog,'twenty-warehouse-export-selection.png');dialog.accept()
            elif name=='deliveryConfirm':
                shot(dialog,'trigger-signature-confirmation.png');choose(dialog,'确认签发并导出')
            elif isinstance(dialog,QMessageBox):
                text=dialog.text();detail=dialog.detailedText();messages.append({'mode':mode,'title':dialog.windowTitle(),'text':text,'detail':detail})
                shot(dialog,mode+'-message.png')
                if mode=='trigger-failure':
                    assert 'IntegrityError' in text and 'QA_SIGNING_TRIGGER' in text,text
                    assert '提交状态：未开始最终提交' in text and '诊断记录：' in text,text
                    assert '关闭占用文件' not in text,text
                elif mode=='memory-message':
                    assert 'MemoryError' in text and '无法继续分配内存' in text,text
                    assert '最终提交结果待核' in text and '关闭占用文件' not in text,text
                elif mode=='legacy-message':
                    assert '提交状态尚未核实' in text and '关闭占用文件' not in text,text
                elif mode=='export':
                    assert dialog.windowTitle()=='导出已签发版本',dialog.windowTitle()
                    dialog.button(QMessageBox.StandardButton.Yes).click();return
                else:raise AssertionError('Unexpected modal '+str(mode)+': '+dialog.windowTitle()+': '+text)
                dialog.accept()
        except Exception:
            state['automation_error']=traceback.format_exc();dialog.reject()
    timer=QTimer(win);timer.setInterval(25);timer.timeout.connect(automate);timer.start()
    def mode(value):state.update(mode=value,seen=set());state.pop('automation_error',None)
    def clean():
        if state.get('automation_error'):raise AssertionError(state['automation_error'])
    def wait(predicate,timeout=25):
        loop=QEventLoop();watch=QTimer();watch.setInterval(20);started=time.monotonic()
        watch.timeout.connect(lambda:loop.quit() if predicate() or time.monotonic()-started>timeout else None)
        watch.start();loop.exec();watch.stop()
        if not predicate():raise AssertionError('Timed out waiting for actual Qt result')
        clean()
    def finish(error=None):
        if state.get('finished'):return
        state['finished']=True;timer.stop()
        report={'passed':error is None,'error':error,'checks':checks,'screenshots':shots,'messages':messages,
                'frozen_exe':bool(getattr(sys,'frozen',False)),'version':diagnostic(),'live_platform':False,
                'database':str(win.work_repository.path),'parent_count':len(win.orders.headers())}
        (out/'ui-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        win.job_dirty=False;win._discard_close=True;win.close()
        QTimer.singleShot(100,lambda:app.exit(1 if error else 0))
    def exercise():
        original_snapshot=win.snapshot
        try:
            assert win.work_repository.path.resolve()==(out/'settings/workbench.sqlite3').resolve(),'QA settings escaped private root'
            payload['day']=wr.today()
            win.restore_job({'day':payload['day'],'saved':'','payload':payload},owned=True)
            # Input is entirely generated by the verifier; normal prepare, confirm,
            # SQLite commit, fixed-revision read and export remain the real code.
            win.snapshot=lambda:copy.deepcopy(payload)
            for field,value,fragment in [('worker',object(),'另一个作业'),('input_dirty',True,'输入资料已变化'),('readonly',True,'只读工作区'),('job_day',(date.fromisoformat(wr.today())-timedelta(days=1)).isoformat(),'不是今日')]:
                previous=getattr(win,field);setattr(win,field,value)
                try:win.confirm_normal();assert fragment in win.status.text(),win.status.text()
                finally:setattr(win,field,previous)
            assert not win.orders.headers();shot(win,'blocked-entry-feedback.png');checks.append('Four entry guards explain this attempt without starting a signature')
            mode('cancel-selection');win.confirm_normal();clean()
            assert '本次未发起签发' in win.status.text() and not win.orders.headers()
            mode('cancel-confirm');win.confirm_normal();clean()
            assert '已取消本次签发确认' in win.status.text() and not win.orders.headers()
            checks.append('Real store selection and confirmation cancellation leave no signature and replace stale status')
            mode('confirm');win.confirm_normal();clean()
            heads=win.orders.headers();assert len(heads)==11 and all(h['revision']==1 for h in heads),heads
            assert all(h['number'] in win.status.text() for h in heads),win.status.text()
            shot(win,'signature-success-identities.png');checks.append('Actual cross-process first confirmation commits 11 exact BH V001 parents')
            win.show_section(3);win.followup.clear_filters()
            wait(lambda:len(win.followup.records)==expected)
            assert len({(h['oid'],h['warehouse']) for h in win.followup.records})==expected
            shot(win,'tracking-twenty-warehouse-orders.png');checks.append('Actual tracking reader lists all 20 warehouse orders from the same database')
            mode('export');(out/'exports').mkdir();win.export_current();clean()
            assert len(list((out/'exports').glob('*.xlsx')))==expected
            assert [(h['oid'],h['revision'],h['digest']) for h in win.orders.headers()]==[(h['oid'],h['revision'],h['digest']) for h in heads]
            checks.append('Real merchant selection exports 20 fixed-revision files without a second signature')
            with closing(win.work_repository.connect()) as db,db:
                db.execute("CREATE TRIGGER qa_signing_failure BEFORE INSERT ON normal_events BEGIN SELECT RAISE(ABORT,'QA_SIGNING_TRIGGER'); END")
            first=codes[0];payload['results'][first]['rows'][0]['AS']+=1
            item=payload['results'][first]['rows'][0];item['_candidate_qty']=item['AS'];item['BV']=item['_candidate_amount']=item['AS']*item['V']
            import result_semantics as sem
            payload['results'][first]['summary']=sem.summarize(payload['results'][first]['rows'])
            win.results=payload['results'];mode('trigger-failure');win.export_current();clean()
            assert any(m['mode']=='trigger-failure' for m in messages)
            assert [(h['oid'],h['revision'],h['digest']) for h in win.orders.headers()]==[(h['oid'],h['revision'],h['digest']) for h in heads]
            evidence=list((out/'settings/diagnostics').glob('work-io-*.json'));assert evidence
            errors=[json.loads(p.read_text('utf-8')) for p in evidence]
            assert any(e['exception_type']=='IntegrityError' and 'QA_SIGNING_TRIGGER' in e['traceback'] for e in errors)
            checks.append('Actual SQLite failure crosses Session with type, stack and saved diagnostic; transaction rolls back')
            with closing(win.work_repository.connect()) as db,db:db.execute('DROP TRIGGER qa_signing_failure')
            from work_failures import FailureContext,WorkFailure
            from readable_ui import failure
            context=FailureContext('synthetic-memory-render');context.stage='合成内存异常展示';context.commit_state='unknown'
            mode('memory-message');failure(win,'合成异常验收',WorkFailure(context.capture(MemoryError(),win.work_repository.path)));clean()
            mode('legacy-message');failure(win,'旧回执验收','导出操作未全部完成：签发结果待核。\n已生成 0/20 份：无\n未生成：合成旧路径.xlsx\n可到“订单跟踪 → 导出任务与恢复”查看逐文件结果。');clean()
            checks.append('Real QMessageBox renders bare MemoryError and legacy unknown-commit receipt with readable stage and safe guidance')
            win.snapshot=original_snapshot;finish()
        except Exception:
            win.snapshot=original_snapshot;finish(traceback.format_exc())
    QTimer.singleShot(350,exercise)
