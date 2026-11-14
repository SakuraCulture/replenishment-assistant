"""One explicit review of additions; conflicts never authorize replacement."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QHeaderView,QAbstractItemView,QPlainTextEdit
from ui_controls import label,button

TITLES={'ADD':'新增','DUPLICATE':'完全重复，跳过','CONFLICT':'有差异，保留本机','DELETED':'本机已删除，不恢复',
        'added':'新增','duplicate':'完全重复，跳过','conflict':'有差异，保留本机','deleted':'本机已删除，不恢复'}


def review(parent,plan):
    report=plan['merge_report'];counts=report['counts'];rows=report.get('orders',[])
    dialog=QDialog(parent);dialog.setWindowTitle('核对订单合并范围');dialog.resize(1120,700)
    layout=QVBoxLayout(dialog)
    layout.addWidget(label('只新增无冲突订单；完全重复跳过，冲突保留本机','section'))
    layout.addWidget(label('新增 {added} 单 · 完全重复 {duplicate} 单 · 冲突 {conflict} 单 · 本机已删除 {deleted} 单'.format(**counts)))
    layout.addWidget(label('本次不补齐或覆盖已有订单。门店参数、必卖清单、草稿、当前仓资料、浏览器和缺货率数据保留本机；迁入任务暂停，不自动上传或下单。'))
    table=QTableWidget(len(rows),6);table.setObjectName('orderMergePreview')
    table.setHorizontalHeaderLabels(['业务日期','门店编码','门店名称','签发单号','处理结果','原因'])
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);table.setAlternatingRowColors(True);table.setWordWrap(False)
    detail=QPlainTextEdit();detail.setReadOnly(True);detail.setMaximumHeight(115);detail.setPlaceholderText('选择一行查看完整原因；冲突订单不会覆盖本机数据。')
    reasons=[]
    for i,row in enumerate(rows):
        why=row.get('reason',row.get('reasons',''))
        why='；'.join(str(x) for x in why) if isinstance(why,list) else str(why)
        reasons.append(why)
        values=[row.get('day',''),row.get('store',''),row.get('name',row.get('store_name','')),row.get('number',''),TITLES.get(row.get('status'),str(row.get('status',''))),why]
        for j,value in enumerate(values):
            item=QTableWidgetItem(str(value));item.setToolTip(str(value));table.setItem(i,j,item)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    for i,width in enumerate((105,150,180,260,180,360)):table.setColumnWidth(i,width)
    table.horizontalHeader().setStretchLastSection(True)
    table.itemSelectionChanged.connect(lambda:detail.setPlainText(reasons[table.currentRow()] if 0<=table.currentRow()<len(reasons) else ''))
    layout.addWidget(table,1);layout.addWidget(detail)
    layout.addWidget(label('来源文件：'+plan['package']))
    layout.addWidget(label('原资料完整备份：'+plan['backup']))
    layout.addWidget(label('确认后软件安全退出，在独立副本完成合并和校验后切换；重新打开可查看结果。' if counts['added'] else '没有可新增订单，本机不会改变，无需退出或重复导入。'))
    actions=QHBoxLayout();layout.addLayout(actions)
    cancel=button('返回，不修改本机',dialog.reject);cancel.setObjectName('cancelOrderMerge');actions.addWidget(cancel)
    confirm=button('确认新增 '+str(counts['added'])+' 单，保留本机其他数据',dialog.accept,True)
    confirm.setObjectName('confirmOrderMerge');confirm.setEnabled(counts['added']>0);actions.addWidget(confirm)
    return dialog.exec()==QDialog.DialogCode.Accepted
