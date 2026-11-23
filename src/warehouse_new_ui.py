"""Workbench view of the current warehouse date basis and unresolved archive rows."""
from PySide6.QtWidgets import (QAbstractItemView,QComboBox,QDialog,QHBoxLayout,
                               QLineEdit,QMessageBox,QTableWidget,QTableWidgetItem,QVBoxLayout)
from ui_controls import button,label
from result_semantics import fmt
from warehouse_new_exports import DETAIL_HEADERS,export_detail
from warehouse_newness import AGE_LIMIT,MISSING_COLUMN,MISSING_VIRTUAL_CATEGORY,view


class NewWarehouseProducts(QDialog):
    def __init__(self,parent,warehouse,catalog,day):
        super().__init__(parent)
        self.setObjectName('warehouseRecentDialog')
        self.setWindowTitle(warehouse+' · 45天新品')
        self.resize(1040,620)
        self.warehouse=warehouse
        self.day=day
        self.revision=catalog['revision'] if catalog else None
        layout=QVBoxLayout(self)
        entries=view(catalog,day) if catalog else None
        pending=(catalog or {}).get('quarantined',[])
        summary=label(f'{warehouse}  ·  判定日 {day}（北京时间）  ·  仓资料版本 '+(catalog['revision'] if catalog else '暂无'),'section')
        layout.addWidget(summary)
        self.groups={}
        if entries is not None:
            self.groups['新品']=[self._row(item,basis) for item,basis in entries if basis['new']]
            self.groups['已排除']=[self._row(item,basis) for item,basis in entries
                                   if not basis['issue'] and basis['age']<=AGE_LIMIT and basis['excluded_reason']]
            self.groups['日期待核']=[self._row(item,basis) for item,basis in entries if basis['issue']]
            self.groups['日期早于建档']=[self._row(item,basis) for item,basis in entries if basis['warning']]
            text=(f"仓新品 {len(self.groups['新品']):,} 款 · 45天内已排除 {len(self.groups['已排除']):,} 款"
                  f" · 日期待核 {len(self.groups['日期待核']):,} 款"
                  f" · 日期早于建档 {len(self.groups['日期早于建档']):,} 款 · 档案记录待核 {len(pending):,} 行")
            if any(basis['screening_issue'] for _,basis in entries):
                text+='\n'+MISSING_VIRTUAL_CATEGORY
        else:
            text=('该仓尚未导入商品资料' if catalog is None else MISSING_COLUMN)
        status=label(text,'muted');status.setObjectName('warehouseRecentCounts');layout.addWidget(status)
        if pending:
            self.groups['档案记录待核']=[self._row(item,None) for item in pending]
        tools=QHBoxLayout();layout.addLayout(tools)
        self.scope=QComboBox();self.scope.setObjectName('warehouseRecentScope');tools.addWidget(self.scope)
        for name,rows in self.groups.items():self.scope.addItem(f'{name}（{len(rows)}）',name)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索 SKU / 商品名');tools.addWidget(self.search,1)
        self.table=QTableWidget(0,len(DETAIL_HEADERS));self.table.setObjectName('warehouseRecentRows')
        self.table.setHorizontalHeaderLabels(DETAIL_HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for column,width in enumerate((185,240,110,79,76,78,240)):self.table.setColumnWidth(column,width)
        layout.addWidget(self.table,1)
        actions=QHBoxLayout();layout.addLayout(actions)
        self.export_button=button('导出新品明细',self.export_new_products,True)
        self.export_button.setToolTip('导出本仓全部符合条件的 45 天新品')
        self.export_button.setEnabled(bool(self.groups.get('新品')))
        actions.addWidget(self.export_button);actions.addStretch();actions.addWidget(button('关闭',self.accept))
        self.scope.currentIndexChanged.connect(self.refresh)
        self.search.textChanged.connect(self.refresh)
        self.refresh()

    def export_new_products(self):
        from export_files import explain_error
        from export_locations import save_file

        rows=self.groups.get('新品')
        if not rows:return
        filename=f'{self.warehouse}_45天新品_{self.day}.xlsx'
        path,_=save_file(self,'导出新品明细',filename,'Excel 工作簿 (*.xlsx)')
        if not path:return
        try:
            saved=export_detail(path,rows,self.warehouse,self.day,self.revision)
        except Exception as exc:
            QMessageBox.warning(self,'新品明细未导出',explain_error(exc))
            return
        QMessageBox.information(self,'导出完成',f'已导出本仓 {len(rows):,} 款新品。\n{saved}')

    @staticmethod
    def _row(item,basis):
        message=item.get('reason','')
        if basis:message='，'.join(basis[key] for key in ('excluded_reason','screening_issue','issue','warning') if basis[key])
        return (item['商品编码'],item.get('商品名称'),(basis or {}).get('first_date'),
                (basis or {}).get('age'),item.get('商品状态'),item.get('可用数'),message,item.get('_row'))

    def refresh(self):
        search=self.search.text().strip().casefold()
        rows=[row for row in self.groups.get(self.scope.currentData(),[]) if search in
              (row[0]+' '+str(row[1] or '')).casefold()]
        self.table.setRowCount(len(rows))
        for i,row in enumerate(rows):
            for j,value in enumerate(row):
                item=QTableWidgetItem(fmt(value) if value is not None else '')
                item.setToolTip(item.text())
                self.table.setItem(i,j,item)
