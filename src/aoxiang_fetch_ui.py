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
          'ENDED': '已结束，可新建任务',
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
        self.pending_maintenance = None
        self.plan_stores=[i['code'] for i in (getattr(parent,'active_plan',None) or {}).get('stores',[])]
        self.current = None
        self.checks = []
        self.adoption = None
        self.pause_pending = False
        self.event_cursors = {}
        self.last_phase = ''
        self._checking_new_fetch=False
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
        self.reexport_btn=button('重新导出所选门店',self.reexport_fetch)
        self.reexport_btn.setToolTip('安全结束所选门店的旧采集项，再创建新导出；其他门店、旧文件和已采用数据保留。')
        options.addWidget(self.reexport_btn)
        layout.addLayout(options)
        history = QHBoxLayout()
        history.addWidget(QLabel('本机采集记录'))
        self.history = QComboBox()
        self.history.currentIndexChanged.connect(self.choose_job)
        history.addWidget(self.history, 1)
        self.refresh_btn = button('刷新原任务状态', self.refresh_job)
        history.addWidget(self.refresh_btn)
        self.end_btn = button('结束本次采集', lambda: self.maintain('end'))
        self.delete_btn = button('删除采集记录', lambda: self.maintain('delete'))
        history.addWidget(self.end_btn)
        history.addWidget(self.delete_btn)
        self.end_btn.setToolTip('放弃本批未采用的采集，释放门店后可以新建；先预览受影响的旧任务。')
        self.delete_btn.setToolTip('删除已结束的采集记录；已采用文件、草稿和签发数据保留。')
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
        self.jobs.synchronize(self.client)
        return self.client.request('configuration'), self.jobs.recent(include_unfinished=True,exclude_analysis=True)

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
        self._checking_new_fetch=True
        self.progress.setRange(0,0);self.status.setText('正在核验本次登录；尚未创建所选门店的采集任务。下方为历史记录。')
        def operation(progress):
            self.ensure_client()
            self.jobs.synchronize(self.client)
            from aoxiang_current_login import current_configuration
            configuration=current_configuration(self.client,progress)
            from aoxiang_acquisition import PendingAcquisition
            try:job = prepare(self.jobs, configuration, codes, policy, day=self.day)
            except PendingAcquisition as exc:
                return {'blockers':exc.blockers,'message':str(exc),'configuration':configuration,
                        'history':self.jobs.recent(include_unfinished=True,exclude_analysis=True)}
            try:
                self.jobs.dispatch(self.client, job['id'])
            except ComponentError:
                # The intention and transport error remain available; next action queries/replays this exact ID.
                pass
            return self.jobs.get(job['id']), self.jobs.recent(include_unfinished=True,exclude_analysis=True),configuration
        self.run(operation, self.start_checked)

    def start_checked(self,data):
        self._checking_new_fetch=False
        if not isinstance(data,dict) or 'blockers' not in data:return self.created(data)
        self.configuration=data['configuration'];first=data['blockers'][0]['id']
        self.fill_history(data['history'],first)
        self.show_job(self.jobs.get(first))
        self.failure(data['message'])

    def created(self, data):
        job, history, *configuration = data
        if configuration:
            self.configuration=configuration[0]
            self.identity.setText(self.configuration['display_name']+' · '+self.configuration['account_binding']['merchant_code'])
        self.fill_history(history, job['id'])
        self.show_job(job)

    def reexport_fetch(self):
        if self.worker or self._closing or not self.selected_codes():return
        codes=self.selected_codes();self.poll.stop()
        def operation(progress):
            self.ensure_client()
            from aoxiang_current_login import current_configuration
            configuration=current_configuration(self.client,progress)
            plan=self.jobs.reexport_preview(self.client,configuration,codes,progress=progress,cancel=lambda:self._closing)
            plan['configuration']=configuration
            return plan
        self.run(operation,self.maintenance_previewed)

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
        if not record and job['state']=='PREPARED':
            # Display the original local scope without inventing a platform receipt.
            items=[{'warehouse_code':code,'state':'PENDING'} for code in job['command']['scope']['warehouse_codes']]
        names = {i['buyerWarehouseCode']: i['displayName'] for i in job['context']['stores']}
        self.details.setRowCount(len(items))
        for row, item in enumerate(items):
            self.details.setRowHeight(row, 48)
            result, error = item.get('result') or {}, item.get('error') or {}
            values = [names[item['warehouse_code']] + '\n' + item['warehouse_code'], STATES[item['state']],
                      result.get('rows', '—'), ('今日复用' if result.get('reused') else '本次下载') if result else '—',
                      failure_message(str(error.get('code',''))+'：'+str(error.get('message',''))) if error else PHASES.get(item.get('phase'), '')]
            if item['state'] == 'ENDED':
                values[4] = '本地采集已结束；旧平台任务可能继续完成，后续采集使用独立新任务。' if item.get('retained_platform_task') else '本地采集已结束，可重新获取；原错误见双击详情。'
            elif item.get('resumed_accepted_task') and not error:
                values[4] = '接续原平台任务；文件保留原采集时间。' + values[4]
            elif not record and job['state']=='PREPARED':
                values[4] = '本地请求已登记，平台受理情况尚未核对；可继续原任务或按所选门店重导。'
            elif job['state'] not in ACTIVE and item['state']=='RUNNING':
                values[1] = '已停止本地取数'
            for column, value in enumerate(values):
                self.details.setItem(row, column, QTableWidgetItem(str(value)))
            self.details.item(row,4).setToolTip(str(error) if error else '平台任务编号：'+str(item.get('task_id','尚未取得')))
            if self.details.columnCount()<6:self.details.setColumnCount(6);self.details.setHorizontalHeaderItem(5,QTableWidgetItem('单项操作'))
            if item['state']=='FAILED':
                b=button('重跑此任务',lambda checked=False,index=row:self.retry_items([index]));b.setEnabled(job['state'] in {'PAUSED','INTERRUPTED','COMPLETED_WITH_ERRORS'} and not self.worker);self.details.setCellWidget(row,5,b)
            else:self.details.removeCellWidget(row,5)
        finished = sum(i['state'] in {'SUCCEEDED', 'FAILED', 'ENDED'} for i in items)
        self.progress.setRange(0, max(len(items), 1))
        self.progress.setValue(finished)
        error = job.get('last_error') or record.get('error') or {}
        if job['state']=='NEEDS_REVIEW':
            self.last_phase='可核对原回执并继续其他门店；需要新文件时勾选上方门店，点击“重新导出所选门店”。旧记录和原件保留。'
        accepted = sum(bool(i.get('task_id')) for i in items)
        self.progress.setFormat('%v / %m 家已处理')
        self.status.setText(f'{STATES[job["state"]]} · 已受理导出 {accepted} 家 · 已处理 {finished}/{len(items)} 家 · {self.last_phase}' +
                            (f' · 本窗口已等待 {int(time.monotonic() - self.began)} 秒' if job['state'] in ACTIVE else '') +
                            (' · '+failure_message(str(error.get('code',''))+'：'+str(error.get('message',''))) if error else ''))
        self.status.setToolTip(str(error) if error else '')
        if job['state'] == 'ENDED':
            self.status.setText('本次采集已结束，可以重新选择门店获取。已采用文件、草稿和签发数据保留。')
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
            self.ensure_client()
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
            self.ensure_client()
            self.jobs.observe(command_id, self.client.request('pause', command_id=command_id))
            return self.jobs.get(command_id)
        self.run(operation, self.show_job)

    def on_idle(self):
        if self.pending_maintenance is not None:
            plan, self.pending_maintenance = self.pending_maintenance, None
            if plan['action']=='reexport':
                def operation(progress):
                    self.ensure_client();self.jobs.reexport_apply(self.client,plan)
                    job=prepare(self.jobs,plan['configuration'],plan['codes'],'refresh',day=self.day)
                    try:self.jobs.dispatch(self.client,job['id'])
                    except ComponentError:pass  # Original new ID is durable; never create another on lost acknowledgement.
                    return self.jobs.get(job['id']),self.jobs.recent(include_unfinished=True,exclude_analysis=True),plan['configuration']
                self.run(operation,self.created)
                return
            def operation(progress):
                progress('正在结束采集并释放门店…' if plan['action'] == 'end' else '正在删除采集记录，保留已采用文件…')
                job = self.jobs.maintenance_apply(self.client, plan)
                return job, self.jobs.recent(include_unfinished=True,exclude_analysis=True), plan['action']
            self.run(operation, self.maintenance_done)
            return
        if self.pause_pending:
            self.pause_pending = False
            self.pause_fetch()

    def resume_fetch(self):
        if not self.current:
            return
        command_id = self.current['id']
        state = self.current['state']
        def operation(progress):
            self.ensure_client()
            latest=self.jobs.refresh(self.client,command_id) if state!='PREPARED' else self.jobs.get(command_id)
            if latest['state'] == 'PREPARED':
                return self.jobs.dispatch(self.client, command_id)
            if latest['state'] not in RESUMABLE | {'NEEDS_REVIEW'}:return latest
            self.jobs.observe(command_id, self.client.request('collection.continue' if latest['state']=='NEEDS_REVIEW' else 'resume', command_id=command_id))
            return self.jobs.get(command_id)
        self.run(operation, self.show_job)

    def retry_items(self,indices=None):
        if not self.current or self.worker:return
        current=self.current;record=current.get('record') or {}
        if indices is None or isinstance(indices,bool):indices=[i['index'] for i in record.get('items',[]) if i['state']=='FAILED']
        def operation(progress):
            self.ensure_client()
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

    def maintain(self, action):
        if self.worker or self._closing or not self.current:
            return
        command_id = self.current['id']
        self.poll.stop()
        def operation(progress):
            self.ensure_client()
            if action=='end':
                latest=self.jobs.refresh(self.client,command_id) if self.current['record'] else self.jobs.get(command_id)
                if latest['state'] in ACTIVE:
                    self.jobs.observe(command_id,self.client.request('pause',command_id=command_id));deadline=time.monotonic()+90
                    while self.jobs.get(command_id)['state'] in ACTIVE:
                        progress('等待当前页面动作安全结束，不等待平台生成完成…')
                        if self._closing or time.monotonic()>=deadline:raise ValueError('当前动作仍在安全收尾，未强杀任务；稍后可继续结束')
                        time.sleep(.15);self.jobs.refresh(self.client,command_id)
            return self.jobs.maintenance_preview(self.client, command_id, action)
        self.run(operation, self.maintenance_previewed)

    def maintenance_previewed(self, plan):
        if self._closing:
            return
        if plan['blocked']:
            self.failure('\n'.join(f"{b['store']}：{b['reason']}" + (f"（平台任务 {b['task_id']}）" if b.get('task_id') else '') for b in plan['blocked']))
            return
        if self.confirm_maintenance(plan):
            self.pending_maintenance = plan
            if self.worker is None and not self._closing:
                QTimer.singleShot(0, self.on_idle)

    def confirm_maintenance(self, plan):
        reexport=plan['action']=='reexport'
        ending = plan['action'] in {'end','reexport'}
        title = '重新导出所选门店' if reexport else '结束本次采集' if ending else '删除采集记录'
        stores = [s['name'] for s in plan['stores']]
        text = ('结束所选门店旧采集项，并创建新的导出。' if reexport else '结束后不再继续本次本地采集，相关门店可新建任务。' if ending else '这条采集记录将从本机列表删除，不能恢复。')
        text += '\n门店：' + '、'.join(stores[:8]) + (f' 等 {len(stores)} 家（完整范围见详细信息）' if len(stores)>8 else '')
        if plan.get('related'):
            text += f"\n同时结束 {len(plan['related'])} 条旧记录中阻塞这些门店的任务；旧记录的其他门店保持不变。"
        if plan.get('retained'):
            text += '\n旧平台任务可能继续完成，重新导出可能产生重复文件；旧回执和原件保留，不会覆盖新结果。'
        text += '\n\n已采用文件、草稿、签发数据及原下载文件均保留。不会删除翱象平台任务。'
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText('确认重新导出' if reexport else '确认结束' if ending else '确认删除')
        box.button(QMessageBox.StandardButton.No).setText('取消')
        details = '\n'.join(f"{s['name']}（{s['code']}）" for s in plan['stores'])
        if plan.get('related'):
            details += '\n关联旧记录：\n' + '\n'.join(f"{r['id']}：{'、'.join(r['stores'])}" for r in plan['related'])
        box.setDetailedText(details)
        return box.exec() == QMessageBox.StandardButton.Yes

    def maintenance_done(self, data):
        job, history, action = data
        self.current = None
        self.fill_history(history, job['id'] if job else None)
        if job or history:
            self.show_job(job or history[0])
        else:
            self.details.setRowCount(0)
            self.progress.setRange(0, 1)
            self.progress.setValue(0)
        if action == 'delete':
            self.status.setText('采集记录已删除；已采用文件、草稿和签发数据保留。可重新选择门店获取。')
        self.update_buttons()

    def failure(self, text):
        self.poll.stop()
        if self._checking_new_fetch:
            self._checking_new_fetch=False;self.progress.setRange(0,1);self.progress.setValue(0)
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
        self.reexport_btn.setEnabled(available and bool(count) and bool((self.configuration or {}).get('enabled')))
        self.history.setEnabled(available and not active)
        self.refresh_btn.setEnabled(available and bool(self.current))
        self.end_btn.setEnabled(available and bool(self.current) and state not in {'ENDED', 'CANCELLED', 'SUCCEEDED'})
        self.delete_btn.setEnabled(available and state in {'ENDED', 'CANCELLED', 'SUCCEEDED'})
        self.pause_btn.setEnabled(not self._closing and state in {'QUEUED', 'RUNNING'} and not self.pause_pending)
        self.pause_btn.setText('已停止取数' if state in RESUMABLE else '停止后续取数')
        self.pause_btn.setToolTip('本地取数已停止。可以继续原任务，或结束本次采集后新建；已受理平台任务保留。' if state in RESUMABLE else '等待当前动作安全结束，保留原平台任务。')
        self.resume_btn.setText('核对原回执 / 继续其他门店' if state == 'NEEDS_REVIEW' else '继续原采集任务')
        self.resume_btn.setToolTip('已有确切回执的项接回原任务；其余待核项保持保护，不会重发。等待中的其他门店可以继续。')
        self.resume_btn.setEnabled(available and state in RESUMABLE | {'PREPARED', 'NEEDS_REVIEW'})
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
