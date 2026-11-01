"""Operator-confirmed existing purchase association, sharing the parent's component session."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QGroupBox, QMessageBox
from ui_controls import button, label
from aoxiang_directory import Directory
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_receipts import checked_json

BINDING_STATES = {'RUNNING':'正在核对原采购单', 'READY':'核对一致，尚未确认关联', 'PAUSED':'核对已停止，可恢复原任务',
                  'FAILED':'核对未通过，原件与任务保留', 'CONFIRMED':'已确认关联原采购单',
                  'SUPERSEDED':'原采购任务已更新；请点击“刷新本单记录”继续原任务'}


class BindingActions:
    def setup_binding(self, layout):
        self.binding_record = None
        group=QGroupBox('已有平台采购单 · 核对并关联')
        v=QVBoxLayout(group);v.addWidget(label('仅建立本地关系，不上传、提交或修改平台采购；无回执时必须核对本次标记下唯一采购单。','muted'))
        row=QHBoxLayout()
        self.binding_start_btn=button('核对已有采购单',self.binding_start)
        self.binding_read_btn=button('读取原关联核对状态',self.binding_refresh)
        self.binding_details_btn=button('查看逐商品核对',self.binding_details)
        self.binding_confirm_btn=button('确认关联已核对采购单',self.binding_confirm)
        for b in (self.binding_start_btn,self.binding_read_btn,self.binding_details_btn,self.binding_confirm_btn):row.addWidget(b)
        v.addLayout(row);self.binding_note=label('尚无关联核对；正常上传成功不需要此操作。','muted');self.binding_note.setWordWrap(True);v.addWidget(self.binding_note);layout.addWidget(group)

    def binding_active(self):return bool((self.binding_record or {}).get('active'))

    def restore_binding(self, record):
        proposal=(record or {}).get('bindingProposal')
        self.binding_record={'purchase_id':record['id'],'sequence':record['sequence'],'proposal':proposal,'active':proposal.get('state')=='RUNNING'} if proposal else None
        if self.binding_active():self.poll.start()

    def update_binding_buttons(self):
        idle=not self.worker and not self._closing
        r=self.current or {};p=(self.binding_record or {}).get('proposal') or {}
        active=self.binding_active() or bool(r.get('active')) or bool(self.template_id)
        eligible=r.get('state') in ('RESERVED','PAUSED','FAILED_PRECHECK','NEEDS_REVIEW') and not r.get('taskId') and not r.get('additionalTaskIds') and p.get('state')!='SUPERSEDED'
        available=bool(self.package) and not self.head.get('deleted')
        self.binding_start_btn.setEnabled(idle and not active and eligible and available)
        self.binding_read_btn.setEnabled(idle and not r.get('active') and bool(r))
        self.binding_details_btn.setEnabled(idle and bool(self.package) and p.get('state') in ('READY','CONFIRMED'))
        self.binding_confirm_btn.setEnabled(idle and not active and eligible and available and p.get('state')=='READY')
        text=BINDING_STATES.get(p.get('state'),'尚无关联核对；正常上传成功不需要此操作。')
        if p.get('request'):
            request=p['request'];text+=' · '+request['order_id']+' · 平台制单日 '+request['created_day']+'\n核对原因：'+request['reason']
        if p.get('summary'):text+='\n平台状态：'+p['summary']['status']+'；逐商品数量金额一致。'
        if p.get('error'):text+='\n'+p['error']['message']
        self.binding_note.setText(text)

    def binding_start(self):
        dialog=QDialog(self);dialog.setWindowTitle('核对已有翱象采购单');dialog.resize(590,280)
        v=QVBoxLayout(dialog);v.addWidget(label('请从翱象核对准确单号及制单日；本次只查询并取得原件，核对后还需确认关联。'))
        form=QFormLayout();order=QLineEdit();day=QLineEdit();reason=QLineEdit()
        order.setPlaceholderText('必填，精确采购单号');day.setPlaceholderText('必填，平台制单日 YYYY-MM-DD');reason.setPlaceholderText('必填，例如上传回执丢失，已在平台核实')
        for text,field in [('采购单号（必填）',order),('平台制单日（必填）',day),('核对原因（必填）',reason)]:form.addRow(text,field)
        v.addLayout(form);row=QHBoxLayout();row.addWidget(button('开始核对',dialog.accept));cancel=button('取消',dialog.reject);cancel.setDefault(True);row.addWidget(cancel);v.addLayout(row)
        accepted=dialog.exec();request={'order_id':order.text().strip(),'created_day':day.text().strip(),'reason':reason.text().strip()};dialog.deleteLater()
        if accepted!=QDialog.DialogCode.Accepted:return
        self.start_binding_request(request)

    def start_binding_request(self, request):
        package=self.package;id=self.current['id']
        def operation(_):
            validate_for_upload(self.host.work_repository.path,package,self.client.request('configuration'),Directory(self.folder).suppliers())
            return self.client.request('binding.start',purchase_id=id,binding_request=request)
        self.run(operation,self.binding_status)

    def binding_refresh(self):
        if self.current:self.run(lambda _:self.client.request('binding.status',purchase_id=self.current['id']),self.binding_status)

    def binding_status(self, result):
        if not self.current or result['purchase_id']!=self.current['id']:return
        if self.binding_record and result['sequence']<self.binding_record['sequence']:return
        self.binding_record=result
        self.status.setText(result.get('stage') or BINDING_STATES.get((result.get('proposal') or {}).get('state'),'原关联状态已读取'))
        if result.get('active'):self.poll.start()
        else:self.poll.stop()
        self.update_buttons()

    def binding_details(self):
        p=(self.binding_record or {})['proposal'];manifest=p['manifest']
        try:
            from business_ui import run,grid
            proof=run(self,lambda progress,cancel:checked_json(manifest['file'],manifest['sha256']),(),'读取已核对采购原件')
            expected={r['sku']:r for r in self.package['intent']['rows']}
            rows=[[r['sku'],expected[r['sku']]['name'],expected[r['sku']]['quantity'],r['quantity'],expected[r['sku']]['amount'],r['amount'],r['status']] for r in proof['evidence']['rows']]
            d=QDialog(self);d.resize(1260,650);d.setWindowTitle('逐商品关联核对 · '+p['request']['order_id']);v=QVBoxLayout(d)
            v.addWidget(label(self.head['name']+' · '+self.head['warehouse']+' · '+self.head['number'],'section'))
            v.addWidget(label('平台制单日 '+p['request']['created_day']+'；原件观察时间 '+proof['evidence']['observedAt']+'。只读核对，不调整付款或采购。','muted'))
            search=QLineEdit();search.setPlaceholderText('查找商品编码或名称');v.addWidget(search)
            table,model=grid(d,['商品编码','商品名称','付款基础数量','采购基础数量','付款商品金额','采购商品金额','平台状态'],rows)
            for column,width in enumerate((220,240,130,130,130,130,140)):table.setColumnWidth(column,width)
            search.textChanged.connect(model.filter);v.addWidget(table,1);v.addWidget(button('关闭',d.accept));d.exec();d.deleteLater()
        except Exception as exc:QMessageBox.warning(self,'未打开关联核对',str(exc))

    def binding_confirm(self):
        record=self.binding_record;p=record['proposal'];package=self.package
        id,proposal_id,sequence,sha=record['purchase_id'],p['id'],record['sequence'],p['manifest']['sha256']
        text=f"{self.head['name']} · {self.head['warehouse']}\n本地补货单：{self.head['number']}\n关联翱象采购单：{p['request']['order_id']}\n平台状态：{p['summary']['status']}\n{p['summary']['rows']}款 · 数量{p['summary']['quantity']} · 金额{p['summary']['amount']}元\n原因：{p['request']['reason']}\n\n只建立本地关联，不代表重新下单或已收货；不会修改原签发及付款。"
        if QMessageBox.question(self,'确认关联已有采购单',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        def operation(_):
            validate_for_upload(self.host.work_repository.path,package,self.client.request('configuration'),Directory(self.folder).suppliers())
            return self.client.request('binding.confirm',purchase_id=id,proposal_id=proposal_id,sequence=sequence,manifest_sha256=sha)
        self.run(operation,self.purchase_status)
