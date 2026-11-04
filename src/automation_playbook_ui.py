"""Modeless recipe picker: preview/copy only, no background business actions."""
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QHBoxLayout, QListWidget,
    QComboBox, QLineEdit, QDoubleSpinBox, QFormLayout, QPlainTextEdit, QCheckBox, QWidget)
from ui_controls import label, button
from app_defaults import WAREHOUSES
from automation_playbooks import PLAYBOOKS, STORE_CASES, TASK_CASES, render, store_codes


class PlaybookDialog(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        self.setWindowTitle('AI 常用业务 · 选场景，复制到 Codex / WorkBuddy')
        self.setObjectName('aiPlaybooks')
        self.setModal(False)
        self.resize(1040, 720)
        v = QVBoxLayout(self)
        v.addWidget(label('选一个目标 → 填清操作对象 → 复制给 AI。这里不会启动计算或发送数据。'))
        self.connection = label('', 'muted'); v.addWidget(self.connection)
        body = QHBoxLayout(); v.addLayout(body, 1)
        self.cases = QListWidget(); self.cases.setMaximumWidth(260)
        for _, title, _, _ in PLAYBOOKS: self.cases.addItem(title)
        body.addWidget(self.cases)
        right = QVBoxLayout(); body.addLayout(right, 1)
        self.description = label(''); right.addWidget(self.description)
        self.form = QFormLayout(); right.addLayout(self.form)
        self.fields = {}
        self.scope = QComboBox(); self.scope.addItems(['使用当前勾选门店', '手动填写门店编码'])
        self.codes = QLineEdit(); self.codes.setPlaceholderText('例如：编码A,编码B；按填写顺序处理')
        self.add_field('scope', '门店范围', self.scope)
        self.add_field('codes', '门店编码', self.codes)
        self.task = QComboBox(); self.add_field('task', 'AI 任务', self.task)
        self.order = QLineEdit(); self.order.setPlaceholderText('粘贴完整补货单号，包含分仓后缀')
        self.add_field('order', '补货单号', self.order)
        self.sku = QLineEdit(); self.sku.setPlaceholderText('准确商品编码，保留前导零')
        self.add_field('sku', '商品编码', self.sku)
        self.warehouse = QComboBox(); self.warehouse.addItem('请选择所属仓', '')
        for warehouse in WAREHOUSES: self.warehouse.addItem(warehouse, warehouse)
        self.add_field('warehouse', '所属仓', self.warehouse)
        self.days_a = QDoubleSpinBox(); self.days_b = QDoubleSpinBox()
        for field, value in [(self.days_a, 5), (self.days_b, 7)]:
            field.setRange(.1, 365); field.setDecimals(1); field.setValue(value); field.setSuffix(' 天')
        pair = QWidget(); pair_layout = QHBoxLayout(pair); pair_layout.setContentsMargins(0, 0, 0, 0)
        pair_layout.addWidget(self.days_a); pair_layout.addWidget(label('对比')); pair_layout.addWidget(self.days_b)
        self.add_field('days', '备货天数', pair)
        self.reviewed = QCheckBox('我已核对所选任务结果，允许采用到本次作业；仍不保存或签发')
        right.addWidget(self.reviewed)
        self.hint = label('', 'muted'); right.addWidget(self.hint)
        self.preview = QPlainTextEdit(); self.preview.setReadOnly(True)
        self.preview.setPlaceholderText('补齐操作对象后生成可复制的完整提示词。')
        right.addWidget(self.preview, 1)
        self.show_full = QCheckBox('查看发给 AI 的完整指令（含操作标识，无需手填）')
        right.addWidget(self.show_full); self.show_full.toggled.connect(self.update_prompt)
        actions = QHBoxLayout(); v.addLayout(actions)
        actions.addWidget(button('↻ 刷新当前门店与任务', self.reload))
        self.copy_button = button('复制提示词', self.copy_prompt)
        self.copy_return = button('复制并返回工作台', lambda: self.copy_prompt(True), True)
        actions.addWidget(self.copy_button); actions.addWidget(self.copy_return)
        actions.addWidget(button('× 关闭', self.close))
        self.cases.currentRowChanged.connect(self.scene_changed)
        self.scope.currentIndexChanged.connect(self.update_prompt)
        self.codes.textChanged.connect(self.update_prompt); self.task.currentIndexChanged.connect(self.task_changed)
        for field in (self.order, self.sku): field.textChanged.connect(self.update_prompt)
        self.warehouse.currentIndexChanged.connect(self.update_prompt)
        self.days_a.valueChanged.connect(self.update_prompt); self.days_b.valueChanged.connect(self.update_prompt)
        self.reviewed.toggled.connect(self.update_prompt)
        self.context = {}; self.bound_token = ''
        self.cases.setCurrentRow(0); self.reload()

    def add_field(self, key, title, widget):
        caption = label(title); self.form.addRow(caption, widget); self.fields[key] = (caption, widget)

    def scene_changed(self, *_):
        self.reviewed.setChecked(False)
        self.update_prompt()

    def task_changed(self, *_):
        self.reviewed.setChecked(False)
        self.update_prompt()

    def reload(self):
        self.context = self.host.automation.workbench_context({})
        self.bound_token = self.context['context_token']
        previous = self.task.currentData()
        self.task.blockSignals(True); self.task.clear(); self.task.addItem('请选择具体 AI 任务', '')
        g = self.host.automation
        if g.journal:
            ids = list(g.calculations)
            if g.active and g.active not in ids: ids.append(g.active)
            for tid in reversed(ids):
                from automation_ui import STATES
                record = g.journal.get(tid)
                if not isinstance(g.calculations.get(tid), dict) and tid != g.active: continue
                names = '、'.join(s.get('name', s['code']) for s in record.get('stores', []))
                self.task.addItem(record['created'][:19].replace('T', ' ') + ' · ' + names + ' · ' + STATES.get(record['state'], record['state']) + ' · ' + tid[:8], tid)
        idx = self.task.findData(previous or g.active or '')
        self.task.setCurrentIndex(max(0, idx)); self.task.blockSignals(False)
        self.reviewed.setChecked(False)
        self.update_prompt()

    def values(self, case):
        result = dict(task_id=self.task.currentData(), sku=self.sku.text(), warehouse=self.warehouse.currentData(),
            order_id=self.order.text(), days_a=self.days_a.value(), days_b=self.days_b.value(), reviewed=self.reviewed.isChecked())
        if case in STORE_CASES:
            if self.scope.currentIndex() == 0 and self.host.selected_codes() != self.context.get('selected_stores', []):
                raise ValueError('勾选门店已变化，请刷新后重新复制。')
            raw = ','.join(self.context.get('selected_stores', [])) if self.scope.currentIndex() == 0 else self.codes.text()
            result['stores'] = store_codes(raw, self.host.store_widgets)
        if case in TASK_CASES and result['task_id']:
            g = self.host.automation; tid = result['task_id']; record = g.journal.get(tid)
            if case in {'review', 'explain', 'adopt', 'retry'}:
                r = g.require_result(tid)
                if case == 'adopt':
                    if r['prepared']['mode'] != 'trial' or record['state'] != 'completed':
                        raise ValueError('只能采用已完成的独立试算；请先查看结果。')
                    g.check_context(r['prepared']['context_token'])
                if case == 'retry':
                    g.check_context(r['prepared']['context_token'])
                    if not any(s['state'] in ('计算失败', '已停止') for s in r['states'].values()):
                        raise ValueError('这个任务没有失败或已停止的门店，无需重试。')
                if case == 'explain' and result.get('stores') and result['stores'][0] not in r['codes']:
                    raise ValueError('所选门店不在这个任务中。')
            if case == 'stop' and g.active != tid: raise ValueError('这个任务已结束，不需要停止。')
        if case == 'compare' and result.get('stores'):
            ui = self.host.current_parameter_record(result['stores'][0])['ui']
            if result['warehouse'] not in (ui.get('primary'), ui.get('secondary')):
                raise ValueError('请选择这家门店实际供货的仓库。')
        return result

    def update_prompt(self, *_):
        index = self.cases.currentRow()
        if index < 0 or not getattr(self, 'context', None): return
        case, title, effect, detail = PLAYBOOKS[index]
        self.description.setText(title + ' · ' + effect + '\n' + detail)
        shown = set()
        if case in STORE_CASES: shown.add('scope')
        if case in STORE_CASES and self.scope.currentIndex() == 1: shown.add('codes')
        if case in TASK_CASES: shown.add('task')
        if case in {'compare', 'explain', 'history'}: shown.add('warehouse')
        if case in {'explain', 'history'}: shown.add('sku')
        if case == 'history': shown.add('order')
        if case == 'compare': shown.add('days')
        for key, widgets in self.fields.items():
            for widget in widgets: widget.setVisible(key in shown)
        self.reviewed.setVisible(case == 'adopt')
        codes = self.context.get('selected_stores', [])
        self.connection.setText(('本机连接已启用' if self.host.automation.enabled else '尚未启用：请先到「AI 协助」启用本机连接') +
            ' · 业务日期 ' + self.context['business_date'] + ' · 当前勾选 ' + str(len(codes)) + ' 家')
        try:
            if not self.host.automation.enabled: raise ValueError('可先浏览场景；启用本机连接并刷新后即可复制。')
            self.host.automation.check_context(self.bound_token)
            values = self.values(case)
            self.full_prompt = render(case, self.context, values)
            overview = ['请帮我' + title + '。', '', '本次影响：' + effect, '业务日期：' + self.context['business_date']]
            if case in STORE_CASES:
                overview.append('门店顺序：' + ' → '.join(self.host.states[s]['name'] + '（' + s + '）' for s in values['stores']))
            if case in TASK_CASES: overview.append('指定任务：' + self.task.currentText())
            if case in {'compare', 'explain', 'history'}: overview.append('所属仓：' + values['warehouse'])
            if case == 'compare': overview.append(f"备货天数：{values['days_a']:g} 天 对比 {values['days_b']:g} 天；其他设置保持原值。")
            if case in {'explain', 'history'}: overview.append('商品编码：' + values['sku'])
            if case == 'history': overview.append('补货单号：' + values['order_id'])
            overview.extend(['', detail, '', '如果资料缺失、操作对象有歧义或工作台已变化，请说明并停下。',
                             '不保存草稿、不签发、不修改永久默认、不采购、不删除。', '',
                             '复制时会自动附带准确的作业标识、工具步骤和核对要求，无需手填。'])
            self.preview.setPlainText(self.full_prompt if self.show_full.isChecked() else '\n'.join(overview))
            self.hint.setText('复制后粘贴到 Codex / WorkBuddy 发送。工作台变化后须刷新并重新复制。')
            self.copy_button.setEnabled(True); self.copy_return.setEnabled(True)
            return True
        except ValueError as exc:
            self.preview.clear(); self.hint.setText(str(exc))
            self.copy_button.setEnabled(False); self.copy_return.setEnabled(False)
            return False

    def copy_prompt(self, close_after=False):
        # Revalidate at click time: a modeless window may outlive a manual change.
        if not self.update_prompt(): return
        QApplication.clipboard().setText(self.full_prompt)
        self.hint.setText('已复制。到 Codex / WorkBuddy 粘贴发送；这里尚未执行任何业务动作。')
        if close_after: self.close()


def show_playbooks(host):
    dialog = getattr(host, '_automation_playbooks', None)
    if dialog is None:
        dialog = PlaybookDialog(host); host._automation_playbooks = dialog
    else: dialog.reload()
    dialog.show(); dialog.raise_(); dialog.activateWindow()
    return dialog
