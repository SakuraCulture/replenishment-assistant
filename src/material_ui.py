"""Material directory, store requests, and stock allocation review."""
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QFileDialog, QFormLayout,
                               QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
                               QMessageBox, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

import material_orders as orders
import material_requests as requests
import warehouse_catalog
import work_repository as wr
from app_defaults import WAREHOUSES
from business_ui import grid, run
from interaction_ui import confirm, review_rows
from runtime_support import Cancelled, checkpoint
from sqlite_write import begin, commit
from ui_controls import button, label


def request_edit(path, stores, wh, rows, original, *, progress, cancel):
    stores = tuple(dict.fromkeys(stores))
    if not stores:
        raise ValueError('请先勾选门店')
    if original and stores != (original['store'],):
        raise ValueError('请在原门店更正申请')
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        begin(db,cancel)
        saved = []
        for store in stores:
            checkpoint(cancel)
            progress('保存 '+store+' 的物料申请')
            saved.append(requests.save(db,store,wh,rows,request_id=original.get('id'),expected=original.get('version')))
        commit(db,cancel)
        return saved


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


def stock_text(item):
    try:
        return orders.allocation.text(orders.allocation.number(item.get('可用数'),'仓可用数'))
    except ValueError:
        return '待核'


class RequestChoices(QWidget):
    checked_changed = Signal()
    match_entered = Signal(str)

    def __init__(self, title, placeholder):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.addWidget(label(title,'section'))
        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.setAccessibleName(placeholder)
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)
        self.list = QListWidget()
        self.list.setAccessibleName(title)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setTextElideMode(Qt.TextElideMode.ElideRight)
        layout.addWidget(self.list,1)
        self.footer = QHBoxLayout()
        self.status = label('','muted')
        self.footer.addWidget(self.status,1)
        layout.addLayout(self.footer)
        self.items, self.names, self.checked = {}, {}, {}
        self.visible = []
        self.search.textChanged.connect(self.filter)
        self.search.returnPressed.connect(self.enter_match)
        self.list.itemChanged.connect(self.toggle)

    def fill(self, choices):
        with QSignalBlocker(self.list):
            self.list.clear()
            self.items.clear()
            self.names.clear()
            self.checked.clear()
            for code, name, detail in choices:
                item = QListWidgetItem(' · '.join(part for part in (name,code,detail) if part))
                item.setToolTip(item.text())
                item.setData(Qt.ItemDataRole.UserRole,code)
                item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                self.list.addItem(item)
                self.items[code] = item
                self.names[code] = name
        self.filter()
        self.checked_changed.emit()

    def filter(self, *_):
        terms = self.search.text().casefold().split()
        self.visible = []
        for code, item in self.items.items():
            text = code.casefold()+' '+self.names[code].casefold()
            matches = all(term in text for term in terms)
            item.setHidden(not matches)
            if matches:
                self.visible.append(code)
        self.show_count()

    def show_count(self):
        self.status.setText(f'已勾选 {len(self.checked)} · 显示 {len(self.visible)} / {len(self.items)}')

    def enter_match(self):
        text = self.search.text().strip()
        if not text:
            return
        if text in self.items:
            self.match_entered.emit(text)
            return
        matches = [code for code in self.visible if text.casefold() in (code.casefold(),self.names[code].casefold())]
        matches = matches or self.visible
        if len(matches)==1:
            self.match_entered.emit(matches[0])
        else:
            self.status.setText('有多条匹配，请勾选或输入完整编码' if matches else '未匹配到资料，请核对名称或编码')

    def toggle(self, item):
        code = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState()==Qt.CheckState.Checked:
            self.checked[code] = None
        else:
            self.checked.pop(code,None)
        self.show_count()
        self.checked_changed.emit()

    def check(self, code):
        self.items[code].setCheckState(Qt.CheckState.Checked)

    def clear_checked(self):
        with QSignalBlocker(self.list):
            for code in self.checked:
                self.items[code].setCheckState(Qt.CheckState.Unchecked)
        self.checked.clear()
        self.show_count()
        self.checked_changed.emit()


class RequestEditor(QDialog):
    SKU, QUANTITY, NOTE, NAME = range(4)

    def __init__(self, parent, original=None, preset_skus=(), preset_warehouse=None):
        super().__init__(parent)
        self.path, self.original = parent.path, original or {}
        self.setWindowTitle('更正原物料申请' if original else '登记门店物料需求')
        self.resize(1120 if not original else 870,570)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        with closing(wr.read_connection(self.path)) as db:
            self.store_names = {code:str(wr.loads(raw).get('name') or code)
                                for code, raw in db.execute('SELECT code,data FROM stores ORDER BY code')}
        self.warehouse = QComboBox()
        self.warehouse.addItems(WAREHOUSES)
        self.warehouse.setMinimumWidth(120)
        self.warehouse.setMaximumWidth(200)
        self.warehouse.setCurrentText(self.original.get('warehouse') or preset_warehouse or WAREHOUSES[0])
        form.addRow('供货仓',self.warehouse)
        if original:
            code = original['store']
            form.addRow('门店',label(self.store_names.get(code,code)+' · '+code))
            form.addRow('申请修订',label(f"{original['id']} · V{original['version']:03d}"))
            self.warehouse.setEnabled(False)
        layout.addLayout(form)

        panels = QSplitter(Qt.Orientation.Horizontal)
        panels.setChildrenCollapsible(False)
        self.stores = None
        if not original:
            self.stores = RequestChoices('门店','搜门店名称或编码，回车勾选')
            self.stores.fill((code,name,'') for code,name in self.store_names.items())
            self.stores.footer.addWidget(button('清空勾选',self.stores.clear_checked))
            self.stores.match_entered.connect(self.stores.check)
            panels.addWidget(self.stores)
        self.products = RequestChoices('本仓物料','搜物料名称或编码，回车加入')
        self.add_checked = button('加入已勾物料',self.add_picked)
        self.products.footer.addWidget(self.add_checked)
        self.products.match_entered.connect(lambda sku:self.add_items((sku,)))
        panels.addWidget(self.products)

        draft = QWidget()
        content = QVBoxLayout(draft)
        content.setContentsMargins(0,0,0,0)
        content.addWidget(label('需求明细','section'))
        content.addWidget(label('可直接填写商品编码、数量和备注。','muted'))
        self.lines = QTableWidget(0,4)
        self.lines.setHorizontalHeaderLabels(('商品编码','申请数量（基础单位）','备注','物料名称'))
        self.lines.setColumnWidth(0,165)
        self.lines.setColumnWidth(1,135)
        self.lines.setColumnWidth(2,78)
        self.lines.setColumnWidth(self.NAME,115)
        self.lines.horizontalHeader().moveSection(self.NAME,1)
        self.lines.horizontalHeader().setStretchLastSection(True)
        for row in self.original.get('lines', ()):
            self.add_row(row['sku'],row['quantity'],row['note'])
        if not self.lines.rowCount():
            self.add_row()
        content.addWidget(self.lines,1)
        actions = QHBoxLayout()
        actions.addWidget(button('新增一行',self.new_row))
        actions.addWidget(button('移除所选行',self.remove_rows))
        actions.addStretch()
        content.addLayout(actions)
        panels.addWidget(draft)
        panels.setSizes([260,300,510] if self.stores else [305,520])
        panels.setStretchFactor(panels.count()-1,1)
        layout.addWidget(panels,1)

        actions = QHBoxLayout()
        self.summary = label('','muted')
        actions.addWidget(self.summary,1)
        actions.addStretch()
        self.save_btn = button('保存申请',self.save,True)
        actions.addWidget(self.save_btn)
        actions.addWidget(button('关闭',self.reject))
        layout.addLayout(actions)
        self.lines.itemChanged.connect(self.line_changed)
        if self.stores:
            self.stores.checked_changed.connect(self.update_summary)
        self.products.checked_changed.connect(self.update_summary)
        self.warehouse.currentTextChanged.connect(self.read_products)
        self.read_products()
        if preset_skus and not original:
            self.add_items(preset_skus)
        if self.stores:
            self.stores.search.setFocus()

    def stores_to_save(self):
        return tuple(self.stores.checked) if self.stores else (self.original['store'],)

    def read_products(self, *_):
        warehouse = self.warehouse.currentText()
        try:
            catalogs, _ = warehouse_catalog.Repository(Path(self.path).parent).snapshots((warehouse,))
            catalog = catalogs.get(warehouse)
            self.products.fill((sku,str(item.get('商品名称') or ''),'可用 '+stock_text(item))
                               for sku,item in (catalog or {}).get('items',{}).items() if not requests.eligibility(item))
            if not catalog:
                self.products.status.setText('本仓尚未采用商品资料')
        except (ValueError, OSError, RuntimeError) as exc:
            self.products.fill(())
            self.products.status.setText('仓资料未打开 '+str(exc))
        with QSignalBlocker(self.lines):
            for row in range(self.lines.rowCount()):
                self.set_product_name(row)
        self.update_summary()

    def set_product_name(self, row):
        sku = value(self.lines,row,self.SKU)
        self.lines.item(row,self.NAME).setText(self.products.names.get(sku,''))

    def line_changed(self, item):
        if item.column()==self.SKU:
            with QSignalBlocker(self.lines):
                self.set_product_name(item.row())
        self.update_summary()

    def draft_rows(self):
        for row in range(self.lines.rowCount()):
            cells = tuple(value(self.lines,row,col) for col in range(3))
            if any(cells):
                yield dict(zip(('sku','quantity','note'),cells))

    def update_summary(self, *_):
        stores = self.stores_to_save()
        count = sum(1 for _ in self.draft_rows())
        self.summary.setText(f'{self.warehouse.currentText()} · {len(stores)} 家门店 · {count} 行需求')
        self.summary.setToolTip(' → '.join(self.store_names.get(code,code)+' · '+code for code in stores))
        self.save_btn.setText(f'为 {len(stores)} 家保存同一份需求' if len(stores)>1 else '保存申请')
        self.save_btn.setEnabled(bool(stores) and bool(count))
        self.add_checked.setEnabled(bool(self.products.checked))

    def add_picked(self):
        self.add_items(tuple(self.products.checked))
        self.products.clear_checked()

    def add_items(self, codes):
        existing = {value(self.lines,i,0):i for i in range(self.lines.rowCount()) if value(self.lines,i,0)}
        empty = iter(i for i in range(self.lines.rowCount()) if not any(value(self.lines,i,col) for col in range(3)))
        first = None
        with QSignalBlocker(self.lines):
            for code in dict.fromkeys(codes):
                row = existing.get(code)
                if row is None:
                    row = next(empty,None)
                    if row is None:
                        row = self.add_row(code)
                    else:
                        self.lines.item(row,0).setText(code)
                        self.set_product_name(row)
                if first is None:
                    first = row
        if first is not None:
            self.lines.setCurrentCell(first,1)
        self.update_summary()

    def add_row(self, sku='', qty='', note=''):
        index = self.lines.rowCount()
        with QSignalBlocker(self.lines):
            self.lines.insertRow(index)
            for col, text in enumerate((sku,qty,note)):
                self.lines.setItem(index,col,QTableWidgetItem(str(text)))
            name = QTableWidgetItem()
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.lines.setItem(index,self.NAME,name)
            self.set_product_name(index)
        return index

    def new_row(self):
        self.lines.setCurrentCell(self.add_row(),0)
        self.lines.setFocus()
        self.lines.editItem(self.lines.currentItem())

    def remove_rows(self):
        for row in sorted({index.row() for index in self.lines.selectedIndexes()},reverse=True):
            self.lines.removeRow(row)
        if not self.lines.rowCount():
            self.add_row()
        self.update_summary()

    def save(self):
        try:
            run(self,request_edit,(self.path,self.stores_to_save(),self.warehouse.currentText(),list(self.draft_rows()),
                                   self.original),'保存门店物料申请')
        except Cancelled:
            return
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
        self.catalog_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
            self.catalog_rows.append((sku,name,item['分类'],item['商品状态'],self.catalog_units.get(sku,'库存单位'),stock_text(item)))
        self.catalog_model.replace(self.catalog_rows)

    def from_catalog(self):
        selected = sorted(self.catalog_table.selectionModel().selectedRows(),key=lambda index:index.row())
        self.edit_request(preset_skus=tuple(self.catalog_model.data(index) for index in selected),
                          preset_warehouse=self.wh.currentText())

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

    def edit_request(self, *, preset_skus=(), preset_warehouse=None, original=None):
        editor = RequestEditor(self,original,preset_skus,preset_warehouse)
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
