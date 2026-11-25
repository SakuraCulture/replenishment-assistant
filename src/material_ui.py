"""Material directory, store requests, and stock allocation review."""
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLineEdit, QMessageBox, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

import material_orders as orders
import material_requests as requests
import warehouse_catalog
import work_repository as wr
from app_defaults import WAREHOUSES
from business_ui import grid, run
from interaction_ui import confirm, review_rows
from ui_controls import button, label


def request_edit(path, store, wh, rows, original, *, progress, cancel):
    from order_lifecycle import begin

    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        begin(db,cancel)
        return requests.save(db,store,wh,rows,request_id=original.get('id'),expected=original.get('version'))


def preview_task(path, selections, previous, *, progress, cancel):
    return orders.preview(path,selections,previous=previous,cancel=cancel)


def import_task(path, source, *, progress, cancel):
    return requests.import_scanned(path,source,cancel)


def scan_task(path, *, progress, cancel):
    return requests.scan(path,cancel)


def confirm_task(path, plan, *, progress, cancel):
    progress('保存本批范围和仓资料版本')
    batch = orders.register(path,plan,accept_issues=True,cancel=cancel)
    progress('核对仓库存并保存物料仓单')
    return orders.commit(path,batch,cancel)


def value(table, row, col):
    item = table.item(row,col)
    return item.text() if item else ''


class RequestEditor(QDialog):
    def __init__(self, parent, original=None, preset_sku='', preset_warehouse=None):
        super().__init__(parent)
        self.path, self.original = parent.path, original or {}
        self.setWindowTitle('更正原物料申请' if original else '登记门店物料需求')
        self.resize(820,490)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.store = QComboBox()
        with closing(wr.read_connection(self.path)) as db:
            for code, raw in db.execute('SELECT code,data FROM stores ORDER BY code'):
                name = wr.loads(raw).get('name') or code
                self.store.addItem(f'{name}（{code}）',code)
        self.warehouse = QComboBox()
        self.warehouse.addItems(WAREHOUSES)
        if preset_warehouse:
            self.warehouse.setCurrentText(preset_warehouse)
        form.addRow('门店', self.store)
        form.addRow('供货仓',self.warehouse)
        if original:
            form.addRow('申请修订',label(f"{original['id']} · V{original['version']:03d}"))
            self.store.setCurrentIndex(self.store.findData(original['store']))
            self.warehouse.setCurrentText(original['warehouse'])
            self.store.setEnabled(False)
            self.warehouse.setEnabled(False)
        layout.addLayout(form)
        self.lines = QTableWidget(0,3)
        self.lines.setHorizontalHeaderLabels(('商品编码','申请数量（基础单位）','备注'))
        self.lines.setColumnWidth(0,255)
        self.lines.setColumnWidth(1,180)
        self.lines.horizontalHeader().setStretchLastSection(True)
        for row in self.original.get('lines', (dict(sku=preset_sku,quantity='',note=''),)):
            self.add_row(row['sku'],row['quantity'],row['note'])
        layout.addWidget(self.lines,1)
        actions = QHBoxLayout()
        actions.addWidget(button('新增一行',lambda: self.add_row()))
        actions.addWidget(button('移除空行或选中行',self.remove_row))
        actions.addStretch()
        actions.addWidget(button('保存申请',self.save,True))
        actions.addWidget(button('关闭',self.reject))
        layout.addLayout(actions)

    def add_row(self, sku='', qty='', note=''):
        index = self.lines.rowCount()
        self.lines.insertRow(index)
        for col, text in enumerate((sku,qty,note)):
            self.lines.setItem(index,col,QTableWidgetItem(str(text)))

    def remove_row(self):
        row = self.lines.currentRow()
        if row >= 0:
            self.lines.removeRow(row)

    def save(self):
        rows = [dict(sku=value(self.lines,i,0),quantity=value(self.lines,i,1),note=value(self.lines,i,2))
                for i in range(self.lines.rowCount()) if any(value(self.lines,i,c) for c in range(3))]
        try:
            run(self,request_edit,(self.path,self.store.currentData(),self.warehouse.currentText(),rows,self.original),'保存门店物料申请')
        except Exception as exc:
            QMessageBox.warning(self,'物料申请未保存',str(exc))
            return
        self.accept()


class MaterialDesk(QDialog):
    def __init__(self, host):
        super().__init__(host)
        self.path = host.work_repository.path
        self.setWindowTitle('物料单')
        self.resize(1340,770)
        self.open_tracking = False
        self.request_rows = []
        self.store_order = []
        self.last_result = None
        box = QVBoxLayout(self)
        self.tabs = QTabWidget()
        box.addWidget(self.tabs,1)
        self.catalog_tab()
        self.demand_tab()
        self.read_catalog()
        self.read_demands()

    def catalog_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.wh = QComboBox()
        self.wh.addItems(WAREHOUSES)
        self.wh.currentIndexChanged.connect(self.read_catalog)
        self.term = QLineEdit()
        self.term.setPlaceholderText('商品编码或名称')
        self.term.textChanged.connect(self.filter_catalog)
        bar.addWidget(label('供货仓'))
        bar.addWidget(self.wh)
        bar.addWidget(self.term,1)
        bar.addWidget(button('读取当前仓资料',self.read_catalog))
        bar.addWidget(button('为选中商品登记需求',self.from_catalog))
        layout.addLayout(bar)
        self.catalog_note = label('','muted')
        layout.addWidget(self.catalog_note)
        self.catalog_table, self.catalog_model = grid(page,('商品编码','商品名','产品分类','状态','库存单位','仓可用数'))
        layout.addWidget(self.catalog_table,1)
        self.tabs.addTab(page,'物料目录')

    def read_catalog(self):
        self.catalog = None
        self.catalog_units = {}
        try:
            catalogs, _ = warehouse_catalog.Repository(Path(self.path).parent).snapshots()
            self.catalog = catalogs.get(self.wh.currentText())
            if self.catalog:
                wanted = {sku for sku,item in self.catalog['items'].items() if not requests.eligibility(item)}
                if wanted:
                    with closing(wr.read_connection(self.path)) as db:
                        self.catalog_units = orders._units(db,self.catalog,wanted,None)
                self.catalog_note.setText(f"{self.wh.currentText()} · 版本 {self.catalog['revision']} · 采用 {self.catalog.get('imported_at','未留存')}")
            else:
                self.catalog_note.setText('本仓尚未采用商品资料')
        except Exception as exc:
            self.catalog_note.setText('仓资料未打开 '+str(exc))
        self.filter_catalog()

    def filter_catalog(self):
        term = self.term.text().strip().casefold()
        self.catalog_rows = []
        for sku,item in (self.catalog or {}).get('items',{}).items():
            name = str(item.get('商品名称') or '')
            if requests.eligibility(item) or term not in (sku+' '+name).casefold():
                continue
            try:
                stock = orders.allocation.text(orders.allocation.number(item.get('可用数'),'仓可用数'))
            except ValueError:
                stock = '待核'
            self.catalog_rows.append((sku,name,item['分类'],item['商品状态'],self.catalog_units.get(sku,'库存单位'),stock))
        self.catalog_model.replace(self.catalog_rows)

    def from_catalog(self):
        selected = self.catalog_table.selectionModel().selectedRows()
        sku = self.catalog_model.data(selected[0]) if selected else ''
        self.edit_request(preset_sku=sku,preset_warehouse=self.wh.currentText())

    def demand_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        actions.addWidget(button('登记门店需求',self.edit_request))
        actions.addWidget(button('导入需求表',self.import_file))
        actions.addWidget(button('下载需求模板',self.template))
        actions.addWidget(button('更正所选申请',self.revise))
        actions.addWidget(button('导出所选申请',lambda:self.template(self.selected_request())))
        actions.addStretch()
        layout.addLayout(actions)
        self.lines = QTableWidget(0,10)
        self.lines.setHorizontalHeaderLabels(('选择','门店','供货仓','商品编码','申请量','已分配','未分配','核对量','申请修订','备注'))
        self.lines.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.lines.setColumnWidth(0,48)
        self.lines.setColumnWidth(3,195)
        self.lines.setColumnWidth(8,220)
        layout.addWidget(self.lines,1)
        self.note = label('','muted')
        layout.addWidget(self.note)
        actions = QHBoxLayout()
        actions.addWidget(button('勾选有剩余需求',self.select_remaining))
        actions.addWidget(button('所选门店提前',lambda:self.move_store(-1)))
        actions.addWidget(button('所选门店后移',lambda:self.move_store(1)))
        self.preview_button = button('预览物料分配',self.allocate,True)
        actions.addWidget(self.preview_button)
        actions.addWidget(button('继续原分配',self.resume))
        actions.addWidget(button('结束未分配批次',self.end_pending))
        actions.addStretch()
        actions.addWidget(button('查看物料订单',self.tracking))
        actions.addWidget(button('关闭',self.reject))
        layout.addLayout(actions)
        self.tabs.addTab(page,'门店需求与分配')

    def selected_request(self):
        i = self.lines.currentRow()
        return self.request_rows[i][0] if i>=0 else None

    def edit_request(self, *, preset_sku='', preset_warehouse=None, original=None):
        editor = RequestEditor(self,original,preset_sku,preset_warehouse)
        if editor.exec()==QDialog.DialogCode.Accepted:
            self.read_demands()
            self.tabs.setCurrentIndex(1)
        editor.deleteLater()

    def revise(self):
        request = self.selected_request()
        if request:
            self.edit_request(original=request)
        else:
            self.note.setText('先选中要更正的申请行')

    def template(self, request=None):
        target, _ = QFileDialog.getSaveFileName(self,'保存物料需求表','物料需求.xlsx','Excel (*.xlsx)')
        if target:
            try:
                requests.write_template(target,request)
                self.note.setText('物料需求表已保存 '+target)
            except Exception as exc:
                QMessageBox.warning(self,'物料需求表未保存',str(exc))

    def import_file(self):
        file, _ = QFileDialog.getOpenFileName(self,'导入物料需求表','','Excel (*.xlsx)')
        if not file:
            return
        try:
            source = run(self,scan_task,(file,),'识别物料需求原件')
            if source['issues']:
                raise ValueError('\n'.join(source['issues']))
            rows = [(group['store'],group['warehouse'],r['sku'],r['quantity'],r['row'],r['note'])
                    for group in source['groups'] for r in group['lines']]
            if not review_rows(self,'核对物料需求表',('门店','仓','商品编码','申请量','原行','备注'),rows,
                               f"{source['source']['name']} · {len(rows)} 行",'确认保存申请'):
                return
            saved = run(self,import_task,(self.path,source),'保存物料需求原件和申请')
            self.read_demands()
            self.note.setText(f'已保存或识别原申请 {len(saved)} 笔')
        except Exception as exc:
            QMessageBox.warning(self,'物料需求导入未完成',str(exc))

    def read_demands(self, edits=None):
        data = orders.available_requests(self.path)
        found = list(dict.fromkeys(request['store'] for request in data))
        self.store_order = [s for s in self.store_order if s in found]+[s for s in found if s not in self.store_order]
        self.request_rows = [(r,line) for store in self.store_order for r in data if r['store']==store for line in r['lines']]
        self.lines.setRowCount(len(self.request_rows))
        for i, (request,line) in enumerate(self.request_rows):
            checked, amount = (edits or {}).get((request['id'],line['sku']),(False,line['remaining']))
            pick = QTableWidgetItem()
            pick.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable|Qt.ItemFlag.ItemIsSelectable)
            pick.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.lines.setItem(i,0,pick)
            display = (request['store'],request['warehouse'],line['sku'],line['quantity'],line['claimed'],line['remaining'],amount,
                       request['id'][:12]+f" · V{request['version']:03d}",line['note'])
            for col, text in enumerate(display,1):
                item = QTableWidgetItem(text)
                if col!=7:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.lines.setItem(i,col,item)
        self.note.setText(f'共 {len(data)} 笔申请 · {len(self.request_rows)} 行 · 门店顺序 '+ ' → '.join(self.store_order))

    def select_remaining(self):
        for i, (_,line) in enumerate(self.request_rows):
            if orders.allocation.number(line['remaining'],'未分配'):
                self.lines.item(i,0).setCheckState(Qt.CheckState.Checked)

    def move_store(self, direction):
        request = self.selected_request()
        if not request:
            return
        i = self.store_order.index(request['store'])
        target = i+direction
        if 0 <= target < len(self.store_order):
            edits = {(r['id'],line['sku']):(self.lines.item(row,0).checkState()==Qt.CheckState.Checked,value(self.lines,row,7))
                     for row, (r,line) in enumerate(self.request_rows)}
            self.store_order.insert(target,self.store_order.pop(i))
            self.read_demands(edits)
            self.lines.selectRow(next(j for j,(r,_) in enumerate(self.request_rows) if r['store']==request['store']))

    def allocate(self):
        selected = [dict(request=r['id'],version=r['version'],sku=line['sku'],quantity=value(self.lines,i,7))
                    for i,(r,line) in enumerate(self.request_rows) if self.lines.item(i,0).checkState()==Qt.CheckState.Checked]
        self.review(selected)

    def review(self, selections, previous=None):
        try:
            plan = run(self,preview_task,(self.path,selections,previous),'预览物料分配')
            values = plan['payload']['rows']
            keys = ('store','warehouse','sku','name','unit','requested','demand','stock','before','quantity','after','missing','step','reason')
            shown = [[r[k] if r[k] is not None else '待核' for k in keys] for r in values]
            count = sum(r['issue'] for r in values)
            versions = ' · '.join(wh+' '+b['catalog']['revision'] for wh,b in plan['payload']['stocks'].items())
            note = f"批次 {plan['payload']['id'][:12]} · {versions} · 待核 {count} 行"
            if not review_rows(self,'核对物料分配',('门店','仓','商品编码','商品名','单位','原申请','核对量','快照可用','分配前','实分','分配后','缺口','出库箱规','原因'),
                               shown,note,'确认分配可安排行' if count else '确认分配'):
                return
            self.last_result = run(self,confirm_task,(self.path,plan),'确认物料分配')
            self.read_demands()
            self.note.setText(f"本批 {self.last_result['batch'][:12]} 已保存 {len(self.last_result['orders'])} 张物料仓单")
        except Exception as exc:
            QMessageBox.warning(self,'物料分配未完成',str(exc))

    def choose_pending(self):
        rows = orders.pending(self.path)
        if not rows:
            self.note.setText('暂无已登记待确认的物料分配批次')
            return None
        choices = [r['created']+' · '+r['id'][:12] for r in rows]
        selected, ok = QInputDialog.getItem(self,'选择原物料分配批次','选择批次',choices,0,False)
        return rows[choices.index(selected)] if ok else None

    def resume(self):
        previous = self.choose_pending()
        if previous:
            self.review(previous['selections'],previous)

    def end_pending(self):
        previous = self.choose_pending()
        if previous and confirm(self,'结束尚未落账的物料分配','结束这一批已登记但尚未保存的物料分配？'):
            try:
                orders.end_pending(self.path,previous['id'])
                self.note.setText('已结束所选未分配批次，申请可重新选择')
            except Exception as exc:
                QMessageBox.warning(self,'批次未结束',str(exc))

    def tracking(self):
        self.open_tracking = True
        self.accept()


def open_desk(host):
    desk = MaterialDesk(host)
    try:
        desk.exec()
        if desk.open_tracking:
            host.followup.order_kind.setCurrentIndex(host.followup.order_kind.findData(requests.KIND))
            host.show_section(3)
    finally:
        desk.deleteLater()
