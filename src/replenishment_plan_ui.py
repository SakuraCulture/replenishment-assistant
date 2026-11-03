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
        self.setWindowTitle('补货计划 · 门店名单与一套完整预设');self.resize(900,650)
        v=QVBoxLayout(self);self.name=QLineEdit((value['name']+(' 副本' if copying else '')) if value else '');self.name.setPlaceholderText('计划名称，例如青岛线');v.addWidget(self.name)
        self.note=QLineEdit(value.get('note','') if value else '');self.note.setPlaceholderText('备注（可选）');v.addWidget(self.note)
        v.addWidget(label('勾选门店，拖动行调整先后顺序。两仓周期和分类超参作为完整值保存；预算、主从仓各店保留。','muted'))
        search=QLineEdit();search.setPlaceholderText('搜索门店名称或编码');v.addWidget(search)
        self.stores=QListWidget();self.stores.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove);v.addWidget(self.stores,1)
        known=plans.available(host);selected={i['code'] for i in value['stores']} if value else set(host.selected_codes())
        names={i['code']:i['name'] for i in value['stores']} if value else {}
        names.update({s:n for s,n in known.items() if s not in names})
        for s,n in names.items():
            from PySide6.QtWidgets import QListWidgetItem
            item=QListWidgetItem(n+' · '+s);item.setData(Qt.ItemDataRole.UserRole,{'code':s,'name':n});item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if s in selected else Qt.CheckState.Unchecked);self.stores.addItem(item)
        search.textChanged.connect(lambda text:[self.stores.item(i).setHidden(text.lower() not in self.stores.item(i).text().lower()) for i in range(self.stores.count())])
        row=QHBoxLayout();self.sources=QComboBox();self.sources.addItem('系统标准参数',schema.standard())
        for s in host.selected_codes():self.sources.addItem('本次：'+host.loaded['stores'][s],host.config_for(s))
        for p in host.work_repository.presets():self.sources.addItem('已保存方案：'+p['name'],p['config'])
        row.addWidget(self.sources,1);row.addWidget(button('用所选参数填充',self.use_source));row.addWidget(button('编辑计划完整预设',self.edit));v.addLayout(row)
        self.summary=label('','muted');v.addWidget(self.summary);self.update_summary()
        row=QHBoxLayout();row.addWidget(button('取消，不保存',self.reject));row.addWidget(button('保存补货计划',self.save,True));v.addLayout(row)
    def update_summary(self):self.summary.setText('当前预设：'+'；'.join(w+f' 备货{x[0]:g}天/到货{x[1]:g}天' for w,x in self.config['warehouses'].items())+'。每店共同使用这份完整参数，采用后可单店调整。')
    def use_source(self):
        value=self.sources.currentData()
        if value!=self.config and not confirm(self,'替换计划表单参数','用“'+self.sources.currentText()+'”填充当前计划表单；不影响本次作业或门店默认。'):return
        self.config=copy.deepcopy(value);self.update_summary()
    def edit(self):
        p={'id':'plan-form','name':self.name.text() or '计划预设','version':1,'builtin':False,'config':self.config}
        d=ParameterDialog(self.host,self.config,p,scope='所选计划的完整预设',intent='plan')
        if d.exec()==QDialog.DialogCode.Accepted:self.config=copy.deepcopy(d.config);self.update_summary()
    def save(self):
        try:
            value={'name':self.name.text().strip(),'note':self.note.text().strip(),'stores':[self.stores.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.stores.count()) if self.stores.item(i).checkState()==Qt.CheckState.Checked],'config':self.config}
            plans.validate(value)
            self.saved=plans.Repository(self.host.work_repository.path).save(value,self.expected);self.accept()
        except Exception as exc:QMessageBox.warning(self,'计划未保存',str(exc))

class Library(QDialog):
    def __init__(self,host,select=False):
        super().__init__(host);self.host=host;self.select=select;self.repo=plans.Repository(host.work_repository.path);self.setWindowTitle('选择补货计划' if select else '维护补货计划');self.resize(950,600)
        v=QVBoxLayout(self);v.addWidget(label('一份计划保存门店名单和一套完整预设。采用只带入本次，不自动取数、计算或签发。','section'))
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['计划','门店数','更新时间','周期摘要']);self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.setColumnWidth(0,210);self.table.setColumnWidth(2,180);self.table.horizontalHeader().setStretchLastSection(True);v.addWidget(self.table,1)
        row=QHBoxLayout();row.addWidget(button('新建补货计划',lambda:self.edit(new=True)));row.addWidget(button('编辑选中计划',self.edit));row.addWidget(button('复制选中计划',lambda:self.edit(copying=True)));row.addWidget(button('删除选中计划',self.remove));v.addLayout(row)
        row=QHBoxLayout();row.addWidget(button('关闭',self.reject))
        if select:row.addWidget(button('采用到本次作业',self.adopt,True))
        v.addLayout(row);self.refresh()
    def refresh(self):
        self.values=self.repo.list();self.table.setRowCount(len(self.values))
        for i,p in enumerate(self.values):
            for j,x in enumerate([p['name'],len(p['stores']),p['updated'],'；'.join(w+f' {v[0]:g}+{v[1]:g}天' for w,v in p['config']['warehouses'].items())]):self.table.setItem(i,j,QTableWidgetItem(str(x)))
        if self.values:self.table.selectRow(0)
    def current(self):
        r=self.table.currentRow();return self.values[r] if 0<=r<len(self.values) else None
    def edit(self,*,new=False,copying=False):
        value=None if new else self.current()
        if not new and value is None:return
        d=Editor(self.host,value,copying)
        if d.exec()==QDialog.DialogCode.Accepted:self.refresh()
    def remove(self):
        p=self.current()
        if not p or not confirm(self,'删除补货计划','删除 '+p['name']+'；已经采用的作业与历史签发保留当时副本。'):return
        try:self.repo.delete(p);self.refresh()
        except Exception as exc:QMessageBox.warning(self,'未删除',str(exc))
    def adopt(self):
        p=self.current()
        if not p:return
        if self.host.worker or self.host.readonly:QMessageBox.information(self,'暂不能采用','请结束当前任务；历史作业只读。');return
        if self.host.loaded or self.host.job_dirty:
            from interaction_rules import configuration_changes
            configs={s:self.host.config_for(s) for s in (i['code'] for i in p['stores']) if s in self.host.store_widgets}
            rows=configuration_changes(configs,p['config'])
            rows=[['本次名单','门店','当前已选 '+str(len(self.host.selected_codes()))+' 家','、'.join(i['name'] for i in p['stores'])],*rows]
            if not review_rows(self,'采用计划并核对替换范围',['范围','项目','原值','新值'],rows,'所选计划门店将采用完整预设；原本次计算结果失效，预算和主从仓保留。','采用到本次作业'):return
        plans.apply(self.host,p);self.accept()

def open_library(host,select=False):
    if host.worker:return
    Library(host,select).exec()
