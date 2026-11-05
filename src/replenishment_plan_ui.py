"""Choose once on the preparation page; advanced maintenance stays optional."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QTableWidget,QTableWidgetItem,QMessageBox,QComboBox,QListWidget,QAbstractItemView
from ui_controls import button,label
from interaction_ui import confirm,review_rows
from parameter_ui import ParameterDialog
import replenishment_plans as plans
import parameter_schema as schema

class Editor(QDialog):
    def __init__(self,host,value=None,copying=False):
        super().__init__(host);self.host=host;self.expected=None if copying else value;self.config=copy.deepcopy(value['config'] if value else schema.standard())
        import parameter_maintenance as pm
        self.parameter_fields=copy.deepcopy(value.get('parameter_fields',[list(f) for f in pm.fields([self.config])+pm.fields([self.config],'common')]) if value else [])
        self.setWindowTitle('补货预案 · 门店名单与勾选参数');self.resize(1000,650)
        v=QVBoxLayout(self);self.name=QLineEdit((value['name']+(' 副本' if copying else '')) if value else '');self.name.setPlaceholderText('计划名称，例如青岛线');v.addWidget(self.name)
        self.note=QLineEdit(value.get('note','') if value else '');self.note.setPlaceholderText('备注（可选）');v.addWidget(self.note)
        v.addWidget(label('勾选门店，拖动行调整顺序。预案只采用明确勾选的参数；分类映射全局共用，其他参数、预算和主从仓保留各店原值。','muted'))
        search=QLineEdit();search.setPlaceholderText('搜索门店名称或编码');v.addWidget(search)
        self.stores=QListWidget();self.stores.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove);v.addWidget(self.stores,1)
        known=plans.available(host);selected={i['code'] for i in value['stores']} if value else set(host.selected_codes())
        names={i['code']:i['name'] for i in value['stores']} if value else {}
        names.update({s:n for s,n in known.items() if s not in names})
        for s,n in names.items():
            from PySide6.QtWidgets import QListWidgetItem
            item=QListWidgetItem(n+' · '+s);item.setData(Qt.ItemDataRole.UserRole,{'code':s,'name':n});item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if s in selected else Qt.CheckState.Unchecked);self.stores.addItem(item)
        search.textChanged.connect(lambda text:[self.stores.item(i).setHidden(text.lower() not in self.stores.item(i).text().lower()) for i in range(self.stores.count())])
        row=QHBoxLayout();self.sources=QComboBox();self.sources.addItem('选择已保存或本次参数',None)
        for s in host.selected_codes():self.sources.addItem('本次：'+host.loaded['stores'][s],host.config_for(s))
        for p in host.work_repository.presets():
            if not p.get('builtin'):self.sources.addItem('已保存方案：'+p['name'],p['config'])
        row.addWidget(self.sources,1);row.addWidget(button('用所选补货参数填充',self.use_source));row.addWidget(button('设置预案补货参数',self.edit));row.addWidget(button('设置预案通用参数',lambda:self.edit('common')));v.addLayout(row)
        self.summary=label('','muted');v.addWidget(self.summary);self.update_summary()
        row=QHBoxLayout();row.addWidget(button('取消，不保存',self.reject));row.addWidget(button('保存补货计划',self.save,True));v.addLayout(row)
    def update_summary(self):
        import parameter_maintenance as pm
        self.summary.setText(f'预案采用 {len(self.parameter_fields)} 项参数；未选项保留各店原值。采用后可单店调整。')
        self.summary.setToolTip('\n'.join(pm.title(tuple(f))+'：'+str(pm.value(self.config,tuple(f))) for f in self.parameter_fields))
    def use_source(self):
        value=self.sources.currentData()
        if value is None:return
        if value!=self.config and not confirm(self,'替换计划表单参数','用“'+self.sources.currentText()+'”填充当前计划表单；不影响本次作业或门店默认。'):return
        import parameter_maintenance as pm
        selected=pm.fields([value]);self.config=pm.patch(self.config,{f:pm.value(value,f) for f in selected})
        self.parameter_fields=[list(f) for f in selected]+[f for f in self.parameter_fields if f[0]=='parameters'];self.update_summary()
    def edit(self,section='store'):
        from parameter_maintenance_ui import PatchDialog
        import parameter_maintenance as pm
        if isinstance(section,bool):section='store'
        d=PatchDialog(self,{'预案':self.config},section,self.parameter_fields,allow_empty=True)
        if d.exec()==QDialog.DialogCode.Accepted:
            self.config=pm.patch(self.config,d.changes)
            section_fields=set(pm.fields([self.config],section))
            self.parameter_fields=[f for f in self.parameter_fields if tuple(f) not in section_fields]+[list(f) for f in d.changes];self.update_summary()
    def save(self):
        try:
            value={'name':self.name.text().strip(),'note':self.note.text().strip(),'stores':[self.stores.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.stores.count()) if self.stores.item(i).checkState()==Qt.CheckState.Checked],'config':self.config,'parameter_fields':self.parameter_fields}
            plans.validate(value)
            from business_ui import run
            self.saved=run(self,plans.persist,(self.host.work_repository.path,value,self.expected),'保存补货计划',cancellable=False);self.accept()
        except Exception as exc:QMessageBox.warning(self,'计划未保存',str(exc))

class Library(QDialog):
    def __init__(self,host,select=False):
        super().__init__(host);self.host=host;self.select=select;self.repo=plans.Repository(host.work_repository.path);self.setWindowTitle('选择补货计划' if select else '维护补货计划');self.resize(950,600)
        v=QVBoxLayout(self);v.addWidget(label('补货预案保存门店名单及各店的参数选择。采用后冻结本次配置，不自动取数、计算或签发。','section'))
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['计划','门店数','更新时间','周期摘要']);self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.setColumnWidth(0,210);self.table.setColumnWidth(2,180);self.table.horizontalHeader().setStretchLastSection(True);v.addWidget(self.table,1)
        row=QHBoxLayout();row.addWidget(button('新建补货计划',lambda:self.edit(new=True)));row.addWidget(button('编辑选中计划',self.edit));row.addWidget(button('复制选中计划',lambda:self.edit(copying=True)));row.addWidget(button('删除选中计划',self.remove));v.addLayout(row)
        v.addWidget(button('将旧预案改为逐店选择参数',self.convert))
        row=QHBoxLayout();row.addWidget(button('关闭',self.reject))
        if select:row.addWidget(button('采用到本次作业',self.adopt,True))
        v.addLayout(row);self.refresh()
    def refresh(self):
        self.values=self.repo.list();self.table.setRowCount(len(self.values))
        for i,p in enumerate(self.values):
            for j,x in enumerate([p['name'],len(p['stores']),p['updated'],'；'.join(w+f' {v[0]:g}+{v[1]:g}天' for w,v in p['config']['warehouses'].items())]):self.table.setItem(i,j,QTableWidgetItem(str(x)))
            self.table.item(i,3).setText('逐店选择参数预案' if p.get('parameter_mode')=='per_store_presets' else '旧预案 · 保持原勾选参数')
        if self.values:self.table.selectRow(0)
    def current(self):
        r=self.table.currentRow();return self.values[r] if 0<=r<len(self.values) else None
    def edit(self,*,new=False,copying=False):
        value=None if new else self.current()
        if not new and value is None:return
        from parameter_preset_ui import StorePlanEditor
        cls=StorePlanEditor if value is None or value.get('parameter_mode')=='per_store_presets' else Editor
        d=cls(self.host,value,copying)
        if d.exec()==QDialog.DialogCode.Accepted:self.refresh()
    def convert(self):
        p=self.current()
        if not p or p.get('parameter_mode')=='per_store_presets':return
        if not confirm(self,'转换旧补货预案','转换后逐店选择完整参数，原勾选字段不再叠加。先在编辑表核对各店选择，保存才生效；当前作业和历史不变。'):return
        from parameter_preset_ui import StorePlanEditor
        d=StorePlanEditor(self.host,p)
        if d.exec()==QDialog.DialogCode.Accepted:self.refresh()
    def remove(self):
        p=self.current()
        if not p or not confirm(self,'删除补货计划','删除 '+p['name']+'；已经采用的作业与历史签发保留当时副本。'):return
        try:
            from business_ui import run
            run(self,plans.remove,(self.host.work_repository.path,p),'删除补货计划',cancellable=False);self.refresh()
        except Exception as exc:QMessageBox.warning(self,'未删除',str(exc))
    def adopt(self):
        p=self.current()
        if not p:return
        if self.host.worker or self.host.readonly:QMessageBox.information(self,'暂不能采用','请结束当前任务；历史作业只读。');return
        try:
            from parameter_presets import freeze
            p=freeze(p,self.host.work_repository)
        except ValueError as exc:QMessageBox.warning(self,'预案未采用',str(exc));return
        if self.host.loaded or self.host.job_dirty:
            from interaction_rules import configuration_changes
            configs={s:self.host.config_for(s) for s in (i['code'] for i in p['stores']) if s in self.host.store_widgets}
            import parameter_maintenance as pm
            mapping=pm.mapping_snapshot(self.host.work_repository.path)
            if mapping['conflict']:QMessageBox.warning(self,'预案未采用','请先统一分类映射');return
            rows=[]
            for s,c in configs.items():
                from workbench_actions import plan_record
                target=plan_record(p,{'preset':{'config':c},'ui':{}},mapping,s)['preset']['config'];rows+=configuration_changes({s:c},target)
            rows=[['本次名单','门店','当前已选 '+str(len(self.host.selected_codes()))+' 家','、'.join(i['name'] for i in p['stores'])],*rows]
            note='采用各店选定的完整参数及全局映射；原本次结果失效，预算和主从仓保留。' if p.get('parameter_mode')=='per_store_presets' else '采用旧预案勾选参数及全局映射；原本次结果失效，其他参数、预算和主从仓保留。'
            if not review_rows(self,'采用预案并核对范围',['范围','项目','原值','新值'],rows,note,'采用到本次作业'):return
        try:plans.apply(self.host,p);self.accept()
        except Exception as exc:QMessageBox.warning(self,'预案未采用',str(exc))

def open_library(host,select=False):
    if host.worker:return
    Library(host,select).exec()
