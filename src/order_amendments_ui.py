"""Shared workspace for reviewing and confirming SKU corrections."""
from pathlib import Path

from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
                               QHeaderView, QInputDialog, QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

from business_ui import grid, run
from interaction_ui import review_rows
from lifecycle_io import TITLES
from ui_controls import button, label


class Editor(QDialog):
    def __init__(self, parent, host, head, stage, snapshot, prepare, save):
        super().__init__(parent)
        self.host, self.head, self.stage = host, head, stage
        self.snapshot, self.prepare, self.save = snapshot, prepare, save
        self.path = self.host.work_repository.path
        self.payment = stage == 'payment'
        self.evidence = None
        title = TITLES.get(stage, {'allocation': '库存分配', 'signed': '本地签发'}.get(stage, stage))
        self.setWindowTitle('手动更正 · '+title)
        self.resize(1040, 780)
        layout = QVBoxLayout(self)
        layout.addWidget(label(f"{head['name']} · {head['warehouse']} · {head['number']} · V{head['revision']:03d}", 'section'))
        layout.addWidget(label(f"{title} · 原版本 {snapshot['version']} · {snapshot['saved']}", 'muted'))
        search = QLineEdit(); search.setPlaceholderText('搜索原单商品编码或名称'); layout.addWidget(search)
        originals = [[r['sku'], r.get('name'), r.get('base_quantity'), r.get('amount')] for r in snapshot['rows']]
        self.originals, self.source = grid(self, ['商品编码', '商品名称', '原数量（库存单位）', '原金额（元）'], originals)
        search.textChanged.connect(self.source.filter)
        layout.addWidget(self.originals, 2)
        actions = QHBoxLayout()
        actions.addWidget(button('更正所选商品', lambda: self.from_original('设置')))
        actions.addWidget(button('删除所选商品', lambda: self.from_original('删除')))
        actions.addWidget(button('添加商品', lambda: self.set_row(['设置', '', '', ''] + ([''] if self.payment else []))))
        actions.addWidget(button('粘贴表格', self.paste))
        actions.addWidget(button('移除所选更正', self.remove_edit))
        layout.addLayout(actions)
        self.headers = ['操作', '商品编码', '数量（库存单位）', '商品名称'] + (['实际金额（元）'] if self.payment else [])
        self.table = QTableWidget(0, len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 2)
        self.originals.doubleClicked.connect(lambda _: self.from_original('设置'))
        layout.addWidget(label('粘贴列顺序为商品编码、数量、商品名称'+('、实际金额' if self.payment else '')+'。操作可填「设置」或「删除」。', 'muted'))
        form = QFormLayout()
        self.operator = QLineEdit(); self.operator.setMaxLength(80); form.addRow('经办人', self.operator)
        self.reason = QLineEdit(); self.reason.setMaxLength(500); form.addRow('更正原因', self.reason)
        if stage not in ('allocation','signed'):
            self.file = button('选择凭据（选填）', self.choose_file)
            form.addRow('凭据', self.file)
        layout.addLayout(form)
        actions = QHBoxLayout(); actions.addWidget(button('取消', self.reject))
        self.review = button('核对差异并保存', self.confirm_changes, True)
        actions.addWidget(self.review); layout.addLayout(actions)

    def cells(self, index):
        return [self.table.item(index, c).text().strip() if self.table.item(index, c) else ''
                for c in range(self.table.columnCount())]

    def set_row(self, values, *, index=None):
        if index is None:
            index=next((i for i in range(self.table.rowCount()) if values[1] and self.cells(i)[1] == values[1]),
                       self.table.rowCount())
        if index == self.table.rowCount(): self.table.insertRow(index)
        for col, value in enumerate(values):
            self.table.setItem(index, col, QTableWidgetItem('' if value is None else str(value)))
        self.table.setCurrentCell(index, 2 if values[1] else 1)

    def from_original(self, action):
        index = self.originals.currentIndex()
        if not index.isValid(): return
        sku, name, quantity, amount = self.source.rows[self.source.indices[index.row()]]
        self.set_row([action, sku, quantity, name] + ([amount] if self.payment else []))

    def paste(self):
        text = QApplication.clipboard().text()
        rows = [line.split('\t') for line in text.splitlines() if line.strip()]
        if rows and rows[0][0].strip() == '商品编码': rows.pop(0)
        if any(len(row) < 2 or len(row) >= len(self.headers) for row in rows):
            QMessageBox.warning(self, '粘贴未完成', '每行需包含商品编码、数量和可选字段，请从 Excel 按列复制。')
            return
        codes=[row[0].strip() for row in rows]
        if not all(codes) or len(set(codes)) != len(codes):
            QMessageBox.warning(self,'粘贴未完成','粘贴范围内的商品编码须填写且唯一。')
            return
        existing = {self.cells(i)[1]: i for i in range(self.table.rowCount())}
        self.table.setUpdatesEnabled(False)
        try:
            for row, sku in zip(rows, codes):
                self.set_row(['设置', *[cell.strip() for cell in row], *([''] * (len(self.headers)-1-len(row)))],
                             index=existing.get(sku,self.table.rowCount()))
        finally:
            self.table.setUpdatesEnabled(True)

    def remove_edit(self):
        if (index := self.table.currentRow()) >= 0: self.table.removeRow(index)

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择更正凭据')
        if path:
            self.evidence = path
            self.file.setText(Path(path).name)

    def confirm_changes(self):
        from lifecycle_ui import warn
        try:
            changes=[]
            for index in range(self.table.rowCount()):
                action, sku, quantity, name, *amount = self.cells(index)
                if not sku and not quantity and not name and not any(amount): continue
                if action not in ('设置', '删除'):
                    raise ValueError(f'第 {index+1} 行操作请填写「设置」或「删除」')
                changes.append({'action': 'set' if action=='设置' else 'delete', 'sku': sku,
                                'quantity': quantity, 'name': name, 'amount': amount[0] if amount else None})
            plan=run(self,self.prepare,(self.path,self.head,self.stage,self.snapshot['token'],changes,
                                        self.operator.text().strip(),self.reason.text().strip(),self.evidence),'核对更正依据')
            rows=[]
            for change in plan['changes']:
                before, after = change['before'], change['after']
                def display(row, key):
                    return '—' if row is None else '空白' if row.get(key) is None else row[key]
                rows.append([change['sku'], '新增' if before is None else '删除' if after is None else '更正',
                             display(before,'base_quantity'), display(after,'base_quantity'),
                             display(before,'amount'), display(after,'amount')])
            headers=['商品编码','更正操作','更正前数量','更正后数量','更正前金额','更正后金额']
            note=self.head['number']+' · '+self.head['warehouse']+f" · 共 {len(rows)} 项更正。已核对数量采用库存单位。"
            if plan.get('note'):note+='\n'+plan['note']
            if not review_rows(self,'核对手动更正',headers,rows,note,'确认保存更正'): return
            self.saved_receipt=self.save(self,self.host,plan['write'])
            self.accept()
        except Exception as exc:
            warn(self,exc)


def open_editor(parent, host, head, stage):
    from lifecycle_ui import commit, warn
    if stage == 'allocation':
        import allocation_amendments as service
        def commit(parent, host, plan):
            return run(parent,service.commit,(host.work_repository.path,plan),'保存本仓分配更正')
    elif stage == 'signed':
        import signing_amendments as service
        def commit(parent, host, plan):
            return run(parent,service.commit,(host.work_repository.path,plan),'保存本仓签发更正')
    else:
        import order_amendments as service
    try:
        snapshot = run(parent, service.read, (host.work_repository.path, head, stage), '读取原单及版本')
        dialog = Editor(parent,host,head,stage,snapshot,service.prepare,commit)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.saved_receipt
    except Exception as exc:
        warn(parent,exc)


def choose_stage(parent, host, head, view):
    titles={title: stage for stage,title in TITLES.items() if view['documents'].get(stage)
            and not (head.get('kind')=='material' and stage in ('return','operations'))}
    if head.get('kind')!='material':titles={'本地签发':'signed',**titles}
    titles['库存分配']='allocation'
    title, ok = QInputDialog.getItem(parent,'手动更正','选择本仓单的更正环节',list(titles),0,False)
    if ok:
        return open_editor(parent,host,head,titles[title])
