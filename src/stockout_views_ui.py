"""Editors for optional analysis views; candidate values come from a bounded worker query."""
from copy import deepcopy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QListWidget,QListWidgetItem,
    QLineEdit,QDialogButtonBox,QCheckBox,QMessageBox,QAbstractItemView)
from ui_controls import button
from stockout_metrics import DIMENSIONS,dimension_value
from stockout_views import validate,combinations

NAMES={**{k:v[1] for k,v in DIMENSIONS.items()},'store':'门店','sku':'商品'}


def describe(dimensions):return ' ＋ '.join(NAMES[k] for k in dimensions)


def check_item(listing,text,value,checked=False):
    item=QListWidgetItem(text);item.setToolTip(text);item.setData(Qt.ItemDataRole.UserRole,value)
    item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked);listing.addItem(item);return item


class CombinationDialog(QDialog):
    def __init__(self,parent,config,current):
        super().__init__(parent);self.setWindowTitle('自定义切面组合');self.resize(850,620)
        self.config=deepcopy(config);self.previous=-1
        if not any(c['dimensions']==current for c in self.config['combinations']) and len(self.config['combinations'])<32:
            name='当前页面组合'
            while any(c['name']==name for c in self.config['combinations']):name+='·'
            self.config['combinations'].append({'name':name,'dimensions':list(current)})
        layout=QVBoxLayout(self);note=QLabel('每套组合选择1至12个维度；从上到下就是表格列顺序。门店、商品自动同时显示编码和名称。');note.setWordWrap(True);layout.addWidget(note)
        body=QHBoxLayout();left=QVBoxLayout();self.saved=QListWidget();left.addWidget(self.saved)
        row=QHBoxLayout();row.addWidget(button('新增组合',self.add));row.addWidget(button('删除组合',self.remove));left.addLayout(row);body.addLayout(left,1)
        right=QVBoxLayout();self.name=QLineEdit();self.name.setMaxLength(40);self.name.setPlaceholderText('组合名称');right.addWidget(self.name)
        self.fields=QListWidget();self.fields.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove);right.addWidget(self.fields)
        order=QHBoxLayout();order.addWidget(button('向上',lambda:self.move(-1)));order.addWidget(button('向下',lambda:self.move(1)));right.addLayout(order);body.addLayout(right,2);layout.addLayout(body)
        layout.addWidget(QLabel('保存仅影响本分析板块；不会更改补货参数、日报或统计口径。'))
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('保存并查看选中组合');buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)
        self.saved.currentRowChanged.connect(self.select)
        for item in self.config['combinations']:self.saved.addItem(item['name'])
        index=next((i for i,c in enumerate(self.config['combinations']) if c['dimensions']==current),0)
        self.saved.setCurrentRow(index)

    def flush(self):
        if self.previous<0:return
        item={'name':self.name.text().strip(),'dimensions':[self.fields.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.fields.count()) if self.fields.item(i).checkState()==Qt.CheckState.Checked]}
        self.config['combinations'][self.previous]=item;self.saved.item(self.previous).setText(item['name'] or '未命名组合')

    def select(self,index):
        self.flush();self.previous=index
        if index<0:return
        item=self.config['combinations'][index];self.name.setText(item['name']);self.fields.clear()
        order=item['dimensions']+[k for k in DIMENSIONS if k not in item['dimensions']]
        for key in order:check_item(self.fields,NAMES[key],key,key in item['dimensions'])

    def move(self,step):
        index=self.fields.currentRow();target=index+step
        if 0<=index<self.fields.count() and 0<=target<self.fields.count():
            item=self.fields.takeItem(index);self.fields.insertItem(target,item);self.fields.setCurrentRow(target)

    def add(self):
        self.flush()
        if len(self.config['combinations'])>=32:
            QMessageBox.information(self,'组合数量','最多保存32套组合，请先删除不再使用的组合。');return
        number=1;names={c['name'] for c in self.config['combinations']}
        while '自定义组合'+str(number) in names:number+=1
        name='自定义组合'+str(number)
        self.config['combinations'].append({'name':name,'dimensions':['store']});self.saved.addItem(name);self.saved.setCurrentRow(self.saved.count()-1)

    def remove(self):
        if self.saved.count()<=1:return
        index=self.saved.currentRow();self.previous=-1;self.config['combinations'].pop(index)
        self.saved.blockSignals(True);self.saved.takeItem(index);self.saved.blockSignals(False)
        self.saved.setCurrentRow(min(index,self.saved.count()-1));self.select(self.saved.currentRow())

    def accept(self):
        self.flush()
        try:
            self.config['active']=self.config['combinations'][self.saved.currentRow()]['dimensions']
            self.config=validate(self.config)
        except ValueError as exc:QMessageBox.information(self,'请完善组合',str(exc));return
        super().accept()


class ExportDialog(QDialog):
    def __init__(self,parent,config,current):
        super().__init__(parent);self.setWindowTitle('选择导出的切面');self.resize(760,510)
        layout=QVBoxLayout(self);note=QLabel('每个勾选的组合一张表，全部分组逐行导出；日期、剔除视角和多维筛选与当前页面一致。');note.setWordWrap(True);layout.addWidget(note)
        self.listing=QListWidget();layout.addWidget(self.listing)
        values=deepcopy(config['combinations'])
        if not any(c['dimensions']==current for c in values):
            name='当前页面组合'
            while any(c['name']==name for c in values):name+='·'
            values.insert(0,{'name':name,'dimensions':current})
        # At most 32 sheets per export; the current unsaved cut can replace a saved selection.
        selected=False
        for value in values:
            checked=value['dimensions']==current and not selected;selected=selected or checked
            check_item(self.listing,value['name']+'\n'+describe(value['dimensions']),value,checked)
        row=QHBoxLayout();row.addWidget(button('全部勾选',lambda:self.check_all(True)));row.addWidget(button('清空勾选',lambda:self.check_all(False)));layout.addLayout(row)
        self.details=QCheckBox('附带商品明细（包含剔除标记，大范围可能耗时）');layout.addWidget(self.details)
        layout.addWidget(QLabel('各工作表带 Excel 筛选和冻结表头；多套组合可先在“管理 / 自定义组合”中保存。'))
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('选择保存位置');buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);layout.addWidget(buttons)

    def check_all(self,checked):
        for i in range(self.listing.count()):self.listing.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def accept(self):
        try:self.combinations=combinations([self.listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.listing.count()) if self.listing.item(i).checkState()==Qt.CheckState.Checked])
        except ValueError as exc:QMessageBox.information(self,'请选择切面',str(exc));return
        super().accept()


class FilterDialog(QDialog):
    def __init__(self,page):
        super().__init__(page);self.page=page;self.entry=page.entry;self.filters=deepcopy(page.filters);self.names=dict(page.identity_names)
        self.active=True;self.token=0;self.offset=0;self.pages=[];self.next_offset=None;self.rendering=False;self.pending=False
        self.setWindowTitle('多维筛选');self.resize(980,650)
        layout=QVBoxLayout(self);note=QLabel('同一维度多选为“或”，不同维度为“且”。候选项来自当前日期和其他已选条件；跨页勾选保留，未勾选的维度不限。');note.setWordWrap(True);layout.addWidget(note)
        body=QHBoxLayout();self.dimensions=QListWidget();body.addWidget(self.dimensions,1)
        for key in DIMENSIONS:item=QListWidgetItem(NAMES[key]);item.setData(Qt.ItemDataRole.UserRole,key);self.dimensions.addItem(item)
        right=QVBoxLayout();search=QHBoxLayout();self.search=QLineEdit();self.search.setMaxLength(200);self.search.setPlaceholderText('搜索编码或名称；回车查询')
        search.addWidget(self.search);self.search_btn=button('搜索',self.restart);search.addWidget(self.search_btn);right.addLayout(search)
        self.selected_only=QCheckBox('只看本维度已勾选项（可逐项取消）');right.addWidget(self.selected_only)
        self.values=QListWidget();right.addWidget(self.values);self.note=QLabel('');self.note.setWordWrap(True);right.addWidget(self.note)
        pages=QHBoxLayout();self.prev=button('上一页',lambda:self.turn(-1));self.next=button('下一页',lambda:self.turn(1));pages.addWidget(self.prev);pages.addWidget(self.next)
        self.select_page=button('勾选本页',lambda:self.check_page(True));pages.addWidget(self.select_page);self.clear_page=button('取消本页',lambda:self.check_page(False));pages.addWidget(self.clear_page);right.addLayout(pages)
        right.addWidget(button('清空当前维度',self.clear_dimension));body.addLayout(right,3);layout.addLayout(body)
        footer=QHBoxLayout();footer.addWidget(button('清空所有维度',self.clear_all));self.summary=QLabel('');footer.addWidget(self.summary,1);layout.addLayout(footer)
        self.buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText('应用筛选');self.buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
        self.buttons.accepted.connect(self.accept);self.buttons.rejected.connect(self.reject);layout.addWidget(self.buttons)
        self.values.itemChanged.connect(self.changed);self.search.returnPressed.connect(self.restart);self.selected_only.toggled.connect(self.restart)
        self.dimensions.currentRowChanged.connect(self.change_dimension);self.finished.connect(self.closed)
        self.dimensions.setCurrentRow(0);self.update_summary()

    def key(self):return self.dimensions.currentItem().data(Qt.ItemDataRole.UserRole)

    def change_dimension(self,*_):
        self.search.clear();self.restart()

    def restart(self,*_):self.offset=0;self.pages=[];self.fetch()

    def fetch(self):
        if not self.active:return
        self.token+=1;token=self.token;key=self.key();self.values.clear();self.next_offset=None;self.pending=False
        if self.selected_only.isChecked():
            needle=self.search.text().strip().casefold();values=self.filters.get(key,[])
            rows=[{key:v,**({key+'_name':self.names.get((key,v),'名称未提供')} if key in ('store','sku') else {})} for v in values]
            rows=[r for r in rows if needle in self.row_text(key,r).casefold()]
            self.show_values({'dimension':key,'items':rows[self.offset:self.offset+200],'total':len(rows),'offset':self.offset,'next_offset':self.offset+200 if self.offset+200<len(rows) else None});return
        self.pending=True;self.note.setText('正在查询候选项…')
        options={**self.page.options(),'filters':deepcopy(self.filters)}
        def ready(value):
            if self.active and token==self.token:self.pending=False;self.show_values(value)
        self.entry.start('filter_values',{'query':options,'dimension':key,'search':self.search.text(),'offset':self.offset},ready)
        self.controls()

    def row_text(self,key,row):
        value=row[key];text=str(dimension_value(value,key))
        if key in ('store','sku'):text+=' · '+row.get(key+'_name','名称未提供')
        return text

    def show_values(self,value):
        if not self.active:return
        key=value['dimension'];self.rendering=True;self.values.clear()
        for row in value['items']:
            val=row[key]
            if key in ('store','sku'):self.names[key,val]=row[key+'_name']
            check_item(self.values,self.row_text(key,row),val,val in self.filters.get(key,[]))
        self.rendering=False;self.next_offset=value['next_offset']
        self.note.setText(f"第 {self.offset+1 if value['items'] else 0} 起，当前 {len(value['items'])} 项 / 共 {value['total']} 项；每维度最多勾选1000项。")
        self.controls();self.update_summary()

    def controls(self):
        if not self.active:return
        busy=self.entry.worker is not None or self.entry.queued is not None
        if self.pending and not busy:
            self.pending=False;self.note.setText(self.entry.message)
        self.prev.setEnabled(not self.pending and bool(self.pages));self.next.setEnabled(not self.pending and self.next_offset is not None)
        self.select_page.setEnabled(not self.pending);self.clear_page.setEnabled(not self.pending)

    def changed(self,item):
        if self.rendering:return
        key=self.key();values=self.filters.setdefault(key,[]);value=item.data(Qt.ItemDataRole.UserRole)
        if item.checkState()==Qt.CheckState.Checked:
            if value not in values:
                if len(values)>=1000:
                    self.rendering=True;item.setCheckState(Qt.CheckState.Unchecked);self.rendering=False
                    self.note.setText('当前维度已勾选1000项；可先按类目或供应商缩小范围。');return
                values.append(value)
        elif value in values:values.remove(value)
        if not values:self.filters.pop(key,None)
        self.update_summary()

    def update_summary(self):
        for i in range(self.dimensions.count()):
            item=self.dimensions.item(i);key=item.data(Qt.ItemDataRole.UserRole);count=len(self.filters.get(key,[]))
            item.setText(NAMES[key]+(f'（{count}项）' if count else '（不限）'))
        self.summary.setText(f'已设置 {len(self.filters)} 个维度，共勾选 {sum(len(v) for v in self.filters.values())} 项')

    def check_page(self,checked):
        for i in range(self.values.count()):self.values.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def clear_dimension(self):self.filters.pop(self.key(),None);self.update_summary();self.restart()
    def clear_all(self):self.filters={};self.update_summary();self.restart()

    def turn(self,step):
        if step>0:
            if self.next_offset is None:return
            self.pages.append(self.offset);self.offset=self.next_offset
        elif self.pages:self.offset=self.pages.pop()
        else:return
        self.fetch()

    def closed(self,*_):
        self.active=False;self.token+=1
        if self.entry.queued and self.entry.queued[0]=='filter_values':self.entry.queued=None
        if self.entry.worker and self.entry.worker.operation=='filter_values':self.entry.worker.cancel.set()
