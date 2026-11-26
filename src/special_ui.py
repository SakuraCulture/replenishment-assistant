"""Desktop workbenches for store targets, opening packages and original purchase receipts."""
import csv
from datetime import datetime,timedelta
from decimal import Decimal
import getpass
import io
import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,
    QHeaderView,QInputDialog,QLabel,QLineEdit,QMessageBox,QPlainTextEdit,QPushButton,QSpinBox,QTabWidget,
    QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget)

import special_plans as sp
import special_reports
import special_schedule
import special_settlement as settle
import special_waves as waves


def button(layout,title,action):
    b=QPushButton(title)
    b.clicked.connect(action)
    layout.addWidget(b)
    return b


def table(titles):
    t=QTableWidget(0,len(titles))
    t.setHorizontalHeaderLabels(titles)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill(t,data):
    t.setUpdatesEnabled(False)
    try:
        t.setRowCount(len(data))
        for i,row in enumerate(data):
            for j,v in enumerate(row):
                t.setItem(i,j,QTableWidgetItem('' if v is None else str(v)))
    finally:
        t.setUpdatesEnabled(True)


def combo(values):
    c=QComboBox()
    c.addItems(values)
    return c


def file_in(parent,title,pattern='JSON 原件 (*.json)'):
    return QFileDialog.getOpenFileName(parent,title,'',pattern)[0]


def ask(parent,title,message):
    return QMessageBox.question(parent,title,message,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No)==QMessageBox.StandardButton.Yes


def read_file(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


class Editor(QDialog):
    COLUMNS=('store','sku','unit','target')
    def __init__(self,parent,original=None):
        super().__init__(parent)
        self.kind=parent.kind
        self.extra={}
        self.setWindowTitle('修订原主单' if original else '新建'+sp.KINDS[self.kind])
        self.resize(810,620)
        layout=QVBoxLayout(self)
        form=QFormLayout()
        self.name=QLineEdit()
        self.stores=QLineEdit()
        self.mode=combo(sp.MODES if self.kind=='policy' else ('固定新增',))
        self.strategy=combo(('覆盖优先','优先配齐'))
        self.start=QLineEdit(sp.now())
        self.end=QLineEdit((datetime.now(sp.BEIJING)+timedelta(days=7)).isoformat(timespec='seconds'))
        self.reason=QLineEdit('首次发布' if not original else '')
        self.wh=combo(('山东仓','河北仓'))
        self.account,self.shop,self.merchant,self.supplier=(QLineEdit() for _ in range(4))
        for label,widget in (('名称',self.name),('门店编码（按优先顺序，以逗号分隔）',self.stores),('目标模式',self.mode),
            ('缺货分配',self.strategy),('北京时间开始',self.start),('截止（不含）',self.end),('供货仓',self.wh),
            ('聚水潭企业编码',self.account),('聚水潭店铺编码',self.shop),('翱象商家编码',self.merchant),
            ('供应商编码',self.supplier),('发布／修订依据',self.reason)):
            form.addRow(label,widget)
        layout.addLayout(form)
        self.lines=QPlainTextEdit('store,sku,unit,target\n')
        self.lines.setPlaceholderText('每行填写门店、商品编码、库存单位、数量。空门店表示全部已选门店。')
        layout.addWidget(QLabel('货盘目标行（CSV，商品编码保留前导零）'))
        layout.addWidget(self.lines)
        row=QHBoxLayout()
        button(row,'读取方案文件',self.load)
        button(row,'导入货盘 CSV',self.load_csv)
        layout.addLayout(row)
        self.error=QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        box=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        box.button(QDialogButtonBox.StandardButton.Ok).setText('确认发布')
        box.accepted.connect(self.save)
        box.rejected.connect(self.reject)
        layout.addWidget(box)
        if original:
            self.populate(original)

    def load_csv(self):
        path=file_in(self,'读取货盘','CSV 货盘 (*.csv)')
        if path:
            try:self.lines.setPlainText(Path(path).read_text('utf-8-sig'))
            except Exception as exc:self.error.setText(str(exc))

    def load(self):
        path=file_in(self,'读取专项方案文件')
        if path:
            try:self.populate(read_file(path))
            except Exception as exc:self.error.setText(str(exc))

    def populate(self,data):
        if data['kind']!=self.kind:
            raise ValueError('文件业务类型与当前工作台不符')
        self.extra=dict(data)
        self.name.setText(data['title'])
        self.stores.setText(','.join(data['stores']))
        self.mode.setCurrentText(data.get('mode','固定新增'))
        self.strategy.setCurrentText(data.get('strategy','覆盖优先'))
        self.start.setText(data['start'])
        self.end.setText(data['end'])
        default=data.get('scope') or data['lines'][0]
        self.wh.setCurrentText(default['warehouse'])
        for key in ('account','shop','merchant','supplier'):
            getattr(self,key).setText(default[key])
        columns=list(dict.fromkeys([*self.COLUMNS,*(k for r in data['lines'] for k in r)]))
        buf=io.StringIO(newline='')
        writer=csv.DictWriter(buf,columns)
        writer.writeheader()
        writer.writerows(data['lines'])
        self.lines.setPlainText(buf.getvalue())

    def save(self):
        try:
            lines=[]
            for row in csv.DictReader(io.StringIO(self.lines.toPlainText())):
                row={k:v for k,v in row.items() if v not in ('',None)}
                if None in row:
                    raise ValueError('货盘 CSV 列数与表头不一致')
                for k in ('strict','round_up'):
                    if k in row:row[k]=row[k].lower() in ('true','1','是')
                lines.append(row)
            defaults={**self.extra.get('scope',{}),'warehouse':self.wh.currentText(),
                      **{k:getattr(self,k).text() for k in ('account','shop','merchant','supplier')}}
            self.value={**self.extra,'kind':self.kind,'title':self.name.text(),'scope':defaults,'lines':lines,
                        'stores':[s.strip() for s in self.stores.text().replace('，',',').split(',') if s.strip()],
                        'strategy':self.strategy.currentText(),'mode':self.mode.currentText(),'start':self.start.text(),'end':self.end.text()}
            sp.normalize(self.value)
            sp.required(self.reason.text(),'发布或修订依据')
            self.accept()
        except Exception as exc:
            self.error.setText(str(exc))


from PySide6.QtCore import QThread


class ClaimWorker(QThread):
    def __init__(self,parent,ticket):
        super().__init__(parent)
        self.path=parent.path
        self.ticket=ticket
        self.result=self.error=None

    def run(self):
        try:self.result=waves.claim(self.path,self.ticket)
        except Exception as exc:self.error=str(exc)


class Desk(QDialog):
    def __init__(self,host,kind):
        super().__init__(host)
        self.host,self.kind,self.path=host,kind,host.work_repository.path
        self.view=None
        self.claim_worker=None
        self.packages=[]
        self.setWindowTitle(sp.KINDS[kind]+'工作台')
        self.resize(1170,700)
        layout=QVBoxLayout(self)
        top=QHBoxLayout()
        self.plans=QComboBox()
        self.plans.setMinimumWidth(210)
        top.addWidget(self.plans)
        button(top,'新建'+sp.KINDS[kind],lambda:self.edit(False))
        button(top,'批准修订',lambda:self.edit(True))
        self.actor=QLineEdit(getpass.getuser())
        self.actor.setMaximumWidth(130)
        top.addWidget(QLabel('经办人'))
        top.addWidget(self.actor)
        self.on=QCheckBox('允许本类专项执行')
        self.on.clicked.connect(self.toggle)
        top.addWidget(self.on)
        button(top,'刷新原单',self.refresh)
        layout.addLayout(top)
        self.tabs=QTabWidget()
        layout.addWidget(self.tabs,1)
        page=QWidget()
        p=QVBoxLayout(page)
        toolbar=QHBoxLayout()
        button(toolbar,'门店确认',self.confirm)
        button(toolbar,'登记门店快照',self.observe)
        button(toolbar,'采用仓资料',self.stock)
        button(toolbar,'核对快照已含占用',self.stock_inclusion)
        button(toolbar,'预览本轮',self.preview)
        button(toolbar,'确认本轮分配',self.claim)
        button(toolbar,'登记定时认领',self.schedule)
        button(toolbar,'运行已到期票据',self.due)
        self.limit=QSpinBox()
        self.limit.setRange(1,30000)
        self.limit.setValue(800)
        if kind=='opening':
            toolbar.addWidget(QLabel('每包款数'))
            toolbar.addWidget(self.limit)
        p.addLayout(toolbar)
        self.rows=table(['原行','门店','仓','SKU','原目标／本版','确认量','已占或已收','门店持有','可新下','分配前','本轮','分配后','超目标','说明'])
        p.addWidget(self.rows)
        self.tabs.addTab(page,'目标与本轮分配')
        packet=QWidget()
        p=QVBoxLayout(packet)
        bar=QHBoxLayout()
        button(bar,'聚水潭原任务',self.native_jst)
        button(bar,'采用平台原单明细',self.adopt_original)
        button(bar,'核对聚水潭原件',self.original)
        button(bar,'人工确认付款',self.pay)
        button(bar,'更正本包付款',self.correct_payment)
        button(bar,'撤销未发包',self.release_local)
        button(bar,'冻结合并采购',self.freeze)
        button(bar,'翱象采购与收货',self.native_purchase)
        p.addLayout(bar)
        self.orders=table(['轮次','包序','仓单号','供货仓','不同SKU','件数','商品额','线上号','平台原号','原单状态','付款版本'])
        p.addWidget(self.orders)
        self.tabs.addTab(packet,'子单与付款')
        group=QWidget()
        p=QVBoxLayout(group)
        bar=QHBoxLayout()
        button(bar,'查看组级收货',self.group_status)
        button(bar,'回读原采购实收',self.native_receipt)
        if self.kind=='policy':button(bar,'登记本仓实收',self.policy_receipt)
        button(bar,'更正已入账实收',self.correct_receipt)
        button(bar,'导出逐行对账',self.export_rows)
        button(bar,'全部经营实绩',self.operating)
        p.addLayout(bar)
        self.detail=QPlainTextEdit()
        self.detail.setReadOnly(True)
        p.addWidget(self.detail)
        self.tabs.addTab(group,'采购、实收与追溯')
        self.status=QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.plans.currentIndexChanged.connect(self.refresh)
        self.reload()

    def key(self):
        return sp.required(self.plans.currentData(),'已发布主单')

    def run(self,op):
        try:return op()
        except Exception as exc:
            self.status.setText('待核 · '+str(exc))
            return None

    def reload(self,wanted=None):
        data=sp.all_plans(self.path,self.kind)
        self.plans.blockSignals(True)
        self.plans.clear()
        for key,title,v in data:self.plans.addItem(f'{title} · V{v}',key)
        if wanted:self.plans.setCurrentIndex(self.plans.findData(wanted))
        self.plans.blockSignals(False)
        self.refresh()

    def refresh(self,*args):
        self.view=None
        self.packages=[]
        self.orders.setRowCount(0)
        self.rows.setRowCount(0)
        self.detail.clear()
        with sp.connection(self.path) as db:
            self.on.setChecked(sp.enabled(db,self.kind))
            if not self.plans.currentData():
                self.status.setText('请新建主单，或读取已有方案文件。')
                return
            key=self.key()
            body=sp.read(db,key)
            self.packages=settle.packages(db,key)
            data=[]
            for p in self.packages:
                obs=sp.latest(db,p['oid'],'原单')
                paid=sp.latest(db,p['oid'],'付款')
                wave=db.execute('SELECT wave FROM special_runs WHERE run=?',(p['run'],)).fetchone()[0]
                pos=db.execute('SELECT position FROM special_packages WHERE oid=?',(p['oid'],)).fetchone()[0]
                data.append([wave,pos,p['number'],p['scope']['warehouse'],len(p['items']),sum(Decimal(x['quantity']) for x in p['items']),
                             sum(Decimal(x['amount']) for x in p['items']),p['online'],obs['body']['internal'] if obs else '',
                             obs['body']['status'] if obs else ('已撤销未发包' if sp.latest(db,p['oid'],'未发送撤销') else '待回读'),
                             ('原款 V'+str(paid['version'])+' 待人工重核' if obs and paid and (paid['body']['source']!=obs['sha'] or obs['body']['status']=='已取消')
                              else paid['version'] if paid else '待人工核对')])
            fill(self.orders,data)
            fill(self.rows,[[r['source_row'],r['store'],r['warehouse'],r['sku'],r['target']] for r in body['lines'][:200]])
            self.status.setText(f"主单 {key} · V{body['version']} · {len(body['lines'])} 个目标行 · {len(data)} 张仓单。列表显示前 200 个目标行，逐行导出含全量。")
            if data:self.orders.selectRow(0)

    def toggle(self,checked):
        if not checked or ask(self,'确认启用执行','允许当前业务类型创建本地执行仓单并认领库存？'):
            sp.set_enabled(self.path,self.kind,checked)
        self.refresh()

    def edit(self,revision):
        def action():
            body=None
            if revision:
                with sp.connection(self.path) as db:body=sp.read(db,self.key())
            dialog=Editor(self,body)
            if dialog.exec():
                key=sp.publish(self.path,dialog.value,self.actor.text(),plan=body['plan'] if body else None,
                               expected=body['version'] if body else 0,reason=dialog.reason.text())
                self.reload(key)
        self.run(action)

    def observe(self):
        def action():
            file=file_in(self,'读取同一门店时点快照')
            if file:
                sp.observe(self.path,self.key(),read_file(file))
                self.refresh()
                self.status.setText('门店快照已登记')
        self.run(action)

    def stock(self):
        def action():
            import warehouse_catalog
            wh,ok=QInputDialog.getItem(self,'选择供货仓','本份原件所属仓',warehouse_catalog.WAREHOUSES,0,False)
            if not ok:return
            file=file_in(self,'读取本仓原商品资料','Excel 原件 (*.xlsx)')
            if not file:return
            repo=warehouse_catalog.Repository(Path(self.path).parent)
            before=repo.current_digest(wh)
            new=warehouse_catalog.import_catalog(file,wh)
            if ask(self,'采用当前仓资料',f"{wh} · {len(new['items'])} 款 · {new['revision']}"):
                repo.save(new,expected_digest=before)
                self.status.setText('已采用 '+wh+' 新资料，原预览须复核差额')
        self.run(action)

    def stock_inclusion(self):
        def action():
            file=file_in(self,'读取仓快照已含代次原件')
            if file and ask(self,'核对仓快照已含占用','按原仓资料摘要和逐 SKU 原代次记录已含量？'):
                count=settle.note_stock_inclusion(self.path,file,self.actor.text())
                self.status.setText('已登记 '+str(count)+' 项快照占用口径，请重做预览')
        self.run(action)

    def confirm(self):
        def action():
            file=file_in(self,'读取门店确认原件')
            if file:
                value=read_file(file)
                if value['plan']!=self.key():raise ValueError('门店回复不属于所选主单')
                seq=sp.confirm(self.path,self.key(),value['version'],value['store'],value['quantities'],self.actor.text())
                self.refresh()
                self.status.setText(f'已保存门店确认 V{seq}')
        self.run(action)

    def preview(self):
        def action():
            self.view=waves.preview(self.path,self.key(),limit=self.limit.value())
            v=self.view['view']
            fill(self.rows,[[r[k] for k in ('source_row','store','warehouse','sku','target','chosen','claimed','observed','demand','before','quantity','after','extra')]+[r['reason'] or ('可执行' if r['window_open'] else '等待有效时段')] for r in v['rows'][:200]])
            self.status.setText(f"第 {v['wave']} 轮预览 · {len(v['rows'])} 行 · {len(v['packets'])} 包 · "+'、'.join(str(len(p['items'])) for p in v['packets'])+' 款')
        self.run(action)

    def claim(self):
        def action():
            if self.claim_worker:return
            if not self.view:raise ValueError('请先预览本轮')
            if not ask(self,'确认本轮分配','按当前门店、仓及数量建立执行包并占用库存？'):return
            worker=ClaimWorker(self,self.view)
            self.claim_worker=worker
            self.status.setText('正在核对原轮次与库存…')
            def finish():
                self.claim_worker=None
                if worker.error:
                    self.status.setText('待核 · '+worker.error)
                else:
                    self.refresh()
                    self.status.setText('原轮次已登记 '+str(len(worker.result))+' 张执行仓单')
                worker.deleteLater()
            worker.finished.connect(finish)
            worker.start()
        self.run(action)

    def closeEvent(self,event):
        if self.claim_worker:
            event.ignore()
        else:super().closeEvent(event)

    def schedule(self):
        def action():
            if not self.view:raise ValueError('先核对本轮预览')
            if ask(self,'登记定时认领','在有效时段内依本次确认的预览认领本轮库存？'):
                special_schedule.register(self.path,self.view)
                self.status.setText('已登记原轮次定时票据')
        self.run(action)

    def due(self):
        self.run(lambda:self.status.setText(str(special_schedule.dispatch(self.path)) or '未到认领时段'))

    def selected(self):
        pos=self.orders.currentRow()
        if pos<0 or pos>=len(self.packages):raise ValueError('请先在子单列表选择原包')
        return self.packages[pos]

    def original(self):
        def action():
            p=self.selected()
            file=file_in(self,'读取逐 SKU 原平台核对件')
            if file and ask(self,'核对原单逐行证据',p['number']+' · 以所选原件核对平台实配、取消及可释放余量？'):
                settle.receive_original(self.path,p['oid'],file,self.actor.text())
                self.refresh()
        self.run(action)

    def adopt_original(self):
        def action():
            p=self.selected()
            if ask(self,'采用已回读的原单',p['number']+' · 核对本包当前平台状态、实配明细及凭证？'):
                settle.receive_original(self.path,p['oid'],None,self.actor.text())
                self.refresh()
        self.run(action)

    def release_local(self):
        def action():
            p=self.selected()
            reason,ok=QInputDialog.getText(self,'撤销未发包',p['number']+' · 已核实未发送，填写撤销依据')
            if ok and ask(self,'撤销本包未发占用',p['number']+' · 仅释放该原代次的剩余占用？'):
                with sp.connection(self.path,True) as db:settle.release_unsubmitted(db,p['oid'],self.actor.text(),reason)
                self.refresh()
        self.run(action)

    def pay(self):
        def action():
            p=self.selected()
            with sp.connection(self.path) as db:obs=sp.latest(db,p['oid'],'原单')
            if not obs:raise ValueError('先回读或核对当前聚水潭原单')
            v=obs['body']
            qty=sum(Decimal(r['quantity']) for r in v['rows'])
            amount=sum(Decimal(r['amount']) for r in v['rows'])
            if ask(self,'人工确认本版付款',f"{p['number']} · {v['internal']} · 原单版本 {obs['version']} · {qty} 件 · 商品 {amount} 元 · 运费 {v.get('freight',0)} · 调整 {v.get('adjustment',0)}。已核款并确认登记？"):
                settle.payment(self.path,p['oid'],obs['sha'],self.actor.text())
                self.refresh()
        self.run(action)

    def freeze(self):
        def action():
            if self.kind!='opening':raise ValueError('政策仓单使用原采购入口')
            f=settle.freeze(self.path,self.selected()['oid'],self.actor.text())
            v=f['body']
            self.detail.setPlainText(json.dumps({k:v[k] for k in ('id','scope','total_quantity','total_amount','payment_total_amount','freight','adjustment','rounding_delta','inputs')},ensure_ascii=False,indent=2))
            self.tabs.setCurrentIndex(2)
            self.status.setText(f"采购预览 V{f['version']} · {len(v['inputs'])} 张来源单 · {v['total_quantity']} 件 · {v['total_amount']} 元")
        self.run(action)

    def correct_payment(self):
        def action():
            p=self.selected()
            file=file_in(self,'读取本包付款金额更正')
            if not file:return
            data=read_file(file)
            with sp.connection(self.path) as db:
                obs=sp.latest(db,p['oid'],'原单')
                paid=sp.latest(db,p['oid'],'付款')
            if not obs or not paid or (data.get('internal'),data.get('online'))!=(obs['body']['internal'],p['online']):
                raise ValueError('更正须对应原包、内部单号及现有付款版本')
            if ask(self,'人工复核本包金额',f"{p['number']} · 付款 V{data['version']} · {data['reason']} · 确认修改本包金额？"):
                settle.payment(self.path,p['oid'],obs['sha'],self.actor.text(),amounts=data['amounts'],reason=data['reason'],expected=data['version'])
                self.refresh()
        self.run(action)

    def native_jst(self):
        def action():
            from jst_auto_order import prepare
            from jst_auto_order_ui import open_orders
            p=self.selected()
            with sp.connection(self.path) as db:
                h=settle.head(db,p)
            result=prepare(self.path,[{'gid':p['gid'],'head':h}])
            open_orders(self,self.host,result['ready'],result['failed'])
            self.refresh()
        self.run(action)

    def native_purchase(self):
        def action():
            from aoxiang_purchase_ui import open_purchase
            with sp.connection(self.path) as db:h=settle.head(db,self.selected())
            open_purchase(self,self.host,h)
        self.run(action)

    def native_receipt(self):
        def action():
            from aoxiang_receipt_ui import open_receipts
            with sp.connection(self.path) as db:h=settle.head(db,self.selected())
            open_receipts(self,self.host,settle.resolve_head(self.path,h))
        self.run(action)

    def policy_receipt(self):
        def action():
            from lifecycle_ui import import_document
            p=self.selected()
            with sp.connection(self.path) as db:h=settle.head(db,p)
            import_document(self,self.host,'receipt',h)
            self.refresh()
        self.run(action)

    def group_status(self):
        def action():
            p=self.selected()
            group=settle.group_id(p)
            with sp.connection(self.path) as db:
                effect=settle.effect_for(db,group)
                result=sp.latest(db,group,'正式实收')
                ack=sp.latest(db,group,'原平台回执')
            if result:
                b=result['body']
                value={'原采购号':b['original'],'实收版本':result['version'],'首次入账回执':ack['body'] if ack else '待核',
                    '实际件数':str(sum(Decimal(r['quantity']) for r in b['rows'])),'待认领件数':str(sum(Decimal(r['pending']) for r in b['rows']))}
            else:value={'原采购任务':effect,'状态':'待原采购实收' if effect else '待上传'}
            self.detail.setPlainText(json.dumps(value,ensure_ascii=False,indent=2))
        self.run(action)

    def correct_receipt(self):
        def action():
            group=settle.group_id(self.selected())
            file=file_in(self,'读取原采购实收更正')
            if not file:return
            data=read_file(file)
            if not ask(self,'确认实收更正',f"原采购 {data['original']} · V{data['version']} · {data['reason']}"):return
            with sp.connection(self.path,True) as db:
                _,sha=settle.note_file(db,file)
                settle.post(db,group,data['task'],data['original'],data['rows'],sha,self.actor.text(),
                            correction={'version':data['version'],'reason':data['reason']})
            self.group_status()
        self.run(action)

    def export_rows(self):
        def action():
            path=QFileDialog.getSaveFileName(self,'保存完整逐行对账','专项逐行对账.csv','CSV (*.csv)')[0]
            if path:
                rows=special_reports.export(self.path,self.key(),path)
                self.status.setText(f'已导出 {len(rows)} 行 · {path}')
        self.run(action)

    def operating(self):
        def action():
            with sp.connection(self.path) as db:value=special_reports.operating(db)
            self.detail.setPlainText(json.dumps(value,ensure_ascii=False,indent=2))
            self.status.setText('当前有效付款与实收，按仓单或原采购组取一次')
        self.run(action)


def open_desk(host,kind):
    desk=Desk(host,kind)
    desk.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    desk.show()
    desk.raise_()
    return desk
