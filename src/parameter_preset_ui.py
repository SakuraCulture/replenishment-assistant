"""Business-facing preset library, store assignment and per-store plan editor."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLineEdit,QTableWidget,QTableWidgetItem,
                              QComboBox,QMessageBox,QHeaderView)
from ui_controls import button,label
from interaction_ui import confirm,review_rows
import parameter_presets as pp
import replenishment_plans as plans


def title(p):return '系统默认参数（不可删除）' if p['id']=='standard' else p['name']


class PresetLibrary(QDialog):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.repo=host.work_repository
        self.setWindowTitle('参数预案');self.resize(1000,650)
        v=QVBoxLayout(self);v.addWidget(label('完整参数预案 · 分类参数、通用参数、分仓备货与到货天数','section'))
        v.addWidget(label('分类映射全局共用。保存修改用于后续采用；当前作业、历史签发不变。主从仓与预算在门店独立设置。','muted'))
        self.table=QTableWidget(0,4);self.table.setHorizontalHeaderLabels(['参数预案','版本','门店默认引用','补货预案指定引用'])
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows);self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);v.addWidget(self.table,1)
        row=QHBoxLayout()
        for text,fn in [('新建参数预案',self.create),('查看参数',self.view),('复制新建',lambda:self.edit(True)),('编辑预案',self.edit),('删除预案',self.remove),('关闭',self.accept)]:row.addWidget(button(text,fn))
        v.addLayout(row);self.refresh()
    def refresh(self):
        self.data=pp.snapshot(self.repo);self.values=list(pp.visible(self.data).values());self.table.setRowCount(len(self.values))
        for n,p in enumerate(self.values):
            stores,refs=pp.references(self.data,p['id'])
            for col,value in enumerate((title(p),p['version'],len(stores),len(refs))):self.table.setItem(n,col,QTableWidgetItem(str(value)))
        self.table.selectRow(0)
    def current(self):
        n=self.table.currentRow();return self.values[n] if 0<=n<len(self.values) else None
    def view(self):
        p=self.current()
        if p:
            from parameter_ui import ParameterDialog
            ParameterDialog(self.host,p['config'],p,scope=title(p),readonly=True).exec()
    def create(self):self.edit(True,preset={**pp.resolve(self.data,'standard'),'name':'新参数预案'})
    def edit(self,copying=False,preset=None):
        p=preset or self.current()
        if not p:return
        if p['id']=='standard' and not copying:
            QMessageBox.information(self,'系统默认只读','请使用“复制新建”。系统默认参数始终保留。');return
        from parameter_ui import ParameterDialog
        d=ParameterDialog(self.host,p['config'],p,scope='所选参数预案 · '+title(p),intent='plan')
        d.setWindowTitle('复制新建参数预案' if copying else '编辑参数预案')
        name=QLineEdit(p['name']+' 副本' if copying else p['name']);name.setPlaceholderText('参数预案名称');d.layout().insertWidget(0,name)
        from PySide6.QtWidgets import QPushButton
        for b in d.findChildren(QPushButton):
            if b.text()=='采用到计划表单':b.setText('核对并保存参数预案')
        if d.exec()!=QDialog.DialogCode.Accepted:return
        try:
            codes,refs=pp.references(self.data,p['id']) if not copying else ([],[])
            if not confirm(self,'保存参数预案',f'保存“{name.text()}”。后续采用时影响 {len(codes)} 家门店默认、{len(refs)} 条补货预案指定；当前作业与历史保持不变。'):return
            pp.save(self.repo,name.text(),d.config,None if copying else p['id'],self.data);self.refresh()
        except Exception as exc:QMessageBox.warning(self,'预案未保存',str(exc))
    def remove(self):
        p=self.current()
        if not p:return
        if p['id']=='standard':QMessageBox.information(self,'不能删除','系统默认参数不可删除，可复制新建其他预案。');return
        stores,refs=pp.references(self.data,p['id'])
        d=QDialog(self);d.setWindowTitle('删除参数预案 · 替换引用');v=QVBoxLayout(d)
        v.addWidget(label(f'删除“{p["name"]}”。当前作业与历史保留原参数；引用处必须选择替换预案。'))
        table=QTableWidget(len(stores)+len(refs),2);table.setHorizontalHeaderLabels(['引用位置','门店']);table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        rows=[('门店默认',self.data['stores'][s].get('name') or s) for s in stores]+[(plan['name'],item['name']) for plan,item in refs]
        for i,row in enumerate(rows):
            for j,x in enumerate(row):table.setItem(i,j,QTableWidgetItem(x))
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);v.addWidget(table)
        choices=QComboBox()
        choices.addItem('取消引用，之后按门店默认或自动填充',pp.FOLLOW_DEFAULTS)
        for option in self.values:
            if option['id']!=p['id']:choices.addItem(title(option),option['id'])
        v.addWidget(label('将上述引用替换为：'));v.addWidget(choices)
        row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button('确认删除并替换引用',d.accept));v.addLayout(row)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        try:pp.delete(self.repo,p['id'],choices.currentData(),self.data);self.refresh()
        except Exception as exc:QMessageBox.warning(self,'预案未删除',str(exc))


def assign_defaults(scene):
    codes=scene.require()
    if not codes:return
    host=scene.host;data=pp.snapshot(host.work_repository)
    d=QDialog(scene);d.setWindowTitle('选择门店默认参数预案');v=QVBoxLayout(d)
    v.addWidget(label(f'为 {len(codes)} 家门店选择完整预案。预算、主从仓保留；当前作业不变。'))
    choices=QComboBox()
    for p in pp.visible(data).values():choices.addItem(title(p),p['id'])
    if not choices.count():
        QMessageBox.information(scene,'尚无参数预案','请先在“参数预案”中新建。未保存门店参数时，计算准备会自动填写。');return
    v.addWidget(choices);row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button('核对差异',d.accept,True));v.addLayout(row)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    from interaction_rules import configuration_changes
    from store_parameter_import import base
    p=pp.resolve(data,choices.currentData());rows=[]
    for s in codes:
        old=(data['stores'].get(s) or base())['preset'];target=copy.deepcopy(p['config']);target['category_profiles']=old['config']['category_profiles']
        rows.append([s,'默认参数预案',old['name'],title(p)])
        rows+=configuration_changes({s:old['config']},target)
    if not review_rows(scene,'核对门店默认',['门店','参数','原值','新值'],rows,'只影响后续采用；本次如需使用，请重新采用预案或显式应用参数。','保存默认选择'):return
    try:pp.assign(host.work_repository,codes,p['id'],data);scene.refresh()
    except Exception as exc:QMessageBox.warning(scene,'默认未保存',str(exc))


def delete_store_defaults(scene):
    import store_defaults
    codes=scene.require()
    if not codes:return
    repo=scene.host.work_repository;data=pp.snapshot(repo)
    codes=[s for s in codes if store_defaults.has_parameters(data['stores'].get(s))]
    if not codes:QMessageBox.information(scene,'没有可删除的参数','所选门店均未保存默认参数。');return
    rows=[[data['stores'][s].get('name') or s,data['stores'][s]['preset']['name'],'下次计算准备自动填写'] for s in codes]
    if not review_rows(scene,'删除门店默认参数',['门店','将删除的默认参数','删除后'],rows,
        '仅删除上述门店保存的参数及预案绑定。供货仓、预算、其他门店、当前作业与历史签发保留。跟随门店默认的补货预案下次采用时自动填充；明确指定其他预案的门店仍按指定预案。','确认删除默认参数'):return
    try:
        from business_ui import run
        def remove(progress=lambda _:None,cancel=None):
            progress('正在删除所选门店默认参数…');return pp.clear_defaults(repo,codes,data)
        run(scene,remove,(),'删除默认参数',cancellable=False)
        scene.refresh();scene.note.setText(f'已删除 {len(codes)} 家门店默认参数；当前作业未改变，下次计算准备自动填写。')
    except Exception as exc:QMessageBox.warning(scene,'默认参数未删除',str(exc))


def copy_store_default(scene):
    codes=scene.require()
    if len(codes)!=1:
        if codes:QMessageBox.information(scene,'请选择一家门店','选一家门店作为复制来源；保存的新预案可供多家门店选择。')
        return
    library=PresetLibrary(scene.host)
    try:library.edit(True,preset=scene.preset(codes[0]))
    finally:library.deleteLater();scene.refresh()


class StorePlanEditor(QDialog):
    def __init__(self,host,value=None,copying=False):
        super().__init__(host);self.host=host;self.expected=None if copying else value
        self.setWindowTitle('补货预案 · 每店选择参数');self.resize(1050,700)
        self.data=pp.snapshot(host.work_repository);self.options=pp.visible(self.data)
        v=QVBoxLayout(self);self.name=QLineEdit(value['name']+(' 副本' if copying else '') if value else '')
        self.name.setPlaceholderText('补货预案名称，例如青岛线');v.addWidget(self.name)
        self.note=QLineEdit(value.get('note','') if value else '');self.note.setPlaceholderText('备注（可选）');v.addWidget(self.note)
        v.addWidget(label('勾选门店，按顺序执行。各店默认跟随门店默认，也可指定完整参数预案。预算与主从仓不在这里设置。','muted'))
        if value and value.get('parameter_mode')!=pp.MODE:v.addWidget(label('正在转换旧预案：保存后不再使用旧勾选字段；请逐店核对参数选择。','section'))
        self.search=QLineEdit();self.search.setPlaceholderText('搜索门店');self.search.textChanged.connect(self.filter);v.addWidget(self.search)
        self.table=QTableWidget(0,3);self.table.setHorizontalHeaderLabels(['选择','门店','本预案采用的参数']);self.table.setColumnWidth(0,60);self.table.setColumnWidth(1,400);self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(self.table,1)
        chosen={i['code']:i for i in (value or {}).get('stores',[])}
        names={s:i['name'] for s,i in chosen.items()};names.update({s:n for s,n in plans.available(host).items() if s not in names})
        self.rows=[{'code':s,'name':n,'selected':s in chosen if value else s in host.selected_codes(),'preset_id':chosen.get(s,{}).get('preset_id')} for s,n in names.items()]
        self.fill()
        row=QHBoxLayout();row.addWidget(button('勾选当前筛选',lambda:self.check_visible(True)));row.addWidget(button('清空全部勾选',lambda:self.check_visible(False)));row.addWidget(button('↑ 上移',lambda:self.move(-1)));row.addWidget(button('↓ 下移',lambda:self.move(1)));v.addLayout(row)
        row=QHBoxLayout();self.batch=QComboBox();self.batch.addItem('跟随门店默认',None)
        for p in self.options.values():self.batch.addItem(title(p),p['id'])
        row.addWidget(self.batch,1);row.addWidget(button('设置所有勾选门店的参数选择',self.set_selected));v.addLayout(row)
        row=QHBoxLayout();row.addWidget(button('取消',self.reject));row.addWidget(button('保存补货预案',self.save,True));v.addLayout(row)
    def fill(self):
        self.table.setRowCount(len(self.rows))
        for i,r in enumerate(self.rows):
            check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);check.setCheckState(Qt.CheckState.Checked if r['selected'] else Qt.CheckState.Unchecked);self.table.setItem(i,0,check)
            self.table.setItem(i,1,QTableWidgetItem(r['name']+' · '+r['code']))
            from store_defaults import has_parameters
            saved=self.data['stores'].get(r['code']);default=saved['preset']['name'] if has_parameters(saved) else '未保存参数，准备时自动填写'
            box=QComboBox();box.addItem('跟随门店默认 · '+default,None)
            for p in self.options.values():box.addItem(title(p),p['id'])
            index=box.findData(r['preset_id'])
            if index<0:box.addItem('原有固定配置（兼容保留）' if r['preset_id']=='standard' else '原预案已不可用，请重新选择',r['preset_id']);index=box.count()-1
            box.setCurrentIndex(index);self.table.setCellWidget(i,2,box)
        self.filter()
    def collect(self):
        for i,r in enumerate(self.rows):r.update(selected=self.table.item(i,0).checkState()==Qt.CheckState.Checked,preset_id=self.table.cellWidget(i,2).currentData())
    def filter(self):
        text=self.search.text().lower()
        for i,r in enumerate(self.rows):self.table.setRowHidden(i,text not in (r['name']+' '+r['code']).lower())
    def check_visible(self,checked):
        for i in range(len(self.rows)):
            if not checked or not self.table.isRowHidden(i):self.table.item(i,0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
    def set_selected(self):
        for i in range(len(self.rows)):
            if self.table.item(i,0).checkState()==Qt.CheckState.Checked:
                box=self.table.cellWidget(i,2);box.setCurrentIndex(box.findData(self.batch.currentData()))
    def move(self,delta):
        i=self.table.currentRow();j=i+delta
        if 0<=i<len(self.rows) and 0<=j<len(self.rows):
            self.collect();self.rows[i],self.rows[j]=self.rows[j],self.rows[i];self.fill();self.table.setCurrentCell(j,1)
    def save(self):
        try:
            self.collect();value={'name':self.name.text().strip(),'note':self.note.text().strip(),'parameter_mode':pp.MODE,
                'stores':[{k:r[k] for k in ('code','name','preset_id')} for r in self.rows if r['selected']], 'config':__import__('parameter_schema').standard()}
            plans.validate(value)
            from business_ui import run
            self.saved=run(self,plans.persist,(self.host.work_repository.path,value,self.expected),'保存补货预案',cancellable=False);self.accept()
        except Exception as exc:QMessageBox.warning(self,'预案未保存',str(exc))
