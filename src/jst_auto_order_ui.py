"""Qt selection, confirmation and task results for 聚水潭 automatic orders."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout,
    QLineEdit, QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout)

from business_ui import grid, run
from interaction_ui import confirm
from app_defaults import WAREHOUSES
import jst_auto_order as service
from jst_auto_order_store import RELEASED, list_orders, save_profiles
from jst_auto_order_template import CONTACT
import jst_delivery_profiles as delivery
import jst_delivery_profiles_xlsx as delivery_xlsx
from runtime_support import Cancelled
from ui_controls import button, label


def notify(parent, fn):
    try:
        return fn()
    except Cancelled:
        return None
    except (ValueError, RuntimeError, OSError) as exc:
        QMessageBox.warning(parent,'聚水潭自动下单',str(exc))


class DeliveryReview(QDialog):
    def __init__(self, parent, names, changes):
        super().__init__(parent)
        self.setWindowTitle('核对配送资料变更')
        self.resize(1060,460)
        root=QVBoxLayout(self)
        root.addWidget(label(f'共 {len(changes)} 份门店仓资料有修改。请核对后保存。'))
        rows=[]
        for change in changes:
            code=change['store']
            for field,(_,title) in CONTACT.items():
                before,after=change['before'][field],change['after'][field]
                if before!=after:
                    rows.append((change['row'],names.get(code) or code,code,change['warehouse'],title,before or '—',after or '—'))
        table,_=grid(self,('Excel 行','门店','门店编码','供货仓','字段','原值','新值'),rows)
        for i,width in enumerate((72,156,150,88,120,235,235)):
            table.setColumnWidth(i,width)
        root.addWidget(table,1)
        controls=QHBoxLayout()
        controls.addWidget(button('取消',self.reject))
        controls.addWidget(button('确认批量保存',self.accept,True))
        root.addLayout(controls)


class Profiles(QDialog):
    def __init__(self,parent,host,known,data,first=None):
        super().__init__(parent)
        self.host=host
        self.path=host.work_repository.path
        self.known=known
        self.records=data['records']
        self.keys=[(code,warehouse) for code in sorted(data['names']) for warehouse in WAREHOUSES]
        self.setWindowTitle('聚水潭买家和收货资料')
        self.resize(690,440)
        root=QVBoxLayout(self)
        hint=label('下载空白模板或导出已有资料，填写后批量导入。空白字段保留原值。','muted')
        hint.setWordWrap(True)
        root.addWidget(hint)
        self.scope=QComboBox()
        for code,warehouse in self.keys:
            name=data['names'][code]
            display=f'{name} ({code})' if name else code
            self.scope.addItem(f'{display} / {warehouse}')
        if first in self.keys:
            self.scope.setCurrentIndex(self.keys.index(first))
        root.addWidget(self.scope)
        form=QFormLayout()
        self.fields={}
        for key,(_,name) in CONTACT.items():
            field=QLineEdit()
            field.setMaxLength(delivery.ADDRESS_LIMIT if key=='address' else delivery.TEXT_LIMIT)
            self.fields[key]=field
            form.addRow(name,field)
        root.addLayout(form)
        self.status=label('','muted')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        actions=QHBoxLayout()
        actions.addWidget(button('下载导入模板',self.template_file))
        export_btn=button('导出两仓配送资料表',self.export_file)
        import_btn=button('批量导入配送资料',self.import_file)
        actions.addWidget(export_btn)
        actions.addWidget(import_btn)
        root.addLayout(actions)
        row=QHBoxLayout()
        row.addWidget(button('聚水潭店铺名称维护',self.open_shop_names))
        row.addStretch()
        save_btn=button('保存本仓收货资料',self.save,True)
        row.addWidget(save_btn)
        row.addWidget(button('关闭',self.reject))
        root.addLayout(row)
        self.scope.currentIndexChanged.connect(self.reload)
        if self.keys:
            self.reload()
            if data['warning']:
                self.status.setText(data['warning'])
        else:
            for widget in (self.scope,export_btn,import_btn,save_btn,*self.fields.values()):
                widget.setEnabled(False)
            self.status.setText('可先下载导入模板。维护门店目录后，按门店编码批量导入。')

    def key(self):
        return self.keys[self.scope.currentIndex()]

    def reload(self):
        values=(self.records.get(self.key()) or {}).get('values',{})
        for name,field in self.fields.items():
            field.setText(values.get(name,''))
        self.status.setText('当前门店仓的收货资料')

    def save(self):
        key=self.key()
        values={name:field.text() for name,field in self.fields.items()}
        expected=(self.records.get(key) or {}).get('version')
        result=notify(self,lambda:run(self,delivery.save_profile,(self.path,*key,values,expected),'保存收货资料'))
        if result:
            self.records[key]=result
            self.reload()
            self.status.setText('已保存当前门店仓的收货资料')

    def export_file(self):
        path,_=QFileDialog.getSaveFileName(self,'导出两仓配送资料表','聚水潭配送资料维护.xlsx','Excel (*.xlsx)')
        if path:
            self.export_path(path if Path(path).suffix else path+'.xlsx')

    def template_file(self):
        path,_=QFileDialog.getSaveFileName(self,'下载配送资料导入模板','聚水潭配送资料导入模板.xlsx','Excel (*.xlsx)')
        if path:
            self.template_path(path if Path(path).suffix else path+'.xlsx')

    def template_path(self,path):
        result=notify(self,lambda:run(self,delivery_xlsx.download_template,(path,),'下载配送资料导入模板'))
        if result:
            self.status.setText('已保存空白导入模板，请按“填写说明”录入配送资料。')
        return bool(result)

    def export_path(self,path):
        def export():
            data=run(self,delivery.directory,(self.path,self.known),'读取配送资料')
            return run(self,delivery_xlsx.export,(path,data),'导出两仓配送资料表')

        if notify(self,export):
            self.status.setText('已导出两仓配送资料表，修改后可批量导入。')
            return True
        return False

    def import_file(self):
        path,_=QFileDialog.getOpenFileName(self,'导入配送资料表','','Excel (*.xlsx)')
        if path:
            self.import_path(path)

    def import_path(self,path):
        old=(self.records.get(self.key()) or {}).get('values',{})
        if any(field.text()!=old.get(key,'') for key,field in self.fields.items()):
            self.status.setText('请先保存当前表单，或切换门店仓后再导入。')
            return False

        def import_changes():
            data=run(self,delivery.directory,(self.path,self.known),'读取当前配送资料')
            changes=run(self,delivery_xlsx.plan,(path,data),'核对配送资料表')
            if getattr(self.host,'closing',False):
                return None
            if not changes:
                self.status.setText('表格没有需要保存的配送资料变更。')
                return None
            dialog=DeliveryReview(self,data['names'],changes)
            try:
                accepted=dialog.exec()==QDialog.DialogCode.Accepted
            finally:
                dialog.deleteLater()
            if not accepted or getattr(self.host,'closing',False):
                return None
            saved=run(self,save_profiles,(self.path,changes),'保存本批配送资料')
            return {**data['records'],**saved},len(saved)

        result=notify(self,import_changes)
        if result:
            self.records,count=result
            self.reload()
            self.status.setText(f'已批量保存 {count} 份门店仓配送资料。')
        return bool(result)

    def open_shop_names(self):
        from payment_shop_names_ui import open_names
        open_names(self,self.host)


def profiles(parent,host,heads=()):
    if getattr(host,'closing',False):
        return
    known={code:state.get('name',code) for code,state in getattr(host,'states',{}).items()}
    for head in heads:
        known[head['store']]=head.get('name') or known.get(head['store'],'')
    data=notify(parent,lambda:run(parent,delivery.directory,(host.work_repository.path,known),'读取门店与配送资料'))
    if data is None or getattr(host,'closing',False):
        return
    first=(heads[0]['store'],heads[0]['warehouse']) if heads else None
    dialog=Profiles(parent,host,known,data,first)
    try:
        dialog.exec()
    finally:
        dialog.deleteLater()


class Orders(QDialog):
    def __init__(self,parent,host,focus=(),failures=()):
        super().__init__(parent)
        self.path=host.work_repository.path
        self.host=host
        self.focus=list(focus)
        self.records=[]
        self.busy=False
        self.initial=True
        self.redo_target=None
        self.setWindowTitle('聚水潭自动下单')
        self.resize(1280,580)
        pane=QVBoxLayout(self)
        pane.addWidget(label('先平台预览，再确认导入。已受理的仓单可核对原单，查回后自动准备付款资料。'))
        pane.addWidget(label('聚水潭的「已付款待审核」表示导入完成。实际付款由业务在付款核对中确认。','muted'))
        headers=('选择','门店','仓','补货单号','聚水潭店铺','款数','数量','分配金额','平台预览金额','线上单号','内部单号','下单状态','付款资料','处理提示')
        self.table=QTableWidget(0,len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setSortingEnabled(False)
        self.table.itemChanged.connect(self.controls)
        pane.addWidget(self.table,1)
        select=QHBoxLayout()
        select.addWidget(button('勾选当前列表',lambda:self.select(True)))
        select.addWidget(button('清空勾选',lambda:self.select(False)))
        select.addWidget(button('刷新任务',self.reload))
        self.allocation=button('补取付款资料并核对',self.pull_allocation)
        select.addWidget(self.allocation)
        self.payment_history=button('历次付款资料',self.show_payment_history)
        select.addWidget(self.payment_history)
        import jst_fulfillment_jobs as fulfillment_jobs
        from PySide6.QtWidgets import QCheckBox
        self.auto_allocation=QCheckBox('检查历史出库资料')
        self.auto_allocation.setChecked(fulfillment_jobs.enabled(self.path))
        self.auto_allocation.toggled.connect(self.configure_allocation)
        select.addWidget(self.auto_allocation)
        pane.addLayout(select)
        self.selection_hint=label('','muted')
        pane.addWidget(self.selection_hint)
        self.note=label('\n'.join(e['order']+'：'+e['reason'] for e in failures),'muted')
        self.note.setWordWrap(True)
        pane.addWidget(self.note)
        row=QHBoxLayout()
        self.buttons={}
        for action,title in (('probe','核对平台账号和店铺'),('preview','平台预览'),
                ('submit','确认导入'),('recover','核对原单')):
            control=button(title,lambda checked=False,a=action:self.perform(a),action=='submit')
            self.buttons[action]=control
            row.addWidget(control)
        self.export=button('保存核对用 XLS',self.save_file)
        row.addWidget(self.export)
        self.redo_btn=button('核对取消并重做',self.redo)
        row.addWidget(self.redo_btn)
        self.manual_btn=button('人工处理',self.open_manual)
        row.addWidget(self.manual_btn)
        row.addStretch()
        row.addWidget(button('关闭',self.reject))
        pane.addLayout(row)
        self.reload()

    def reload(self):
        chosen={r['online'] for r in self.selected()}
        all_rows=list_orders(self.path)
        keyed={r['online']:r for r in all_rows}
        self.records=[keyed[key] for key in self.focus if key in keyed] if self.focus else all_rows
        from contextlib import closing
        import jst_fulfillment_store as fulfillment
        import work_repository as wr
        with closing(wr.read_connection(self.path)) as db:
            allocation={r['online']:fulfillment.current(db,r['online']) for r in self.records}
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.records))
        for i,r in enumerate(self.records):
            item=QTableWidgetItem()
            item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if r['online'] in chosen or self.initial and self.focus else Qt.CheckState.Unchecked)
            self.table.setItem(i,0,item)
            state=allocation[r['online']]
            labels={'READY':'待人工确认付款','WAIT_ALLOCATION':'等待实配','CANCELLED':'原单取消',
                    'WAIT_CONNECTION':'等待仓连接','SOURCE_UNVERIFIED':'来源待核','NEEDS_REVIEW':'付款资料待核',
                    'RETRY_PAYMENT_READ':'等待补取付款资料','PAYMENT_SOURCE_PENDING':'平台原单待核',
                    'MATCHES_PAYMENT':'沿用已确认付款'}
            values=(r['name'] or r['store'],r['warehouse'],r['number'],r['shop'],r['sku_count'],r['quantity'],
                    r['amount'],r['preview_amount'],r['online'],r['internal'],r['stage_label'],
                    labels.get((state or {}).get('state'),'尚未拉取'),
                    r['reason'] if r['stage'] in ('IMPORT_UNKNOWN','IMPORT_FAILED') else (state or {}).get('message') or r['reason'])
            for j,value in enumerate(values,1):
                cell=QTableWidgetItem(str(value if value is not None else '—'))
                if j==13:
                    cell.setToolTip(cell.text())
                cell.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable)
                self.table.setItem(i,j,cell)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(13,330)
        self.table.blockSignals(False)
        self.initial=False
        self.controls()

    def selected(self):
        return [r for i,r in enumerate(self.records) if self.table.item(i,0) and self.table.item(i,0).checkState()==Qt.CheckState.Checked]

    def select(self,wanted):
        for i in range(self.table.rowCount()):
            self.table.item(i,0).setCheckState(Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)

    def eligible(self,rows,action):
        if action=='preview':
            return [r for r in rows if r['stage'] in ('PREPARED','PREVIEWED','IMPORT_UNKNOWN','IMPORT_FAILED')
                    and not (r['historical'] or r['internal'] or r['import_outcome']=='accepted')]
        if action=='submit':
            return [r for r in rows if r['stage']=='PREVIEWED' and not (r['historical'] or r['internal'])]
        if action=='recover':
            return [r for r in rows if r['stage'] in RELEASED and r['stage']!='MANUAL_HANDOFF']
        return rows

    def controls(self,*_):
        rows=self.selected()
        stages={r['stage'] for r in rows}
        free=not self.busy and bool(rows)
        self.buttons['probe'].setEnabled(free and 'MANUAL_HANDOFF' not in stages and not any(r.get('historical') for r in rows))
        for action in ('preview','submit','recover'):
            self.buttons[action].setEnabled(free and bool(self.eligible(rows,action)))
        counts=[len(self.eligible(rows,a)) for a in ('preview','submit','recover')]
        hint=f'已勾选 {len(rows)} 张，可平台预览 {counts[0]} 张，可确认导入 {counts[1]} 张，可核对原单 {counts[2]} 张'
        if any(r['stage']=='IMPORT_UNKNOWN' and r['import_outcome'] is None for r in rows):
            hint+='。上次发送结果不明的仓单只可预览、核对'
        self.selection_hint.setText(hint)
        self.export.setEnabled(free and len(rows)==1 and not stages&RELEASED)
        self.export.setText('保存历史原文件' if stages=={'WITHDRAWN'} else '保存核对用 XLS')
        self.allocation.setEnabled(free and all(r['internal'] and r['stage']!='CANCELLED' for r in rows))
        self.payment_history.setEnabled(free and len(rows)==1 and bool(rows[0]['internal']))
        self.redo_btn.setEnabled(free and len(rows)==1 and 'MANUAL_HANDOFF' not in stages
                                  and not rows[0].get('historical'))
        self.manual_btn.setEnabled(free and len(rows)==1 and 'WITHDRAWN' not in stages)

    def open_manual(self):
        rows=self.selected()
        if not self.busy and len(rows)==1:
            from jst_manual_order_ui import open_manual_order
            open_manual_order(self,self.host,[rows[0]['online']])
            self.reload()

    def redo(self):
        rows = self.selected()
        if self.busy or len(rows) != 1 or rows[0].get('historical'):
            return
        row = rows[0]
        order_label = f"{row['number']} · {row['warehouse']}"
        from catalog_session_ui import manager
        from jst_cancellation import begin_redo
        self.busy = True
        self.controls()
        self.note.setText(order_label+' · 正在核实聚水潭原单是否取消…')
        try:
            owner = manager(self.host).session
            result = run(self,service.run_browser,
                         (self.path,[row['online']],owner,'recover_cancelled'),'核实聚水潭原单是否取消')
            if not result:
                raise service.Check('未取得原单回查结果，请继续核对原任务。')
            checked = result[0]
            if not checked['ok'] or checked['stage'] != 'CANCELLED':
                raise service.Check('尚未核实原单已取消。\n'+checked['reason'])
            self.note.setText(checked['reason'])
            if not confirm(self,'确认沿原仓单重做',
                           f"{order_label} · 聚水潭 {checked['internal']} 已核实取消。\n"
                           '确认后按原记录释放占用，保留历史，并返回本仓单。点「准备新分配批次」按仓刷新库存。'):
                return
            redo = run(self,begin_redo,(self.path,row['online']),'登记后继执行并核销原占用')
            if not redo:
                raise service.Check('未收到重做结果，请刷新任务并核对原记录。')
            self.redo_target = redo['head']
            self.accept()
        except Cancelled:
            self.note.setText(order_label+' · 本次操作已取消。')
        except (ValueError, RuntimeError, OSError) as exc:
            detail = f"{order_label}\n聚水潭线上单号 {row['online']}\n{exc}"
            self.note.setText(detail)
            QMessageBox.warning(self,'取消重做未完成',detail)
        finally:
            self.busy = False
            if self.redo_target is None:
                self.reload()

    def show_payment_history(self):
        from contextlib import closing
        from jst_order_payment import KIND, LABEL, LEGACY_KIND, LEGACY_LABEL
        from jst_fulfillment_store import history
        from readable_ui import details
        from work_repository import read_connection
        rows = self.selected()
        if self.busy or len(rows) != 1:
            return
        with closing(read_connection(self.path)) as db:
            records = history(db, rows[0]['online'])
        details(self, '原单的历次付款资料', ['原件保存时间','来源','商品数量','商品金额','摘要'],
                [[saved, {KIND: LABEL, LEGACY_KIND: LEGACY_LABEL}.get(kind, '聚水潭销售出库或历史实配明细'), quantity, amount, digest]
                 for saved,kind,quantity,amount,digest in records])

    def pull_allocation(self):
        rows=self.selected()
        if self.busy or not rows:return
        self.busy=True;self.controls()
        try:
            from jst_fulfillment_ui import pull_and_review
            receipt=notify(self,lambda:pull_and_review(self,self.host,[r['online'] for r in rows]))
            if receipt is not None:
                self.note.setText(f'付款核对完成 {len(receipt)} 单。')
        finally:
            self.busy=False;self.reload()

    def configure_allocation(self,enabled):
        import jst_fulfillment_jobs as jobs
        result=notify(self,lambda:run(self,jobs.configure,(self.path,enabled),'保存实配自动检查设置'))
        if result is None:
            self.auto_allocation.blockSignals(True)
            self.auto_allocation.setChecked(jobs.enabled(self.path))
            self.auto_allocation.blockSignals(False)
        runner=getattr(self.host,'jst_fulfillment_runner',None)
        if runner and result is not None:
            runner.configured(result)

    def save_file(self):
        r=self.selected()[0]
        suffix = '_撤销历史' if r['stage']=='WITHDRAWN' else ''
        dest,_=QFileDialog.getSaveFileName(self,self.export.text(),r['online']+suffix+'.xls','Excel 97-2003 (*.xls)')
        if dest:
            target=Path(dest)
            if target.suffix.lower()!='.xls':target=target.with_suffix('.xls')
            notify(self,lambda:run(self,service.export_file,(self.path,r['online'],target),'保存核对文件'))

    def perform(self,action):
        rows=self.eligible(self.selected(),action)
        if self.busy or not rows or not self.buttons[action].isEnabled():
            return
        approved={r['online']:r['sequence'] for r in rows}
        if action=='submit':
            lines='\n'.join(f"{r['name'] or r['store']} / {r['warehouse']} / {r['number']} / {r['online']} / "
                            f"{r['sku_count']}款、{r['quantity']}件 / 预览金额 {r['preview_amount'] or '待核'}" for r in rows)
            if not confirm(self,'确认导入',f'确认导入以下 {len(rows)} 张仓单？\n\n'+lines):
                return
        from catalog_session_ui import manager
        owner=manager(self.host).session
        self.busy=True
        self.controls()
        try:
            from jst_fulfillment import run_order_actions
            result=notify(self,lambda:run(self,run_order_actions,
                (self.path,[r['online'] for r in rows],owner,action,approved),self.buttons[action].text()))
            if result is not None:
                replacements={r['replaces']:r['online'] for r in result if r.get('replaces')}
                self.focus=[replacements.get(key,key) for key in self.focus]
                self.note.setText('\n'.join(r['online']+'  '+r['reason'] for r in result))
        finally:
            self.busy=False
            self.reload()


def open_orders(parent,host,focus=(),failures=()):
    dialog=Orders(parent,host,focus,failures)
    try:
        dialog.exec()
        return dialog.redo_target
    finally:
        dialog.deleteLater()
