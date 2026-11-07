"""Persistent store defaults without requiring a loaded daily calculation."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QLineEdit, QDialog, QFormLayout, QComboBox, QCheckBox, QMessageBox, QToolButton, QMenu)
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
        title=QHBoxLayout();title.addWidget(label('参数设置', 'title'));title.addStretch()
        self.reset_btn=button('全部初始化（开发测试）',lambda:__import__('application_reset_ui').request_reset(host))
        title.addWidget(self.reset_btn);v.addLayout(title)
        v.addWidget(label('先选门店，再明确应用到本次作业，或保存供以后使用。历史签发不会变化。', 'muted'))
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索门店名称或编码')
        self.search.textChanged.connect(self.filter)
        v.addWidget(self.search)
        from parameter_maintenance_ui import open_mapping
        from store_parameter_ui import import_parameters
        from import_ui import download
        row=QHBoxLayout()
        row.addWidget(button('批量设置开关 / 预算 / 供货仓',self.quick_settings,True))
        row.addWidget(button('应用参数方案',lambda:__import__('parameter_preset_ui').assign_defaults(self)))
        from admission_overview_ui import open_overview
        row.addWidget(button('二类医疗资质',lambda:open_overview(self,self.host,'qualification')))
        row.addWidget(button('门店拒收清单',lambda:open_overview(self,self.host,'refusals')))
        row.addStretch();v.addLayout(row)
        row=QHBoxLayout()
        row.addWidget(button('全局分类映射',lambda:open_mapping(self,host)))
        row.addWidget(button('参数预案',lambda:self.open_presets()))
        row.addWidget(button('补货预案',lambda:__import__('replenishment_plan_ui').open_library(host)))
        row.addWidget(button('导入完整门店参数',lambda:import_parameters(self,host,kind='classified_parameters')))
        row.addWidget(button('下载完整门店参数模板',lambda:download(self,'classified_parameters')))
        row.addWidget(button('刷新',self.refresh));row.addStretch();v.addLayout(row)
        row=QHBoxLayout()
        more=QToolButton();more.setText('通用参数 / 分组导入导出');more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup);menu=QMenu(more)
        for name,kind in [('分仓周期','warehouse_cycles'),('门店通用参数','common_parameters'),('旧版混合参数','store_parameters')]:
            menu.addAction('导入'+name,lambda checked=False,k=kind:import_parameters(self,host,kind=k))
            menu.addAction('下载'+name+'模板',lambda checked=False,k=kind:download(self,k))
        menu.addAction('导出勾选门店补货参数',self.export_parameters)
        menu.addAction('导出勾选门店通用参数',self.export_common)
        more.setMenu(menu);row.addWidget(more)
        other=QToolButton();other.setText('其他门店设置');other.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup);menu=QMenu(other)
        menu.addAction('设置补货策略',self.edit_switches);menu.addAction('设置主从仓与预算',self.edit_routes)
        menu.addAction('将单店默认另存参数预案',lambda:__import__('parameter_preset_ui').copy_store_default(self))
        for name,kind in [('补货策略','store_policy'),('供货关系','store_routes'),('门店预算','store_budget')]:
            menu.addAction('导入'+name,lambda checked=False,k=kind:import_parameters(self,host,kind=k));menu.addAction('下载'+name+'模板',lambda checked=False,k=kind:download(self,k))
        from admission_ui import open_editor,import_records
        menu.addAction('门店资质 / 拒收',lambda:open_editor(self,host,self.selected()[0] if len(self.selected())==1 else None))
        for name,kind in [('二类医疗资质','store_qualification'),('门店拒收清单','store_refusals')]:
            menu.addAction('导入'+name,lambda checked=False,k=kind:import_records(self,host,k));menu.addAction('下载'+name+'模板',lambda checked=False,k=kind:download(self,k))
        other.setMenu(menu);row.addWidget(other)
        history=QToolButton();history.setText('变更记录 / 恢复');history.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup);menu=QMenu(history)
        from settings_maintenance_ui import open_history,restore_presets
        menu.addAction('默认变更记录 / 撤销',lambda:(open_history(self,host),self.refresh()))
        menu.addAction('恢复停用方案',lambda:restore_presets(self,host));menu.addAction('旧完整参数方案库',host.open_preset_library)
        history.setMenu(menu);row.addWidget(history);row.addStretch()
        self.scope=QComboBox();self.scope.addItems(['已保存门店配置','本次作业门店']);self.scope.setToolTip('已保存配置与本次临时参数分开查看；删除只移除已保存配置。');row.addWidget(self.scope)
        row.addWidget(button('勾选当前筛选',lambda:self.check_visible(True)));row.addWidget(button('清空全部勾选',lambda:self.check_visible(False)));v.addLayout(row)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['选择', '门店', '默认主仓', '默认从仓', '参数依据 / 通用参数', '默认预算', '本次作业'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(lambda row,col:self.edit_one(row))
        for i, width in enumerate([48, 235, 100, 100, 250, 135, 170]):
            self.table.setColumnWidth(i, width)
        v.addWidget(self.table, 1)
        self.note = label('', 'muted')
        v.addWidget(self.note)
        delete_row=QHBoxLayout();delete_row.addStretch();delete_row.addWidget(button('删除所选门店配置',self.delete_defaults));v.addLayout(delete_row)
        row = QHBoxLayout()
        row.addWidget(button('查看保存的默认参数', self.view_defaults))
        row.addWidget(button('设置所选门店补货参数', self.edit_defaults))
        row.addWidget(button('设置所选门店通用参数', self.edit_common))
        row.addWidget(button('编辑本次作业参数', self.edit_current, True))
        v.addLayout(row)
        self.codes = []
        self.saved = {}
        self.scope.currentIndexChanged.connect(self.refresh)

    def open_presets(self):
        from parameter_preset_ui import PresetLibrary
        PresetLibrary(self.host).exec();self.refresh()

    def quick_settings(self):
        from parameter_quick_ui import open_quick
        open_quick(self,self.host,self.require());self.refresh()

    def delete_defaults(self):
        from parameter_preset_ui import delete_store_defaults
        delete_store_defaults(self)

    def selected(self):
        return [code for i, code in enumerate(self.codes) if self.table.item(i, 0).checkState() == Qt.CheckState.Checked]

    def refresh(self,*_):
        selected = set(self.selected())
        self.saved = self.host.work_repository.stores()
        self.codes = list(self.saved if self.scope.currentIndex()==0 else self.host.store_widgets)
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
            name = self.host.states.get(code, {}).get('name') or saved.get('name') or code
            values = [f'{name}（{code}）' if name != code else code, ui.get('primary') or '未指定',
                      ui.get('secondary') or '无', p.get('name', '未保存默认'), budget,
                      '已载入，可编辑本次参数' if code in self.host.store_widgets else '本次未载入']
            for j, value in enumerate(values, 1):
                self.table.setItem(i, j, QTableWidgetItem(str(value)))
            from restock_policy import summary
            from parameter_maintenance import common_is_default
            common='默认通用方案' if common_is_default(p.get('config') or schema.standard()) else '自定义通用参数'
            self.table.item(i,4).setText(str(values[3])+'\n'+common)
            if p.get('default_binding'):self.table.item(i,4).setText(('已保存参数（原有配置）' if p['id']=='standard' else p['name'])+' · 默认参数预案\n'+common)
            elif p:self.table.item(i,4).setText(str(values[3])+' · 门店自定义默认\n'+common)
            if not saved:self.table.item(i,4).setText('未保存配置 · 仅本次作业')
            elif not store_defaults.has_parameters(saved):self.table.item(i,4).setText('未保存参数')
        self.table.resizeRowsToContents()
        self.filter()
        self.note.setText('选择默认预案可长期跟随；直接修改默认参数会转为门店自定义。当前作业需明确应用，历史不变。' if self.codes else
                          '暂无已保存配置。可导入门店参数，或切换“本次作业门店”设置并保存。' if self.scope.currentIndex()==0 else '本次作业尚未载入门店。请先在补货出单中检查文件。')

    def import_parameters(self):
        from store_parameter_ui import import_parameters
        import_parameters(self, self.host)
    def edit_switches(self):
        from restock_policy_ui import open_policy
        open_policy(self,self.host,self.require(),scope='defaults');self.refresh()

    def export_parameters(self):
        from store_parameter_ui import export_parameters
        export_parameters(self, self.host, self.selected())

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
        if not store_defaults.has_parameters(self.saved.get(codes[0])):
            QMessageBox.information(self,'未保存门店参数','进入计算准备时会自动填写参数；可在那里查看本次实际生效值。');return
        p = self.preset(codes[0])
        title='保存的门店默认 · ' if codes[0] in self.saved else '尚未保存默认，仅显示当前参考 · '
        ParameterDialog(self.host, p['config'], p, scope=title + codes[0], readonly=True).exec()

    def edit_defaults(self):
        from parameter_maintenance_ui import edit_stores
        edit_stores(self,self.host,self.require());return
    def edit_current(self):
        codes = self.require()
        if not codes:
            return
        if self.host.readonly or self.host.worker or any(s not in self.host.store_widgets for s in codes):
            QMessageBox.information(self, '当前不能应用', '请先在补货出单中载入这些门店，并结束当前任务；历史作业只读。')
            return
        self.host.edit_parameters(codes)
        self.refresh()

    def edit_common(self):
        from parameter_maintenance_ui import edit_stores
        edit_stores(self,self.host,self.require(),'common')

    def edit_one(self,row):
        from parameter_maintenance_ui import edit_stores
        if 0<=row<len(self.codes):edit_stores(self,self.host,[self.codes[row]])

    def check_visible(self,checked):
        for i in range(self.table.rowCount()):
            if not checked or not self.table.isRowHidden(i):self.table.item(i,0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def export_common(self):
        from store_parameter_ui import export_parameters
        export_parameters(self,self.host,self.require(),kind='common_parameters')

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
                values[code] = store_defaults.record(p, p['config'], ui, old.get(code), include_budget=budget.isChecked(), saved=wr.now(),preserve_implicit=True)
                for k in ('primary', 'secondary', 'mode', 'amount'):
                    if before.get(k) != values[code]['ui'].get(k):
                        differences.append([code, {'primary':'主仓','secondary':'从仓','mode':'预算方式（0未指定/1不限/2限额）','amount':'预算金额'}[k], before.get(k), values[code]['ui'].get(k)])
            from interaction_ui import review_rows
            if not review_rows(self, '核对门店默认变化', ['门店', '项目', '原值', '新值'], differences,
                               '仅保存以后使用的默认；当前作业和历史签发不变。', '保存门店默认'):
                return
            self.host.work_repository.save_stores(values,expected={s:old.get(s) for s in codes})
            self.host.saved_store_defaults = {**old, **values}
            self.refresh()
            self.note.setText('门店默认已保存。当前作业未应用。')
        except Exception as exc:
            QMessageBox.warning(self, '未保存', str(exc))
