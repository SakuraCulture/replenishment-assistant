"""Warehouse-scoped sync dialog. All I/O runs on workers; no AX workflow calls."""
import time
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QFileDialog,QCheckBox,QMessageBox,QProgressBar)
from ui_controls import button,label
from business_ui import grid,run
from ui_tasks import Worker
from runtime_support import Cancelled
from catalog_session_ui import manager
from catalog_sync import run_sync
from interaction_ui import confirm
from app_defaults import WAREHOUSES

STAGES={'QUEUED':'待处理','WAIT_HOST':'准备查询依据','INTENT_RECORDED':'准备导出','SUBMITTING':'等待平台受理','SUBMISSION_UNKNOWN':'平台受理待核，不重提','ACCEPTED':'平台已受理','DOWNLOADED':'已下载','READY_TO_ADOPT':'已校验，待采用','APPLIED':'已采用','CANCELLED_BEFORE_SUBMIT':'提交前已停止','FAILED_CONFIRMED':'平台拒绝'}

def connect(client,progress=lambda _:None,cancel=None):
    progress('读取聚水潭共享连接；健康连接继续复用…')
    if not client.started:client.start()
    return client.request('sync.status')

def action(client,command,values,progress=lambda _:None,cancel=None):
    ack=client.request(command,**values)
    if 'operation_id' not in ack:return ack
    stopped=False
    while True:
        state=client.request('sync.status');op=state['operations'][ack['operation_id']]
        progress((state.get('active') or {}).get('phase',{}).get('message','核验中…'))
        if cancel and cancel.is_set() and not stopped:client.request('sync.stop');stopped=True
        if op['state']!='RUNNING':return op
        time.sleep(.15)

def shutdown(client,progress=lambda _:None,cancel=None):
    while not client.close(timeout=.3):
        progress('正在结束当前操作；保留健康连接，不强杀提交、不关闭浏览器…')
        time.sleep(.15)

class Dialog(QDialog):
    def __init__(self,host,warehouses=None,purpose='default_catalog',parent=None):
        super().__init__(parent or host);self.host=host;self.purpose=purpose;self.worker=None;self._completed=False;self.committing=False;self.close_pending=False;self.state={};self.probe=None;self.adopted=[]
        self.client=manager(host).session.acquire()
        self.setWindowTitle('聚水潭商品资料 · '+('更新本次分配库存' if purpose=='allocation_baseline' else '同步默认仓资料'));self.resize(1090,710)
        v=QVBoxLayout(self);v.addWidget(label('选择仓 → 核验账号 → 获取并采用；两仓独立登录，按仓依次处理。','section'))
        v.addWidget(label('首次连接按浏览器提示允许；关闭本窗口保留连接，退出整个软件才断开。翱象和两仓保持独立。'+('将更新本次分配依据；不会自动重新分配订单。' if purpose=='allocation_baseline' else '更新默认资料不会重置已分配库存；已有计算需要重新检查。'),'muted'))
        row=QHBoxLayout();self.warehouse=QComboBox();self.warehouse.addItems(WAREHOUSES)
        if warehouses:self.warehouse.setCurrentText(warehouses[0])
        row.addWidget(self.warehouse);self.account=label('尚未读取连接设置');row.addWidget(self.account,1);v.addLayout(row)
        self.profile=QLineEdit();self.profile.setPlaceholderText('首次选择该仓已有专用浏览器的 Default 或 Profile 目录');row=QHBoxLayout();row.addWidget(self.profile,1);self.pick=button('选择专用浏览器',self.choose);row.addWidget(self.pick);v.addLayout(row)
        self.company=QLineEdit();self.company.setPlaceholderText('公司名称（无法自动识别时填写聚水潭页顶部公司名）');v.addWidget(self.company)
        row=QHBoxLayout();self.probe_btn=button('核验所选浏览器',self.probe_account);row.addWidget(self.probe_btn);self.login_btn=button('打开登录页',self.login);row.addWidget(self.login_btn);self.bind_btn=button('确认对应仓并保存',self.bind);row.addWidget(self.bind_btn);self.disable_btn=button('停用本仓自动同步',lambda:self.enable(False));row.addWidget(self.disable_btn);self.enable_btn=button('启用本仓自动同步',lambda:self.enable(True));row.addWidget(self.enable_btn);v.addLayout(row)
        self.identity=label('先核验，再确认账号的商品资料及可用库存确属所选仓。','muted');self.identity.setWordWrap(True);v.addWidget(self.identity)
        row=QHBoxLayout();self.connection=label('尚未连接浏览器','muted');self.connection.setWordWrap(True);row.addWidget(self.connection,1)
        self.reconnect_btn=button('重新连接本仓',self.reconnect);row.addWidget(self.reconnect_btn);v.addLayout(row)
        self.mapping=QCheckBox('我确认上方账号的商品资料和可用库存对应所选物理仓');v.addWidget(self.mapping)
        row=QHBoxLayout();self.fetch=button('刷新所选仓资料',lambda:self.start([self.warehouse.currentText()]),True);row.addWidget(self.fetch);self.both=button('刷新所有已启用仓资料',self.start_enabled);row.addWidget(self.both);v.addLayout(row)
        self.table,self.model=grid(self,['仓库','公司','步骤','商品数','结果 / 下一步']);v.addWidget(self.table,1)
        for i,width in enumerate([85,240,140,75,450]):self.table.setColumnWidth(i,width)
        row=QHBoxLayout();self.records=QComboBox();row.addWidget(self.records,1);self.resume_btn=button('继续原记录未完成项',self.resume);row.addWidget(self.resume_btn);self.resume_one=button('仅继续所选仓原任务',lambda:self.resume(True));row.addWidget(self.resume_one);v.addLayout(row)
        self.bar=QProgressBar();self.bar.setRange(0,1);v.addWidget(self.bar);self.note=label('等待操作','muted');self.note.setWordWrap(True);v.addWidget(self.note)
        row=QHBoxLayout();self.stop=button('停止本次同步',self.cancel);self.stop.setEnabled(False);row.addWidget(self.stop);row.addWidget(button('关闭',self.reject));v.addLayout(row)
        self.clock=QTimer(self);self.clock.setInterval(500);self.clock.timeout.connect(self.tick)
        self.warehouse.currentIndexChanged.connect(self.select_account);self.records.currentIndexChanged.connect(self.show_record);self.profile.textChanged.connect(self.clear_probe);self.company.textChanged.connect(self.clear_probe);self.mapping.toggled.connect(self.controls)
        try:self.state=run(self,connect,(self.client,),'读取聚水潭连接设置');self.refresh(self.state)
        except Exception:
            run(self,shutdown,(self.client,),'安全关闭连接',cancellable=False);raise
    def choose(self):
        p=QFileDialog.getExistingDirectory(self,'选择已有的专用浏览器配置目录')
        if p:self.profile.setText(p)
    def clear_probe(self,*_):self.probe=None;self.mapping.setChecked(False);self.controls()
    def account_data(self):return next((a for a in self.state.get('accounts',[]) if a['warehouse']==self.warehouse.currentText()),None)
    def select_account(self,*_):
        self.clear_probe();a=self.account_data();self.account.setText((a['expectedCompany']+' · '+('已启用' if a['enabled'] else '已停用')) if a else '尚未绑定，可继续使用手动资料')
        self.profile.setText(a['binding']['actualProfilePath'] if a else '');self.company.setText(a['expectedCompany'] if a else '');self.show_connection();self.controls()
    def show_connection(self):
        wh=self.warehouse.currentText();c=next((x for x in self.state.get('connections',[]) if x['warehouse']==wh),{})
        names={'CONNECTED':'已连接，可复用','DISCONNECTED':'连接已断开，下次操作需重连','CONNECTING':'正在连接，请留意浏览器授权提示','NOT_CONNECTED':'尚未连接浏览器'}
        self.connection.setText(wh+' · '+names.get(c.get('state'),'尚未连接浏览器')+(' · '+c['reason'] if c.get('reason') else ''))
    def reconnect(self):
        a=self.account_data()
        if not a:return
        def operation(progress,cancel):
            progress('主动重新连接本仓；另一仓和翱象连接保持不变。请留意浏览器授权提示。')
            self.client.reconnect(a['warehouse'])
            return action(self.client,'accounts.verify',{'warehouse':a['warehouse']},progress,cancel)
        try:
            op=run(self,operation,(),'重新连接本仓');self.reload()
            self.identity.setText('本仓连接及账号已核验，可继续原任务。' if op['state']=='SUCCEEDED' else op.get('error',{}).get('message','连接未完成'))
        except Exception as exc:QMessageBox.warning(self,'连接未完成',str(exc))
    def invoke(self,command,**values):
        try:
            result=run(self,action,(self.client,command,values),'聚水潭账号设置')
            self.state=run(self,connect,(self.client,),'读取连接状态');self.show_connection()
            return result
        except Exception as exc:QMessageBox.warning(self,'操作未完成',str(exc));return None
    def probe_account(self):
        self.clear_probe();op=self.invoke('accounts.probe',warehouse=self.warehouse.currentText(),profilePath=self.profile.text().strip(),company=self.company.text().strip())
        self.probe=op
        if op and op['state']=='SUCCEEDED':
            scope=op['result']['scope'];self.identity.setText(scope['company']+f" · 全部状态 {scope['expectedRows']:,} 款。请核对物理仓后保存。")
        elif op:self.identity.setText(op.get('error',{}).get('message','核验未完成'))
        self.controls()
    def login(self):
        op=self.invoke('accounts.open_login',warehouse=self.warehouse.currentText(),probe_id=self.probe.get('id') if self.probe else None)
        if op:self.identity.setText('请在该仓专用浏览器正常登录，完成后重新核验。' if op['state']=='SUCCEEDED' else op.get('error',{}).get('message','打开失败'))
    def bind(self):
        if not self.probe or self.probe['state']!='SUCCEEDED' or not self.mapping.isChecked():return
        a=self.account_data();op=self.invoke('accounts.bind',warehouse=self.warehouse.currentText(),expected_version=a['bindingVersion'] if a else 0,probe_id=self.probe['id'],mapping_verified=True)
        if op:self.reload()
    def enable(self,value):
        a=self.account_data()
        if a and self.invoke('accounts.set_enabled',warehouse=a['warehouse'],expected_version=a['bindingVersion'],enabled=value):self.reload()
    def reload(self):self.refresh(run(self,connect,(self.client,),'读取采集状态'))
    def refresh(self,state):
        previous=self.records.currentData();self.state=state;self.records.blockSignals(True);self.records.clear()
        for b in state.get('batches',[]):self.records.addItem(b['createdAt'][:19].replace('T',' ')+' · '+('默认资料' if b['purpose']=='default_catalog' else '分配库存')+' · '+'、'.join(i['account']['warehouse'] for i in b['items']),b['id'])
        index=self.records.findData(previous);self.records.setCurrentIndex(max(index,0));self.records.blockSignals(False);self.select_account();self.show_record()
    def show_record(self,*_):
        b=next((b for b in self.state.get('batches',[]) if b['id']==self.records.currentData()),None);rows=[]
        for i in (b or {}).get('items',[]):
            receipt=i.get('receipt') or {};error=i.get('adoption_error') or (i.get('error') or {}).get('message','');a=i['account']
            rows.append([a['warehouse'],a['expectedCompany'],('未完成，需处理' if error else STAGES.get(i['stage'],i['stage'])),receipt.get('row_count') or receipt.get('count') or (i.get('native') or {}).get('scope',{}).get('expectedRows','—'),'已采用' if i.get('adoption') else error or '等待处理'])
        self.model.replace(rows);self.controls()
    def controls(self,*_):
        busy=self.worker is not None
        for widget in [self.warehouse,self.profile,self.company,self.pick,self.probe_btn,self.login_btn,self.mapping,self.records]:widget.setEnabled(not busy)
        a=self.account_data();self.fetch.setEnabled(not busy and bool(a and a['enabled']));self.both.setEnabled(not busy and any(a['enabled'] for a in self.state.get('accounts',[])))
        self.reconnect_btn.setEnabled(not busy and bool(a))
        self.bind_btn.setEnabled(not busy and bool(self.probe and self.probe['state']=='SUCCEEDED' and self.mapping.isChecked()));self.disable_btn.setEnabled(not busy and bool(a and a['enabled']));self.enable_btn.setEnabled(not busy and bool(a and not a['enabled']))
        b=next((b for b in self.state.get('batches',[]) if b['id']==self.records.currentData()),None);self.resume_btn.setEnabled(not busy and bool(b and b['purpose']==self.purpose and any(not i.get('adoption') for i in b['items'])))
        self.resume_one.setEnabled(self.resume_btn.isEnabled() and any(not i.get('adoption') and i['account']['warehouse']==self.warehouse.currentText() for i in (b or {}).get('items',[])))
        self.stop.setEnabled(busy and not self.committing and not self.worker.cancel.is_set())
    def start_enabled(self):self.start([a['warehouse'] for a in self.state.get('accounts',[]) if a['enabled']])
    def resume(self,only_selected=False):
        b=next(b for b in self.state['batches'] if b['id']==self.records.currentData());ids=[i['id'] for i in b['items'] if not i.get('adoption') and (not only_selected or i['account']['warehouse']==self.warehouse.currentText())]
        if ids:self.start([],{'batch_id':b['id'],'item_ids':ids})
    def start(self,warehouses,resume=None):
        if self.worker:return
        if self.purpose=='default_catalog' and getattr(self.host,'results',{}) and not confirm(self,'更新后需重新检查','成功采用后，本次计算将需要重新检查；已签发单及历史快照不变。'):return
        self.worker=Worker(run_sync,(self.client,self.host.repository.folder,self.host.work_repository.path,warehouses,self.purpose,resume),self)
        self.worker.status.connect(self.note.setText);self.worker.business_event.connect(self.on_business_event);self.worker.commit_state.connect(self.commit_state);self.worker.success.connect(self.success);self.worker.failed.connect(lambda m:self.note.setText('未完成：'+m));self.worker.cancelled.connect(lambda:self.note.setText('已停止，原任务保留'));self.worker.finished.connect(self.worker_finished)
        self.started=time.monotonic();self.phase='准备中';self.clock.start();self.bar.setRange(0,0);self.controls();self.worker.start()
    def on_business_event(self,event):
        if event['kind']=='adopted':
            self.adopted.append(event['ack'])
            if event['catalog'] and not event['ack'].get('unchanged'):
                self.host.catalogs[event['ack']['warehouse']]=event['catalog'];self.host.refresh_catalog_labels();self.host.file_changed()
        elif event['kind']=='status':
            self.state=event['state'];b=self.state['batches'][0];self.records.blockSignals(True);self.records.clear();self.records.addItem(b['createdAt'][:19],b['id']);self.records.blockSignals(False);self.show_record()
            self.show_connection()
            self.phase=(self.state.get('active') or {}).get('phase',{}).get('message','本轮处理结束');done=sum(bool(i.get('adoption') or i.get('error') or i.get('adoption_error')) for i in b['items']);self.bar.setRange(0,len(b['items']));self.bar.setValue(done)
    def tick(self):self.note.setText(f'{self.phase} · 已用时 {time.monotonic()-self.started:.0f} 秒'+(' · 已请求停止，等待当前安全步骤结束' if self.worker and self.worker.cancel.is_set() else ''))
    def commit_state(self,active):self.committing=active;self.controls()
    def success(self,result):
        self.state['batches']=[result['batch']];self.show_record()
        # The last status event can precede the host adoption acknowledgement.
        # Use the final returned batch for progress, just as for the result table.
        items=result['batch']['items'];done=sum(bool(i.get('adoption') or i.get('error') or i.get('adoption_error')) for i in items)
        self.bar.setRange(0,len(items));self.bar.setValue(done)
        self.phase=f"本轮已采用 {result['adopted']} 仓；未完成项可按原记录继续";self.note.setText(self.phase)
    def worker_finished(self):
        self.clock.stop();w=self.worker;self.worker=None;w.deleteLater();self.controls()
        if self.close_pending:QTimer.singleShot(0,self.reject)
    def cancel(self):
        if self.worker and not self.committing:self.worker.cancel.set();self.controls();self.tick()
    def reject(self):
        if self.worker:self.close_pending=True;self.cancel();return
        try:run(self,shutdown,(self.client,),'结束当前操作并保留连接',cancellable=False)
        except Exception as exc:QMessageBox.warning(self,'连接仍需处理',str(exc));return
        self._completed=True;super().reject()
    def closeEvent(self,event):
        self.reject();event.accept() if self._completed else event.ignore()


def open_sync(host,warehouses=None,purpose='default_catalog',parent=None):
    if getattr(host,'worker',None) or getattr(host,'readonly',False):return []
    d=None;previous=getattr(host,'_large_io_dialog',None)
    try:
        d=Dialog(host,warehouses,purpose,parent);host._large_io_dialog=d;d.exec();return d.adopted
    except Exception as exc:
        QMessageBox.warning(parent or host,'商品资料同步未打开',str(exc));return []
    finally:
        host._large_io_dialog=previous
        if d:d.deleteLater()
        if host.closing:QTimer.singleShot(0,host.close)
