"""Run both import-recovery windows against an isolated, controlled platform."""
import copy
import json
import os
import sys
import time
import traceback
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest


def start_smoke(app, win, args):
    root, phase = Path(args[0]).resolve(), args[1]
    fixture = json.loads((root/'import-failure-fixture.json').read_text('utf-8'))
    if (phase not in ('exercise','reopen','preview') or fixture.get('synthetic') is not True
            or fixture.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=root/'settings'
            or win.work_repository.path.resolve()!=root/'settings/workbench.sqlite3'):
        raise ValueError('必须使用平台导入失败验收的隔离目录')

    def run():
        from aoxiang_workflow_ui import QueueDialog
        import catalog_session_ui
        from jst_auto_order_store import read
        from jst_auto_order_ui import Orders
        from jst_one_click_fixture import Component
        import jst_orders as inventory
        import order_lifecycle as lifecycle
        import work_repository as wr
        from version_info import diagnostic

        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),
                'synthetic':True,'live_platform':False,'phase':phase}
        manager=win.aoxiang_workflows
        win.auto_check.stop()
        win.jst_generator.stopped=True
        win.browser_status.paused=True
        win.browser_status.initial.stop()
        for task in (manager,win.jst_generator,win.jst_fulfillment_runner,win.browser_status,win.scheduled):
            task.timer.stop()

        if phase=='preview':
            try:
                report.update(preview_case(app,win,root,fixture),passed=True,build=diagnostic())
            except Exception:
                report['error']=traceback.format_exc()
            (root/'preview-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            win.job_dirty=False
            win._discard_close=True
            win.close()
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
            return

        job_id=fixture['ax_batch']
        row=manager.store.get(job_id)['items'][0]
        purchase_id=row['package']['id']
        affected=fixture['affected']
        task_id=fixture['task']
        failure={'taskId':task_id,'kind':'failed','errorCode':'IMPORT_TASK_FAILED','nativeStatus':'部分失败',
                 'expectedCount':601,'successCount':0,'failCount':601,'observedAt':'2026-11-25T12:15:17Z'}
        events=[]
        record={'id':purchase_id,'taskId':task_id,'state':'ACCEPTED','uploadAttempted':True,
                'confirmedImportRequests':1,'additionalTaskIds':[],'active':False,'error':None}
        if phase=='reopen':
            record.update(state='NEEDS_REVIEW',importResult=failure,error={'code':'IMPORT_TASK_FAILED'})

        class Aoxiang:
            def __init__(self,*_):self.running=False
            def start(self):self.running=True
            def close(self,timeout=.5):self.running=False;return True
            def request(self,action,**values):
                if action=='configuration':return {'purchase_pipeline':True}
                if action=='session.release':return {'idle':True}
                assert values.get('purchase_id')==purchase_id
                if action=='purchase.status':return copy.deepcopy(record)
                events.append((action,task_id))
                if action=='purchase.start':
                    assert phase=='exercise' and values['expected_task_id']==task_id
                    record.update(state='NEEDS_REVIEW',importResult=failure,
                                  error={'code':'IMPORT_TASK_FAILED','message':'合成回执 0/601'})
                elif action=='purchase.verify':
                    record['error']={'code':'TASK_NOT_FOUND','message':'合成暂时查不到原任务'}
                else:
                    raise AssertionError('意外的翱象请求 '+action)
                return copy.deepcopy(record)

        class Jst(Component):
            def order(self,action,spec,values):
                if action=='order.import' and values['warehouse']==affected:
                    source=self.permits.pop(values['permit'])
                    return {'dispatched':True,'receipt':{'online':source['online'],'binding':source['binding'],
                        'success_count':0,'fail_count':1,'text':'成功条数：0，失败条数：1',
                        'observed_at':'2026-11-25T12:15:17Z'}}
                return super().order(action,spec,values)

        path=win.work_repository.path
        platform=Jst(root/'jst-platform')
        ax_window=jst_window=None

        def wait(predicate):
            deadline=time.monotonic()+25
            while not predicate() and time.monotonic()<deadline:
                app.processEvents()
                QTest.qWait(20)
            if not predicate():raise TimeoutError('隔离验收未读到作业结束')

        def stock():
            with closing(wr.read_connection(path)) as db:
                return {wh:{sku:str(qty) for sku,qty in inventory.reservations(db,wh,set()).items()}
                        for wh in ('山东仓','河北仓')}

        try:
            before=stock()
            untouched=manager.store.get(job_id)['items'][1:]
            if manager.session.client is not None:
                assert manager.session.close()
            manager.client_factory=Aoxiang
            ax_window=QueueDialog(win,manager,job_id)
            ax_window.resize(1420,820)
            ax_window.show()
            if phase=='exercise':
                manager.tick()
                wait(lambda:manager.worker is None and manager.store.get(job_id)['state']=='COMPLETED_WITH_ERRORS')
                assert events==[('purchase.start',task_id)],events
            ax_window.refresh()
            for n in range(2):
                assert ax_window.table.item(0,4).text()=='导入失败'
                assert '成功 0、失败 601' in ax_window.table.item(0,7).text()
                assert ax_window.grab().save(str(root/f'{phase}-翱象-{n+1}.png'))
                with patch('aoxiang_upload_pipeline.original_units',return_value={'0001':'件'}):
                    ax_window.table.cellWidget(0,8).click()
                    wait(lambda:manager.worker is None and not manager.store.get(job_id).get('verification_queue'))
                ax_window.refresh()
            saved=manager.store.get(job_id)
            assert saved['items'][1:]==untouched and len(untouched)==15
            assert saved['items'][0]['purchase_observation']['importResult']==failure
            assert len([event for event in events if event[0]=='purchase.verify'])==2
            assert all(action in ('purchase.start','purchase.verify') for action,_ in events)
            report['aoxiang']={'row':saved['items'][0],'other_15_unchanged':True,'calls':events,
                               'new_uploads':0,'batch_state':saved['state']}
            ax_window.close()

            jst_window=Orders(win,win,fixture['jst_keys'])
            jst_window.show()
            with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=platform)):
                if phase=='exercise':
                    jst_window.buttons['preview'].click()
                    assert jst_window.buttons['submit'].isEnabled()
                    with patch('jst_auto_order_ui.confirm',return_value=True):
                        jst_window.buttons['submit'].click()
                failed=next(n for n,value in enumerate(jst_window.records) if value['warehouse']==affected)
                jst_window.select(False)
                jst_window.table.item(failed,0).setCheckState(Qt.CheckState.Checked)
                assert not jst_window.buttons['submit'].isEnabled()
                for n in range(2):
                    assert jst_window.table.item(failed,11).text()=='导入失败'
                    assert '成功 0 张、失败 1 张' in jst_window.table.item(failed,13).text()
                    assert jst_window.grab().save(str(root/f'{phase}-聚水潭-{n+1}.png'))
                    jst_window.buttons['recover'].click()
            attempts=[item for item in platform.calls if item[0]=='order.import']
            assert len(attempts)==(2 if phase=='exercise' else 0), attempts
            with closing(wr.read_connection(path)) as db:
                orders=[read(db,key,workbook=True) for key in fixture['jst_keys']]
                for order in orders:
                    head=order['payload']['head']
                    assert lifecycle.current(db,head,'payment') is None and lifecycle.current(db,head,'receipt') is None
                failed,= [r for r in orders if r['payload']['head']['warehouse']==affected]
                sibling,= [r for r in orders if r is not failed]
                assert failed['stage']=='IMPORT_UNKNOWN' and failed['internal'] is None
                receipt=failed['proof']['import_receipt']
                assert receipt['fail_count']==1 and sibling['internal']
            assert stock()==before
            report['jst']={'rejected':receipt,'failed_file_sha':failed['file_sha'],'other_warehouse_internal':sibling['internal'],
                           'calls':platform.calls,'new_imports':len(attempts),'stock_reservations':before,
                           'formal_payment_count':0,'formal_receipt_count':0}
            report.update(passed=True,build=diagnostic())
        except Exception:
            report['error']=traceback.format_exc()
        finally:
            for dialog in (ax_window,jst_window):
                if dialog:dialog.close()
            win.job_dirty=False
            win._discard_close=True
            win.close()
            try:wait(lambda:manager.owner is None)
            except Exception:report.update(passed=False,shutdown_error=traceback.format_exc())
            (root/f'{phase}-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
            QTimer.singleShot(100,lambda:app.exit(0 if report['passed'] else 1))
    QTimer.singleShot(250,run)


def preview_case(app,win,root,fixture):
    from PySide6.QtWidgets import QMessageBox
    import catalog_session_ui
    import jst_auto_order_store as store
    from jst_auto_order_ui import Orders
    from lifecycle_home_ui import StageDialog
    from platform_import_manual_fixture import JstManual
    import work_repository as wr

    case=json.loads((root/'pending-preview.json').read_text('utf-8'))
    keys=fixture['jst_keys']
    portal=JstManual(root/'jst-test')
    path=win.work_repository.path
    ready=case['ready']
    report={'expected_ready':ready,'keys':keys}
    errors=[]

    def handle():
        dialog=app.activeModalWidget()
        if not isinstance(dialog,Orders):
            errors.append('自动下单任务窗口未打开')
            if dialog:dialog.reject()
            return
        try:
            for i,row in enumerate(dialog.records):
                if row['online'] in keys:
                    dialog.table.item(i,0).setCheckState(Qt.CheckState.Checked)
            others={r['online']:(r['stage'],r['sequence']) for r in dialog.records if r['online'] not in keys}
            assert set(keys)=={r['online'] for r in dialog.selected()}
            assert dialog.buttons['preview'].isEnabled() and not dialog.buttons['submit'].isEnabled()
            dialog.buttons['probe'].click()
            dialog.buttons['preview'].click()
            report['rows_after_preview']={r['online']:{k:r[k] for k in ('stage','stage_label','reason','preview_amount')}
                                          for r in dialog.records if r['online'] in keys}
            assert [key for key in keys if report['rows_after_preview'][key]['stage']=='PREVIEWED']==ready
            report['hint']=dialog.selection_hint.text()
            report['preview_calls']=[key for action,wh,key in portal.calls if action=='order.preview']
            assert set(report['preview_calls'])==set(case['preview']) and len(report['preview_calls'])==len(case['preview'])
            assert not any(action=='order.import' for action,wh,key in portal.calls)
            assert dialog.buttons['submit'].isEnabled()==bool(ready)
            dialog.table.horizontalScrollBar().setValue(dialog.table.horizontalScrollBar().maximum())
            QTest.qWait(80)
            assert dialog.grab().save(str(root/'预览后按钮.png'))
            if ready:
                answer=[]
                def choose(button):
                    box=app.activeModalWidget()
                    if not isinstance(box,QMessageBox):return
                    answer.append(box.text())
                    box.button(button).click()
                for button in (QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Yes):
                    QTimer.singleShot(100,lambda b=button:choose(b))
                    dialog.buttons['submit'].click()
                    assert all(k in answer[-1] for k in ready) and all(k not in answer[-1] for k in keys if k not in ready)
                    if button==QMessageBox.StandardButton.Cancel:
                        assert not any(a=='order.import' for a,wh,k in portal.calls)
                report['confirmation']=answer[-1]
            report['actual_submissions']=[key for a,wh,key in portal.calls if a=='order.import']
            assert report['actual_submissions']==ready
            assert others=={r['online']:(r['stage'],r['sequence']) for r in dialog.records if r['online'] not in keys}
            with closing(wr.read_connection(path)) as db:
                for key in keys:
                    original=store.read(db,key)
                    assert bool(original['internal'])==(key in ready)
                    assert original['file_sha']==case['file_sha'][key]
            report['calls']=portal.calls
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            dialog.reject()

    stage=StageDialog(win.followup,'jst')
    try:
        stage.setModal(True)
        stage.show()
        deadline=time.monotonic()+25
        while stage.panel.data is None and time.monotonic()<deadline:QTest.qWait(20)
        assert stage.panel.data is not None
        with patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=portal)):
            QTimer.singleShot(150,handle)
            stage.panel.auto_tasks_btn.click()
        assert not errors,errors
        assert 'actual_submissions' in report
    finally:
        stage.reject()
    return report
