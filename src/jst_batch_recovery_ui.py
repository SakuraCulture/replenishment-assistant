"""Explicit local recovery of pending allocation claims; no platform operations."""
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox
from ui_controls import button,label
from business_ui import grid


class RecoveryDialog(QDialog):
    def __init__(self,parent,groups):
        super().__init__(parent);self.setObjectName('jstBatchRecovery');self.setWindowTitle('处理未完成分配批次');self.resize(1100,540)
        self.action=None;self.batch_id=None;self.groups=groups
        v=QVBoxLayout(self)
        n=sum(len(g['selected']) for g in groups)
        note=label(f'本次所选中有{n}张仓单仍在原批等待或失败，尚不能直接建立另一批。请选择处理方式。','section');note.setWordWrap(True);v.addWidget(note)
        table,model=grid(self,['原分配批次','本次所选仓单','门店 / 仓','任务状态'],[
            [g['label'],m['head']['number'],m['head'].get('name',m['head']['store'])+' / '+m['head']['warehouse'],{'pending':'等待分配','failed':'分配失败'}.get(m['status'],m['status'])]
            for g in groups for m in g['selected']]);v.addWidget(table,1)
        row=QHBoxLayout();self.batch=QComboBox()
        for g in groups:self.batch.addItem(g['label']+f" · 本次所选{len(g['selected'])}单 / 原批剩余{len(g['remaining'])}单",g['batch_id'])
        row.addWidget(self.batch,1);self.resume=button('继续原批',lambda:self.choose('resume'));self.resume.setObjectName('resumeOriginalAllocationBatch');row.addWidget(self.resume);v.addLayout(row)
        note=label('继续原批：先预览该原批全部剩余订单，沿用其冻结库存和顺序；确认生成后才执行。\n结束所选未完成项：仅结束上表所列任务，再用当前选定仓资料重新预览。已生成单、未选单及其库存记录保留；原批其他未完成项暂停，可另行继续。','muted');note.setWordWrap(True);v.addWidget(note)
        row=QHBoxLayout();cancel=button('返回，不改动原批',self.reject);cancel.setDefault(True);row.addWidget(cancel)
        self.restart=button('结束所选未完成项并重新预览',lambda:self.choose('restart'),True);self.restart.setObjectName('restartSelectedPendingAllocation');row.addWidget(self.restart);v.addLayout(row)
        self.batch.currentIndexChanged.connect(self.describe);self.describe()

    def describe(self,*_):
        g=self.groups[self.batch.currentIndex()];self.resume.setText(f"继续此原批剩余{len(g['remaining'])}单")

    def choose(self,action):
        self.action=action;self.batch_id=self.batch.currentData();self.accept()
