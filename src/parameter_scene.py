"""Persistent store defaults without requiring a loaded daily calculation."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QLineEdit, QDialog, QFormLayout, QComboBox, QCheckBox, QMessageBox)
from ui_controls import label, button
from parameter_ui import ParameterDialog
import parameter_schema as schema
import store_defaults
import work_repository as wr
from app_defaults import WAREHOUSES


class ParameterScene(QWidget):
    def __init__(self, host):
        super().__init__(host)
        self.host = host
        v = QVBoxLayout(self)
        v.addWidget(label('参数设置', 'title'))
        v.addWidget(label('先选门店，再明确应用到本次作业，或保存供以后使用。历史签发不会变化。', 'muted'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索门店名称或编码')
        self.search.textChanged.connect(self.filter)
        v.addWidget(self.search)
        row = QHBoxLayout()
        row.addWidget(button('管理参数方案', host.open_preset_library))
        row.addWidget(button('管理商品资料与清单', lambda: (host.show_section(1), host.navigate(0))))
        row.addWidget(button('刷新门店设置', self.refresh))
        row.addStretch()
        v.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['选择', '门店', '默认主仓', '默认从仓', '默认参数方案', '默认预算', '本次作业'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i, width in enumerate([48, 235, 100, 100, 250, 135, 170]):
            self.table.setColumnWidth(i, width)
        v.addWidget(self.table, 1)
        self.note = label('', 'muted')
        v.addWidget(self.note)
        row = QHBoxLayout()
        row.addWidget(button('查看保存的默认参数', self.view_defaults))
        row.addWidget(button('编辑并保存门店默认参数', self.edit_defaults))
        row.addWidget(button('保存门店默认主从仓与预算', self.edit_routes))
        row.addWidget(button('编辑本次作业参数', self.edit_current, True))
        v.addLayout(row)
        self.codes = []
        self.saved = {}

    def selected(self):
        return [code for i, code in enumerate(self.codes) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def refresh(self):
        selected = set(self.selected())
        self.saved = self.host.work_repository.stores()
        self.codes = list(dict.fromkeys([*self.saved, *self.host.store_widgets]))
        self.table.setRowCount(len(self.codes))
        for i, code in enumerate(self.codes):
            saved = self.saved.get(code, {})
            ui = saved.get('ui', {})
            p = saved.get('preset', {})
            item = QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if code in selected else Qt.CheckState.Unchecked)
            self.table.setItem(i, 0, item)
            mode = ui.get('mode', 0)
            budget = str(ui.get('amount', '')) if mode == 2 else '不限预算' if mode == 1 else '未指定'
            name = self.host.states.get(code, {}).get('name', code)
            values = [f'{name}（{code}）' if name != code else code, ui.get('primary') or '未指定',
                      ui.get('secondary') or '无', p.get('name', '未保存默认'), budget,
                      '已载入，可编辑本次参数' if code in self.host.store_widgets else '本次未载入']
            for j, value in enumerate(values, 1):
                self.table.setItem(i, j, QTableWidgetItem(str(value)))
        self.filter()
        self.note.setText('保存门店默认只供后续作业带入；当前作业需明确应用。' if self.codes else
                          '尚无门店记录。可先管理参数方案；导入补货文件识别门店后，即可保存其主从仓和完整参数。')

    def filter(self, *_):
        words = self.search.text().lower().split()
        for i in range(self.table.rowCount()):
            text = ' '.join(self.table.item(i, j).text() for j in range(1, 7)).lower()
            self.table.setRowHidden(i, any(word not in text for word in words))

    def require(self):
        codes = self.selected()
        if not codes:
            QMessageBox.information(self, '请选择门店', '请先勾选要查看或设置的门店。')
        return codes

    def preset(self, code):
        return copy.deepcopy(self.saved.get(code, {}).get('preset') or self.host.preset_for(code))

    def view_defaults(self):
        codes = self.require()
        if not codes:
            return
        p = self.preset(codes[0])
        ParameterDialog(self.host, p['config'], p, scope='保存的门店默认 · ' + codes[0], readonly=True).exec()

    def edit_defaults(self):
        codes = self.require()
        if not codes:
            return
        p = self.preset(codes[0])
        d = ParameterDialog(self.host, p['config'], p, scope=f'所选 {len(codes)} 店默认',
                            store_configs={s: self.preset(s)['config'] for s in codes}, intent='defaults')
        if d.exec() != QDialog.DialogCode.Accepted:
            return
        old = self.host.work_repository.stores()
        # Detect changes made while the editor was open instead of overwriting them.
        if any(old.get(s) != self.saved.get(s) for s in codes):
            QMessageBox.warning(self, '默认设置已变化', '请刷新后重新核对。')
            return
        values = {}
        for code in codes:
            ui = old.get(code, {}).get('ui') or (self.host.store_ui(code) if code in self.host.store_widgets else {})
            values[code] = store_defaults.record(d.preset, d.config, ui, old.get(code), saved=wr.now())
        self.host.work_repository.save_stores(values)
        self.host.saved_store_defaults = {**old, **values}
        self.refresh()
        self.note.setText(f'已保存 {len(codes)} 店默认参数。当前作业和历史签发未改变。')

    def edit_current(self):
        codes = self.require()
        if not codes:
            return
        if self.host.readonly or self.host.worker or any(s not in self.host.store_widgets for s in codes):
            QMessageBox.information(self, '当前不能应用', '请先在补货出单中载入这些门店，并结束当前任务；历史作业只读。')
            return
        self.host.edit_parameters(codes)
        self.refresh()

    def edit_routes(self):
        codes = self.require()
        if not codes:
            return
        d = QDialog(self)
        d.setWindowTitle('保存门店默认主从仓与预算')
        v = QVBoxLayout(d)
        v.addWidget(label('范围：' + '、'.join(codes) + '。仅修改勾选项；不应用到当前作业。'))
        form = QFormLayout()
        primary, secondary = QComboBox(), QComboBox()
        primary.addItem('未指定', None)
        secondary.addItem('无从仓', None)
        for wh in WAREHOUSES:
            primary.addItem(wh, wh)
            secondary.addItem(wh, wh)
        routes = QCheckBox('修改默认主从仓')
        form.addRow(routes)
        form.addRow('默认主仓', primary)
        form.addRow('默认从仓', secondary)
        budget = QCheckBox('修改默认预算')
        mode = QComboBox()
        mode.addItems(['未指定', '不限预算', '限定预算'])
        amount = QLineEdit()
        first = self.saved.get(codes[0], {}).get('ui', {})
        primary.setCurrentIndex(max(0, primary.findData(first.get('primary'))))
        secondary.setCurrentIndex(max(0, secondary.findData(first.get('secondary'))))
        mode.setCurrentIndex(first.get('mode', 0))
        amount.setText(str(first.get('amount', '')))
        form.addRow(budget)
        form.addRow('默认预算方式', mode)
        form.addRow('默认预算金额', amount)
        v.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(button('取消', d.reject))
        row.addWidget(button('核对并保存默认', d.accept, True))
        v.addLayout(row)
        if d.exec() != QDialog.DialogCode.Accepted or not (routes.isChecked() or budget.isChecked()):
            return
        try:
            old = self.host.work_repository.stores()
            if any(old.get(s) != self.saved.get(s) for s in codes):
                raise ValueError('门店默认已变化，请刷新后重新核对')
            if budget.isChecked() and mode.currentIndex() == 2:
                from decimal import Decimal
                number = Decimal(amount.text())
                if not number.is_finite() or number < 0:
                    raise ValueError('预算须为非负有限数字')
            values = {}
            differences = []
            for code in codes:
                before = old.get(code, {}).get('ui') or (self.host.store_ui(code) if code in self.host.store_widgets else {})
                ui = copy.deepcopy(before)
                if routes.isChecked():
                    ui.update(primary=primary.currentData(), secondary=secondary.currentData())
                if budget.isChecked():
                    ui.update(mode=mode.currentIndex(), amount=amount.text().strip())
                p = self.preset(code)
                values[code] = store_defaults.record(p, p['config'], ui, old.get(code), include_budget=budget.isChecked(), saved=wr.now())
                for k in ('primary', 'secondary', 'mode', 'amount'):
                    if before.get(k) != values[code]['ui'].get(k):
                        differences.append([code, {'primary':'主仓','secondary':'从仓','mode':'预算方式（0未指定/1不限/2限额）','amount':'预算金额'}[k], before.get(k), values[code]['ui'].get(k)])
            from interaction_ui import review_rows
            if not review_rows(self, '核对门店默认变化', ['门店', '项目', '原值', '新值'], differences,
                               '仅保存以后使用的默认；当前作业和历史签发不变。', '保存门店默认'):
                return
            self.host.work_repository.save_stores(values)
            self.host.saved_store_defaults = {**old, **values}
            self.refresh()
            self.note.setText('门店默认已保存。当前作业未应用。')
        except Exception as exc:
            QMessageBox.warning(self, '未保存', str(exc))
