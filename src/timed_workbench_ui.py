"""Desktop entry points for report plans, prerequisites and persistent runs."""
from pathlib import Path
import sqlite3
import threading
import uuid

from PySide6.QtCore import QDate, QObject, QTime, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QLineEdit, QMessageBox, QPlainTextEdit, QSpinBox,
                               QTabWidget, QTimeEdit, QVBoxLayout, QWidget)

from aoxiang_ui_common import Call
import business_daily_sources as sources
import export_locations
from scheduled_ui import fill, table
from stockout_common import today
from timed_exports import DATABASE, KINDS, STATES, Repository, Runner, plan_value, preview
from timed_report_services import Reports
from ui_controls import button, label
from unattended_resources import configured


def _date(value):
    result = QDateEdit(QDate.fromString(value, 'yyyy-MM-dd'))
    result.setCalendarPopup(True)
    result.setDisplayFormat('yyyy-MM-dd')
    return result


def _day(widget):
    return widget.date().toString('yyyy-MM-dd')


def _open(path):
    if not Path(path).exists():
        raise ValueError('文件或目录已被移动：' + str(path))
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


class Manager(QObject):
    def __init__(self, host, nav):
        super().__init__(host)
        self.host = host
        self.root = Path(host.repository.folder)
        self.repo = Repository(self.root / DATABASE)
        self.backend = Reports(self.root)
        self.reader = self.worker = self.window = self.active = None
        self.cancel = None
        self.closing = self.recovered = False
        self.selected_run = None
        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        nav.insertWidget(len(host.top_nav), button('定时工作台', self.open))

    def open(self):
        if self.window is None:
            self.window = Workbench(self)
        self.window.show()
        self.window.raise_()
        self.window.refresh()
        return self.window

    def edit(self, report_type=None, scope=None, dates=None, *, existing=None, manual=False):
        dialog = PlanEditor(self, report_type, scope, dates, existing, manual)
        dialog.exec()
        if dialog.run_id:
            self.selected_run = dialog.run_id
            self.open().tabs.setCurrentIndex(0)
        elif self.window:
            self.window.refresh()
        self.poll()

    def _start(self, rid):
        self.cancel = threading.Event()
        self.active = rid
        from catalog_session_ui import manager as catalog_manager
        resources = catalog_manager(self.host).resources if configured(self.root) else None
        task = Call(lambda _: Runner(self.repo, self.backend, self.cancel, resources=resources).execute(rid), self)
        self.worker = task
        def done():
            self.worker = self.active = None
            task.deleteLater()
            if self.window and self.window.isVisible():
                self.window.refresh()
            if self.closing:
                QTimer.singleShot(0, self.host.close)
            else:
                self.poll()
        task.finished.connect(done)
        task.start()

    def poll(self):
        if self.reader or self.closing or not self.host.scheduled.owner:
            return
        def read(_):
            if not self.recovered:
                self.repo.recover()
                self.recovered = True
            self.repo.due()
            return self.repo.next()
        task = Call(read, self)
        self.reader = task
        def take(rid):
            pending = getattr(self.host, '_large_io_dialog', None)
            parallel = configured(self.root)
            busy = (getattr(self.host, '_data_location_preparing', False)
                    or getattr(self.host.aoxiang_workflows,'maintenance_depth',0)
                    or not parallel and (self.host.scheduled.worker or self.host.stockout_hub.worker
                                         or any(child.worker for child in self.host.stockout_hub.children.values()))
                    or pending and not pending._completed and not getattr(pending,'scoped_collection',False))
            if rid and not self.worker and not busy and not self.closing:
                self._start(rid)
        def done():
            self.reader = None
            task.deleteLater()
            if self.window and self.window.isVisible():
                self.window.refresh()
            if self.closing:
                QTimer.singleShot(0, self.host.close)
        task.succeeded.connect(take)
        task.finished.connect(done)
        task.start()

    def pause(self, rid):
        if self.active == rid:
            self.cancel.set()
        else:
            self.repo.request(rid, pause=True)

    def prepare_close(self):
        self.closing = True
        self.timer.stop()
        if self.cancel and self.worker:
            self.cancel.set()
        return not self.reader and not self.worker

    def resume_after_cancelled_close(self):
        self.closing = False
        self.timer.start()

    def original_stockout(self, schedule=False):
        entry = self.host.stockout_hub
        entry.enter()
        if schedule and entry.page:
            entry.page.schedule()


class PlanEditor(QDialog):
    def __init__(self, manager, report_type, scope, dates, existing, manual):
        super().__init__(manager.host)
        self.manager, self.existing, self.run_id = manager, existing, None
        self.request_id = uuid.uuid4().hex
        self.scope = (existing or {}).get('scope') or scope or {}
        existing = existing or {}
        self.setWindowTitle('立即导出' if manual else '定时导出安排')
        self.resize(760, 685)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)
        self.kind = QComboBox()
        for key, title in KINDS.items():
            self.kind.addItem(title, key)
        self.kind.setCurrentIndex(self.kind.findData(existing.get('type', report_type or 'business_daily')))
        self.kind.setEnabled(report_type is None and not existing)
        form.addRow('报表', self.kind)
        self.name = QLineEdit(existing.get('name', self.kind.currentText()))
        form.addRow('安排名称', self.name)
        self.rule = QComboBox()
        for code, title in (('fixed', '固定日期'), ('today', '计划当天'), ('yesterday', '计划前一天'), ('last7', '截至前一天近7天')):
            self.rule.addItem(title, code)
        self.rule.setCurrentIndex(self.rule.findData(existing.get('date_rule', 'fixed' if manual else 'yesterday')))
        form.addRow('业务日期规则', self.rule)
        start, end = existing.get('dates') or dates or (today(), today())
        self.start, self.end = _date(start), _date(end)
        day_row = QHBoxLayout()
        day_row.addWidget(self.start)
        day_row.addWidget(label('至'))
        day_row.addWidget(self.end)
        form.addRow('固定业务日期', day_row)
        self.clock = QTimeEdit(QTime.fromString(existing.get('time', '08:30'), 'HH:mm'))
        self.clock.setDisplayFormat('HH:mm')
        form.addRow('开始时间（北京时间）', self.clock)
        self.repeat = QComboBox()
        self.repeat.addItem('每周所选日', 'weekly')
        self.repeat.addItem('仅指定日期', 'once')
        self.repeat.setCurrentIndex(self.repeat.findData(existing.get('kind', 'weekly')))
        form.addRow('重复方式', self.repeat)
        week = QHBoxLayout()
        self.weekdays = []
        for i, name in enumerate('一二三四五六日'):
            field = QCheckBox('周' + name)
            field.setChecked(i in existing.get('weekdays', range(7)))
            self.weekdays.append(field)
            week.addWidget(field)
        form.addRow('每周执行日', week)
        self.once = _date(existing.get('date', today()))
        form.addRow('执行日期', self.once)
        self.wait = QSpinBox()
        self.wait.setRange(1, 1440)
        self.wait.setValue(existing.get('wait_minutes', 30))
        self.wait.setSuffix(' 分钟')
        form.addRow('最多等待', self.wait)
        self.partial = QCheckBox('以已覆盖的所选范围交付参考文件')
        self.partial.setChecked(existing.get('partial', False))
        form.addRow('覆盖不足', self.partial)
        path = QHBoxLayout()
        self.folder = QLineEdit(existing.get('directory') or export_locations.initial_directory(manager.host))
        path.addWidget(self.folder, 1)
        path.addWidget(button('选择目录', self.choose))
        form.addRow('输出目录', path)
        self.scope_label = label(self.describe(), 'muted')
        self.scope_label.setWordWrap(True)
        layout.addWidget(self.scope_label)
        self.note = QPlainTextEdit()
        self.note.setReadOnly(True)
        self.note.setMaximumHeight(130)
        layout.addWidget(self.note)
        controls = QHBoxLayout()
        self.ready = button('立即导出', lambda:self.save('manual'), manual)
        controls.addWidget(self.ready)
        self.materials = button('日报资料', self.materials_for_day)
        self.materials.setVisible(self.kind.currentData() == 'business_daily')
        self.kind.currentIndexChanged.connect(
            lambda:self.materials.setVisible(self.kind.currentData() == 'business_daily'))
        controls.addWidget(self.materials)
        controls.addWidget(button('预览未来三次', self.refresh_preview))
        controls.addWidget(button('保存草稿', lambda:self.save('draft')))
        controls.addWidget(button('保存并启用', lambda:self.save('enable'), not manual))
        layout.addLayout(controls)
        self.refresh_preview()

    def describe(self):
        if self.scope.get('orders'):
            return '指定原仓单 ' + '、'.join(self.scope['orders'][:6]) + f'，共 {len(self.scope["orders"])} 单'
        if self.scope.get('mode'):
            names = {'store': '门店', 'warehouse': '大仓', 'both': '门店和大仓'}
            warehouses = self.scope['warehouse_query'].get('filters', {}).get('warehouse') or ['山东仓', '河北仓']
            return f'{names[self.scope["mode"]]}范围。所选大仓为' + '、'.join(warehouses)
        return '执行时按保存的报表条件锁定当次资料与范围。'

    def choose(self):
        value = QFileDialog.getExistingDirectory(self, '选择本次导出位置', self.folder.text())
        if value:
            self.folder.setText(value)

    def materials_for_day(self):
        dialog = Materials(self, self.manager.root, _day(self.end))
        dialog.exec()

    def value(self, enabled=False):
        scope = dict(self.scope)
        report_type = self.kind.currentData()
        if report_type == 'stockout' and not scope:
            from stockout_views import defaults as store_defaults
            from warehouse_stockout_views import defaults as warehouse_defaults
            scope = {'mode': 'both', 'store_query': {}, 'warehouse_query': {},
                     'store_combinations': store_defaults()['combinations'],
                     'warehouse_combinations': warehouse_defaults()['combinations'], 'include_detail': False}
        return plan_value({'name': self.name.text(), 'type': report_type, 'date_rule': self.rule.currentData(),
                           'dates': [_day(self.start), _day(self.end)], 'time': self.clock.time().toString('HH:mm'),
                           'kind': self.repeat.currentData(), 'date': _day(self.once),
                           'weekdays': [n for n, field in enumerate(self.weekdays) if field.isChecked()],
                           'wait_minutes': self.wait.value(), 'directory': self.folder.text(),
                           'partial': self.partial.isChecked(), 'scope': scope, 'enabled': enabled})

    def refresh_preview(self):
        try:
            rows = preview(self.value(), self.manager.repo.clock())
            lines = [f'{r["time"][:16].replace("T"," ")}  ·  {" 至 ".join(dict.fromkeys(r["dates"]))}\n{r["name"]}'
                     for r in rows]
            self.note.setPlainText('\n'.join(lines) if lines else '所选时间之后没有执行时点')
        except (KeyError, ValueError) as exc:
            self.note.setPlainText(str(exc))

    def save(self, mode):
        try:
            p = self.value(mode == 'enable')
            if mode == 'manual':
                self.run_id = self.manager.repo.manual(p, self.request_id)['id']
            else:
                self.manager.repo.save_plan(p, self.existing)
            self.accept()
        except Exception as exc:
            self.note.setPlainText(str(exc))


class Workbench(QDialog):
    def __init__(self, manager):
        super().__init__(manager.host)
        self.manager = manager
        self.offset = 0
        self.setWindowTitle('定时工作台')
        self.resize(1160, 750)
        layout = QVBoxLayout(self)
        shortcuts = QHBoxLayout()
        shortcuts.addWidget(button('原定时补货', manager.host.scheduled.open))
        shortcuts.addWidget(button('缺货每日采集计划', lambda:manager.original_stockout(True)))
        shortcuts.addWidget(button('缺货采集记录', manager.original_stockout))
        shortcuts.addStretch()
        shortcuts.addWidget(button('刷新', self.refresh))
        layout.addLayout(shortcuts)
        parallel = QCheckBox('资源充足时允许独立浏览器与本地作业并行')
        parallel.setChecked(configured(manager.root))
        parallel.toggled.connect(lambda value:self.guard(lambda:self.set_parallel(parallel,value)))
        layout.addWidget(parallel)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        run_page, plan_page = QWidget(), QWidget()
        self.tabs.addTab(run_page, '运行记录')
        self.tabs.addTab(plan_page, '执行计划')
        run_layout, plan_layout = QVBoxLayout(run_page), QVBoxLayout(plan_page)
        self.runs = table(['原定时点', '业务日', '安排', '报表', '状态', '当前阶段 / 缺项'])
        self.plans = table(['安排', '报表', '重复方式', '下次执行', '状态'])
        run_layout.addWidget(self.runs, 1)
        plan_layout.addWidget(self.plans, 1)
        self.runs.itemSelectionChanged.connect(self.select)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(130)
        run_layout.addWidget(self.detail)
        actions = QHBoxLayout()
        for title, fn in (('查看缺项', self.show_missing), ('补日报资料', self.supply), ('暂停本次', self.pause),
                          ('继续原运行', self.resume), ('按新资料另跑', self.repeat), ('打开成品', self.open_file),
                          ('打开目录', self.open_folder)):
            actions.addWidget(button(title, lambda checked=False, call=fn:self.guard(call)))
        run_layout.addLayout(actions)
        paging = QHBoxLayout()
        paging.addWidget(button('上一页', lambda:self.turn(-50)))
        paging.addWidget(button('下一页', lambda:self.turn(50)))
        paging.addStretch()
        paging.addWidget(button('新建立即导出', lambda:manager.edit(manual=True)))
        run_layout.addLayout(paging)
        actions = QHBoxLayout()
        for title, fn in (('新建计划', manager.edit), ('编辑计划', lambda:manager.edit(existing=self.plan())),
                          ('停用未来计划', self.disable), ('立即运行一次', self.run_plan)):
            actions.addWidget(button(title, lambda checked=False, call=fn:self.guard(call)))
        plan_layout.addLayout(actions)
        self.note = label('应用保持运行时按北京时间触发。关闭工作台后可返回查看同一运行。', 'muted')
        layout.addWidget(self.note)

    def guard(self, fn):
        try:
            fn()
        except Exception as exc:
            self.note.setText(str(exc))

    def set_parallel(self, checkbox, value):
        from unattended_resources import configure
        if not self.manager.host.scheduled.owner or getattr(self.manager.host,'readonly',False):
            checkbox.blockSignals(True)
            checkbox.setChecked(configured(self.manager.root))
            checkbox.blockSignals(False)
            raise ValueError('请在持有定时执行权的窗口修改并行设置')
        configure(self.manager.root,value)
        self.note.setText('新作业将按机器余量领取资源。当前作业继续安全完成。' if value else
                          '新作业恢复串行。已领取的作业继续安全完成。')

    def refresh(self):
        self.run_rows = self.manager.repo.runs(offset=self.offset)
        self.plan_rows = self.manager.repo.specs()
        existing = self.manager.selected_run
        self.runs.blockSignals(True)
        fill(self.runs, [[r['scheduled_at'][:16].replace('T', ' '), ' 至 '.join(dict.fromkeys(r['dates'])),
                          r['plan']['name'], KINDS[r['plan']['type']], STATES[r['state']], r['message'].split('\n')[0]]
                         for r in self.run_rows])
        self.runs.blockSignals(False)
        for i, r in enumerate(self.run_rows):
            if r['id'] == existing:
                self.runs.selectRow(i)
                break
        plan_id = self.plan_rows[self.plans.currentRow()]['id'] if 0 <= self.plans.currentRow() < len(self.plan_rows) else None
        fill(self.plans, [[p['name'], KINDS[p['type']], p['time'] + (' 每周' if p['kind']=='weekly' else ' 单次'),
                           (p['next_at'] or '—')[:16].replace('T', ' '), '已启用' if p.get('enabled') else '已停用']
                          for p in self.plan_rows])
        for i, p in enumerate(self.plan_rows):
            if p['id'] == plan_id:
                self.plans.selectRow(i)
                break
        if self.runs.currentRow() >= 0:
            self.select()

    def plan(self):
        i = self.plans.currentRow()
        if i < 0 or i >= len(self.plan_rows):
            raise ValueError('请先选择一行执行计划')
        return self.plan_rows[i]

    def run(self):
        i = self.runs.currentRow()
        if i < 0 or i >= len(self.run_rows):
            raise ValueError('请先选择一行运行记录')
        item = self.run_rows[i]
        return self.manager.repo.inspect(item['id'])

    def select(self):
        if self.runs.currentRow() >= 0:
            self.guard(self.show_missing)

    def show_missing(self):
        run, artifact = self.run()
        self.manager.selected_run = run['id']
        parts = [run['message'], '最晚等到 ' + run['deadline'][:16].replace('T', ' '), '运行号 ' + run['id']]
        if artifact:
            parts.extend([artifact['path'], 'SHA-256 ' + artifact['sha256']])
        self.detail.setPlainText('\n'.join(parts))

    def supply(self):
        try:
            run, _ = self.run()
        except ValueError:
            run = None
        if run and run['plan']['type'] != 'business_daily':
            raise ValueError('该运行使用缺货采集快照或原仓单。请在原工作页补齐后继续。')
        day = run['dates'][1] if run else today()
        dialog = Materials(self, self.manager.root, day)
        dialog.exec()
        self.manager.poll()
        self.refresh()

    def pause(self):
        self.manager.pause(self.run()[0]['id'])
        self.refresh()

    def resume(self):
        self.manager.repo.request(self.run()[0]['id'])
        self.manager.poll()
        self.refresh()

    def repeat(self):
        run, _ = self.run()
        plan = {**run['plan'], 'date_rule': 'fixed', 'dates': run['dates']}
        self.manager.selected_run = self.manager.repo.manual(plan, uuid.uuid4().hex, repeated_from=run['id'])['id']
        self.manager.poll()
        self.refresh()

    def disable(self):
        old = self.plan()
        self.manager.repo.save_plan({**old, 'enabled': False}, old)
        self.refresh()

    def run_plan(self):
        old = self.plan()
        self.manager.edit(old['type'], old.get('scope'), old.get('dates'), existing=old, manual=True)

    def open_file(self):
        run, artifact = self.run()
        if not artifact or not artifact.get('published_at'):
            raise ValueError('成品回执尚未完成')
        from export_files import sha256
        path = Path(artifact['path'])
        if not path.exists() or sha256(path) != artifact['sha256']:
            raise ValueError('成品已移动或内容发生变化：' + str(path))
        _open(path)

    def open_folder(self):
        run, artifact = self.run()
        _open(Path(artifact['path']).parent if artifact else run['plan']['directory'])

    def turn(self, delta):
        self.offset = max(0, self.offset + delta)
        self.refresh()


class Materials(QDialog):
    def __init__(self, parent, root, day):
        super().__init__(parent)
        self.root, self.day = root, day
        self.setWindowTitle('经营日报资料 · ' + day)
        self.resize(950, 475)
        layout = QVBoxLayout(self)
        guidance = label('业务日 ' + day + '。自动读取已采用的门店规则和本业务日快照。补充的规则表可沿用到后续日期。')
        guidance.setWordWrap(True)
        layout.addWidget(guidance)
        form = QFormLayout()
        layout.addLayout(form)
        self.files, self.actions, self.reusable = {}, {}, set()
        for key, title in sources.SUPPLEMENTS.items():
            row = QHBoxLayout()
            name = self.files[key] = label('', 'muted')
            name.setWordWrap(True)
            row.addWidget(name, 1)
            action = self.actions[key] = button('选择当日原件', lambda checked=False, role=key:self.choose(role))
            row.addWidget(action)
            form.addRow(title, row)
        self.coverage = label('', 'muted')
        self.coverage.setWordWrap(True)
        layout.addWidget(self.coverage)
        layout.addStretch()
        actions = QHBoxLayout()
        actions.addWidget(button('登记历史资料清单', lambda:self.choose(None)))
        actions.addStretch()
        actions.addWidget(button('完成', self.accept, True))
        layout.addLayout(actions)
        self.refresh()

    def refresh(self):
        try:
            profile = sources.profile(self.root, self.day)
            refs = sources.supplement_refs(self.root, profile)
            rules = sources.store_rules(self.root, self.day)
            self.reusable = set(sources.RULE_FILES) if self.day == today() else set()
            if not rules:
                self.reusable.discard('stores')
            for key, label_widget in self.files.items():
                ref = refs.get(key)
                note = '需登记该日必卖清单·剔除大仓缺货率' if key == 'must_sell' else '待登记规则原件'
                if ref:
                    date_note = f' · {ref["effective_day"]} 起采用' if ref.get('effective_day') else ' · 当日原件'
                    note = f'{ref["name"]} · {ref["sha256"][:12]}' + date_note
                elif key == 'weeks' and (defaults := sources.template_weeks(self.day)):
                    note = f'{defaults["month"]} 目标已采用 · {defaults["source"]["name"]}\n可补已发布周实际'
                if key == 'stores' and (not ref or ref.get('effective_day')):
                    if rules:
                        if ref:
                            try:
                                sources.resolved_roster(self.root, ref, rules)
                            except (ValueError, OSError) as exc:
                                note += '\n' + str(exc)
                        note = (f'已引用门店 {len(rules["stores"])} 家、主从仓 {len(rules["routes"])} 家'
                                f' · {rules["version"][:12]}\n'
                                + (note if ref else '待补门店标签和运营负责人'))
                    else:
                        note = '未找到该日采用的门店范围和主从仓，需登记当日完整清单'
                label_widget.setText(note)
                self.actions[key].setText('采用标签与负责人' if key == 'stores' and key in self.reusable else
                                          '采用规则原件' if key in self.reusable else '选择当日原件')
            catalogs = sources.catalog_refs(self.root, self.day, profile)
            lines = []
            for warehouse, ref in catalogs.items():
                if not ref:
                    lines.append(warehouse + ' · 所选业务日无可采用的库存快照')
                    continue
                source = ref.get('file', ref)
                kind = '已登记' if 'file' in ref else '已核验的仓资料同步' if 'adopted' in ref else '缺货率已发布快照'
                proof = '' if source.get('account') else ' · 仓账号待核'
                lines.append(f'{warehouse} · {kind} · {source["observed"]} · '
                             f'{(source.get("sha256") or source["source_sha"])[:12]}{proof}')
            if profile['sales']:
                lines.append(f'已登记门店建议原件 {len(profile["sales"])} 份')
            else:
                stores = {s['code'] for s in rules['stores']} if rules and (not refs.get('stores') or refs['stores'].get('effective_day')) else None
                count = len(sources.native_stores(self.root, self.day, stores))
                lines.append(f'采用该日已发布门店建议 {count} 家' if count else '所选业务日暂无已发布门店建议')
            self.coverage.setText('\n'.join(lines))
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.coverage.setText('暂不能核对日报资料 · ' + str(exc))

    def choose(self, key):
        formats = '日报资料清单 (*.json)' if key is None else '表格 (*.xlsx);;周值 (*.json)' if key == 'weeks' else '表格 (*.xlsx)'
        selected, _ = QFileDialog.getOpenFileName(self, '登记该业务日资料', '', formats)
        if not selected:
            return
        try:
            if key is None:
                sources.import_profile(self.root, self.day, selected)
            else:
                sources.register_supplement(self.root, self.day, key, selected, as_rule=key in self.reusable)
            self.refresh()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '资料未登记', str(exc))
