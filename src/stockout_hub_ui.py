"""Compact hub controls; only the selected analysis page is constructed."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QTabBar,QComboBox,QCheckBox,QMessageBox,QFileDialog,QDialog,QDialogButtonBox,QLineEdit
from ui_controls import button,label
from stockout_common import WAREHOUSES,today


class Page(QWidget):
    def __init__(self,host,entry):
        super().__init__(host);self.host,self.entry=host,entry;self.current=None;self.metadata_value={};self.last_error='';self.queries={}
        from stockout_hub_service import MODES
        v=QVBoxLayout(self);v.setContentsMargins(8,4,8,4)
        row=QHBoxLayout();row.addWidget(label('缺货率','title'));self.mode=QComboBox()
        for key,text in MODES.items():self.mode.addItem(text,key)
        self.mode.setCurrentIndex(2);row.addWidget(self.mode);self.warehouses={}
        for wh in WAREHOUSES:
            box=QCheckBox(wh);box.setChecked(True);row.addWidget(box);self.warehouses[wh]=box
        from stockout_common import read_json
        pref=host.repository.folder/'analysis'/'stockout-hub-selection.json'
        self.preference_error=''
        if pref.exists():
            try:
                saved=read_json(pref,8192)
                if saved.get('mode') in MODES:self.mode.setCurrentIndex(self.mode.findData(saved['mode']))
                for wh,box in self.warehouses.items():box.setChecked(wh in saved.get('warehouses',WAREHOUSES))
            except (ValueError,OSError,AttributeError,TypeError):self.preference_error='上次范围偏好未读取，请重新选择；分析结果未变。'
        self.collect=button('按所选范围采集',self.start_collect,True);row.addWidget(self.collect)
        self.stop=button('安全暂停',lambda:entry.pause());row.addWidget(self.stop)
        self.background=QCheckBox('离开后继续');self.background.toggled.connect(self.background_changed);row.addWidget(self.background)
        row.addWidget(button('每日计划',self.schedule));v.addLayout(row)
        row=QHBoxLayout();self.scope=label('门店范围沿用下方已采用设置；仓勾选为大仓输出范围','muted');row.addWidget(self.scope)
        self.runs=QComboBox();self.runs.setMinimumWidth(200);row.addWidget(self.runs,1)
        self.resume=button('继续原作业',self.continue_run);row.addWidget(self.resume)
        self.end=button('结束本地准备',self.end_run);row.addWidget(self.end)
        self.reuse=button('原仓资料补算大仓',self.reuse_run);row.addWidget(self.reuse)
        self.export_btn=button('统一导出',self.export);row.addWidget(self.export_btn);v.addLayout(row)
        self.tabs=QTabBar();self.tabs.addTab('门店缺货');self.tabs.addTab('大仓缺货');v.addWidget(self.tabs)
        self.body=QVBoxLayout();v.addLayout(self.body,1);self.note=label('按需加载；固定剔除虚拟分类淘便考核','muted');v.addWidget(self.note)
        self.tabs.setCurrentIndex(1 if getattr(entry,'initial_kind','store')=='warehouse' else 0)
        self.tabs.currentChanged.connect(self.show_child);self.show_child(self.tabs.currentIndex())
        self.runs.currentIndexChanged.connect(self.show_run_state)
    def background_changed(self,value):
        if self.entry.worker:self.entry.background=value
    def show_child(self,index):
        kind='store' if index==0 else 'warehouse'
        if self.current==kind:return
        if self.current:
            old=self.entry.children[self.current]
            if old.page:
                self.queries[self.current]=old.page.options()
                if old.worker and not old.background:old.pause('切换分析子页，正在安全暂停；可在原作业继续')
                self.body.removeWidget(old.page);old.page.deleteLater();old.page=None;old.callback=None;old.generation+=1
        self.current=kind;child=self.entry.children[kind];child.generation+=1;child.index=self.entry.index
        child.page=child.make_page();self.body.addWidget(child.page)
        child.page.note.hide()
        for i in (0,1):
            widget=child.page.layout().itemAt(i).widget()
            if widget:widget.hide()
        if kind in self.queries:
            query=self.queries[kind];child.page.filters=query.get('filters',{}).copy()
            from PySide6.QtCore import QDate
            for name in ('start','end'):
                if query.get(name):
                    widget=getattr(child.page,name);widget.blockSignals(True);widget.setDate(QDate.fromString(query[name],'yyyy-MM-dd'));widget.blockSignals(False)
        # Collection/scheduling for new work is owned by the three explicit hub modes.
        for name in ('collect','stop','background','schedule'):
            widget=getattr(child.page,name,None)
            if hasattr(widget,'hide'):widget.hide()
        child.page.refresh_metadata()
    def controls(self):
        busy=bool(self.entry.worker or any(c.worker for c in self.entry.children.values()))
        self.collect.setEnabled(not busy);self.stop.setEnabled(busy)
        self.export_btn.setEnabled(not busy)
        self.resume.setEnabled(not busy and self.runs.currentData() is not None);self.end.setEnabled(not busy and self.runs.currentData() is not None);self.reuse.setEnabled(not busy and self.runs.currentData() is not None)
    def refresh_metadata(self):self.entry.start('hub_open',{},self.metadata)
    def metadata(self,value):
        self.metadata_value=value;prior=self.runs.currentData();self.runs.blockSignals(True);self.runs.clear()
        for r in value['runs']:
            state={'PREPARED':'待开始','RUNNING':'进行中','COMPLETE':'全部完成','PARTIAL':'部分完成','PAUSED':'已暂停','REVIEW':'需继续核对','ENDED':'已结束'}.get(r['state'],r['state'])
            self.runs.addItem(r['created'][5:16]+' · '+state+' · '+str(len(r['stores']))+'店 / '+str(len(r['warehouses']))+'仓',r['id'])
            self.runs.setItemData(self.runs.count()-1,'；'.join(r.get('errors',{}).values()),Qt.ItemDataRole.ToolTipRole)
        self.runs.setCurrentIndex(max(0,self.runs.findData(prior)));self.runs.blockSignals(False);self.scope.setText(f"分析已采用 {len(value['stores'])} 家门店；单跑不更新另一个子页")
        self.show_run_state()
        self.controls()
    def show_run_state(self,*_):
        row=next((r for r in self.metadata_value.get('runs',[]) if r['id']==self.runs.currentData()),None)
        if row:self.completed(row)
        elif self.preference_error:self.note.setText(self.preference_error)
    def selected(self):return [w for w,b in self.warehouses.items() if b.isChecked()]
    def start_collect(self):
        def ready(plan):
            from stockout_hub_service import MODES
            text=f"{MODES[plan['mode']]}\n门店 {len(plan['stores'])} 家\n大仓输出：{'、'.join(plan['warehouses']) or '不输出'}\n所需仓资料：{'、'.join(plan['required']) or '无'}\n同仓只采一次，两个缺货率分别计算；不会更新补货侧资料。"
            if QMessageBox.question(self,'确认本次分析范围',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
            from stockout_common import atomic_json
            atomic_json(self.host.repository.folder/'analysis'/'stockout-hub-selection.json',{'mode':self.mode.currentData(),'warehouses':self.selected()})
            self.entry.start('hub_collect',{'plan':plan},self.completed,background=self.background.isChecked())
        self.entry.start('hub_preview',{'mode':self.mode.currentData(),'warehouses':self.selected()},ready)
    def completed(self,row):
        names={'COMPLETE':'全部完成','PARTIAL':'已保存部分结果，可继续未完成项','PAUSED':'已安全暂停，可继续原作业','ENDED':'本地已结束；已完成结果和原平台任务保留','REVIEW':'原作业需继续核对'}
        self.note.setText(names.get(row['state'],row['state'])+'；'+'；'.join(row.get('errors',{}).values()))
    def continue_run(self):
        if self.runs.currentData():self.entry.start('hub_collect',{'resume':self.runs.currentData()},self.completed,background=self.background.isChecked())
    def end_run(self):
        if self.runs.currentData():self.entry.start('hub_end',{'run':self.runs.currentData()},self.completed)
    def reuse_run(self):
        rid=self.runs.currentData()
        if not rid:return
        row=next((r for r in self.metadata_value.get('runs',[]) if r['id']==rid),None)
        if not row:return
        text='仅用该作业原仓资料补算，不连接平台，不更新观测日期。\n日期：'+row['day']+'\n仓：'+'、'.join(row['required'])
        if QMessageBox.question(self,'确认原仓资料范围',text,QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)==QMessageBox.StandardButton.Yes:self.entry.start('hub_reuse',{'run':rid},lambda _:self.note.setText('已按原观测日期补算大仓；在大仓子页查询'))
    def schedule(self):
        from stockout_hub_schedule import plans,save
        from stockout_hub_service import MODES
        dialog=QDialog(self);dialog.setWindowTitle('缺货率每日计划');v=QVBoxLayout(dialog)
        rows=plans(self.host.repository.folder)
        v.addWidget(label('当前计划：'+'；'.join(MODES[r['mode']]+' '+r['at'] for r in rows) if rows else '当前未启用每日计划'))
        v.addWidget(label('保存将替换上列计划。软件运行时执行，忙时让行；缺采日期不补0。'))
        enabled=QCheckBox('启用每日采集');enabled.setChecked(bool(rows));v.addWidget(enabled)
        mode=QComboBox()
        for key,text in MODES.items():mode.addItem(text,key)
        mode.setCurrentIndex(mode.findData(rows[0]['mode'] if len(rows)==1 else self.mode.currentData()));v.addWidget(mode)
        at=QLineEdit(rows[0]['at'] if rows else '08:00');v.addWidget(at)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);v.addWidget(buttons)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            try:save(self.host.repository.folder,enabled.isChecked(),at.text().strip(),mode.currentData(),self.selected());self.entry.arm();self.note.setText('计划已保存；启停不撤销已受理的导出')
            except ValueError as exc:QMessageBox.information(self,'计划未保存',str(exc))
    def export(self):
        from stockout_hub_export_ui import show
        show(self)
