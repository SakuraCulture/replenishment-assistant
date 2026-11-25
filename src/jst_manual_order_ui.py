"""Manual handling for the exact 聚水潭 import shown on the order screen."""
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QMessageBox, QVBoxLayout

from business_ui import run
from interaction_ui import confirm
from platform_import_ui import export_target, operator_input, rows_dialog
from runtime_support import Cancelled
from ui_controls import button, label
import jst_manual_order as manual
import jst_auto_order_store as store


class ManualOrderDialog(QDialog):
    def __init__(self, parent, host, orders):
        super().__init__(parent)
        self.host = host
        self.path = host.work_repository.path
        self.setWindowTitle('聚水潭人工处理')
        self.resize(1040, 375)
        layout = QVBoxLayout(self)
        self.online = QComboBox()
        for online in orders:
            self.online.addItem(online)
        layout.addWidget(self.online)
        self.scope = label('', 'section')
        self.note = label('', 'muted')
        self.note.setWordWrap(True)
        layout.addWidget(self.scope)
        layout.addWidget(self.note)
        files = QHBoxLayout()
        self.check_file = button('导出本次导入单（核对用）', lambda:self.export('check'))
        self.takeover = button('接管本次导入',self.take_over)
        self.manual_file = button('导出手工导入文件',lambda:self.export('manual'))
        for control in (self.check_file,self.takeover,self.manual_file):
            files.addWidget(control)
        layout.addLayout(files)
        binding = QHBoxLayout()
        self.lookup = button('核对已有聚水潭订单',self.preview)
        self.details = button('查看逐商品核对',self.show_rows)
        self.bind = button('确认关联已核对订单',self.confirm_binding,True)
        for control in (self.lookup,self.details,self.bind):
            binding.addWidget(control)
        layout.addLayout(binding)
        layout.addStretch()
        layout.addWidget(button('关闭',self.accept))
        self.checked = None
        self.online.currentIndexChanged.connect(self.reload)
        self.reload()

    def reload(self):
        self.checked = None
        self.row = manual.record(self.path,self.online.currentText())
        h=self.row['payload']['head']
        proof=self.row['proof'] or {}
        receipt=proof.get('import_receipt')
        handoff, proposal, bound=(proof.get(key) for key in ('manual_handoff','manual_proposal','manual_binding'))
        mode='物料单' if h.get('kind')=='material' else '补货单'
        self.scope.setText(f"{h.get('name') or h['store']} · {h['warehouse']} · {mode} · {h['number']} · V{h['revision']:03d}")
        stage=store.display_stage(self.row['stage'],receipt)
        values=[f"{store.STAGES[stage]} · 执行轮次 {self.row['gid']}",
                f"内部单号 {self.row['internal'] or '未关联'}",
                store.display_reason(stage,self.row['reason'],receipt)]
        binding=(proof.get('binding') or {})
        if binding.get('company'):values.append(f"原仓账号 {binding['company']} · 店铺 {binding.get('shop','')}")
        if handoff:values.append(f"人工接管 · {handoff['operator']} · {handoff['at']} · {handoff['reason']}")
        if receipt and receipt.get('fail_count'):values.append('原回执未提供 SKU 级失败行，保留整单失败结果和原导入文件。')
        if bound or proposal:
            checked=bound or proposal
            if bound:checked={'evidence':bound,'existing':True}
            self.checked={**checked,'differences':manual.difference_rows(self.row,checked['evidence']['rows'])}
            values.append('已保存人工关联' if bound else '已有核对草稿，确认前会再读原单')
        self.note.setText('\n'.join(str(value) for value in values if value))
        live=not self.row.get('historical') and self.row['stage']!='CANCELLED'
        self.check_file.setEnabled(bool(self.row.get('file_sha')))
        self.takeover.setEnabled(live and not handoff and manual._ready_for_handoff(self.row))
        self.manual_file.setEnabled(live and bool(handoff) and not (proposal or self.row['internal']))
        self.lookup.setEnabled(live)
        self.details.setEnabled(bool(self.checked))
        self.bind.setEnabled(live and bool(proposal) and not bound and not self.row['internal']
                             and proposal['sequence']==self.row['sequence'])

    def owner(self):
        from catalog_session_ui import manager
        return manager(self.host).session

    def action(self, title, function, args):
        try:
            return run(self,function,args,title)
        except Cancelled:
            self.note.setText('本次查询或导出已停止，请从原记录继续。')
        except (RuntimeError, ValueError, OSError) as exc:
            QMessageBox.warning(self,title,str(exc))
        return None

    def export(self,usage):
        h=self.row['payload']['head']
        path=export_target(self,h['number'],'聚水潭','.xls',usage)
        if path:
            receipt=self.action('导出聚水潭原导入单',manual.export_original,(self.path,self.row['online'],path,usage))
            if receipt:self.note.setText('聚水潭原件已保存 '+receipt['path'])

    def take_over(self):
        values=operator_input(self,'接管聚水潭导入','系统先查原线上单号。确认没有原单后，手工导入文件从本任务导出。')
        if values:
            _,operator,reason=values
            if self.action('核实原单并登记人工接管',manual.take_over,
                           (self.path,self.row['online'],self.row['sequence'],operator,reason,self.owner())):
                self.reload()

    def preview(self):
        values=operator_input(self,'核对已有聚水潭订单','按上方原线上单号核对原仓账号、店铺和完整明细。已有内部单号可填写以便交叉核对。',
                              order_label='内部单号（可留空）')
        if values:
            internal,operator,reason=values
            result=self.action('读取已有聚水潭订单',manual.preview,
                               (self.path,self.row['online'],self.row['sequence'],internal,operator,reason,self.owner()))
            if result:
                self.reload()
                self.checked=result
                self.details.setEnabled(True)
                self.show_rows()

    def show_rows(self):
        checked=self.checked
        if not checked:return
        evidence=checked['evidence']
        rows=[]
        for row in checked['differences']:
            actual=row['new'] or {}
            rows.append([row['sku'],actual.get('unit'),row['old_quantity'],actual.get('quantity'),row['old_price'],
                         actual.get('price'),actual.get('amount'),row['change']])
        rows_dialog(self,'核对聚水潭实际商品',
                    f"{evidence['online']} · 内部单号 {evidence['internal']} · {evidence['status']} · 应付 {evidence['totals']['payable']} 元",
                    ['商品编码','数量单位','原数量','平台数量','原单价','成交单价','行金额','差异'],rows)

    def confirm_binding(self):
        p=self.checked
        if not p or p.get('existing'):return
        e=p['evidence']
        changed=sum(r['change']!='一致' for r in p['differences'])
        if not confirm(self,'确认关联聚水潭原单',
                       f"仓单 {self.row['payload']['head']['number']}\n聚水潭 {e['internal']} · {e['status']}\n"
                       f"{len(e['rows'])} 款 · 应付 {e['totals']['payable']} 元 · {changed} 款有差异\n保存实际平台明细和原单关联。"):
            return
        result=self.action('再次核对并关联原单',manual.confirm,
                           (self.path,self.row['online'],p['id'],p['digest'],self.owner()))
        self.reload()
        if result:self.note.setText(self.note.text()+'\n已关联 '+result['internal'])


def open_manual_order(parent, host, orders):
    if not orders:
        QMessageBox.information(parent,'聚水潭人工处理','所选签发修订未留存聚水潭自动下单任务。')
        return
    try:
        dialog=ManualOrderDialog(parent,host,orders)
        dialog.exec()
    except (ValueError, RuntimeError, OSError) as exc:
        QMessageBox.warning(parent,'无法打开原导入任务',str(exc))
