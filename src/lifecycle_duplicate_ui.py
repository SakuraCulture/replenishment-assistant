"""Explicit whole-row choices for conflicting SKUs; source workbooks stay intact."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,
    QTableWidgetItem,QComboBox,QPlainTextEdit)
from ui_controls import button,label
import lifecycle_duplicates as duplicates


FIELDS={'quantity':'数量','unit':'数量单位','factor':'换算系数',
    'conversion_basis':'换算依据','base_quantity':'折合数量','amount':'金额',
    'currency':'币种','note':'备注','name':'商品名称'}


def value_text(value):
    return '空白' if value is None or value=='' else str(value)


def row_text(row):
    source=row['source']
    text=f"{source['sheet']} · 第{source['row']}行 · 数量 {value_text(row.get('quantity'))} {row.get('unit') or ''}"
    if row.get('amount') is not None:
        text+=f" · 金额 {row['amount']} {row.get('currency') or ''}"
    return text


class DuplicateResolution(QDialog):
    def __init__(self,parent,document):
        super().__init__(parent)
        self.setObjectName('lifecycleDuplicateResolution')
        self.setWindowTitle('核对重复商品 · 选择最终明细')
        self.document=document;self.conflicts=duplicates.find(document['scan']['rows'])
        self.resolved=None;self.choices=[]
        layout=QVBoxLayout(self)
        layout.addWidget(label('每款商品选择最终采用的一行，再继续核对。','section'))
        layout.addWidget(label('数量为0表示明确不订，也可以选择；数量、金额及其它字段整行采用。原Excel和所有冲突来源保留，不累加。','muted'))
        rows=document['scan']['rows'];source=rows[0]['source'] if rows else {}
        target=document.get('target') or {}
        self.source_note=label('来源：'+source.get('file','')+'\n补货单号：'+str(target.get('number') or (rows[0].get('signed_number') if rows else '') or '尚未确认'))
        self.source_note.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.source_note)
        self.table=QTableWidget(len(self.conflicts),4)
        self.table.setObjectName('lifecycleDuplicateTable')
        self.table.setHorizontalHeaderLabels(['商品编码','商品名称','差异字段','最终采用的原始行（必选）'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        for index,conflict in enumerate(self.conflicts):
            candidate_rows=conflict['rows']
            for column,text in enumerate((conflict['sku'],candidate_rows[0].get('name') or '',
                    '、'.join(FIELDS.get(f,f) for f in conflict['fields']))):
                item=QTableWidgetItem(str(text));item.setToolTip(str(text));self.table.setItem(index,column,item)
            combo=QComboBox();combo.setObjectName('lifecycleDuplicateChoice'+str(index))
            combo.addItem('请选择最终采用的一行',None)
            for row in candidate_rows:
                combo.addItem(row_text(row),duplicates.candidate_id(row))
                combo.setItemData(combo.count()-1,row_text(row),Qt.ItemDataRole.ToolTipRole)
            combo.currentIndexChanged.connect(lambda _=None,n=index:self.update_selection(n))
            self.table.setCellWidget(index,3,combo);self.choices.append(combo)
        layout.addWidget(self.table,1)
        self.details=QPlainTextEdit();self.details.setObjectName('lifecycleDuplicateDetails')
        self.details.setReadOnly(True);self.details.setMaximumHeight(150)
        layout.addWidget(self.details)
        self.note=label('');self.note.setObjectName('lifecycleDuplicateNote');layout.addWidget(self.note)
        controls=QHBoxLayout();controls.addWidget(button('返回文件范围',self.reject))
        self.apply_btn=button('采用所选行并继续核对',self.finish,True)
        self.apply_btn.setObjectName('lifecycleDuplicateApply');controls.addWidget(self.apply_btn)
        layout.addLayout(controls)
        self.table.currentCellChanged.connect(lambda row,*_:self.show_details(row))
        from table_readability import fit_window,install
        fit_window(self,1180,740);install(self.table)
        self.table.reading_controller.apply()
        for column,width in enumerate((160,280,140,500)):self.table.setColumnWidth(column,width)
        row_height=max([42,*[combo.sizeHint().height()+6 for combo in self.choices]])
        self.table.verticalHeader().setMinimumSectionSize(row_height)
        self.table.verticalHeader().setDefaultSectionSize(row_height)
        self.update_selection()
        if self.conflicts:self.table.setCurrentCell(0,0);self.show_details(0)

    def show_details(self,index):
        if index<0 or index>=len(self.conflicts):return
        conflict=self.conflicts[index];selected=self.choices[index].currentData()
        text=duplicates.message([conflict])
        if selected:
            row=next(r for r in conflict['rows'] if duplicates.candidate_id(r)==selected)
            text+='\n\n最终采用：'+row['source']['file']+' / '+row_text(row)
        self.details.setPlainText(text)

    def update_selection(self,index=None):
        selected=sum(combo.currentData() is not None for combo in self.choices)
        self.note.setText(f'已选择 {selected}/{len(self.conflicts)} 款；全部选择后才能继续。')
        self.apply_btn.setEnabled(bool(self.conflicts) and selected==len(self.conflicts))
        if index is not None:self.show_details(index)

    def finish(self):
        if any(combo.currentData() is None for combo in self.choices):
            self.note.setText('还有商品未选择最终明细，请逐款确认；尚未保存。')
            return
        choices={conflict['token']:combo.currentData() for conflict,combo in zip(self.conflicts,self.choices)}
        try:self.resolved=duplicates.resolve(self.document,choices)
        except ValueError as exc:self.note.setText('未采用：'+str(exc));return
        self.accept()


def resolve_document(parent,document):
    dialog=DuplicateResolution(parent,document)
    try:
        return dialog.resolved if dialog.exec()==QDialog.DialogCode.Accepted else None
    finally:dialog.deleteLater()
