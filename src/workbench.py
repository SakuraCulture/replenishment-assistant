"""Persistent workbench: complete store parameters and one confirmed daily record."""
import copy,hashlib,json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout,QMessageBox,QDialog,QVBoxLayout,QTableWidget,QTableWidgetItem,QLineEdit,QHeaderView,QPushButton
from ui_controls import label,button
from batch_app import BatchWindow
from parameter_ui import ParameterUi
import engine,parameter_schema as schema,work_repository as wr
from version_info import MODEL_VERSION,VERSION

class Workbench(ParameterUi,BatchWindow):
    def __init__(self):
        self._hydrating=True;self._restoring=False;self.readonly=False;self.job_dirty=False;self.job_day=wr.today();self.saved_time='';self.store_presets={};self.explicit_defaults=True;self.export_log=[];self._discard_close=False
        super().__init__();self.work_repository=wr.Repository(self.repository.folder)
        row=QHBoxLayout();self.job_label=label('','muted');row.addWidget(self.job_label,1)
        self.history_btn=button('作业记录',self.history);row.addWidget(self.history_btn)
        self.new_job_btn=button('新建今日作业',self.new_job);row.addWidget(self.new_job_btn)
        self.presets_btn=button('参数方案',self.open_preset_library);row.addWidget(self.presets_btn)
        self.save_job_btn=button('保存作业',self.save_current,True);row.addWidget(self.save_job_btn)
        self.centralWidget().layout().insertLayout(1,row)
        row=QHBoxLayout();self.defaults_btn=button('保存所选门店为默认配置',self.save_store_defaults);row.addWidget(self.defaults_btn);self.fresh_btn=button('使用当前资料重新检查',self.use_current_data);row.addWidget(self.fresh_btn);row.addStretch();self.store_page.layout().insertLayout(2,row)
        self.exception.hide();self.store_table.setColumnWidth(7,185);self.nav[1].setText('② 门店与参数')
        self._hydrating=False;self.update_controls()
        if any(r['day']==wr.today() for r in self.work_repository.list_jobs()):self.status.setText('今天已有保存的作业，可从“作业记录”继续；新建内容不会自动覆盖。')
    def touch(self):
        if not self._hydrating and not self.readonly:self.job_dirty=True;self.refresh_job_label()
    def refresh_job_label(self):
        if not hasattr(self,'job_label'):return
        state='历史记录 · 只读' if self.readonly else '有未保存变更' if self.job_dirty else '已保存 '+self.saved_time if self.saved_time else '未保存'
        self.job_label.setText(self.job_day+' · '+state+(' · 已跨天' if self.job_day!=wr.today() and not self.readonly else ''))
    def update_controls(self):
        super().update_controls()
        if not hasattr(self,'save_job_btn'):return
        busy=self.worker is not None
        for b in [self.new_job_btn,self.history_btn,self.presets_btn]:b.setEnabled(not busy)
        self.save_job_btn.setEnabled(not busy and not self.readonly and bool(self.sales.paths or self.loaded));self.defaults_btn.setEnabled(not busy and not self.readonly and bool(self.store_widgets));self.fresh_btn.setEnabled(not busy and not self.readonly and bool(self.loaded))
        if self.readonly:
            self.import_page.setEnabled(False);self.store_page.setEnabled(False);self.calculate_btn.setEnabled(False);self.retry_btn.setEnabled(False)
        self.refresh_job_label()
    def populate_stores(self):
        super().populate_stores()
        if not hasattr(self,'work_repository'):return
        previous=self._hydrating;self._hydrating=True;self.initializing=True
        try:
            saved={} if self._restoring else self.work_repository.stores()
            for s in self.store_widgets:
                if s in saved:
                    p=copy.deepcopy(saved[s]['preset']);schema.validate(p['config']);self.store_presets[s]=p;self.overrides[s]=copy.deepcopy(p['config']['warehouses']);self.set_store_ui(s,saved[s]['ui'])
                self.refresh_parameter_label(s)
        finally:self.initializing=False;self._hydrating=previous
        self.selection_changed()
    def loaded_ok(self,loaded):
        existing={'store_presets':{s:self.preset_for(s) for s in self.store_widgets},'store_ui':{s:self.store_ui(s) for s in self.store_widgets}}
        previously_blocked=(self.loaded or {}).get('blocked',{})
        for s,ui in existing['store_ui'].items():
            if previously_blocked.get(s) and not loaded['blocked'].get(s):ui['selected']=True
        self.store_presets={};super().loaded_ok(loaded)
        pending=getattr(self,'pending_copy',None) or existing
        if pending:
            self._hydrating=True;self.initializing=True
            try:
                for s in self.store_widgets:
                    if s in pending['store_presets']:
                        p=copy.deepcopy(pending['store_presets'][s]);schema.validate(p['config']);self.store_presets[s]=p;self.overrides[s]=copy.deepcopy(p['config']['warehouses']);self.set_store_ui(s,pending['store_ui'][s]);self.refresh_parameter_label(s)
            finally:self.initializing=False;self._hydrating=False
            self.pending_copy=None;self.selection_changed()
        self.touch()
    def selected_settings(self,s):
        result=super().selected_settings(s);c=self.config_for(s);schema.validate(c)
        result.update(parameter_config=c,warehouses=copy.deepcopy(c['warehouses']),exception=c['exception'],preset={k:v for k,v in self.preset_for(s).items() if k!='config'},parameter_signature=schema.signature(c));return result
    def store_ui(self,s):
        w=self.store_widgets[s]
        return {'mode':w['mode'].currentIndex(),'amount':w['amount'].text(),'primary':w['primary'].currentData(),'secondary':w['secondary'].currentData(),'selected':w['check'].isChecked()}
    def set_store_ui(self,s,data):
        w=self.store_widgets[s];w['mode'].setCurrentIndex(data.get('mode',0));w['amount'].setText(data.get('amount',''));w['amount'].setEnabled(w['mode'].currentIndex()==2);w['check'].setChecked(bool(data.get('selected',True)) and not self.loaded['blocked'].get(s))
        for key in ['primary','secondary']:w[key].setCurrentIndex(max(0,w[key].findData(data.get(key))))
    def selection_changed(self,*args):
        super().selection_changed(*args)
        if not self.initializing:self.touch()
    def invalidate(self,codes):
        super().invalidate(codes);self.touch()
    def mark_dirty(self,*args):
        if not self.initializing and self.sender() is getattr(self,'exception',None):
            for s in self.store_widgets:
                p=self.preset_for(s);p['config']['exception']=self.exception.isChecked();self.store_presets[s]=p;self.refresh_parameter_label(s)
        super().mark_dirty(*args)
    def task_changed(self,*args):super().task_changed(*args);self.touch()
    def start_calculation(self,codes=None):
        if self.readonly:return
        super().start_calculation(codes);self.touch()
    def snapshot(self):
        loaded=None
        if self.loaded:
            loaded={**self.loaded,'intake':{k:v for k,v in self.loaded['intake'].items() if k!='frozen'}}
        return {'schema':1,'day':self.job_day,'app_version':VERSION,'model_version':MODEL_VERSION,'loaded':loaded,'states':self.states,'results':self.results,'catalogs':self.catalogs,'profile':self.profile,'input_dirty':self.input_dirty,'store_presets':{s:self.preset_for(s) for s in self.store_widgets},'overrides':self.overrides,'default_by_store':self.default_by_store,'store_ui':{s:self.store_ui(s) for s in self.store_widgets},'sales':self.sales.paths,'products':self.products.path,'legacy':self.master_legacy.isChecked(),'concurrency':self.concurrency.value(),'exception':self.exception.isChecked(),'export_log':self.export_log,'pending_copy':getattr(self,'pending_copy',None)}
    def confirm_overwrite(self,old,new):
        a=old['summary'];b=wr.summary(new);oldstores=a['stores'];newstores=b['stores']
        removed=set(oldstores)-set(newstores);added=set(newstores)-set(oldstores);lost=[s for s in oldstores.keys()&newstores.keys() if oldstores[s]['state']=='已完成' and newstores[s]['state']!='已完成']
        box=QMessageBox(self);box.setObjectName('dailyOverwrite');box.setWindowTitle('覆盖 '+new['day']+' 的作业记录？')
        box.setText('每天只保留一份作业。本次将替换该日整份记录；已导出的Excel不受影响。')
        money=lambda x:'暂无有效金额' if x is None else f'¥{x:,.2f}'
        box.setInformativeText(f"原保存：{old['saved']}\n原记录：{len(oldstores)}家 / 完成{a['completed']}家 / {money(a['amount'])}\n本次保存：{wr.now()}\n本次：{len(newstores)}家 / 完成{b['completed']}家 / {money(b['amount'])}\n新增{len(added)}家，减少{len(removed)}家，完成变失效{len(lost)}家。展开详情核对名单、状态和版本。")
        if removed or lost:box.setInformativeText(box.informativeText()+'\n减少门店：'+('、'.join(oldstores[s]['name']+'('+s+')' for s in sorted(removed)[:8]) or '无')+'\n完成变失效：'+('、'.join(oldstores[s]['name']+'('+s+')' for s in lost[:8]) or '无')+'\n超过8家时完整名单见详情。')
        previous=old['payload'];changed=[s for s in oldstores.keys()&newstores.keys() if wr.dumps(previous.get('store_presets',{}).get(s))!=wr.dumps(new.get('store_presets',{}).get(s)) or previous.get('store_ui',{}).get(s)!=new.get('store_ui',{}).get(s)]
        def versions(payload):return {'输入':(payload.get('loaded') or {}).get('hashes',{}),'仓资料':{w:c.get('revision') for w,c in payload.get('catalogs',{}).items()},'必卖':(payload.get('profile') or {}).get('revision')}
        details={'减少门店':[s+' '+oldstores[s]['name'] for s in sorted(removed)],'新增门店':[s+' '+newstores[s]['name'] for s in sorted(added)],'原已完成现失效':lost,'设置变化门店':changed,'原门店状态':oldstores,'新门店状态':newstores,'原数据版本':versions(previous),'新数据版本':versions(new)}
        box.setDetailedText(json.dumps(details,ensure_ascii=False,indent=2));cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=box.addButton('覆盖当天记录',QMessageBox.ButtonRole.AcceptRole);box.setDefaultButton(cancel);box.setEscapeButton(cancel)
        for b in box.findChildren(QPushButton):
            if b not in [cancel,accept]:b.setText('查看差异明细')
        box.exec();return box.clickedButton() is accept
    def save_current(self):
        if self.worker or self.readonly:return False
        if self.job_day!=wr.today():
            box=QMessageBox(self);box.setWindowTitle('当前作业已跨天');box.setText('作业日期 '+self.job_day+'；今天 '+wr.today()+'。转为今日不会刷新旧输入数据，请核对来源时间。');cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);old_btn=box.addButton('保存原日期作业',QMessageBox.ButtonRole.AcceptRole);new_btn=box.addButton('转为今日作业',QMessageBox.ButtonRole.ActionRole);box.setDefaultButton(cancel);box.exec()
            if box.clickedButton() is cancel:return False
            if box.clickedButton() is new_btn:self.job_day=wr.today();self.touch()
            elif box.clickedButton() is not old_btn:return False
        from ui_work_io import Session,request
        from runtime_support import Cancelled
        try:
            payload=self.snapshot()
            with Session(self.work_repository.path) as session:
                prepared=request(self,session,'prepare_save',payload);old=prepared['old']
                if prepared['same']:
                    self.saved_time=old['saved'];self.job_dirty=False;self.refresh_job_label();self.status.setText('已是最新记录，无需覆盖。');return True
                if old and not self.confirm_overwrite(old,payload):return False
                saved=request(self,session,'save',None)
        except Cancelled:self.status.setText('已取消保存，原记录保留。');return False
        except Exception as exc:self.failed('作业未完成保存，请在作业记录核对状态：'+str(exc));return False
        self.saved_time=saved['saved'];self.job_dirty=False;self.refresh_job_label();self.status.setText('已保存 '+self.job_day+' 的完整作业。');return True
    def leave_current(self):
        if not self.job_dirty:return True
        box=QMessageBox(self);box.setWindowTitle('有未保存的作业变更');box.setText('保存门店默认不等于保存作业。是否保存当前作业后继续？');save=box.addButton('保存后继续',QMessageBox.ButtonRole.AcceptRole);discard=box.addButton('放弃本次变更',QMessageBox.ButtonRole.DestructiveRole);cancel=box.addButton('取消',QMessageBox.ButtonRole.RejectRole);box.setDefaultButton(cancel);box.exec()
        return self.save_current() if box.clickedButton() is save else box.clickedButton() is discard
    def new_job(self):
        if self.worker or not self.leave_current():return
        self.reset_job()
    def reset_job(self):
        self._hydrating=True;self.initializing=True
        try:
            self.readonly=False;self.job_day=wr.today();self.saved_time='';self.loaded=None;self.results={};self.states={};self.store_widgets={};self.store_presets={};self.overrides={};self.default_by_store={};self.run_codes=[];self.current_store=None;self.result=None;self.input_dirty=True;self.dirty=True;self.export_log=[];self.pending_copy=None
            self.sales.paths=[];self.sales.refresh();self.catalogs=self.catalog_repository.catalogs();self.profile=self.repository.profile();self.refresh_catalog_labels();self.refresh_profile_label();self.store_table.setRowCount(0);self.file_report.setRowCount(0);self.refresh_tables();self.navigate(0)
        finally:self.initializing=False;self._hydrating=False;self.job_dirty=False;self.update_controls()
    def restore_job(self,record,owned=False):
        # UI reader transfers an exclusively owned tree; external callers retain copy semantics.
        clone=(lambda value:value) if owned else copy.deepcopy
        p=record['payload']
        if p.get('schema')!=1:raise ValueError('不支持的作业格式')
        self._hydrating=True;self._restoring=True;self.initializing=True
        try:
            self.job_day=record['day'];self.saved_time=record['saved'];self.readonly=self.job_day!=wr.today() or p['model_version']!=MODEL_VERSION
            self.pending_copy=clone(p.get('pending_copy'))
            self.loaded=clone(p['loaded']);self.catalogs=clone(p['catalogs']);self.profile=clone(p['profile']);self.store_presets=clone(p['store_presets']);self.overrides=clone(p['overrides']);self.default_by_store=clone(p['default_by_store']);self.states=clone(p['states']);self.results=clone(p['results']);self.input_dirty=p['input_dirty'];self.export_log=clone(p.get('export_log',[]));self.run_codes=[];self.current_store=None;self.result=None;self.dirty=True
            if self.loaded:self.loaded['intake']['frozen']=True
            self.sales.paths=list(p['sales']);self.sales.refresh();self.products.path=p['products'];self.products.filename.setText(p['products'] or '尚未选择');self.master_legacy.setChecked(p['legacy']);self.concurrency.setValue(p['concurrency']);self.exception.setChecked(p['exception']);self.refresh_catalog_labels();self.refresh_profile_label()
            if self.loaded:
                self.populate_stores();self.initializing=True
                for s,ui in p['store_ui'].items():self.set_store_ui(s,ui)
            else:self.store_widgets={};self.store_table.setRowCount(0)
            self.file_report.setRowCount(0);self.import_note.setText('已恢复当时数据快照；不会自动换成当前资料。若输入尚未检查，原文件仍需可访问。');self.refresh_tables();self.navigate(3 if self.states else 0)
        finally:self.initializing=False;self._restoring=False;self.selection_changed();self._hydrating=False;self.job_dirty=False;self.update_controls()
        self.status.setText('已打开 '+record['day']+(' 历史只读记录。' if self.readonly else '；可继续今日作业，保存更新前仍需确认覆盖。'))
    def history(self):
        if self.worker:return
        records=self.work_repository.list_jobs();dialog=QDialog(self);dialog.setWindowTitle('作业记录 · 每天一份');dialog.resize(1000,650);v=QVBoxLayout(dialog);search=QLineEdit();search.setPlaceholderText('搜索日期、门店名称或编码');v.addWidget(search)
        table=QTableWidget(len(records),5);table.setHorizontalHeaderLabels(['日期','保存时间','门店 / 完成','状态','有效执行金额']);table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection);table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);v.addWidget(table)
        for i,r in enumerate(records):
            s=r['summary'];values=[r['day'],r['saved'],f"{len(s['stores'])} / {s['completed']}",s['phase'],engine.fmt(s['amount'])]
            for j,value in enumerate(values):table.setItem(i,j,QTableWidgetItem(str(value)))
        search.textChanged.connect(lambda t:[table.setRowHidden(i,t.lower() not in (r['day']+json.dumps(r['summary']['stores'],ensure_ascii=False)).lower()) for i,r in enumerate(records)])
        if records:table.selectRow(0)
        action={'value':'open'}
        def choose(value):
            if table.currentRow()>=0:action['value']=value;dialog.accept()
        row=QHBoxLayout();row.addWidget(button('打开 / 继续今日作业',lambda:choose('open'),True));row.addWidget(button('复制配置用于今日',lambda:choose('copy')));row.addWidget(button('关闭',dialog.reject));v.addLayout(row)
        if dialog.exec()!=QDialog.DialogCode.Accepted or not self.leave_current():return
        from ui_work_io import read_job
        from runtime_support import Cancelled
        try:record=read_job(self,self.work_repository.path,records[table.currentRow()]['day'],action['value']!='open')
        except Cancelled:self.status.setText('已取消读取作业。');return
        except Exception as exc:self.failed('作业未打开：'+str(exc));return
        if record is None:self.failed('该作业记录已不存在，请刷新后重试。');return
        if action['value']=='open':self.restore_job(record,owned=True)
        else:
            self.reset_job();self.pending_copy=record;self.status.setText('已复制所选作业门店配置到本次内存；请导入今天数据。未保存为门店默认。');self.touch()
    def use_current_data(self):
        if self.worker or self.readonly:return
        self.catalogs=self.catalog_repository.catalogs();self.profile=self.repository.profile();self.refresh_catalog_labels();self.refresh_profile_label();self.start_load()
    def export_ok(self,path):
        if not self.readonly:self.export_log.append({'path':str(path),'time':wr.now()});self.touch()
        super().export_ok(path)
    def closeEvent(self,event):
        pending=getattr(self,'_large_io_dialog',None)
        if pending is not None and not pending._completed:
            self.closing=True;pending.reject();event.ignore();return
        if self.worker:super().closeEvent(event);return
        if self._discard_close or self.leave_current():event.accept()
        else:self.closing=False;event.ignore()
