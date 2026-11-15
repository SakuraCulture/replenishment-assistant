"""On-demand, in-place recovery of exact local lifecycle save operations."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QTableWidget,QTableWidgetItem
from ui_controls import button,label
from business_ui import run
from runtime_support import Cancelled
import lifecycle_recovery as service
import lifecycle_io


def show(parent,host,heads=None,stage=None):
    if getattr(host,'closing',False):return None
    d=RecoveryDialog(parent,host,heads,stage)
    try:
        if not getattr(host,'closing',False):d.exec()
        return d.receipt
    finally:d.deleteLater()


class RecoveryDialog(QDialog):
    def __init__(self,parent,host,heads=None,stage=None):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.receipt=None;self.review=None
        self.setObjectName('lifecycleRecovery');self.setWindowTitle('恢复并继续 · 原任务与已保存事实');self.resize(1120,650)
        v=QVBoxLayout(self);v.addWidget(label('先核对是否已保存；未提交才恢复原内容，成功部分不重做。','section'))
        v.addWidget(label('按所选订单查找原保存批次；恢复仍处理原完整范围，不自动跳过失败单或缩小成有效子集。','muted'))
        self.choices=QComboBox();v.addWidget(self.choices)
        self.note=label('正在读取本地恢复记录…');self.note.setWordWrap(True);v.addWidget(self.note)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['门店 / 编码','仓 / 签发单号','环节','已保存状态','原因与下一步'])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,width in enumerate((210,320,110,130,300)):self.table.setColumnWidth(i,width)
        v.addWidget(self.table,1);row=QHBoxLayout();row.addWidget(button('重新核对原任务',self.refresh))
        self.go=button('恢复并继续',self.recover,True);self.go.setEnabled(False);row.addWidget(self.go)
        row.addWidget(button('签发与导出原任务恢复',lambda:__import__('export_recovery_ui').show(self,self.path)))
        row.addWidget(button('关闭',self.accept));v.addLayout(row)
        try:
            tasks=run(self,service.recent,(self.path,heads,stage),'读取本地恢复任务')
            if getattr(host,'closing',False):return
            for task in tasks:
                stages='、'.join(dict.fromkeys(lifecycle_io.TITLES[p['stage']] for p in task['entries']))
                self.choices.addItem(task['created']+' · '+stages+' · '+str(len(task['entries']))+'份 · '+{'prepared':'上次结果待核','interrupted':'上次中断','completed':'已保存','source_deleted':'原范围变化','unreadable':'此记录损坏，其他任务可继续'}.get(task['state'],task['state']),task['id'])
            self.choices.currentIndexChanged.connect(self.refresh)
            if tasks:self.refresh()
            else:self.note.setText('当前范围没有保存检查点；旧版任务请按原单历史重新核对。签发导出可使用下方“签发与导出原任务恢复”。未执行任何业务动作。')
        except Exception as exc:self.note.setText('读取未完成：'+str(exc))
    def refresh(self):
        if getattr(self.host,'closing',False):return
        self.review=None;self.go.setEnabled(False)
        if not self.choices.currentData():return
        try:
            status=run(self,service.inspect,(self.path,self.choices.currentData()),'核对原保存事实与当前版本')
            if getattr(self.host,'closing',False):return
            self.review=status;self.note.setText(status['message']);self.table.setRowCount(len(status['rows']))
            for n,row in enumerate(status['rows']):
                h=row['head'];values=[h.get('name',h['store'])+' / '+h['store'],h['warehouse']+' / '+h['number'],lifecycle_io.TITLES[row['stage']],{'saved':'已核实保存','unchanged':'本次修改未提交','needs_review':'需重新核对'}[row['state']],row['reason'] or status['message']]
                for c,value in enumerate(values):
                    cell=QTableWidgetItem(value);cell.setToolTip(value);self.table.setItem(n,c,cell)
            self.go.setText('使用已保存结果' if status['action']=='completed' else '恢复原内容并继续保存')
            self.go.setEnabled(status['action'] in ('completed','retry'))
        except Cancelled:self.note.setText('已取消核对；原事实和恢复材料保留。')
        except Exception as exc:self.note.setText('核对未完成：'+str(exc))
    def recover(self):
        if not self.review or getattr(self.host,'closing',False):return
        try:
            result=run(self,service.recover,(self.path,self.choices.currentData()),'核实原任务并恢复保存')
            self.receipt=result['receipt']
            if not getattr(self.host,'closing',False):self.refresh();self.note.setText(result['message'])
        except Cancelled:
            if not getattr(self.host,'closing',False):self.note.setText('恢复已安全停止；再次核对原保存事实后继续，不按失败重做。')
        except Exception as exc:
            if not getattr(self.host,'closing',False):self.refresh();self.note.setText('未继续：'+str(exc)+'；原资料保留，未自动回滚或改动其他订单。')
