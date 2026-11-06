"""Application-lifetime queue owner and business-facing batch status. No model changes."""
import time
import sqlite3
import logging
import json
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, QTimer, Qt, QSignalBlocker
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QComboBox, QLabel, QProgressBar,
                             QTableWidget, QTableWidgetItem, QMessageBox, QSpinBox, QLineEdit, QPushButton)
from ui_controls import button, label
from aoxiang_session import SessionGate, SessionOwner
from aoxiang_workflows import Workflows, DONE_ITEM
from aoxiang_workflow_runner import Runner
from aoxiang_client import ProcessClient
from aoxiang_ui_common import ComponentDialog, Call, component_root, show_modal
from runtime_support import Cancelled

KINDS = {'prepare': '采购预览', 'upload': '采购上传与核对', 'receipt': '收货同步'}
from aoxiang_workflow_state import can_end_local,local_preparation,summary as task_summary,counts
from aoxiang_purchase_messages import waiting, item_message
STATES = {'QUEUED': '排队', 'RUNNING': '正在处理', 'YIELDED': '等待人工操作结束', 'PAUSED': '已停止，可继续原任务',
          'FAILED': '批次未完成', 'READY': '预览完成，尚未上传', 'COMPLETED': '完成', 'COMPLETED_WITH_ERRORS': '已处理，有待核项', 'CANCELLED': '剩余任务已取消',
          'PENDING': '未处理', 'PREPARED': '预览就绪，未上传', 'BOUND': '已关联采购单', 'OBSERVED': '已观察，尚未完成', 'SYNCED': '正式实收已保存', 'REVIEW': '待核，不自动重试', 'SKIPPED': '周期已停止，本次跳过'}


def database_busy(exc):
    return isinstance(exc,sqlite3.OperationalError) and (getattr(exc,'sqlite_errorcode',0)&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED) or str(exc).lower() in ('database is locked','database table is locked','database schema is locked'))


class QueueWorker(QThread):
    progress = Signal(str)
    def __init__(self, manager, job, token):
        super().__init__(manager)
        self.manager, self.job, self.token = manager, job, token

    def run(self):
        m = self.manager
        client = None
        try:
            if self.token.is_set():
                raise Cancelled('任务尚未开始，已请求停止')
            client = m.session.lease()
            client.start()
            Runner(m.store, client, component_root(), m.host.work_repository.path, cancel=self.token, progress=self.progress.emit).run(self.job['id'])
        except Exception as exc:
            state = 'YIELDED' if isinstance(exc, Cancelled) and m.gate.yielding and not m.explicit_stop else 'PAUSED'
            try:
                m.store.update(self.job['id'], state=state, message=str(exc))
            except Exception as failed:
                self.progress.emit('队列状态保存失败，请保留原记录：' + str(failed))
        finally:
            # Keep the gate until all asynchronous platform actions have settled.
            while client is not None and not client.close(timeout=.5):
                self.progress.emit('等待当前翱象动作安全结束；不会强杀正在提交的任务。')
                time.sleep(.2)
            m.gate.release_background(self.token)


class Manager(QObject):
    changed = Signal()
    def __init__(self, host, *, client_factory=ProcessClient):
        super().__init__(host)
        self.host, self.client_factory = host, client_factory
        self.store = Workflows(Path(host.repository.folder) / 'aoxiang')
        self.gate = SessionGate()
        self.session = SessionOwner(lambda: self.client_factory(component_root(), self.store.folder / 'component-state'))
        self.session_closer = None
        self.worker = self.token = None
        self.stopping = self.explicit_stop = self.close_requested = False
        self.message = '翱象队列空闲；周期同步默认未启用'
        self.owner = None
        self.maintenance_depth=0
        self.queue_error=''
        self.busy=False
        self.recovering=True
        try:
            self.store.folder.mkdir(parents=True, exist_ok=True)
            self.owner = sqlite3.connect(self.store.folder / 'scheduler.lock.sqlite3', timeout=0)
            self.owner.execute('CREATE TABLE IF NOT EXISTS owner_lock(id INTEGER PRIMARY KEY)')
            self.owner.execute('BEGIN IMMEDIATE')
        except Exception as exc:
            if self.owner:self.owner.close()
            self.owner = None;self.stopping = True
            self.queue_error='后台调度权被另一实例占用' if database_busy(exc) else '后台调度锁初始化失败：'+str(exc)
            self.message=self.queue_error+'；其他实例退出后可点击“重新检查后台”。'
            logging.warning('翱象调度锁初始化失败: %s',self.store.folder,exc_info=True)
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        if self.owner:
            try:self.store.recover(timeout=0);self.recovering=False
            except Exception as exc:
                if database_busy(exc):
                    self.busy=True;self.message='后台队列库暂时繁忙，2秒后重新检查原状态'
                else:
                    self.stopping=True;self.queue_error=str(exc);self.message='后台队列恢复失败：'+str(exc)
                    logging.exception('翱象队列启动恢复失败: %s',self.store.path)
        # Embedded/test hosts can destroy an idle window without sending closeEvent.
        host.destroyed.connect(self.shutdown)

    def status(self, text):
        self.message = text
        self.changed.emit()

    def tick(self):
        if self.owner is None or self.worker or self.stopping or self.maintenance_depth or self.gate.yielding or getattr(self.host, 'closing', False) or getattr(self.host,'_large_io_dialog',None):
            return
        try:
            if self.recovering:
                self.store.recover(timeout=0);self.recovering=False
            self.store.enqueue_due(timeout=0)
            job = self.store.next_queued(timeout=0)
            if self.busy:
                self.busy=False;self.status('数据库已恢复可用，后台队列继续检查原任务')
            if not job:
                text='当前没有正在执行或排队的任务；历史待处理项可在批次作业中查看'
                if self.message!=text:self.status(text)
                return
            token = self.gate.claim_background()
            if token is None:
                return
            self.explicit_stop = False
            self.token = token
            self.worker = QueueWorker(self, job, token)
            self.worker.progress.connect(self.status)
            self.worker.finished.connect(self.finished)
            self.status(KINDS[job['kind']] + ' · 正在开始本批作业')
            self.worker.start()
        except Exception as exc:
            if database_busy(exc):
                if not self.busy:logging.warning('翱象队列数据库繁忙，将重查原任务: %s',self.store.path,exc_info=True)
                self.busy=True
                self.status('数据库正被其他操作使用，2秒后自动重新检查；原任务保留，不重复提交')
                return
            self.stopping = True
            self.queue_error=str(exc)
            logging.exception('翱象队列故障: %s',self.store.path)
            self.status('翱象队列停止，原记录保留：' + str(exc))

    def retry_queue(self):
        if self.close_requested or getattr(self.host,'closing',False) or self.maintenance_depth:return
        if self.worker:self.status('当前任务尚在运行，请等待安全结束');return
        try:
            if self.owner is None:
                owner=sqlite3.connect(self.store.folder/'scheduler.lock.sqlite3',timeout=0)
                try:
                    owner.execute('CREATE TABLE IF NOT EXISTS owner_lock(id INTEGER PRIMARY KEY)');owner.execute('BEGIN IMMEDIATE')
                except Exception:owner.close();raise
                self.owner=owner
            self.stopping=False;self.queue_error='';self.recovering=True
            self.status('正在重新检查后台及原任务状态…');self.tick()
        except Exception as exc:
            self.queue_error='后台调度权仍被另一实例占用' if database_busy(exc) else str(exc)
            self.status(self.queue_error+'；未启动新任务')

    @contextmanager
    def maintenance(self):
        self.ensure_owner(for_maintenance=True)
        if self.worker or self.gate.yielding:raise ValueError('平台任务尚未安全退出，请先停止或关闭平台操作窗口')
        self.maintenance_depth+=1
        previous=self.message
        self.status('正在维护本地数据，后台轮询暂缓；结束后自动恢复检查')
        try:yield
        finally:
            self.maintenance_depth-=1
            self.status(previous)
            if not self.maintenance_depth and not self.close_requested:QTimer.singleShot(0,self.tick)

    def finished(self):
        previous, self.worker = self.worker, None
        self.token = None
        previous.deleteLater()
        self.status('本轮翱象作业已安全结束；可在队列查看逐单结果')
        if self.close_requested:
            QTimer.singleShot(0, self.host.close)

    def stop(self, expected_id=None):
        if expected_id and (not self.worker or self.worker.job['id'] != expected_id):
            return
        self.explicit_stop = True
        if self.token:
            self.token.set()
            self.status('已请求停止剩余动作，等待当前动作安全结束；已建单不会撤销')

    def prepare_close(self):
        self.stopping = True
        self.close_requested = True
        self.stop()
        if self.worker is not None or self.session_closer is not None:
            return False
        if self.session.client is None:
            return True
        task = Call(lambda _: self.session.close(timeout=.5), self)
        self.session_closer = task
        task.failed.connect(lambda text: self.status('共享连接尚未退出：' + text))
        def finished():
            self.session_closer = None
            task.deleteLater()
            if self.close_requested:
                QTimer.singleShot(200, self.host.close)
        task.finished.connect(finished)
        task.start()
        return False

    def resume_after_cancelled_close(self):
        self.stopping = self.owner is None or bool(self.queue_error)
        self.close_requested = False

    def shutdown(self):
        self.timer.stop()
        # Normal application close has already drained this off the GUI thread.
        if self.session.client is not None and self.worker is None and self.session_closer is None:
            self.session.close(timeout=.5)
        if self.owner:
            self.owner.rollback();self.owner.close();self.owner=None

    def open_queue(self, id=None):
        QueueDialog(self.host, self, id).exec()

    def ensure_owner(self, *, for_maintenance=False):
        if self.close_requested or getattr(self.host,'closing',False):raise ValueError('程序正在安全关闭，请等待关闭完成后重新打开')
        if self.owner is None:raise ValueError('本窗口未持有后台调度权：'+(self.queue_error or '锁已释放')+'。请在首页点击“重新检查后台”')
        if not for_maintenance and self.stopping:raise ValueError('后台队列已停止：'+self.queue_error+'。请在首页点击“重新检查后台”')


class QueueDialog(QDialog):
    def __init__(self, parent, manager, id=None):
        super().__init__(parent)
        self.manager, self.id, self.current = manager, id, None
        self.setWindowTitle('翱象批次作业 · 按门店和仓查看')
        self.resize(1160, 720)
        v = QVBoxLayout(self)
        v.addWidget(label('采购先预览再确认上传；收货按原采购单查询。各订单按选择顺序串行处理。', 'section'))
        self.jobs = QComboBox()
        self.jobs.currentIndexChanged.connect(self.selected)
        v.addWidget(self.jobs)
        self.summary = label('')
        self.summary.setWordWrap(True)
        v.addWidget(self.summary)
        self.table = QTableWidget(0, 8)
        self.table.setHorizontalHeaderLabels(['选择', '门店', '仓', '补货单号', '本项状态', '款数 / 付款金额', '平台采购单', '提示'])
        self.table.setColumnCount(9);self.table.setHorizontalHeaderItem(8,QTableWidgetItem('单项操作'))
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda *_:self.details())
        v.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.preview_btn=button('查看采购预览', self.details);row.addWidget(self.preview_btn)
        self.reason_btn=button('查看原因与下一步', self.failure_reason);row.addWidget(self.reason_btn)
        self.table.itemSelectionChanged.connect(self.detail_controls)
        self.upload_btn = button('确认上传勾选的采购预览', self.upload, True)
        self.resume_btn = button('继续本批原未完任务', self.resume)
        self.stop_btn = button('停止本批剩余动作', lambda:manager.stop(self.id))
        self.cancel_btn = button('取消本批未开始的剩余任务', self.cancel)
        for b in (self.upload_btn, self.resume_btn, self.stop_btn, self.cancel_btn): row.addWidget(b)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(button('查看 / 配置批量收货同步', lambda: open_receipt_selection(self, manager.host)))
        row.addWidget(button('关闭（已排队作业继续）', self.accept))
        self.retry_btn=button('重跑全部失败项',self.retry_all);row.addWidget(self.retry_btn)
        self.new_batch_btn=button('结束本批并新建',self.finish_new);row.addWidget(self.new_batch_btn)
        self.end_local_btn=button('结束所选本地准备',self.end_local);row.addWidget(self.end_local_btn)
        v.addLayout(row)
        self.bar = QProgressBar();v.addWidget(self.bar)
        self.note = label('');self.note.setWordWrap(True);v.addWidget(self.note)
        self.timer = QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.refresh);self.timer.start()
        self.refresh()

    def selected(self):
        self.id = self.jobs.currentData();self.current = None;self.refresh()

    def refresh(self):
        try:
            jobs = self.manager.store.list()
            if self.id and all(j['id']!=self.id for j in jobs):
                try:jobs.insert(0,self.manager.store.get(self.id))
                except ValueError:self.id=None;self.current=None
            selected = self.id or (jobs[-1]['id'] if jobs else None)
            with QSignalBlocker(self.jobs):
                self.jobs.clear()
                for j in jobs:
                    state_text='已受理，等待核对' if j['state']=='COMPLETED_WITH_ERRORS' and counts(j)['waiting'] and not counts(j)['review'] else STATES[j['state']]
                    self.jobs.addItem(datetime.fromtimestamp(j['created_at']).strftime('%m-%d %H:%M:%S') + ' · ' + KINDS[j['kind']] + ' · ' + state_text, j['id'])
                self.jobs.setCurrentIndex(self.jobs.findData(selected))
            self.id = selected
            job = next((j for j in jobs if j['id'] == selected), None)
            active = bool(self.manager.worker and self.manager.worker.job['id'] == selected)
            self.note.setText(self.manager.message)
            self.stop_btn.setEnabled(active)
            self.retry_btn.setText('继续核对未完成采购' if job and job['kind']=='upload' else '重跑全部失败项')
            self.retry_btn.setEnabled(bool(job and job['state']!='CANCELLED' and not active and any(
                i['state'] in (('FAILED','REVIEW') if job['kind']=='upload' else ('FAILED',)) for i in job['items'])))
            self.new_batch_btn.setEnabled(bool(job and job['state']!='RUNNING' and not any(i['state'] in ('RUNNING','REVIEW') for i in job['items']) and any(i['state'] in ('PENDING','FAILED') for i in job['items'])))
            self.resume_btn.setEnabled(bool(job and job['state'] in ('PAUSED', 'FAILED')) and not active)
            self.cancel_btn.setEnabled(bool(job and job['state'] in ('QUEUED', 'PAUSED', 'YIELDED', 'FAILED')) and not active)
            self.upload_btn.setEnabled(bool(job and job['kind'] == 'prepare' and job['state'] in ('READY', 'COMPLETED_WITH_ERRORS', 'CANCELLED') and any(i['state']=='PREPARED' for i in job['items'])))
            self.end_local_btn.setEnabled(bool(job and not active and any(can_end_local(job,i) for i in job['items'])))
            if not job:
                self.current=None;self.preview_btn.setEnabled(False);self.reason_btn.setEnabled(False)
                self.summary.setText('尚无批次。付款核对页可勾选订单后批量生成采购预览。')
                self.table.setRowCount(0);return
            done = sum(i['state'] in DONE_ITEM for i in job['items'])
            self.bar.setRange(0, len(job['items']));self.bar.setValue(done)
            self.bar.setFormat(f'已检查 {done} / {len(job["items"])} 单 · 就绪/成功 {counts(job)["success"]} 单')
            self.summary.setText(task_summary(job) + '\n停止、结束只影响本地后续动作；已经上传的采购单不会撤销。')
            if self.current and self.current['sequence'] == job['sequence']:
                return
            checked = {self.current['items'][i]['key'] for i in range(self.table.rowCount()) if self.current and i<len(self.current['items']) and getattr(self,'rendered_id',None)==job['id'] and self.table.item(i, 0) and self.table.item(i, 0).checkState() == Qt.CheckState.Checked}
            self.rendered_id=job['id']
            self.current = job
            self.table.setRowCount(len(job['items']))
            for n, i in enumerate(job['items']):
                self.table.setRowHeight(n,54 if job['kind']=='upload' and i.get('error') else 34)
                h = i.get('head') or i.get('basis') or {}
                check = QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                check.setCheckState(Qt.CheckState.Checked if i['key'] in checked else Qt.CheckState.Unchecked)
                if i['state'] != 'PREPARED' and not can_end_local(job,i): check.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(n, 0, check)
                summary = i.get('summary') or {}
                state_text='本地准备未通过，未生成采购预览' if local_preparation(i,job['kind']) and i['state'] in ('FAILED','REVIEW') else '本地准备已结束' if i.get('ended_locally') else STATES.get(i['state'], i['state'])
                if waiting(i,job['kind']):state_text='已受理，等待核对'
                elif job['kind']=='upload' and i['state'] in ('FAILED','REVIEW'):state_text='采购结果需核对'
                values = [i.get('display_name') or h.get('name') or h.get('store'), h.get('warehouse'), h.get('number'), state_text,
                          f"{summary.get('sku_count', '—')} / {summary.get('amount', '—')}", (i.get('result') or {}).get('binding', {}).get('orderId') or i.get('order_id', '—'),
                          item_message(i,job['kind'])]
                for c, x in enumerate(values, 1):
                    item = QTableWidgetItem(str(x));item.setToolTip(str(x));self.table.setItem(n, c, item)
                if i['state'] in ('FAILED','REVIEW'):
                    b=button('继续核对采购结果' if job['kind']=='upload' else '核对原任务并继续' if i['state']=='REVIEW' else '重跑此任务',lambda checked=False,pos=n:self.retry([pos]))
                    b.setEnabled(job['state']!='CANCELLED');self.table.setCellWidget(n,8,b)
                else:self.table.removeCellWidget(n,8)
            for c, width in enumerate((50, 140, 70, 235, 160, 120, 170, 280, 175)):self.table.setColumnWidth(c, width)
            self.detail_controls()
        except Exception as exc:
            self.note.setText('读取队列失败：' + str(exc))

    def detail_controls(self):
        n=self.table.currentRow()
        item=self.current['items'][n] if self.current and 0<=n<len(self.current['items']) else None
        self.preview_btn.setEnabled(bool(item and item.get('package')))
        self.reason_btn.setEnabled(bool(item and item.get('error')))

    def failure_reason(self):
        n=self.table.currentRow()
        if not self.current or not 0<=n<len(self.current['items']):return
        item=self.current['items'][n];message=item_message(item,self.current['kind']) or '本项没有异常记录。'
        if self.current['kind']=='upload':
            head=item.get('head') or {};box=QMessageBox(self);box.setWindowTitle('采购状态与处理方法')
            box.setText((head.get('name') or head.get('store') or '')+' · '+head.get('warehouse','')+'\n'+item['key'])
            box.setInformativeText(message)
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setStyleSheet('QLabel { min-width: 440px; }')
            box.setDetailedText(json.dumps({'原始记录':item.get('error'),'原任务状态':item.get('purchase_observation'),
                                          '本地采购任务':(item.get('package') or {}).get('id')},ensure_ascii=False,indent=2))
            box.setStandardButtons(QMessageBox.StandardButton.Ok)
            ok=box.button(QMessageBox.StandardButton.Ok);ok.setText('知道了')
            for detail in box.findChildren(QPushButton):
                if detail is ok:continue
                detail.setText('查看技术详情');detail.setProperty('expanded',False)
                def toggle(_,b=detail):
                    expanded=not b.property('expanded');b.setProperty('expanded',expanded)
                    b.setText('收起技术详情' if expanded else '查看技术详情')
                detail.clicked.connect(toggle)
            box.exec();return
        if local_preparation(item,self.current['kind']):message+='\n本次未生成采购预览，未进入上传。可核对付款后重跑，或结束本地准备；不必为删除订单而确认付款。'
        else:message+='\n请核对原平台任务；本地停止不代表平台订单已撤销。'
        QMessageBox.information(self,'任务原因',message)

    def end_local(self):
        if not self.current:return
        items=[i for n,i in enumerate(self.current['items']) if self.table.item(n,0).checkState()==Qt.CheckState.Checked]
        if not items or not all(can_end_local(self.current,i) for i in items):
            QMessageBox.information(self,'选择本地准备','请只勾选未运行、未生成采购身份的本地准备项。');return
        text='只结束以下本地准备，未选任务和业务订单保留，不操作平台：\n'+'\n'.join(i['key'] for i in items)
        if QMessageBox.question(self,'结束所选本地准备',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            self.manager.ensure_owner();self.manager.store.end_local_items(self.id,[i['key'] for i in items],self.current['sequence']);self.current=None;self.refresh()
        except Exception as exc:QMessageBox.warning(self,'未结束',str(exc))

    def details(self):
        n = self.table.currentRow()
        if not self.current or n < 0:
            QMessageBox.information(self, '先选择订单', '请先点击要核对的订单行。');return
        p = self.current['items'][n].get('package')
        if not p:
            self.failure_reason();return
        try:
            from business_ui import run, grid
            from aoxiang_receipts import checked_json
            def read(progress, cancel):
                return checked_json(p['plan_file'], p['plan_sha256'])
            plan = run(self, read, (), '读取已冻结付款采购明细')
            d = QDialog(self);d.setWindowTitle('采购预览 · ' + plan['head']['number']);d.resize(950, 650)
            v = QVBoxLayout(d)
            v.addWidget(label(plan['head']['name'] + ' · ' + plan['head']['warehouse'] + ' · 付款数量 ' + plan['total_quantity'] + ' · 金额 ' + plan['total_amount'] + ' 元', 'section'))
            search = QLineEdit();search.setPlaceholderText('查找商品编码或名称');v.addWidget(search)
            table, model = grid(d, ['商品编码', '商品名称', '付款基础数量', '付款商品金额（元）'], [[r['sku'], r['name'], r['quantity'], r['amount']] for r in plan['rows']])
            search.textChanged.connect(model.filter);v.addWidget(table, 1)
            v.addWidget(label('逐项取自已确认付款快照；不换算基础数量，不用最新价格替代付款金额。', 'muted'))
            v.addWidget(button('关闭预览', d.accept));d.exec();d.deleteLater()
        except Exception as exc:QMessageBox.warning(self, '未打开采购明细', str(exc))

    def upload(self):
        if not self.current:return
        prepared_id, expected_sequence = self.current['id'], self.current['sequence']
        positions = [i for i in range(self.table.rowCount()) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked and self.current['items'][i]['state'] == 'PREPARED']
        if any(self.table.item(i,0).checkState()==Qt.CheckState.Checked and self.current['items'][i]['state']!='PREPARED' for i in range(self.table.rowCount())):
            QMessageBox.information(self,'选择就绪预览','所选含未就绪任务；请只勾选成功的采购预览再上传。');return
        if not positions:
            QMessageBox.information(self, '先选择范围', '请勾选要上传的成功采购预览。');return
        items = [self.current['items'][i] for i in positions]
        amount = sum((Decimal(i['summary']['amount']) for i in items), Decimal(0))
        names = '\n'.join(i['head']['name'] + ' · ' + i['head']['warehouse'] + ' · ' + i['head']['number'] for i in items[:12])
        if len(items) > 12: names += f'\n其余 {len(items)-12} 单见已勾选列表。'
        text = f'将上传勾选的 {len(items)} 张采购单，付款商品金额合计 {amount} 元。\n{names}\n\n仅创建为待提交，不自动提交或审核；未勾选及失败项不会上传。'
        if QMessageBox.question(self, '确认批量上传采购', text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        try:
            self.manager.ensure_owner()
            job = self.manager.store.upload(prepared_id, positions, expected_sequence)
            self.id = job['id'];self.current = None;self.manager.tick();self.refresh()
        except Exception as exc:QMessageBox.warning(self, '未开始上传', str(exc))

    def retry(self,positions):
        try:
            self.manager.ensure_owner();self.manager.store.retry_items(self.id,positions,self.current['sequence']);self.manager.tick();self.refresh()
        except Exception as exc:QMessageBox.warning(self,'未排入重跑',str(exc))

    def retry_all(self):
        if self.current:self.retry([i['position'] for i in self.current['items'] if i['state'] in (
            ('FAILED','REVIEW') if self.current['kind']=='upload' else ('FAILED',))])

    def finish_new(self):
        if not self.current:return
        if QMessageBox.question(self,'结束并新建执行批次','保留成功项，将原失败及未开始项移入新批。采购仍使用原任务号，不重建采购单。继续？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            self.manager.ensure_owner();job=self.manager.store.finish_and_new(self.id,self.current['sequence']);self.id=job['id'];self.current=None;self.manager.tick();self.refresh()
        except Exception as exc:QMessageBox.warning(self,'未新建批次',str(exc))

    def resume(self):
        try:self.manager.ensure_owner();self.manager.store.resume(self.id);self.manager.tick();self.refresh()
        except Exception as exc:QMessageBox.warning(self, '未继续', str(exc))

    def cancel(self):
        if QMessageBox.question(self, '取消剩余任务', '只取消本批剩余排队动作；已创建的平台单据与预留采购意图保留。确认取消？', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:return
        try:self.manager.ensure_owner();self.manager.store.cancel_queued(self.id);self.refresh()
        except Exception as exc:QMessageBox.warning(self, '未取消', str(exc))

    def done(self, result):self.timer.stop();super().done(result)


class ReceiptSelection(ComponentDialog):
    def __init__(self, parent, host, heads=None, *, client_factory=ProcessClient):
        super().__init__(parent)
        self.host, self.heads = host, heads
        self.folder = Path(host.repository.folder) / 'aoxiang'
        self.init_client(host, client_factory)
        self.started = False;self.worker = None;self._closing = self._can_close = self._close_done = False
        self.records = [];self.created_job = None
        self.poll = QTimer(self)
        self.setWindowTitle('批量收货同步 · 选择已关联采购单');self.resize(1040, 700)
        v = QVBoxLayout(self);v.addWidget(label('立即同步与周期计划分开。打开本页只读取本地记录；周期仅在应用运行时执行。', 'section'))
        self.search = QLineEdit();self.search.setPlaceholderText('筛选门店、仓、补货单号或采购单号');self.search.textChanged.connect(self.filter);v.addWidget(self.search)
        self.table = QTableWidget(0, 6);self.table.setHorizontalHeaderLabels(['选择', '门店 / 仓', '补货单号', '采购单号', '周期计划', '平台 / 最终同步']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(self.table, 1)
        row = QHBoxLayout();row.addWidget(button('勾选当前筛选', self.select_visible));row.addWidget(button('清空勾选', self.clear));self.now_btn = button('立即同步勾选采购单', self.enqueue, True);row.addWidget(self.now_btn);v.addLayout(row)
        row = QHBoxLayout();row.addWidget(QLabel('周期（分钟）'));self.minutes = QSpinBox();self.minutes.setRange(5, 1440);self.minutes.setValue(30);row.addWidget(self.minutes)
        self.enable_btn = button('启用勾选周期同步', lambda:self.configure(True));self.disable_btn = button('停止勾选周期同步', lambda:self.configure(False));row.addWidget(self.enable_btn);row.addWidget(self.disable_btn);v.addLayout(row)
        v.addWidget(label('30分钟仅为可修改的初始填写值，尚未实测平台负载；不点击启用就不会自动执行。已完成订单不再常规查询，最终实收待办单独恢复。', 'muted'))
        self.bar = QProgressBar();v.addWidget(self.bar);self.status = label('读取本地采购记录中…');self.status.setWordWrap(True);v.addWidget(self.status)
        v.addWidget(button('关闭', self.close));self.update_buttons();QTimer.singleShot(0, self.load)

    def update_buttons(self):
        ready = not self.worker and not self._closing
        for b in (self.now_btn, self.enable_btn, self.disable_btn):b.setEnabled(ready and bool(self.records))
        self.bar.setRange(0, 0 if self.worker else 1)
        if not self.worker:self.bar.setValue(1)

    def load(self):
        def operation(_):
            self.ensure_client();items=[];states={}
            for action in ('purchase.list', 'receipt.list'):
                offset=0
                while True:
                    page=self.client.request(action,offset=offset,limit=100)
                    if action=='purchase.list':items.extend(r for r in page['items'] if r['state']=='BOUND')
                    else:states.update({r['id']:r for r in page['items']})
                    offset+=len(page['items'])
                    if offset>=page['total'] or not page['items']:break
            if self.heads is not None:
                # Selection order follows the source lifecycle list, never completion time.
                items=[r for h in self.heads for r in items if all(r['basis'][k]==h[k] for k in ('number','store','warehouse'))]
            return [{**r,'receipt_terminal':states.get(r['id'],{}).get('terminal',False) or r['binding'].get('status')=='已完成','receipt_state':states.get(r['id'],{}).get('state','未查询')} for r in items]
        self.run(operation,self.loaded)

    def loaded(self, records):
        self.records=records;self.render()
        self.status.setText(f'共 {len(records)} 张已核验关联采购单；未连接平台。')

    def render(self):
        schedules={r['id']:r for r in self.host.aoxiang_workflows.store.schedules()};self.table.setRowCount(len(self.records))
        for n,r in enumerate(self.records):
            check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);check.setCheckState(Qt.CheckState.Unchecked);self.table.setItem(n,0,check)
            s=schedules.get(r['id'],{});plan=('已完成停止' if s.get('terminal') else str(s['interval_seconds']//60)+'分钟' if s.get('enabled') else '未启用')
            if s.get('final_pending'):plan+=' · 最终同步待办'
            from aoxiang_receipt_ui import STATES as RECEIPT_STATES
            values=[r['target']['displayName']+' / '+r['basis']['warehouse'],r['basis']['number'],r['binding']['orderId'],plan,RECEIPT_STATES.get(r['receipt_state'],r['receipt_state'])]
            for c,x in enumerate(values,1):self.table.setItem(n,c,QTableWidgetItem(x))
        for c,w in enumerate((50,190,270,180,150,160)):self.table.setColumnWidth(c,w)
        self.filter()

    def filter(self):
        tokens=self.search.text().lower().split()
        for n,r in enumerate(self.records):self.table.setRowHidden(n,not all(t in str(r).lower() for t in tokens))

    def select_visible(self):
        for n in range(len(self.records)):
            if not self.table.isRowHidden(n):self.table.item(n,0).setCheckState(Qt.CheckState.Checked)

    def clear(self):
        for n in range(len(self.records)):self.table.item(n,0).setCheckState(Qt.CheckState.Unchecked)

    def selected(self):
        return [r for n,r in enumerate(self.records) if self.table.item(n,0).checkState()==Qt.CheckState.Checked]

    def enqueue(self):
        try:
            self.host.aoxiang_workflows.ensure_owner()
            self.created_job=self.host.aoxiang_workflows.store.receipt(self.selected());self.close()
        except Exception as exc:QMessageBox.warning(self,'未加入队列',str(exc))

    def configure(self,enabled):
        records=self.selected()
        if not records:QMessageBox.information(self,'先选择范围','请先勾选采购单。');return
        if enabled and QMessageBox.question(self,'启用周期同步',f'启用勾选的 {len(records)} 单，每 {self.minutes.value()} 分钟查询一次，仅在应用运行时执行。\n完整终态验收后自动保存正式实收；已有人工收货或依据变化时停止待核。',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        try:
            self.host.aoxiang_workflows.ensure_owner()
            self.host.aoxiang_workflows.store.configure_schedules(records,self.minutes.value(),enabled);self.render();self.status.setText('周期计划已保存；当前人工窗口关闭后后台才能接管连接。')
        except Exception as exc:QMessageBox.warning(self,'未保存周期计划',str(exc))


def open_receipt_selection(parent, host, heads=None):
    dialog=ReceiptSelection(parent,host,heads)
    proceed=show_modal(host,dialog)
    if proceed and dialog.created_job:host.aoxiang_workflows.open_queue(dialog.created_job['id'])


def prepare_selected(parent, host, heads):
    if not heads:QMessageBox.information(parent,'先选择范围','请先勾选已确认付款的订单。');return
    try:
        host.aoxiang_workflows.ensure_owner()
        job=host.aoxiang_workflows.store.prepare(heads);host.aoxiang_workflows.tick();host.aoxiang_workflows.open_queue(job['id'])
    except Exception as exc:QMessageBox.warning(parent,'未开始准备',str(exc))


def install(host, layout):
    host.aoxiang_workflows=Manager(host)
    row=QHBoxLayout();text=label(host.aoxiang_workflows.message,'muted');row.addWidget(text,1);row.addWidget(button('重新检查后台',host.aoxiang_workflows.retry_queue));row.addWidget(button('查看翱象批次作业',host.aoxiang_workflows.open_queue));layout.addLayout(row)
    host.aoxiang_workflows.changed.connect(lambda:text.setText(host.aoxiang_workflows.message))
