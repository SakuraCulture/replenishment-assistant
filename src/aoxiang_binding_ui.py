"""Operator-confirmed existing purchase association, sharing the parent's component session."""
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QGroupBox, QMessageBox
from ui_controls import button, label
from aoxiang_directory import Directory
from aoxiang_purchase_tasks import validate_for_upload
from aoxiang_receipts import checked_json
import aoxiang_manual_import as manual
from platform_import_ui import export_target, operator_input

BINDING_STATES = {'RUNNING':'正在核对原采购单', 'READY':'核对完成，尚未确认关联', 'PAUSED':'核对已停止，可恢复原任务',
                  'FAILED':'核对未通过，原件与任务保留', 'CONFIRMED':'已确认关联原采购单',
                  'STALE':'平台明细或本地依据已变化，请重新核对',
                  'SUPERSEDED':'原采购任务已更新；请点击“刷新本单记录”继续原任务'}


class BindingActions:
    def setup_binding(self, layout):
        self.binding_record = None
        group=QGroupBox('人工处理 · 原导入单与平台采购关联')
        v=QVBoxLayout(group)
        self.manual_note=label('导出核对用原件，或确认接管后导出供业务手工导入。','muted')
        self.manual_note.setWordWrap(True);v.addWidget(self.manual_note)
        row=QHBoxLayout()
        self.check_export_btn=button('导出本次导入单（核对用）',lambda:self.export_import('check'))
        self.handoff_btn=button('接管本次导入',self.take_over_import)
        self.manual_export_btn=button('导出手工导入文件',lambda:self.export_import('manual'))
        self.failure_rows_btn=button('查看失败明细',self.show_failure_rows)
        self.failure_export_btn=button('导出失败原件',lambda:self.export_import('failure'))
        for b in (self.check_export_btn,self.handoff_btn,self.manual_export_btn,self.failure_rows_btn,self.failure_export_btn):
            row.addWidget(b)
        v.addLayout(row)
        row=QHBoxLayout()
        self.binding_start_btn=button('核对已有采购单',self.binding_start)
        self.binding_read_btn=button('读取原关联核对状态',self.binding_refresh)
        self.binding_details_btn=button('查看逐商品核对',self.binding_details)
        self.binding_confirm_btn=button('确认关联已核对采购单',self.binding_confirm)
        for b in (self.binding_start_btn,self.binding_read_btn,self.binding_details_btn,self.binding_confirm_btn):row.addWidget(b)
        v.addLayout(row)
        row=QHBoxLayout()
        self.amend_payment_btn=button('手动更正付款依据',self.amend_payment)
        self.remainder_btn=button('核对平台差额',self.compare_remainder)
        row.addWidget(self.amend_payment_btn);row.addWidget(self.remainder_btn);v.addLayout(row)
        self.binding_note=label('尚无关联核对；正常上传成功不需要此操作。','muted');self.binding_note.setWordWrap(True);v.addWidget(self.binding_note);layout.addWidget(group)

    def binding_active(self):return bool((self.binding_record or {}).get('active'))

    def restore_binding(self, record):
        proposal=(record or {}).get('bindingProposal')
        self.binding_record={'purchase_id':record['id'],'sequence':record['sequence'],'proposal':proposal,'active':proposal.get('state')=='RUNNING'} if proposal else None
        if self.binding_active():self.poll.start()

    def update_binding_buttons(self):
        idle=not self.worker and not self._closing
        r=self.current or {};p=(self.binding_record or {}).get('proposal') or {}
        active=self.binding_active() or bool(r.get('active')) or bool(self.template_id)
        result=r.get('importResult') or {}
        settled=result.get('taskId')==r.get('taskId') and result.get('kind') in ('failed','partial','complete')
        unattempted=r.get('state') in ('RESERVED','PAUSED','FAILED_PRECHECK') and not r.get('taskId') and not r.get('uploadAttempted')
        handoff=r.get('manualHandoff')
        eligible=not r.get('binding') and not r.get('additionalTaskIds') and (p.get('state')!='SUPERSEDED' or settled) and (
            r.get('state')=='MANUAL_HANDOFF' or
            r.get('state') in ('RESERVED','PAUSED','FAILED_PRECHECK','NEEDS_REVIEW','ACCEPTED') and (not r.get('taskId') or settled))
        available=bool(self.package) and not self.head.get('deleted')
        self.check_export_btn.setEnabled(idle and not active and bool(r.get('originalFile')) and not self.head.get('deleted'))
        self.handoff_btn.setEnabled(idle and not active and available and eligible and not handoff
                                    and p.get('state')!='READY' and (unattempted or settled and result['kind']=='failed'))
        self.manual_export_btn.setEnabled(idle and not active and available and eligible and bool(handoff)
            and r.get('state')=='MANUAL_HANDOFF' and p.get('state') not in ('RUNNING','READY','STALE'))
        for btn in (self.failure_rows_btn,self.failure_export_btn):
            btn.setEnabled(idle and not active and bool(result.get('failureFile')))
        detail=''
        if handoff:detail=f"人工接管 · {handoff['operator']} · {handoff['at']}\n{handoff['reason']}"
        if result.get('kind') in ('partial','failed') and not result.get('failureFile'):
            detail+='\n平台失败行原件未留存，可在原任务核对计数与原因。'
        self.manual_note.setText(detail.strip() or '平台结果待核时可导出原件核对。接管需确认原范围内未建单。')
        self.binding_start_btn.setEnabled(idle and not active and eligible and available)
        self.binding_read_btn.setEnabled(idle and not r.get('active') and bool(r))
        self.binding_details_btn.setEnabled(idle and bool(self.package) and p.get('state') in ('READY','CONFIRMED'))
        self.binding_confirm_btn.setEnabled(idle and not active and eligible and available and p.get('state')=='READY')
        self.amend_payment_btn.setEnabled(idle and not active and not self.head.get('deleted'))
        self.remainder_btn.setEnabled(idle and not active and available and p.get('state')=='CONFIRMED'
                                      and bool((r.get('binding') or {}).get('manualRevision')))
        text=BINDING_STATES.get(p.get('state'),'尚无关联核对；正常上传成功不需要此操作。')
        if p.get('request'):
            request=p['request'];text+=' · '+request['order_id']+' · '+str(request['created_day'] or '按单号精查')+'\n核对依据 '+request['reason']
        if p.get('summary'):
            summary=p['summary'];differences=summary.get('differences',0)
            text+='\n平台状态 '+summary['status']+f' · {differences} 款与原预览不同'
            if summary.get('unverifiedUnits'):
                text+=f" · {summary['unverifiedUnits']} 款原基础单位未留存"
        if p.get('error'):text+='\n'+p['error']['message']
        self.binding_note.setText(text)

    def amend_payment(self):
        from order_amendments_ui import open_editor
        if open_editor(self,self.host,self.head,'payment'):
            self.load()

    def compare_remainder(self):
        import platform_remainders as service
        from platform_remainders_ui import show
        package=self.package;purchase_id=self.current['id']
        def operation(cancel):
            record=self.client.request('purchase.status',purchase_id=purchase_id)
            return service.aoxiang(self.host.work_repository.path,self.head,package,record,cancel=cancel)
        self.run(operation,lambda report:show(self,self.host,report))

    def binding_start(self):
        dialog=QDialog(self);dialog.setWindowTitle('核对已有翱象采购单');dialog.resize(590,280)
        v=QVBoxLayout(dialog);v.addWidget(label('填写准确采购单号即可精查，无需填写店名或日期；本次只查询原件，核对后确认关联。'))
        form=QFormLayout();order=QLineEdit();day=QLineEdit();reason=QLineEdit();operator=QLineEdit()
        order.setPlaceholderText('必填，精确采购单号');day.setPlaceholderText('可留空；仅平台要求时填已核对的原生制单日');reason.setPlaceholderText('必填，例如上传回执丢失，已在平台核实')
        for text,field in [('采购单号（必填）',order),('制单日（选填）',day),('经办人（必填）',operator),('核对依据（必填）',reason)]:form.addRow(text,field)
        v.addLayout(form);row=QHBoxLayout();row.addWidget(button('开始核对',dialog.accept));cancel=button('取消',dialog.reject);cancel.setDefault(True);row.addWidget(cancel);v.addLayout(row)
        accepted=dialog.exec();request={'order_id':order.text().strip(),'created_day':day.text().strip(),'operator':operator.text().strip(),'reason':reason.text().strip()};dialog.deleteLater()
        if accepted!=QDialog.DialogCode.Accepted:return
        self.start_binding_request(request)

    def start_binding_request(self, request):
        package=self.package;record=self.current;id=record['id']
        def operation(_):
            if 'operator' in request:
                from aoxiang_upload_pipeline import original_units
                from platform_import_files import author
                author(request['operator'],request['reason'])
                units=original_units(self.host.work_repository.path,{'head':self.head,'package':package},record)
                request.update(original_units={sku:unit.strip() for sku,unit in units.items()},host_facts=manual.basis(self,binding=True))
            else:
                validate_for_upload(self.host.work_repository.path,package,self.client.request('configuration'),Directory(self.folder).suppliers(),for_binding=True)
            return self.client.request('binding.start',purchase_id=id,binding_request=request)
        self.run(operation,self.binding_status)

    def take_over_import(self):
        values=operator_input(self,'接管翱象导入','先查原任务和本次来源标记。核实尚未建采购后，操作人可从本单导出手工导入文件。')
        if values:
            _,operator,reason=values
            self.run(lambda _:manual.handoff(self,self.current,operator,reason),self.purchase_status)

    def export_import(self, usage):
        target=export_target(self,self.head['number'],'翱象','.xlsx',usage)
        if target:
            def exported(receipt):
                self.status.setText('原件已保存 '+receipt['path'])
                self.pending_binding_refresh=True
            self.run(lambda _:manual.export_original(self,self.current['id'],usage,target),exported)

    def show_failure_rows(self):
        from platform_import_ui import rows_dialog
        self.run(lambda _:manual.failure_rows(self.client,self.current['id']),lambda rows: rows_dialog(
            self,'翱象失败明细',f"原任务 {self.current.get('taskId')} · {len(rows)} 行",
            ['工作表','原行号','平台原始字段'],rows))

    def binding_refresh(self):
        if self.current:self.run(lambda _:self.client.request('binding.status',purchase_id=self.current['id']),self.binding_status)

    def binding_status(self, result):
        if not self.current or result['purchase_id']!=self.current['id']:return
        if self.binding_record and result['sequence']<self.binding_record['sequence']:return
        self.binding_record=result
        self.status.setText(result.get('stage') or BINDING_STATES.get((result.get('proposal') or {}).get('state'),'原关联状态已读取'))
        if result.get('active'):self.poll.start()
        else:
            self.poll.stop()
            if ((result.get('proposal') or {}).get('request') or {}).get('operator'):
                self.pending_binding_refresh=True
        self.update_buttons()

    def binding_details(self):
        p=(self.binding_record or {})['proposal'];manifest=p['manifest']
        try:
            from business_ui import run,grid
            proof=run(self,lambda progress,cancel:checked_json(manifest['file'],manifest['sha256']),(),'读取已核对采购原件')
            expected={r['sku']:r for r in self.package['intent']['rows']}
            actual={r['sku']:r for r in proof['evidence']['rows']}
            request=proof.get('request',{})
            original_units=request.get('original_units',{})
            rows=[]
            from decimal import Decimal
            def differs(old,new,old_unit):
                fields=(('quantity','quantity'),('unitPrice','price'),('amount','amount'))
                return (old_unit is not None and old_unit!=new.get('unit')) or any(
                    Decimal(old[a])!=Decimal(new[b]) for a,b in fields if old.get(a) is not None and new.get(b) is not None)
            for sku in dict.fromkeys([*expected,*actual]):
                old,new=expected.get(sku,{}),actual.get(sku,{})
                old_unit=original_units.get(sku)
                if not old:state='新增'
                elif not new:state='缺行'
                elif differs(old,new,old_unit):state='变化'
                elif request.get('operator') and old_unit is None:state='原单位未留存'
                else:state='一致'
                rows.append([sku,old.get('name'),old_unit or ('未留存' if old else None),new.get('unit'),
                             old.get('quantity'),new.get('quantity'),
                             old.get('unitPrice'),new.get('price'),old.get('amount'),new.get('amount'),
                             state])
            d=QDialog(self);d.resize(1260,650);d.setWindowTitle('逐商品关联核对 · '+p['request']['order_id']);v=QVBoxLayout(d)
            v.addWidget(label(self.head['name']+' · '+self.head['warehouse']+' · '+self.head['number'],'section'))
            v.addWidget(label('平台制单日 '+str(p['request']['created_day'] or proof.get('nativeCreatedDay') or '未留存')+' · 原件观察时间 '+proof['evidence']['observedAt'],'muted'))
            search=QLineEdit();search.setPlaceholderText('查找商品编码或名称');v.addWidget(search)
            table,model=grid(d,['商品编码','商品名称','原基础单位','平台基础单位','预览数量','采购数量',
                                '预览采购价','平台采购价','预览金额','平台金额','差异'],rows)
            for column,width in enumerate((170,185,95,100,85,85,95,95,90,90,110)):table.setColumnWidth(column,width)
            search.textChanged.connect(model.filter);v.addWidget(table,1);v.addWidget(button('关闭',d.accept));d.exec();d.deleteLater()
        except Exception as exc:QMessageBox.warning(self,'未打开关联核对',str(exc))

    def binding_confirm(self):
        record=self.binding_record;p=record['proposal'];package=self.package
        id,proposal_id,sequence,sha=record['purchase_id'],p['id'],record['sequence'],p['manifest']['sha256']
        summary=p['summary']
        text=f"{self.head['name']} · {self.head['warehouse']}\n本地仓单 {self.head['number']}\n关联翱象采购单 {p['request']['order_id']}\n平台状态 {summary['status']}\n{summary['rows']} 款 · 数量 {summary['quantity']} · 金额 {summary['amount']} 元\n与预览不同 {summary.get('differences',0)} 款"
        if summary.get('unverifiedUnits'):text+=f"\n原基础单位未留存 {summary['unverifiedUnits']} 款"
        text+=f"\n核对依据 {p['request']['reason']}\n\n确认后保存平台采购实际明细与本地关联。"
        if QMessageBox.question(self,'确认关联已有采购单',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)!=QMessageBox.StandardButton.Yes:return
        def operation(_):
            if p['request'].get('operator'):
                extra={'host_facts':manual.basis(self,binding=True),'timeout':240}
            else:
                validate_for_upload(self.host.work_repository.path,package,self.client.request('configuration'),Directory(self.folder).suppliers(),for_binding=True)
                extra={}
            return self.client.request('binding.confirm',purchase_id=id,proposal_id=proposal_id,sequence=sequence,manifest_sha256=sha,**extra)
        self.run(operation,self.purchase_status)
