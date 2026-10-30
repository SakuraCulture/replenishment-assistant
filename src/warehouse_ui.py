"""Business-facing warehouse maintenance controls for the batch workbench."""
from pathlib import Path
from collections import Counter
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame,QVBoxLayout,QHBoxLayout,QCheckBox,QFileDialog,QDialog,QTableWidget,QTableWidgetItem,QLineEdit,QAbstractItemView,QComboBox,QMessageBox
from ui_controls import label,button,FileCard
import result_semantics as semantics
from app_defaults import supply_route_choices
import engine,warehouse_catalog

class WarehouseUi:
    def make_warehouse_panel(self):
        panel=QFrame();panel.setObjectName('card');v=QVBoxLayout(panel);v.addWidget(label('分仓商品资料 · 分别维护','section'));self.catalog_labels={};self.catalog_buttons={}
        for wh in warehouse_catalog.WAREHOUSES:
            row=QHBoxLayout();row.addWidget(label(wh,'section'));row.addStretch();pick=button('导入'+wh+'资料',lambda checked=False,w=wh:self.choose_catalog(w));self.catalog_buttons[wh]=pick;row.addWidget(pick);row.addWidget(button('查看'+wh+'资料',lambda checked=False,w=wh:self.show_catalog(w)));v.addLayout(row)
            text=label('尚未导入','muted');self.catalog_labels[wh]=text;v.addWidget(text)
        v.addWidget(label('仓库由导入入口指定，不从表内猜测。保存后跨批次复用；可用数是导入时的库存快照，更新库存需重新导入。','muted'))
        self.master_legacy=QCheckBox('兼容0.4混合资料（不启用主从仓）');v.addWidget(self.master_legacy)
        self.products=FileCard('旧版混合商品资料','仅用于兼容已有“仓归属”资料');self.products.hide();v.addWidget(self.products)
        self.master_legacy.toggled.connect(self.products.setVisible);self.master_legacy.toggled.connect(self.file_changed);self.products.changed.connect(self.file_changed)
        self.refresh_catalog_labels();return panel
    def refresh_catalog_labels(self):
        for wh,text in self.catalog_labels.items():
            c=self.catalogs.get(wh)
            self.catalog_buttons[wh].setText(('更新' if c else '导入')+wh+'资料')
            if not c:text.setText('尚未导入；表内不需要仓库标识');continue
            text.setText(f"{len(c['items']):,}款 · 版本 {c['revision']}\n{Path(c['source_path']).name} · 导入 {c['imported_at']}")
    def choose_catalog(self,warehouse):
        from import_ui import choose
        paths=choose(self,'catalog','导入到'+warehouse+'（完整更新该仓快照）');path=paths[0] if paths else None
        if path:self.import_catalog_path(path,warehouse)
    def import_catalog_path(self,path,warehouse):self.job(warehouse_catalog.import_catalog,(path,warehouse),self.catalog_imported)
    def catalog_imported(self,catalog):
        from interaction_ui import confirm
        old=self.catalogs.get(catalog['warehouse'])
        message=catalog['warehouse']+f"：{len(old['items']) if old else 0} → {len(catalog['items'])} 款\n原版本：{old['revision'] if old else '未导入'}\n新版本：{catalog['revision']}\n将完整更新并保存，跨批次复用；本次须重新检查，历史快照不变。"
        if not confirm(self,'确认更新并保存仓资料',message):return
        try:self.catalog_repository.save(catalog)
        except Exception as exc:self.failed('仓资料保存失败，原资料保留：'+str(exc));return
        self.catalogs[catalog['warehouse']]=catalog;self.refresh_catalog_labels()
        if not self.master_legacy.isChecked():self.file_changed()
        self.status.setText(catalog['warehouse']+f"资料已保存 {len(catalog['items']):,} 款；请重新检查本次补货建议。")
    def show_catalog(self,warehouse):
        catalog=self.catalogs.get(warehouse)
        if not catalog:QMessageBox.information(self,'尚未导入',warehouse+'尚无保存的商品资料。');return
        dialog=QDialog(self);dialog.setWindowTitle(warehouse+' · 已保存资料');dialog.resize(1040,650);v=QVBoxLayout(dialog)
        counts=Counter(str(r.get('商品状态')) for r in catalog['items'].values());v.addWidget(label(f"{warehouse} · {len(catalog['items']):,}款 · {dict(counts)}",'section'))
        v.addWidget(label(f"来源：{catalog['source_path']}\n导入：{catalog['imported_at']} · 版本：{catalog['revision']}。此处库存不因计算而扣减。",'muted'))
        search=QLineEdit();search.setPlaceholderText('搜索商品名称 / 编码');v.addWidget(search)
        rows=list(catalog['items'].values());fields=['商品编码','商品名称','商品状态','可用数','基本售价','出库箱规','分类']
        table=QTableWidget(len(rows),len(fields));table.setHorizontalHeaderLabels(fields);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.setColumnWidth(0,180);table.setColumnWidth(1,320);v.addWidget(table,1)
        for i,r in enumerate(rows):
            for j,k in enumerate(fields):table.setItem(i,j,QTableWidgetItem(semantics.fmt(r.get(k))))
        search.textChanged.connect(lambda text:[table.setRowHidden(i,text.lower() not in (str(r.get('商品编码',''))+' '+str(r.get('商品名称',''))).lower()) for i,r in enumerate(rows)])
        v.addWidget(button('关闭',dialog.accept));dialog.exec()
    def install_route_widgets(self,store,row,saved):
        primary=QComboBox();primary.addItem('请选择主仓',None);secondary=QComboBox();secondary.addItem('无从仓',None)
        for wh in warehouse_catalog.WAREHOUSES:
            text=wh if wh in self.catalogs else wh+'（未导入）';primary.addItem(text,wh);secondary.addItem(text,wh)
        route=saved.get(store,{})
        for widget,key in [(primary,'primary'),(secondary,'secondary')]:
            index=widget.findData(route.get(key));widget.setCurrentIndex(max(index,0))
        enabled=bool(self.loaded.get('catalogs'));primary.setEnabled(enabled);secondary.setEnabled(enabled)
        self.store_table.setCellWidget(row,3,primary);self.store_table.setCellWidget(row,4,secondary)
        self.store_widgets[store].update(primary=primary,secondary=secondary)
        primary.currentIndexChanged.connect(lambda *_:self.route_changed(store));secondary.currentIndexChanged.connect(lambda *_:self.route_changed(store))
    def route_changed(self,store):
        if self.initializing:return
        self.invalidate([store]);self.selection_changed()
        if hasattr(self,'refresh_parameter_label'):self.refresh_parameter_label(store)
    def route_for(self,store):
        w=self.store_widgets[store]
        return warehouse_catalog.validate_route({'primary':w['primary'].currentData(),'secondary':w['secondary'].currentData()},self.loaded['catalogs'])
    def bulk_routes(self):
        codes=self.selected_codes()
        if not codes or not self.loaded.get('catalogs'):QMessageBox.information(self,'选择门店','请在分仓资料模式下先勾选门店。');return
        dialog=QDialog(self);dialog.setWindowTitle('批量设置门店主从仓');v=QVBoxLayout(dialog);v.addWidget(label(f'设置所选 {len(codes)} 家门店的供货顺序；主从仓不能相同。'))
        choices=supply_route_choices()
        combo=QComboBox()
        for text,value in choices:combo.addItem(text,value)
        v.addWidget(combo);v.addWidget(button('应用到所选门店',dialog.accept,True));v.addWidget(button('取消',dialog.reject))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            route=combo.currentData()
            for s in codes:
                for key in ['primary','secondary']:
                    widget=self.store_widgets[s][key];widget.setCurrentIndex(widget.findData(route[key]))
