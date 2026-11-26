"""Order-redo acceptance through the frozen application's normal buttons."""
from contextlib import closing
from decimal import Decimal
import os
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QComboBox, QDialog, QInputDialog, QLineEdit, QMessageBox, QPushButton, QTableView

from aoxiang_directory import atomic_json
from aoxiang_preview_ui import PreviewScope
from aoxiang_receipt_ui import ReceiptDialog
from aoxiang_workflow_ui import QueueDialog
import catalog_session_ui
import jst_auto_order as automatic
import jst_auto_order_store as orders
from jst_auto_order_ui import Orders
import jst_batches
import jst_cancellation as cancellation
import jst_order_store as allocations
from lifecycle_batch_ui import BatchImport
from lifecycle_home_ui import StageDialog
import order_lifecycle as lifecycle
from order_redo_platform_fixture import Ax, Jst, guard, load
import purchases
import work_repository as wr


def button(widget,label,*,direct=False):
    return next(b for b in widget.findChildren(QPushButton)
                if (not direct or b.parentWidget() is widget) and label in (b.text(),b.accessibleName()))


class Exercise:
    def __init__(self, app, win, root, data, phase):
        self.app,self.win,self.root,self.data,self.phase=app,win,root,data,phase
        self.path=win.work_repository.path
        self.wh=data['warehouse']
        self.other=next(w for w in data['originals'] if w!=self.wh)
        self.heads={h['warehouse']:h for h in purchases.targets(self.path)}
        self.original=data['originals'][self.wh]
        self.component=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parents[1]))/'components/aoxiang'
        self.jst=Jst(root,data,phase)
        self.ax=Ax(root,data,self.component,phase)
        self.manager=win.aoxiang_workflows
        self.steps=[]
        self.shots=[]
        self.details={}

    def shot(self, widget, name):
        file=f'{self.phase}-{name}.png'
        assert widget.grab().save(str(self.root/file)),file
        self.shots.append(file)

    def wait(self,predicate, label):
        limit=time.monotonic()+20
        while not predicate() and time.monotonic()<limit:
            QTest.qWait(20)
        assert predicate(),'等待'+label+'超时'

    def dialog(self, trigger, target, action):
        """Drive this exact modal without replacing its business handlers."""
        matches=(lambda w:isinstance(w,target)) if isinstance(target,type) else (lambda w:w.windowTitle()==target)
        state={'done':False,'seen':False}
        errors=[]
        def tick():
            if state['done'] or state['seen']:
                return
            modal=self.app.activeModalWidget()
            if modal and matches(modal):
                state['seen']=True
                try:
                    action(modal)
                except BaseException:
                    errors.append(traceback.format_exc())
                finally:
                    if modal.isVisible():
                        if isinstance(modal,ReceiptDialog):
                            modal.close()
                        else:
                            modal.reject()
                return
            if isinstance(modal,QMessageBox):
                errors.append(f'{modal.windowTitle()} · {modal.text()}')
                state['seen']=True
                modal.reject()
                return
            QTimer.singleShot(25,tick)
        QTimer.singleShot(25,tick)
        try:
            trigger()
        finally:
            state['done']=True
        assert state['seen'] and not errors,(getattr(target,'__name__',target),errors)

    def confirmation(self, trigger, title, expected):
        def yes(dialog):
            assert isinstance(dialog,QMessageBox) and expected in dialog.text(),dialog.text()
            default=dialog.defaultButton()
            assert default is not None and dialog.standardButton(default) in (
                QMessageBox.StandardButton.No,QMessageBox.StandardButton.Cancel)
            dialog.button(QMessageBox.StandardButton.Yes).click()
        self.dialog(trigger,title,yes)

    def enter(self, label, stage, action):
        def work(dialog):
            assert dialog.stage==stage
            page=dialog.panel
            if stage=='jst':
                self.wait(lambda:page.data is not None and not page.reading,stage)
            else:
                self.wait(lambda:getattr(page,'applied_request',None) is not None,stage)
            action(page)
        self.dialog(button(self.win.followup,label,direct=True).click,StageDialog,work)

    def choose(self, page, warehouses):
        targets={self.heads[w]['number'] for w in warehouses}
        if not targets <= {h['number'] for h in page.records}:
            page.status.setCurrentText('本环节全部')
            self.wait(lambda:targets <= {h['number'] for h in page.records},'仓单筛选')
        for n,h in enumerate(page.records):
            page.table.item(n,0).setCheckState(Qt.CheckState.Checked if h['number'] in targets else Qt.CheckState.Unchecked)
        assert {h['number'] for h in page.checked.values()}==targets

    def choose_jst(self, page, wh):
        from jst_results import key
        page.chosen={key(self.heads[wh])}
        page.render()
        page.controls()
        assert page.one_click_btn.isEnabled()

    def task_selection(self, dialog, key):
        for n,row in enumerate(dialog.records):
            dialog.table.item(n,0).setCheckState(Qt.CheckState.Checked if row['online']==key else Qt.CheckState.Unchecked)
        assert [r['online'] for r in dialog.selected()]==[key]

    def task_lookup(self, page, *, redo=False):
        def task(dialog):
            self.task_selection(dialog,self.original['online'])
            if redo:
                self.confirmation(dialog.redo_btn.trigger,'确认沿原仓单重做',self.original['internal'])
            else:
                dialog.buttons['recover'].click()
                row,=dialog.selected()
                self.details['latest_reason']=row['reason']
                self.shot(dialog,'原单回查')
        self.dialog(page.auto_tasks_btn.click,Orders,task)

    def pending_scope(self, page, reason):
        self.choose(page,[self.wh,self.other])
        def preview(dialog):
            rows={dialog.table.item(n,2).text():n for n in range(dialog.table.rowCount())}
            self_number=self.heads[self.wh]['number']
            other_number=self.heads[self.other]['number']
            assert dialog.table.item(rows[self_number],2).text()==self_number
            assert {h['number'] for h in dialog.ready_heads}=={other_number}
            if reason and not dialog.table.cellWidget(rows[self_number],5):
                assert reason in dialog.table.item(rows[self_number],4).text()
            self.shot(dialog,'付款采购拦截')
        self.dialog(button(page,'勾选单批量生成翱象采购预览').click,PreviewScope,preview)

    def run_queued(self,id):
        self.manager.stopping=False
        self.manager.tick()
        self.wait(lambda:self.manager.worker is None and self.manager.store.get(id)['state']
                  in ('COMPLETED','COMPLETED_WITH_ERRORS'),'原队列')

    def assert_old_guard(self):
        old=self.data['old_preview']
        outcome=guard(self.component,self.path,old['package_file'])
        assert outcome.get('code')=='PURCHASE_BASIS_CHANGED',outcome
        with closing(wr.read_connection(self.path)) as db:
            assert db.execute('SELECT state FROM purchase_dispatch_guards WHERE purchase_id=?',(old['id'],)).fetchone()==('AUTHORIZED',)
        self.details['final_old_guard']=outcome['code']

    def legacy(self):
        def review(page):
            target, = [h for h in page.records if h['warehouse']==self.wh]
            assert '原单已取消，付款待重新核对' in target['note'],target['note']
            self.shot(page,'旧库付款待核')
            self.pending_scope(page,'付款待重新核对')
        self.enter('进入付款核对','payment',review)
        self.assert_old_guard()
        assert not self.ax.state['uploads']
        assert self.balance(self.wh,self.original['gid'])==self.data['stock'][self.wh]-self.data['quantity'][self.wh]
        if self.phase=='exercise':
            assert self.task(self.original['online'])['reason']==self.data['overwritten_reason']
            for failure, text in (('timeout','超时'),('identity','与原任务不同'),('missing','尚未查到唯一')):
                self.jst.failure=failure
                self.enter('聚水潭导入单','jst',self.task_lookup)
                assert text in self.details['latest_reason'],self.details
                self.steps.append('回查 '+failure+' 后保留原取消证据')
            self.run_queued(self.data['queued'])
            item=self.manager.store.get(self.data['queued'])['items'][0]
            assert (item['state'],item['error']['code'])==('REVIEW','PAYMENT_CANCEL_REVIEW'),item
            assert not self.ax.state['uploads']
            self.details['pre_review_uploads']=0
            self.steps.append('升级前已排队的旧预览被上传前校验拦截')
            return
        with closing(wr.read_connection(self.path)) as db:
            assert cancellation.read(db,self.heads[self.wh])['legacy']
            assert not orders.exists(db,'jst_cancellations')
        self.enter('聚水潭导入单','jst',self.task_lookup)
        self.enter('进入付款核对','payment',review)
        assert not self.ax.state['uploads']
        self.details['pre_review_uploads']=0
        self.enter('进入付款核对','payment',self.review_payment)
        self.assert_old_guard()
        if self.data['decision'] != 'stop':
            self.rebuild_original_preview()
        assert len(self.ax.state['uploads'])==int(self.data['decision']!='stop')
        self.steps.append('重开后原款须人工重核；只允许经新预览确认的采购上传')


    def import_file(self, trigger, filename, stage):
        def batch(dialog):
            dialog.add_paths([str(self.root/filename)])
            assert dialog.read_ready and not dialog.issues,dialog.issues
            dialog.preview_btn.click()
            assert dialog.pages.currentIndex()==1,dialog.note.text()
            plan,=dialog.plan['plans']
            assert plan['head']['number']==self.heads[self.wh]['number']
            assert plan['head']['warehouse']==self.wh
            raw=plan['payload']['raw_rows']
            actual=[(r['sku'],Decimal(r['quantity'])) for r in raw]
            qty=self.data.get('redo_quantity',self.data['quantity'][self.wh])
            extra=[('00001',0)] if filename=='payment-correction.xlsx' and self.data.get('zero') else []
            assert actual==[('00000',qty),*extra],actual
            if extra:
                assert Decimal(raw[-1]['amount'])==0 and raw[-1]['quantity_origin']=='zero'
                assert len(raw)==2
            assert all(row[0]==self.heads[self.wh]['number'] for row in dialog.review.all_rows)
            dialog.review.complete.setChecked(True)
            dialog.review.extra_ack.setChecked(True)
            assert dialog.review.save.isEnabled()
            self.shot(dialog,'核对'+stage)
            dialog.review.save.click()
            assert len(dialog.receipt)==1 and dialog.receipt[0]['number']==self.heads[self.wh]['number'],dialog.receipt
        self.dialog(trigger,BatchImport,batch)

    def review_payment(self, page):
        self.choose(page,[self.wh])
        def approved(dialog):
            choice,=dialog.findChildren(QComboBox)
            fields=dialog.findChildren(QLineEdit)
            assert len(fields)==2
            if self.data['decision']=='correct':
                self.import_file(button(dialog,'导入付款更正').click,'payment-correction.xlsx','付款V002')
            choice.setCurrentIndex(choice.findData(self.data['decision']))
            fields[0].setText('隔离财务核对员')
            fields[1].setText('EXE验收 · 原单取消及当前有效财务凭据')
            self.shot(dialog,'人工付款重核')
            button(dialog,'保存本轮人工核对').click()
        self.dialog(button(page,'原单取消付款重核').click,'原单取消付款重核',approved)

    def one_click(self, page, *, recovery):
        self.choose_jst(page,self.wh)
        def outcome(dialog):
            rows=dialog.findChild(QTableView).model().rows
            assert len(rows)==1 and tuple(rows[0][1:3])==(self.wh,self.heads[self.wh]['number']),rows
            assert rows[0][4]==('付款待重新核对' if recovery else '导入结果待核'),rows
            self.details['one_click_result']=list(rows[0])
            self.shot(dialog,'一键结果')
        # One button call opens confirmation, then the results dialog. Leaving the
        # confirmation returns immediately, so watch the result separately.
        def check_confirm(d):
            rows=d.findChild(QTableView).model().rows
            assert len(rows)==1 and rows[0][2]==self.heads[self.wh]['number'],rows
            assert rows[0][4]==('按已保存原单核对' if recovery else '刷新本仓库存后分配并导入'),rows
            self.shot(d,'一键范围')
            button(d,'确认本次作业').click()
            QTimer.singleShot(25,after_confirmation)
        seen=[]
        errors=[]
        def after_confirmation():
            d=self.app.activeModalWidget()
            if not d or d.windowTitle()!='一键作业结果':
                if isinstance(d,QMessageBox):
                    errors.append(d.text());d.reject();return
                if not seen and not errors:
                    QTimer.singleShot(25,after_confirmation)
                return
            try:outcome(d);seen.append(True)
            except BaseException:errors.append(traceback.format_exc())
            d.reject()
        try:
            self.dialog(page.one_click_btn.click,'确认一键分配下单',check_confirm)
            assert seen and not errors,errors
        finally:
            # End the event-loop watcher on all error paths.
            seen.append(True)

    def purchase(self, page, warehouses):
        self.choose(page,warehouses)
        want={self.heads[w]['number'] for w in warehouses}
        state={}
        errors=[]
        done=False
        def queue_poll():
            if done:return
            current=self.app.activeModalWidget()
            try:
                if isinstance(current,QueueDialog):
                    job=self.manager.store.get(current.id)
                    if job['kind']=='prepare' and job['state']=='READY' and not self.manager.worker:
                        assert {i['key'] for i in job['items']}==want
                        assert all(i['state']=='PREPARED' for i in job['items']),job
                        self.shot(current,'新采购预览')
                        current.refresh()
                        current.select_all_btn.click()
                        self.confirmation(current.upload_btn.click,'确认批量上传采购',next(iter(want)))
                        state['prepared']=job['id']
                    elif job['kind']=='upload' and job['state']=='COMPLETED' and not self.manager.worker:
                        assert all(i['state']=='BOUND' for i in job['items']),job
                        assert {i['key'] for i in job['items']}==want
                        state['uploaded']=job['id']
                        self.shot(current,'采购原单关联')
                        current.reject()
                        return
                    elif job['state'] in ('COMPLETED_WITH_ERRORS','FAILED','PAUSED'):
                        raise AssertionError(job)
                elif isinstance(current,QMessageBox):
                    raise AssertionError(current.text())
            except BaseException:
                errors.append(traceback.format_exc())
                if current:current.reject()
                return
            QTimer.singleShot(30,queue_poll)
        def prepare(dialog):
            assert dialog.prepare_btn.isEnabled() and {h['number'] for h in dialog.ready_heads}==want
            self.manager.stopping=False
            dialog.prepare_btn.click()
            QTimer.singleShot(30,queue_poll)
        try:
            self.dialog(button(page,'勾选单批量生成翱象采购预览').click,PreviewScope,prepare)
            assert not errors and {'prepared','uploaded'}<=state.keys(),(state,errors)
        finally:
            done=True

    def redo(self):
        if self.phase=='exercise':
            def release(page):
                batch_id=self.data['original_batch']
                def pick(dialog):
                    combo=dialog.findChild(QComboBox)
                    row,=[i for i in range(combo.count()) if combo.itemText(i).endswith(batch_id[:8])]
                    combo.setCurrentIndex(row)
                    dialog.accept()
                self.dialog(page.batch_btn.click,QInputDialog,pick)
                self.wait(lambda:not page.reading and page.batch_id==batch_id and page.data.get('batch_label'),'原分配批次')
                self.shot(page,'已打开原分配批次')
                self.task_lookup(page,redo=True)
                self.wait(lambda:not page.reading and page.batch_id is None,'重做仓单')
                assert page.one_click_btn.isEnabled() and len(page.scope)==1 and page.scope[0]['warehouse']==self.wh
                self.shot(page,'重做仓单已勾选')
            self.enter('聚水潭导入单','jst',release)
            if self.data['decision']=='correct':
                def change(page):
                    self.choose(page,[self.wh])
                    self.import_file(page.primary_actions[0].click,'return-correction.xlsx','回传V002')
                self.enter('进入回传处理','return',change)
            assert self.balance(self.wh,self.original['gid'])==self.data['stock'][self.wh]
            def check_redo(page):
                def task(dialog):
                    self.task_selection(dialog,self.original['online'])
                    assert not dialog.redo_btn.isEnabled()
                    for _ in range(2):
                        dialog.redo_btn.trigger()
                    self.shot(dialog,'历史原单禁止重复重做')
                self.dialog(page.auto_tasks_btn.click,Orders,task)
            self.enter('聚水潭导入单','jst',check_redo)
            assert self.redo_case()['successor']
            self.steps.append('原批次核对取消后返回可执行范围，旧任务禁止重复重做')
            return
        if self.phase=='allocate':
            self.enter('聚水潭导入单','jst',lambda page:self.one_click(page,recovery=False))
            record=self.task(self.redo_case()['successor'])
            assert record['stage']=='IMPORT_UNKNOWN'
            assert [c[0] for c in self.jst.calls].count('sync.start')==1
            assert [c[0] for c in self.jst.calls].count('order.import')==1
            self.steps.append('刷新本仓库存并分配，平台已导入后断线，保留导入待核')
        else:
            self.enter('聚水潭导入单','jst',lambda page:self.one_click(page,recovery=True))
            record=self.task(self.redo_case()['successor'])
            assert record['stage']=='IMPORTED' and record['internal']
            assert not any(c[0] in ('order.import','sync.start') for c in self.jst.calls)
            self.assert_old_guard()
            self.enter('进入付款核对','payment',lambda p:self.pending_scope(p,'付款待重新核对'))
            self.enter('进入付款核对','payment',self.review_payment)
            self.assert_old_guard()
            accepted=[self.other] if self.data['decision']=='stop' else [self.wh,self.other]
            self.enter('进入付款核对','payment',lambda p:self.purchase(p,accepted))
            assert len(self.ax.state['uploads'])==len(accepted)
            self.steps.extend(('断线后沿唯一后继单号核对，未重提',
                               '旧预览持续失效，人工重核后只上传获准的新预览'))
        expected=self.data['redo_quantity']
        assert self.balance(self.wh,record['gid'])==self.data['stock'][self.wh]-expected
        assert self.balance(self.other,self.data['originals'][self.other]['gid'])==self.data['stock'][self.other]-self.data['quantity'][self.other]

    def amount(self):
        key=self.data['bound']
        if self.phase=='exercise':
            self.import_corrected_payment()
            self.manager.reconcile_orders()
            self.run_queued(self.data['receipt_job'])
            item=self.manager.store.get(self.data['receipt_job'])['items'][0]
            assert (item['state'],item['error']['code'])==('REVIEW','RECEIPT_PAYMENT_REVIEW'),item
            assert not lifecycle.history(self.path,self.heads[self.wh],'receipt')
            s=self.manager.store.schedule(key)
            assert s['needs_review'] and not s.get('retired') and s['final_pending']==self.data['completed'],s
            self.steps.append('更正付款后保留原采购和待办，已完结实收没有自动入账')
        else:
            self.steps.append('重开后从收货核对打开原采购')
            def review(page):
                row=next(i for i,h in enumerate(page.records) if h['warehouse']==self.wh)
                action=page.table.cellWidget(row,2)
                assert '待人工核对' in action.text() and action.isEnabled(),action.text()
                self.shot(page,'收货待办')
                def receipt(dialog):
                    self.wait(lambda:dialog.current and not dialog.worker and dialog.payment_review,'原款现款')
                    assert dialog.current['id']==key and Decimal(dialog.payment_review['difference'])==Decimal('-1')
                    self.shot(dialog,'付款差额')
                    def approve(review_dialog):
                        fields=review_dialog.findChildren(QLineEdit)
                        fields[0].setText('隔离收货核对员')
                        fields[1].setText('EXE验收 · 原采购明细与付款差额')
                        save=button(review_dialog,'确认并保存实收')
                        assert save.isEnabled()
                        save.click()
                    self.dialog(dialog.save_btn.click,'核对原采购实收',approve)
                    self.wait(lambda:dialog.current['state']=='SYNCED' and not dialog.worker,'实收保存')
                    self.shot(dialog,'原采购已入账')
                self.dialog(action.click,ReceiptDialog,receipt)
            self.enter('进入收货核对','receipt',review)
            history=lifecycle.history(self.path,self.heads[self.wh],'receipt')
            doc,=history
            accepted=doc['payload']['aoxiang_payment_review']
            assert (accepted['purchase_id'],accepted['original']['version'],accepted['current']['version'])==(key,1,2)
            from aoxiang_receipts import synchronize
            package=load(self.data['bound_index'])
            state=self.ax.state['receipts'][key]
            saved=synchronize(self.path,package,state['manifest'],self.data['ax_config'],self.data['suppliers'])
            assert saved['version']==1 and not saved['changed']
            assert self.manager.store.get(self.data['receipt_job'])['items'][0]['state']=='SYNCED'
            self.steps.append('同一收货原件重复同步沿用原实收版本')
        assert self.ax.state['uploads']==[key],self.ax.state['uploads']

    def select_queue(self, id, action):
        button(self.win,'首页').click()
        def selected(dialog):
            index=dialog.jobs.findData(id)
            assert index>=0,id
            dialog.jobs.setCurrentIndex(index)
            dialog.refresh()
            assert dialog.current['id']==id
            action(dialog)
        self.dialog(button(self.win,'查看翱象批次作业').click,QueueDialog,selected)

    def rebuild_original_preview(self):
        number=self.heads[self.wh]['number']
        def run(dialog):
            dialog.select_all_btn.click()
            assert dialog.rebuild_btn.isEnabled()
            self.confirmation(dialog.rebuild_btn.click,'确认结束并重建采购预览',number)
            self.wait(lambda:self.manager.worker is None and dialog.id != self.data['queued']
                      and self.manager.store.get(dialog.id)['state']=='READY','付款重核后的新采购预览')
            dialog.refresh()
            assert all(i['state']=='PREPARED' and i['key']==number for i in dialog.current['items'])
            self.shot(dialog,'重核后新采购预览')
            dialog.select_all_btn.click()
            self.confirmation(dialog.upload_btn.click,'确认批量上传采购',number)
            self.wait(lambda:self.manager.worker is None and self.manager.store.get(dialog.id)['items'][0]['state']=='BOUND',
                      '按本次付款关联新采购')
            dialog.refresh()
            self.shot(dialog,'重核后采购成功')
            package=load(dialog.current['items'][0]['package']['package_file'])
            assert package['basis']['paymentVersion']==(2 if self.data['decision']=='correct' else 1)
        self.select_queue(self.data['queued'],run)

    def import_corrected_payment(self):
        def correction(page):
            self.choose(page,[self.wh])
            pending=[]
            errors=[]
            def follow_up():
                if pending or errors:return
                modal=self.app.activeModalWidget()
                if isinstance(modal,PreviewScope):
                    try:
                        assert not modal.ready_heads and modal.table.cellWidget(0,5).text()=='打开原批次'
                        self.shot(modal,'保留原采购关联')
                        pending.append(True)
                    except BaseException:
                        errors.append(traceback.format_exc())
                    modal.reject()
                else:QTimer.singleShot(25,follow_up)
            try:
                QTimer.singleShot(25,follow_up)
                self.import_file(page.primary_actions[0].click,'payment-correction.xlsx','付款V002')
                assert pending and not errors,errors
            finally:
                pending.append(True)
        self.enter('进入付款核对','payment',correction)
        history=lifecycle.history(self.path,self.heads[self.wh],'payment')
        assert [r['version'] for r in history]==[2,1]
        expected_amount=Decimal(self.data['quantity'][self.wh]*self.data['price'][self.wh]-1)
        raw=history[0]['payload']['raw_rows']
        expected=[('00000',Decimal(self.data['quantity'][self.wh]),expected_amount)]
        if self.data.get('zero'):
            expected.append(('00001',Decimal(0),Decimal(0)))
        assert [(r['sku'],Decimal(r['quantity']),Decimal(r['amount'])) for r in raw]==expected
        self.details['imported_payment_rows']=[[sku,str(qty),str(amount)] for sku,qty,amount in expected]

    def ack(self):
        head=self.heads[self.wh]
        key=self.data['bound']
        id=self.data['receipt_job']
        def run(dialog):
            self.manager.stopping=False
            if self.phase=='repeat':
                assert not dialog.recover_btn.isEnabled()
                dialog.recover_btn.click()
                self.wait(lambda:not self.manager.worker,'已办结的原队列保持不动')
            else:
                assert dialog.recover_btn.isEnabled(),dialog.current
                dialog.recover_btn.click()
                wanted='REVIEW' if self.phase=='exercise' else 'SYNCED'
                self.wait(lambda:not self.manager.worker and self.manager.store.get(id)['items'][0]['state']==wanted,
                          '原实收队列 '+wanted)
            dialog.refresh()
            self.shot(dialog,'原实收队列')
        self.select_queue(id,run)
        current=self.ax.state['receipts'][key]
        doc,=lifecycle.history(self.path,head,'receipt')
        if self.phase=='exercise':
            assert current['state']==('READY' if self.data['ack_fault']=='before-delivery' else 'SYNCED')
            assert len(self.ax.state['ack_attempts'])==1
            assert self.manager.store.schedule(key)['final_pending']
            from aoxiang_directory import atomic_json
            atomic_json(self.root/'posted-before-reprice.json',doc)
            self.import_corrected_payment()
            self.steps.append('正式实收 V001 已提交，组件确认通道中断，之后从付款页更正为 V002')
        else:
            assert current['state']=='SYNCED'
            assert doc==load(self.root/'posted-before-reprice.json')
            assert len(self.ax.state['ack_attempts'])==(2 if self.data['ack_fault']=='before-delivery' else 1)
            assert self.ax.state['acknowledged']==[key]
            assert not self.manager.store.schedule(key)['final_pending']
            self.steps.append('按原采购及同一观察核实确认，实收仍为原 V001')
        self.details['ack_attempts']=self.ax.state['ack_attempts']
        assert self.ax.state['uploads']==[key]

    def retired(self):
        original=self.manager.store.get(self.data['receipt_job'])
        if self.phase=='exercise':
            self.manager.reconcile_orders()
            schedule=self.manager.store.schedule(self.data['bound'])
            assert not schedule.get('retired') and schedule['payment_review']
            assert schedule['final_pending']==self.data['completed']
            assert schedule['automation_paused'] and schedule['interval_seconds']==300
            assert self.manager.store.get(original['id'])['items'][0]['state']=='REVIEW'
            self.enter('进入收货核对','receipt',self.shot_receipt_todo)
            self.steps.append('从 rc14 跳过记录恢复同一采购与原任务，保留已保存的周期及暂停状态')
        else:
            self.amount()

    def shot_receipt_todo(self,page):
        row=next(i for i,h in enumerate(page.records) if h['warehouse']==self.wh)
        action=page.table.cellWidget(row,2)
        assert '待人工核对' in action.text() and action.isEnabled(),action.text()
        self.shot(page,'收货待办')


    def redo_case(self):
        with closing(wr.read_connection(self.path)) as db:
            case=cancellation.read(db,self.heads[self.wh])
        assert case and case.get('successor')
        return case

    def task(self,key):
        with closing(wr.read_connection(self.path)) as db:
            return orders.read(db,key)

    def balance(self,wh,gid):
        with closing(wr.read_connection(self.path)) as db:
            gen,_=allocations.read_generation(db,gid)
            context=jst_batches.context(db,gen['batch_id'])
            available=context['stocks'][wh]['catalog']['items']['00000']['可用数']
            reserved=jst_batches.reservations(db,gen['batch_id'],wh).get('00000',0)
            return int(Decimal(available)-reserved)

    def warehouse_rows(self):
        observed=[]
        with closing(wr.read_connection(self.path)) as db:
            assert db.execute('PRAGMA quick_check').fetchone()==('ok',)
            for wh,h in self.heads.items():
                orig=self.data['originals'][wh]
                old=orders.read(db,orig['online'],workbook=True)
                active=allocations.active(db,h)
                case=cancellation.read(db,h)
                successor=orders.read(db,case['successor']) if case and case.get('successor') else None
                review=cancellation.latest_review(db,orig['online'])
                reserved=dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(active,)))
                original=dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(orig['gid'],)))
                released=wr.loads(case['release'])['released'] if case and case.get('release') else {}
                events,=db.execute("SELECT count(*) FROM jst_events WHERE oid=? AND warehouse=? AND action='原取消单释放占用并登记后继'",
                                   (h['oid'],wh)).fetchone()
                assert events==int(bool(released))
                paid=lifecycle.history(self.path,h,'payment')
                assert paid[-1]['digest']==orig['payment_digest']
                for file in paid[-1]['payload']['files']:
                    assert db.execute('SELECT 1 FROM lifecycle_files WHERE sha=?',(file['sha'],)).fetchone()
                rec=lifecycle.history(self.path,h,'receipt')
                current_ax=[r for r in self.ax.state['records'].values()
                            if r['basis']['number']==h['number'] and r['state']=='BOUND']
                b=old['proof']['binding']
                observed.append({'warehouse':wh,'number':h['number'],'sign_revision':h['revision'],
                    'return_version':lifecycle.current(db,h,'return')['version'],
                    'account':{k:b[k] for k in ('company','coid','uid','shop_id')},
                    'original_online':old['online'],'original_internal':old['internal'],'original_reservation':original,
                    'successor_online':case.get('successor') if case else None,
                    'successor_internal':successor['internal'] if successor else None,
                    'release_count':events,'released':released,'active_reservation':reserved,
                    'balance':self.balance(wh,active or orig['gid']),
                    'payment_versions':[r['version'] for r in reversed(paid)],'payment_review':review,
                    'ax_orders':[r['binding']['orderId'] for r in current_ax],
                    'ax_uploads':sum(r['id'] in self.ax.state['uploads'] for r in current_ax),
                    'receipt_versions':[r['version'] for r in rec],
                    'receipt_review':rec[0]['payload'].get('aoxiang_payment_review') if rec else None,
                    'jst_imports_this_process':sum(c[0]=='order.import' and c[1]==wh for c in self.jst.calls)})
        return observed

    def run(self):
        button(self.win,'订单跟踪').click()
        self.win.followup.start.setText(self.data['day'])
        self.win.followup.end.setText(self.data['day'])
        self.win.followup.query()
        getattr(self,self.data['kind'])()


def start_smoke(app,win,args):
    root=Path(args[0]).resolve()
    phase=args[1]
    data=load(root/'synthetic.json')
    if (data['case']!='order-redo-rc15' or data['synthetic'] is not True or data['live_platform'] is not False
            or Path(os.environ.get('REPLENISHMENT_DATA_DIR','')).resolve()!=root/'settings'
            or Path(os.environ.get('LOCALAPPDATA','')).resolve()!=root/'test-appdata'
            or win.work_repository.path.resolve()!=root/'settings/workbench.sqlite3'
            or phase not in data['phases']):
        raise ValueError('验收仅允许在专用隔离资料目录执行')
    win.jst_generator.stopped=True
    win.browser_status.paused=True
    win.browser_status.initial.stop()
    for job in (win.jst_generator,win.aoxiang_workflows,win.browser_status,win.jst_fulfillment_runner,win.scheduled):
        job.timer.stop()
    from aoxiang_ui_common import ComponentDialog
    from version_info import diagnostic
    exercise=Exercise(app,win,root,data,phase)
    factory=lambda *_:exercise.ax
    exercise.manager.client_factory=factory
    original_client=ComponentDialog.init_client
    def isolated_client(dialog,host,_):
        return original_client(dialog,host,factory)
    def run():
        report={'passed':False,'frozen_exe':bool(getattr(sys,'frozen',False)),'build':diagnostic(),
                'kind':data['kind'],'warehouse':data['warehouse'],'phase':phase,'synthetic':True,'live_platform':False,
                'steps':exercise.steps,'shots':exercise.shots,'jst_calls':exercise.jst.calls,'ax_calls':exercise.ax.calls,
                'details':exercise.details}
        try:
            with patch.object(catalog_session_ui,'manager',lambda _:SimpleNamespace(session=exercise.jst)), \
                 patch.object(ComponentDialog,'init_client',isolated_client):
                exercise.run()
                report['orders']=exercise.warehouse_rows()
            report['passed']=True
        except BaseException:
            report['error']=traceback.format_exc()
        finally:
            atomic_json(root/(phase+'-report.json'),report)
        win.job_dirty=False
        win._discard_close=True
        if report['passed']:win.close()
        else:app.exit(1)
    QTimer.singleShot(0,run)
