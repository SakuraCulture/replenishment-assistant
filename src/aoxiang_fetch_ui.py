"""Choose stores, collect ordered originals, then explicitly adopt once into the workbench."""
from pathlib import Path
import time
from datetime import datetime
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QComboBox,
                             QCheckBox, QTableWidget, QTableWidgetItem, QHeaderView,
                             QAbstractItemView, QProgressBar, QMessageBox)
from ui_controls import button, label
from aoxiang_ui_common import ComponentDialog, component_root, show_modal
from aoxiang_client import ProcessClient, ComponentError
from aoxiang_jobs import Jobs
from aoxiang_acquisition import ACTIVE, RESUMABLE, prepare, manifest, freeze_manifest

STATES = {'PREPARED': '待核对受理', 'PENDING': '等待', 'QUEUED': '排队', 'RUNNING': '进行中',
          'CANCELLED': '未受理请求已取消',
          'PAUSE_REQUESTED': '正在安全暂停', 'PAUSED': '已暂停', 'INTERRUPTED': '中断，可继续原任务',
          'NEEDS_REVIEW': '平台受理需核对，不自动重发', 'SUCCEEDED': '已验收',
          'COMPLETED_WITH_ERRORS': '部分失败', 'FAILED': '失败'}
PHASES = {'CONNECTING': '连接 Edge，必要时请允许调试连接', 'PROFILE_VERIFIED': '浏览器配置已核对',
          'RUNNING': '查询所选门店', 'INTENT_RECORDED': '准备创建平台导出', 'ACCEPTED': '平台已受理导出',
          'WAIT_TASK': '等待平台生成文件', 'RECOVER_KNOWN_TASK': '查询原平台任务，不重复导出',
          'SAVED': '下载和文件校验完成', 'REUSED': '复用今日已验收文件', 'COMPLETE': '当前门店已完成'}


def history_label(job):
    context = job['context']
    created = datetime.fromisoformat(context['created']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    names = '、'.join(i['displayName'] for i in context['stores'])
    return f'{created} · {names} · {STATES[job["state"]]}'


class FetchDialog(ComponentDialog):
    def __init__(self, parent, folder, day, *, client_factory=ProcessClient):
        super().__init__(parent)
        self.setWindowTitle('获取翱象补货建议')
        self.resize(1040, 760)
        self.folder = Path(folder) / 'aoxiang'
        self.day = day
        self.init_client(parent, client_factory)
        self.started = False
        self.worker = None
        self._closing = self._can_close = self._close_done = False
        self.jobs = None
        self.configuration = None
        self.plan_stores=[i['code'] for i in (getattr(parent,'active_plan',None) or {}).get('stores',[])]
        self.current = None
        self.checks = []
        self.adoption = None
        self.pause_pending = False
        self.event_cursors = {}
        self.last_phase = ''
        self.began = time.monotonic()
        self.poll = QTimer(self)
        self.poll.setInterval(900)
        self.poll.timeout.connect(self.refresh_job)
        layout = QVBoxLayout(self)
        layout.addWidget(label('选门店 → 获取文件 → 采用并检查；当前计算结果在采用前保持不动。', 'section'))
        self.identity = label('正在读取本机已保存配置…', 'muted')
        layout.addWidget(self.identity)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索已核验门店名称 / 编码')
        self.search.textChanged.connect(self.filter_stores)
        row.addWidget(self.search, 1)
        self.all_btn = button('勾选当前筛选', self.select_visible)
        self.none_btn = button('清空全部勾选', self.clear_selection)
        row.addWidget(self.all_btn)
        row.addWidget(self.none_btn)
        self.count = QLabel('已选 0 家')
        row.addWidget(self.count)
        layout.addLayout(row)
        self.stores = QTableWidget(0, 3)
        self.stores.setHorizontalHeaderLabels(['选择', '门店名称', '门店编码'])
        self.stores.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stores.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.stores.setColumnWidth(0, 55)
        self.stores.setColumnWidth(2, 210)
        layout.addWidget(self.stores, 1)
        options = QHBoxLayout()
        self.policy = QComboBox()
        self.policy.addItem('今日已有合格文件则复用，缺少的再获取', 'reuse')
        self.policy.addItem('重新采集最新文件（明确刷新）', 'refresh')
        options.addWidget(self.policy, 1)
        self.start_btn = button('获取所选门店补货建议', self.start_fetch, True)
        options.addWidget(self.start_btn)
        layout.addLayout(options)
        history = QHBoxLayout()
        history.addWidget(QLabel('本机采集记录'))
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.choose_job)
        history.addWidget(self.history, 1)
        self.refresh_btn = button('刷新原任务状态', self.refresh_job)
        history.addWidget(self.refresh_btn)
        layout.addLayout(history)
        self.details = QTableWidget(0, 5)
        self.details.setHorizontalHeaderLabels(['门店', '状态', '数据行数', '取得方式', '说明'])
        self.details.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.details.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.details.setColumnWidth(0, 220)
        self.details.setColumnWidth(1, 180)
        self.details.cellDoubleClicked.connect(self.show_task_detail)
        self.details.setToolTip('双击门店记录可复制完整错误与平台任务编号；不会重新提交任务。')
        layout.addWidget(self.details, 1)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.status = label('采集可以暂停；已经被平台受理的任务会保留。关闭后从原采集记录继续。', 'muted')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.pause_btn = button('停止后续取数', self.pause_fetch)
        self.resume_btn = button('继续原采集任务', self.resume_fetch)
        self.retry_items_btn=button('重跑全部失败门店',self.retry_items);actions.addWidget(self.retry_items_btn)
        self.cancel_request_btn = button('取消未受理请求', self.cancel_request)
        self.adopt_btn = button('采用已验收文件并检查', self.select_adoption, True)
        self.close_btn = button('关闭', self.close)
        for widget in [self.pause_btn, self.resume_btn, self.cancel_request_btn, self.adopt_btn, self.close_btn]:
            actions.addWidget(widget)
        layout.addLayout(actions)
        self.update_buttons()
        self.run(self.initialize, self.initialized)

    def initialize(self, progress):
        self.ensure_client()
        self.jobs = Jobs(self.folder / 'jobs.sqlite3')
        return self.client.request('configuration'), self.jobs.recent(include_unfinished=True)

    def initialized(self, data):
        self.configuration, history = data
        if self.configuration.get('configured'):
            self.identity.setText(self.configuration['display_name'] + ' · ' + self.configuration['account_binding']['merchant_code'])
            original=self.configuration['targets'];by_code={t['buyerWarehouseCode']:t for t in original}
            targets=[by_code[c] for c in dict.fromkeys([*self.plan_stores,*by_code]) if c in by_code]
            self.stores.setRowCount(len(targets))
            for row, target in enumerate(targets):
                check = QCheckBox()
                check.toggled.connect(self.update_buttons)
                self.checks.append(check)
                self.stores.setCellWidget(row, 0, check)
                self.stores.setItem(row, 1, QTableWidgetItem(target['displayName']))
                self.stores.setItem(row, 2, QTableWidgetItem(target['buyerWarehouseCode']))
                check.setChecked(target['buyerWarehouseCode'] in self.plan_stores)
            missing=[s for s in self.plan_stores if s not in by_code]
            if missing:self.identity.setText(self.identity.text()+'；计划门店未核验：'+'、'.join(missing)+'，请补充门店核验。')
        else:
            self.identity.setText('尚未配置。请关闭本窗口，先完成“翱象连接与门店配置”。')
        self.fill_history(history)
        if history:
            self.show_job(history[0])

    def fill_history(self, values, selected=None):
        self.history.blockSignals(True)
        try:
            self.history.clear()
            for value in values:
                self.history.addItem(history_label(value), value['id'])
            if selected:
                self.history.setCurrentIndex(self.history.findData(selected))
        finally:
            self.history.blockSignals(False)

    def filter_stores(self):
        text = self.search.text().strip().casefold()
        for row in range(self.stores.rowCount()):
            self.stores.setRowHidden(row, text not in ' '.join(self.stores.item(row, c).text() for c in [1, 2]).casefold())

    def select_visible(self):
        for row, check in enumerate(self.checks):
            if not self.stores.isRowHidden(row):
                check.setChecked(True)

    def clear_selection(self):
        for check in self.checks:
            check.setChecked(False)

    def selected_codes(self):
        return [self.stores.item(row, 2).text() for row, check in enumerate(self.checks) if check.isChecked()]

    def start_fetch(self):
        codes, policy = self.selected_codes(), self.policy.currentData()
        if not codes:
            return
        self.poll.stop()
        self.began = time.monotonic()
        self.last_phase = ''
        def operation(progress):
            job = prepare(self.jobs, self.configuration, codes, policy, day=self.day)
            try:
                self.jobs.dispatch(self.client, job['id'])
            except ComponentError:
                # The intention and transport error remain available; next action queries/replays this exact ID.
                pass
            return self.jobs.get(job['id']), self.jobs.recent(include_unfinished=True)
        self.run(operation, self.created)

    def created(self, data):
        job, history = data
        self.fill_history(history, job['id'])
        self.show_job(job)

    def choose_job(self):
        if self.worker or self._closing or not self.history.currentData():
            return
        command_id = self.history.currentData()
        self.poll.stop()
        self.last_phase = ''
        self.run(lambda _: self.jobs.get(command_id), self.show_job)

    def show_job(self, job):
        from readable_ui import failure_message
        self.current = job
        history_index = self.history.findData(job['id'])
        if history_index >= 0:
            self.history.setItemText(history_index, history_label(job))
        record = job.get('record') or {}
        items = record.get('items', [])
        names = {i['buyerWarehouseCode']: i['displayName'] for i in job['context']['stores']}
        self.details.setRowCount(len(items))
        for row, item in enumerate(items):
            self.details.setRowHeight(row, 48)
            result, error = item.get('result') or {}, item.get('error') or {}
            values = [names[item['warehouse_code']] + '\n' + item['warehouse_code'], STATES[item['state']],
                      result.get('rows', '—'), ('今日复用' if result.get('reused') else '本次下载') if result else '—',
                      failure_message(str(error.get('code',''))+'：'+str(error.get('message',''))) if error else PHASES.get(item.get('phase'), '')]
            for column, value in enumerate(values):
                self.details.setItem(row, column, QTableWidgetItem(str(value)))
            self.details.item(row,4).setToolTip(str(error) if error else '平台任务编号：'+str(item.get('task_id','尚未取得')))
            if self.details.columnCount()<6:self.details.setColumnCount(6);self.details.setHorizontalHeaderItem(5,QTableWidgetItem('单项操作'))
            if item['state']=='FAILED':
                b=button('重跑此任务',lambda checked=False,index=row:self.retry_items([index]));b.setEnabled(job['state'] in {'PAUSED','INTERRUPTED','COMPLETED_WITH_ERRORS'} and not self.worker);self.details.setCellWidget(row,5,b)
            else:self.details.removeCellWidget(row,5)
        finished = sum(i['state'] in {'SUCCEEDED', 'FAILED'} for i in items)
        self.progress.setRange(0, max(len(items), 1))
        self.progress.setValue(finished)
        error = job.get('last_error') or record.get('error') or {}
        accepted = sum(bool(i.get('task_id')) for i in items)
        self.progress.setFormat('%v / %m 家已处理')
        self.status.setText(f'{STATES[job["state"]]} · 已受理导出 {accepted} 家 · 已处理 {finished}/{len(items)} 家 · {self.last_phase}' +
                            (f' · 本窗口已等待 {int(time.monotonic() - self.began)} 秒' if job['state'] in ACTIVE else '') +
                            (' · '+failure_message(str(error.get('code',''))+'：'+str(error.get('message',''))) if error else ''))
        self.status.setToolTip(str(error) if error else '')
        if job['state'] in ACTIVE:
            self.poll.start()
        else:
            self.poll.stop()
        self.update_buttons()

    def show_task_detail(self,row,column):
        import json
        from readable_ui import failure
        items=((self.current or {}).get('record') or {}).get('items',[])
        if 0<=row<len(items):
            item=items[row]
            raw={'平台任务编号':item.get('task_id'),'状态':STATES.get(item['state'],item['state']),'错误':item.get('error')}
            failure(self,'门店任务详情 · 只读',json.dumps(raw,ensure_ascii=False,indent=2))

    def refresh_job(self):
        if self.worker or self._closing or not self.current:
            return
        command_id = self.current['id']
        after = self.event_cursors.get(command_id, 0)
        def operation(progress):
            job = self.jobs.refresh(self.client, command_id)
            events = self.client.request('events', command_id=command_id, after=after, limit=100)['events']
            return job, events
        def success(data):
            job, events = data
            for event in events:
                self.event_cursors[command_id] = event['sequence']
                stage = event['detail'].get('stage')
                if stage:
                    self.last_phase = PHASES.get(stage, '正在执行当前门店步骤')
            self.show_job(job)
        self.run(operation, success)

    def pause_fetch(self):
        if not self.current:
            return
        if self.worker:
            self.pause_pending = True
            self.status.setText('已请求停止后续取数，等待当前控制请求返回。')
            return
        command_id = self.current['id']
        def operation(progress):
            self.jobs.observe(command_id, self.client.request('pause', command_id=command_id))
            return self.jobs.get(command_id)
        self.run(operation, self.show_job)

    def on_idle(self):
        if self.pause_pending:
            self.pause_pending = False
            self.pause_fetch()

    def resume_fetch(self):
        if not self.current:
            return
        command_id = self.current['id']
        state = self.current['state']
        def operation(progress):
            if state == 'PREPARED':
                return self.jobs.dispatch(self.client, command_id)
            self.jobs.observe(command_id, self.client.request('resume', command_id=command_id))
            return self.jobs.get(command_id)
        self.run(operation, self.show_job)

    def retry_items(self,indices=None):
        if not self.current or self.worker:return
        current=self.current;record=current.get('record') or {}
        if indices is None or isinstance(indices,bool):indices=[i['index'] for i in record.get('items',[]) if i['state']=='FAILED']
        def operation(progress):
            self.jobs.observe(current['id'],self.client.request('retry',command_id=current['id'],indices=indices,sequence=record['sequence']))
            return self.jobs.get(current['id'])
        self.run(operation,self.show_job)

    def select_adoption(self):
        if not self.current:
            return
        items = (self.current.get('record') or {}).get('items', [])
        missing = sum(i['state'] != 'SUCCEEDED' for i in items)
        if missing and QMessageBox.question(self, '仅采用已完成门店',
                f'仍有 {missing} 家未取得合格文件，本次不会进入检查计算。是否仅采用已验收门店？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        command_id = self.current['id']
        def operation(progress):
            value = manifest(self.jobs.get(command_id), self.folder / 'component-state')
            freeze_manifest(value, self.folder)
            return value
        def success(value):
            self.adoption = value
            self.close()
        self.run(operation, success)

    def cancel_request(self):
        if not self.current or self.current['state'] != 'PREPARED':
            return
        command_id = self.current['id']
        self.run(lambda _: self.jobs.cancel_unaccepted(self.client, command_id), self.show_job)

    def failure(self, text):
        self.poll.stop()
        super().failure(text)

    def update_buttons(self, *_):
        available = not self.worker and not self._closing
        state = self.current['state'] if self.current else None
        active = state in ACTIVE
        count = len(self.selected_codes())
        self.count.setText(f'已选 {count} 家')
        for widget in [self.stores, self.policy, self.all_btn, self.none_btn]:
            widget.setEnabled(available and not active)
        self.start_btn.setEnabled(available and not active and bool(count) and bool((self.configuration or {}).get('enabled')))
        self.history.setEnabled(available and not active)
        self.refresh_btn.setEnabled(available and bool(self.current))
        self.pause_btn.setEnabled(not self._closing and state in {'QUEUED', 'RUNNING'} and not self.pause_pending)
        self.resume_btn.setEnabled(available and state in RESUMABLE | {'PREPARED'})
        self.retry_items_btn.setEnabled(available and state in {'PAUSED','INTERRUPTED','COMPLETED_WITH_ERRORS'} and any(i['state']=='FAILED' for i in ((self.current or {}).get('record') or {}).get('items',[])))
        self.cancel_request_btn.setEnabled(available and state == 'PREPARED')
        successes = any(i['state'] == 'SUCCEEDED' for i in (self.current or {}).get('record', {}).get('items', [])) if self.current and self.current.get('record') else False
        self.adopt_btn.setEnabled(available and not active and successes)
        self.close_btn.setEnabled(not self._closing)


def open_fetch(host):
    if host.worker or host.readonly or host.closing:
        return
    host.auto_check.stop()
    dialog = FetchDialog(host, host.repository.folder, host.job_day)
    may_adopt = show_modal(host, dialog)
    if may_adopt and dialog.adoption and not host.closing:
        from aoxiang_input_ui import adopt
        adopt(host, dialog.adoption)
