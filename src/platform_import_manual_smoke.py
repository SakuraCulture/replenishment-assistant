"""Actual Qt entry points with isolated business data and controlled platform replies."""
from contextlib import closing
import copy
from functools import partial
import json
import os
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QLineEdit, QMessageBox, QPushButton


def start_smoke(app, win, args):
    root,phase=Path(args[0]).resolve(),args[1]
    fixture=json.loads((root/'manual-fixture.json').read_text('utf-8'))
    if (phase not in ('exercise','reopen') or fixture.get('synthetic') is not True
            or fixture.get('live_platform') is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=root/'settings'
            or win.work_repository.path.resolve()!=root/'settings/workbench.sqlite3'):
        raise ValueError('必须使用平台人工处理验收的隔离目录')
    QTimer.singleShot(220,lambda:exercise(app,win,root,fixture,phase))


def exercise(app,win,root,fixture,phase):
    from aoxiang_directory import atomic_json, file_hash
    import aoxiang_purchase_ui as purchase_ui
    from aoxiang_workflow_ui import QueueDialog
    import catalog_session_ui
    from jst_auto_order_ui import Orders
    import jst_manual_order as jst
    import jst_manual_order_ui
    import jst_orders as inventory
    import lifecycle_store
    from platform_import_manual_fixture import JstManual
    import work_repository as wr
    from version_info import diagnostic

    manager=win.aoxiang_workflows
    win.auto_check.stop()
    win.jst_generator.stopped=True
    win.browser_status.paused=True
    win.browser_status.initial.stop()
    for task in (manager,win.jst_generator,win.jst_fulfillment_runner,win.browser_status,win.scheduled):
        task.timer.stop()
    path=win.work_repository.path
    affected=fixture['warehouse']
    jst_key,sibling=fixture['jst_keys']
    portal=JstManual(root/'jst-test')
    jst_portal=root/'manual-jst-state.json'
    portal.orders=json.loads(jst_portal.read_text('utf-8'))
    ax_state=root/'manual-component-state.json'
    ax_records=json.loads(ax_state.read_text('utf-8'))
    a,b=ax_records
    ax_sibling=copy.deepcopy(ax_records[b])
    attempts=[]
    worker_errors=[]
    reports={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),
             'synthetic':True,'live_platform':False,'warehouse':affected,'phase':phase}

    def wait(predicate):
        stop=time.monotonic()+25
        while not predicate() and not worker_errors and time.monotonic()<stop:
            app.processEvents()
            QTest.qWait(15)
        if worker_errors:raise AssertionError(worker_errors[-1])
        if not predicate():
            raise TimeoutError('等待隔离操作结束超时')

    def facts():
        with closing(wr.read_connection(path)) as db:
            rows=[jst.record(path,key) for key in (jst_key,sibling)]
            protected={row['payload']['head']['warehouse']:
                {stage:lifecycle_store.version_row(db,row['payload']['head'],stage)[:4]
                 if lifecycle_store.version_row(db,row['payload']['head'],stage) else None
                 for stage in ('payment','receipt')} for row in rows}
            # Keep the version identity immutable without storing payment bodies in evidence.
            protected={warehouse:{stage:[str(v) if not isinstance(v,bytes) else str(len(v)) for v in values] if values else None
                         for stage,values in documents.items()} for warehouse,documents in protected.items()}
            return {'versions':protected,'held':{wh:{sku:str(qty) for sku,qty in inventory.reservations(db,wh,set()).items()}
                                                for wh in ('河北仓','山东仓')}}

    def present(window,parent_list):
        parent_list.append(window)
        window.show()
        return window

    def input_fields(click,title,values,accept_button='继续'):
        supplied=[]
        timer=QTimer()
        def fill():
            dialog=app.activeModalWidget()
            if dialog and dialog.windowTitle()==title:
                dialog_lines=dialog.findChildren(QLineEdit)
                assert len(dialog_lines)==len(values)
                for field,value in zip(dialog_lines,values):field.setText(value)
                timer.stop()
                supplied.append(True)
                next(btn for btn in dialog.findChildren(QPushButton) if btn.text()==accept_button).click()
        timer.timeout.connect(fill)
        timer.start(20)
        try:click.click()
        finally:timer.stop()
        assert supplied,title

    def save(button, filename):
        target=root/filename
        assert button.isEnabled(), button.text()
        with patch.object(QFileDialog,'getSaveFileName',return_value=(str(target),'')):
            button.click()
        return target

    def unexpected(*args):
        raise AssertionError(str(args[2]))

    class AX:
        def __init__(self,*args):self.running=False
        def start(self):self.running=True
        def close(self,timeout=.5):self.running=False;return True
        def request(self,action,**values):
            attempts.append(action)
            if action=='configuration':return fixture['configuration']
            if action=='purchase.list':return {'total':len(ax_records),'items':list(copy.deepcopy(ax_records).values())}
            if action=='session.release':return {'idle':True}
            assert values.get('purchase_id')==a
            record=ax_records[a]
            handoff=record.get('manualHandoff')
            if action=='purchase.status':return copy.deepcopy(record)
            if action=='binding.status':
                return {'purchase_id':a,'sequence':record['sequence'],'proposal':record.get('bindingProposal'),'active':False}
            if action=='purchase.handoff':
                assert record['sequence']==values['sequence'] and record['importResult']['successCount']==0
                record.update(state='MANUAL_HANDOFF',manualHandoff={'id':'handoff-controlled-'+affected,
                    'operator':values['operator'],'reason':values['reason'],'at':wr.now()})
            elif action in ('purchase.export_source','purchase.exported'):
                usage=values['usage']
                if usage=='manual':assert handoff and not record['binding'] and len(values['host_facts'])==64
                source=record['importResult']['failureFile'] if usage=='failure' else record['originalFile']
                if action=='purchase.export_source':return copy.deepcopy(source)
                receipt={'path':values['output'],'sha256':values['export_sha256'],'usage':usage}
                assert receipt['sha256']==source['sha256']==file_hash(receipt['path'])
                filed=record.setdefault('originalExports',[])
                if receipt in filed:return receipt
                filed.append(receipt)
            elif action=='binding.start':
                request=values['binding_request']
                assert request['order_id']=='PO-QA-ADOPT-'+affected and request['original_units']
                manifest=root/'controlled-AX-proof.json'
                sku=next(iter(request['original_units']))
                rows=[{'sku':sku,'unit':request['original_units'][sku],'quantity':'3','price':'3.50','amount':'10.50'},
                      {'sku':'@00000001','unit':'件','quantity':'2','price':'3','amount':'6'}]
                atomic_json(manifest,{'evidence':{'rows':rows,'observedAt':wr.now()}})
                record['bindingProposal']={'id':'proposal-QA','state':'READY','request':request,
                    'manifest':{'file':str(manifest),'sha256':file_hash(manifest)},'summary':{
                        'status':'部分入库','rows':2,'quantity':'5','amount':'16.50','differences':2}}
            elif action=='binding.confirm':
                proposal=record['bindingProposal']
                assert proposal['id']==values['proposal_id'] and proposal['manifest']['sha256']==values['manifest_sha256']
                assert values['host_facts']==proposal['request']['host_facts'] and values['sequence']==record['sequence']
                record.update(state='BOUND',binding={'orderId':proposal['request']['order_id'],'status':'部分入库',
                    'taskId':record['taskId'],'proposalId':proposal['id'],'proofSha256':proposal['manifest']['sha256']})
                proposal['state']='CONFIRMED'
            else:
                raise AssertionError('不应派发此动作 '+action)
            record['sequence']+=1
            atomic_json(ax_state,ax_records)
            if action=='purchase.exported':return receipt
            if action=='binding.start':return self.request('binding.status',purchase_id=a)
            return copy.deepcopy(record)

    windows=[]
    try:
        before=facts()
        original_jst_sibling=jst.record(path,sibling)
        ax_item=manager.store.get(fixture['ax_job'])['items'][0]
        other_jst=jst.record(path,jst_key)
        if manager.session.client is not None:assert manager.session.close()
        manager.client_factory=AX
        queue=present(QueueDialog(win,manager,fixture['ax_job']),windows)
        queue.table.selectRow(0)
        assert queue.manual_btn.isEnabled()
        with (patch.object(purchase_ui,'show_modal',side_effect=lambda _,dialog:present(dialog,windows)),
              patch.object(purchase_ui,'PurchaseDialog',partial(purchase_ui.PurchaseDialog,client_factory=AX))):
            queue.manual_btn.click()
        ax=windows[-1]
        ax.failure=worker_errors.append
        wait(lambda:ax.current is not None and not ax.worker)
        assert ax.current['id']==a and ax.head['warehouse']==affected
        jst_window=present(Orders(win,win,fixture['jst_keys']),windows)
        jst_window.select(False)
        row=next(n for n,r in enumerate(jst_window.records) if r['online']==jst_key)
        jst_window.table.item(row,0).setCheckState(Qt.CheckState.Checked)
        assert jst_window.manual_btn.isEnabled()
        with patch.object(jst_manual_order_ui.ManualOrderDialog,'exec',lambda dialog:present(dialog,windows)):
            jst_window.manual_btn.click()
        jd=windows[-1]
        assert jd.row['online']==jst_key
        if phase=='exercise':
            with (patch.object(QMessageBox,'warning',side_effect=unexpected),
                  patch.object(catalog_session_ui,'manager',return_value=SimpleNamespace(session=portal)),
                  patch('jst_manual_order_ui.rows_dialog'),patch('jst_manual_order_ui.confirm',return_value=True)):
                assert not jd.manual_file.isEnabled()
                assert file_hash(save(jd.check_file,'核对-聚水潭.xls'))==other_jst['file_sha']
                input_fields(jd.takeover,'接管聚水潭导入',['隔离经办','原线上单号查无已建单'])
                assert jd.manual_file.isEnabled()
                saved=save(jd.manual_file,'手工-聚水潭.xls')
                assert file_hash(saved)==file_hash(save(jd.manual_file,saved.name))==other_jst['file_sha']
                assert jd.grab().save(str(root/'聚水潭-已接管.png'))
                portal.accepted(jst.record(path,jst_key),'88001',changes=True)
                atomic_json(jst_portal,portal.orders)
                input_fields(jd.lookup,'核对已有聚水潭订单',['88001','隔离经办','核对实际平台明细'])
                assert jd.bind.isEnabled() and not jd.manual_file.isEnabled()
                jd.bind.click()
            assert jd.row['internal']=='88001'
            assert not ax.manual_export_btn.isEnabled()
            for control, filename, digest in (
                (ax.check_export_btn,'核对-翱象.xlsx',ax_records[a]['originalFile']['sha256']),
                (ax.failure_export_btn,'失败明细-翱象.xlsx',ax_records[a]['importResult']['failureFile']['sha256'])):
                target=save(control,filename)
                wait(lambda:not ax.worker)
                assert file_hash(target)==digest
            input_fields(ax.handoff_btn,'接管翱象导入',['隔离经办','查到失败且未成采购单'])
            wait(lambda:ax.manual_export_btn.isEnabled() and not ax.worker)
            for _ in range(2):
                target=save(ax.manual_export_btn,'手工-翱象.xlsx')
                wait(lambda:not ax.worker)
                assert file_hash(target)==ax_records[a]['originalFile']['sha256']
            assert ax.grab().save(str(root/'翱象-已接管.png'))
            input_fields(ax.binding_start_btn,'核对已有翱象采购单',['PO-QA-ADOPT-'+affected,'','隔离经办','保留原采购'],'开始核对')
            wait(lambda:ax.binding_confirm_btn.isEnabled() and not ax.worker)
            assert (ax.binding_record['proposal']['summary']['differences'])==2
            with patch.object(QMessageBox,'question',return_value=QMessageBox.StandardButton.Yes):
                ax.binding_confirm_btn.click()
            wait(lambda:ax.current.get('state')=='BOUND' and not ax.worker)
        assert jd.row['internal']=='88001' and not jd.manual_file.isEnabled()
        assert ax.current['state']=='BOUND' and not ax.manual_export_btn.isEnabled()
        assert jst.record(path,sibling)==original_jst_sibling
        assert ax_records[b]==ax_sibling
        assert not {'purchase.start','purchase.submit','purchase.reserve','purchase.rebuild'} & set(attempts)
        assert not any(event[0]=='order.import' for event in portal.calls)
        assert before==facts()
        assert ax.grab().save(str(root/(phase+'-翱象-已关联.png')))
        assert jd.grab().save(str(root/(phase+'-聚水潭-已关联.png')))
        with closing(wr.read_connection(path)) as db:
            receipts=json.loads('['+','.join(row[0] for row in db.execute(
                "SELECT detail FROM jst_events WHERE action='导出聚水潭原导入单' ORDER BY rowid"))+']')
        assert len(receipts)==2
        reports.update(passed=True,build=diagnostic(),protected_facts=before,
            jst={'number':jd.row['payload']['head']['number'],'online':jst_key,'binding':jd.row['proof']['manual_binding'],
                 'exports':receipts,'additional_submissions':0,'component_calls':portal.calls,
                 'original_submissions':sum(event[0]=='order.import' and event[2]==jst_key for event in fixture['jst_seed_calls']),
                 'sibling_internal':original_jst_sibling['internal']},
            aoxiang={'number':ax_item['head']['number'],'task':ax_records[a]['taskId'],'binding':ax_records[a]['binding'],
                     'exports':ax_records[a]['originalExports'],'additional_submissions':0,'component_calls':attempts,
                     'original_submissions':ax_records[a]['confirmedImportRequests'],'sibling_po':ax_sibling['binding']['orderId']})
    except Exception:
        reports.update(passed=False,error=traceback.format_exc())
    finally:
        worker_errors.clear()
        for dialog in reversed(windows):dialog.close()
        win.job_dirty=False
        win._discard_close=True
        win.close()
        try:wait(lambda:manager.owner is None)
        except Exception:reports.update(passed=False,shutdown_error=traceback.format_exc())
        (root/(phase+'-report.json')).write_text(json.dumps(reports,ensure_ascii=False,indent=2),'utf-8')
        QTimer.singleShot(100,lambda:app.exit(0 if reports['passed'] else 1))
