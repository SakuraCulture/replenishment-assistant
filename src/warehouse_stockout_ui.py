"""On-demand warehouse analysis UI, with bounded tables and independent lifecycle."""
import copy,json
from datetime import date,timedelta
from PySide6.QtCore import Qt,QDate
from PySide6.QtWidgets import (QWidget,QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QTabWidget,
    QComboBox,QCheckBox,QDateEdit,QLineEdit,QLabel,QMessageBox,QFileDialog,QInputDialog,QListWidget,QListWidgetItem,QAbstractItemView)
from ui_controls import button,label
from stockout_common import WAREHOUSES,today
from warehouse_stockout import DIMENSIONS,dimensions
from warehouse_stockout_views import validate,combinations
from warehouse_stockout_exports import COUNTS,DETAIL

def show(table,headers,rows):
    table.clear();table.setColumnCount(len(headers));table.setHorizontalHeaderLabels(headers);table.setRowCount(len(rows));table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for i,row in enumerate(rows):
        for j,value in enumerate(row):
            item=QTableWidgetItem('—' if value is None else str(value));item.setToolTip(item.text());table.setItem(i,j,item)
    table.resizeColumnsToContents()
    for j in range(len(headers)):table.setColumnWidth(j,min(330,max(105,table.columnWidth(j))))

def item(listing,text,value,checked):
    v=QListWidgetItem(text);v.setData(Qt.ItemDataRole.UserRole,value);v.setFlags(v.flags()|Qt.ItemFlag.ItemIsUserCheckable);v.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked);listing.addItem(v)

def checked(listing):return [listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).checkState()==Qt.CheckState.Checked]

class Combinations(QDialog):
    def __init__(self,parent,config):
        super().__init__(parent);self.setWindowTitle('自定义大仓分析切面');self.resize(820,620);self.config=copy.deepcopy(config);self.previous=-1
        v=QVBoxLayout(self);v.addWidget(label('勾选维度并拖动排序；可保存多套组合，每套导出到一张表。'))
        body=QHBoxLayout();left=QVBoxLayout();self.saved=QListWidget();left.addWidget(self.saved);row=QHBoxLayout();row.addWidget(button('新增',self.add));row.addWidget(button('删除',self.remove));left.addLayout(row);body.addLayout(left,1)
        right=QVBoxLayout();self.name=QLineEdit();self.name.setMaxLength(40);right.addWidget(self.name);self.fields=QListWidget();self.fields.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove);right.addWidget(self.fields);body.addLayout(right,2);v.addLayout(body)
        v.addWidget(button('保存选中组合并查看',self.accept,True));v.addWidget(button('取消',self.reject));self.saved.currentRowChanged.connect(self.select)
        for c in self.config['combinations']:self.saved.addItem(c['name'])
        self.saved.setCurrentRow(0)
    def flush(self):
        if self.previous>=0:self.config['combinations'][self.previous]={'name':self.name.text().strip(),'dimensions':checked(self.fields)}
    def select(self,index):
        self.flush();self.previous=index
        if index<0:return
        c=self.config['combinations'][index];self.name.setText(c['name']);self.fields.clear()
        for key in c['dimensions']+[k for k in DIMENSIONS if k not in c['dimensions']]:item(self.fields,DIMENSIONS[key][1],key,key in c['dimensions'])
    def add(self):
        if len(self.config['combinations'])>=32:QMessageBox.information(self,'组合数量','最多32套');return
        self.flush();name='自定义组合'+str(len(self.config['combinations'])+1);self.config['combinations'].append({'name':name,'dimensions':['warehouse']});self.saved.addItem(name);self.saved.setCurrentRow(self.saved.count()-1)
    def remove(self):
        if self.saved.count()<2:return
        index=self.saved.currentRow();self.previous=-1;self.config['combinations'].pop(index);self.saved.blockSignals(True);self.saved.takeItem(index);self.saved.blockSignals(False);self.saved.setCurrentRow(min(index,self.saved.count()-1));self.select(self.saved.currentRow())
    def accept(self):
        self.flush()
        try:self.config['active']=self.config['combinations'][self.saved.currentRow()]['dimensions'];self.config=validate(self.config)
        except ValueError as exc:QMessageBox.information(self,'请完善组合',str(exc));return
        super().accept()

class Filters(QDialog):
    def __init__(self,page):
        super().__init__(page);self.page=page;self.filters=copy.deepcopy(page.filters);self.key=None;self.token=0;self.active=True;self.setWindowTitle('大仓多维筛选');self.resize(800,620)
        v=QVBoxLayout(self);v.addWidget(label('同维度多选为或，跨维度为且；候选来自日期范围和其他筛选。'))
        row=QHBoxLayout();self.dim=QComboBox()
        for k,(_,name) in DIMENSIONS.items():self.dim.addItem(name,k)
        row.addWidget(self.dim);self.search=QLineEdit();self.search.setPlaceholderText('搜索候选值，回车查询');row.addWidget(self.search,1);row.addWidget(button('查询',self.load));v.addLayout(row)
        self.list=QListWidget();v.addWidget(self.list,1);self.note=label('');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('勾选本页',lambda:self.set_checks(True)));row.addWidget(button('取消本页',lambda:self.set_checks(False)));row.addWidget(button('清空此维度',self.clear));row.addWidget(button('清空所有筛选',self.clear_all));v.addLayout(row)
        v.addWidget(button('应用筛选',self.accept,True));v.addWidget(button('取消',self.reject));self.dim.currentIndexChanged.connect(self.load);self.search.returnPressed.connect(self.load);self.load()
    def flush(self):
        if self.key is None:return
        values=set(self.filters.get(self.key,[]));visible={self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())};values-=visible;values.update(checked(self.list))
        if values:self.filters[self.key]=sorted(values)
        else:self.filters.pop(self.key,None)
    def load(self):
        self.flush();self.key=self.dim.currentData();self.token+=1;token=self.token;key=self.key;self.list.clear()
        def done(result):
            if not self.active or token!=self.token:return
            for value in result['values']:item(self.list,value or '（空白／未提供）',value,value in self.filters.get(key,[]))
            self.note.setText('已显示前1000项，请搜索缩小候选范围。' if result['more'] else f'本维度{len(result["values"])}项；已选条件{len(self.filters)}个维度')
        self.page.entry.start('values',{'options':{**self.page.options(),'filters':self.filters},'dimension':key,'search':self.search.text().strip()},done)
    def set_checks(self,value):
        for i in range(self.list.count()):self.list.item(i).setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def clear(self):self.set_checks(False);self.filters.pop(self.key,None)
    def clear_all(self):self.filters={};self.set_checks(False)
    def accept(self):self.flush();self.active=False;super().accept()
    def reject(self):self.active=False;super().reject()

class Page(QWidget):
    def __init__(self,host,entry):
        super().__init__(host);self.host,self.entry=host,entry;self.filters={};self.views=None;self.runs=[];self.offset=0;self.cut_rows=[];self.cut_keys=[];self.last_error=''
        v=QVBoxLayout(self);v.addWidget(label('大仓缺货率分析','title'));v.addWidget(label('只按大仓资料：剔除虚拟分类所有淘汰品、仓禁用品和日配标签后，可用库存≤0的商品支数占剩余支数。','muted'))
        row=QHBoxLayout();self.warehouses={}
        for wh in WAREHOUSES:
            box=QCheckBox(wh);box.setChecked(True);self.warehouses[wh]=box;row.addWidget(box)
        self.collect=button('采集今日大仓资料',self.collect_today,True);row.addWidget(self.collect);self.pause=button('安全暂停',lambda:entry.pause());row.addWidget(self.pause);self.background=QCheckBox('离开后继续采集');self.background.toggled.connect(self.leave_setting);row.addWidget(self.background);row.addWidget(button('每日采集计划',self.schedule));row.addWidget(button('导入独立仓原表',self.ingest));v.addLayout(row)
        row=QHBoxLayout();self.start=QDateEdit(QDate.fromString((date.fromisoformat(today())-timedelta(days=29)).isoformat(),'yyyy-MM-dd'));self.end=QDateEdit(QDate.currentDate())
        for field in (self.start,self.end):field.setCalendarPopup(True);field.setDisplayFormat('yyyy-MM-dd')
        row.addWidget(label('观测日期'));row.addWidget(self.start);row.addWidget(label('至'));row.addWidget(self.end);self.search=QLineEdit();self.search.setPlaceholderText('商品编码／名称／上游供应商');row.addWidget(self.search,1);row.addWidget(button('查询',self.query));row.addWidget(button('多维筛选',self.filter_dialog));row.addWidget(button('清空筛选',self.clear_filters));row.addWidget(button('导出本视角 Excel',self.export));v.addLayout(row)
        self.total=label('尚未查询','section');v.addWidget(self.total);self.coverage=label('','muted');v.addWidget(self.coverage);self.filter_note=label('当前筛选：全部','muted');v.addWidget(self.filter_note)
        self.tabs=QTabWidget();self.tables=[]
        for name in ('总览与趋势','切面分析','商品与待核','采集与快照'):
            page=QWidget();layout=QVBoxLayout(page);self.tabs.addTab(page,name)
            if len(self.tables)==1:
                line=QHBoxLayout();self.combo=QComboBox();line.addWidget(self.combo,1);line.addWidget(button('查看组合',self.view));line.addWidget(button('管理／自定义组合',self.manage));layout.addLayout(line)
            if len(self.tables)==2:
                self.mode=QComboBox()
                for name,key in [('全部商品（含剔除）','all'),('纳入基数','included'),('已剔除','excluded'),('待核','pending')]:self.mode.addItem(name,key)
                self.mode.currentIndexChanged.connect(self.view);layout.addWidget(self.mode)
            if len(self.tables)==3:
                self.logs=QComboBox();layout.addWidget(self.logs);line=QHBoxLayout()
                for title,fn in [('继续原任务',self.resume),('重新采集今日',lambda:self.run_action('rerun')),('用原件重算',lambda:self.run_action('recalculate')),('结束本地准备',lambda:self.run_action('end')),('删除所选日志',self.delete_log),('刷新记录',self.refresh_metadata)]:line.addWidget(button(title,fn))
                layout.addLayout(line)
            table=QTableWidget();self.tables.append(table);layout.addWidget(table,1)
        v.addWidget(self.tabs,1);self.tables[1].cellDoubleClicked.connect(self.drill)
        row=QHBoxLayout();self.previous=button('上一页',lambda:self.turn(-1));self.next=button('下一页',lambda:self.turn(1));row.addWidget(self.previous);row.addWidget(self.next);self.page_note=label('每页最多200行，导出包含全部筛选结果');row.addWidget(self.page_note,1);row.addWidget(button('删除日期范围内分析数据…',self.delete_data));row.addWidget(button('重试原件清理',lambda:self.entry.start('cleanup_retry',{},lambda _:self.refresh_metadata())));v.addLayout(row)
        self.note=label('资料仅保存在大仓分析，不更新补货或门店分析资料','muted');v.addWidget(self.note);self.tabs.currentChanged.connect(self.changed_tab);self.search.returnPressed.connect(self.query)

    def controls(self):
        active=self.entry.worker is not None;self.collect.setEnabled(not active);self.pause.setEnabled(active)
    def leave_setting(self,value):
        if self.entry.worker:self.entry.background=value
    def selected(self):return [wh for wh,box in self.warehouses.items() if box.isChecked()]
    def options(self):return {'start':self.start.date().toString('yyyy-MM-dd'),'end':self.end.date().toString('yyyy-MM-dd'),'filters':copy.deepcopy(self.filters),'search':self.search.text().strip()}
    def refresh_metadata(self):self.entry.start('open',{},self.metadata)
    def metadata(self,value):
        self.views=value['views'];self.runs=value['runs'];self.combo.clear()
        for combo in self.views['combinations']:self.combo.addItem(combo['name'],combo['dimensions'])
        self.logs.clear()
        for run in self.runs:self.logs.addItem(run['day']+' · '+run['state']+' · '+run['message'],run['id'])
        self.note.setText(f'大仓独立分析库 {value["database_bytes"]/1024**2:.1f} MiB；原件待清理 {value["pending_cleanup"]} 份')
        for wh,box in self.warehouses.items():box.setChecked(wh in value['selected'])
        show(self.tables[3],['日期','仓范围','状态','说明','作业编号'],[[r['day'],'、'.join(json.loads(r['basis'])['warehouses']),r['state'],r['message'],r['id']] for r in self.runs]);self.query()
    def query(self):
        self.offset=0
        def done(result):
            rate='—' if result['rate'] is None else f'{result["rate"]:.2%}'
            self.total.setText(f'大仓缺货率 {rate} · 缺货 {result["zero"]:,} / 剩余支数 {result["base"]:,} · 剔除 {result["excluded"]:,} · 待核 {result["pending"]:,}')
            self.coverage.setText(f'实际覆盖 {len(result["coverage"])} 个仓日；期间按仓商品日加权，未采日期不补0。'+('影响基数或库存的资料待核，暂不显示百分比。' if result['pending'] else ''))
            self.filter_note.setText('当前筛选：'+'；'.join(DIMENSIONS[k][1]+'='+ '、'.join(v) for k,v in self.filters.items()) if self.filters else '当前筛选：全部商品维度');self.view()
        self.entry.start('query',self.options(),done)
    def changed_tab(self):self.offset=0;self.view()
    def view(self):
        index=self.tabs.currentIndex()
        if index==3:self.previous.setEnabled(False);self.next.setEnabled(False);return
        options=self.options()
        if index==2:
            options['mode']=self.mode.currentData();self.entry.start('details',{'options':options,'offset':self.offset},lambda result:self.draw_details(result));return
        keys=['day','warehouse'] if index==0 else self.combo.currentData() or ['warehouse']
        self.entry.start('cuts',{'options':options,'dimensions':keys,'offset':self.offset},lambda result:self.draw_cuts(result,index))
    def draw_cuts(self,result,index):
        keys=result['dimensions'];rows=result['rows'];show(self.tables[index],[DIMENSIONS[k][1] for k in keys]+[name for _,name in COUNTS],[[r[k] for k in keys]+[('—' if r[k] is None else f'{r[k]:.2%}') if k=='rate' else r[k] for k,_ in COUNTS] for r in rows]);self.paging(result)
        if index==1:self.cut_rows=rows;self.cut_keys=keys
    def draw_details(self,result):show(self.tables[2],[name for _,name in DETAIL],[[r.get(k) for k,_ in DETAIL] for r in result['rows']]);self.paging(result)
    def paging(self,result):self.previous.setEnabled(self.offset>0);self.next.setEnabled(result['more']);self.page_note.setText(f'第{self.offset+1}行起，本页{len(result["rows"])}行；导出包含全部结果')
    def turn(self,direction):self.offset=max(0,self.offset+direction*200);self.view()
    def drill(self,index,column):
        if index<len(self.cut_rows):self.filters.update({k:[self.cut_rows[index][k]] for k in self.cut_keys});self.query()
    def clear_filters(self):self.filters={};self.search.clear();self.query()
    def filter_dialog(self):
        d=Filters(self)
        if d.exec()==QDialog.DialogCode.Accepted:self.filters=d.filters;self.query()
    def manage(self):
        if not self.views:return
        d=Combinations(self,self.views)
        if d.exec()==QDialog.DialogCode.Accepted:self.entry.start('save_views',{'views':d.config,'expected':self.views['revision']},lambda _:self.refresh_metadata())
    def collect_today(self):
        selected=self.selected()
        def collected(_):self.entry.start('collect',{'warehouses':selected},lambda _:self.refresh_metadata(),background=self.background.isChecked())
        self.entry.start('select',{'warehouses':selected},collected)
    def ingest(self):
        path,_=QFileDialog.getOpenFileName(self,'导入大仓原始商品资料（仅分析采用）','','Excel (*.xlsx)')
        if not path:return
        wh,ok=QInputDialog.getItem(self,'标记资料属仓','明确选择这份原件所属大仓',list(WAREHOUSES),0,False)
        if ok:self.entry.start('ingest',{'path':path,'warehouse':wh},lambda _:self.refresh_metadata())
    def chosen_run(self):return next((r for r in self.runs if r['id']==self.logs.currentData()),None)
    def resume(self):self.run_action('collect')
    def run_action(self,operation):
        run=self.chosen_run()
        if run:self.entry.start(operation,{'run':run['id']},lambda _:self.refresh_metadata(),background=self.background.isChecked())
    def delete_log(self):
        run=self.chosen_run()
        if run and QMessageBox.question(self,'删除本条作业日志','删除所选作业日志，保留原件、分析结果及必要平台关联。\n'+run['id'])==QMessageBox.StandardButton.Yes:self.entry.start('delete_run_log',{'run':run['id'],'updated':run['updated']},lambda _:self.refresh_metadata())
    def delete_data(self):
        def preview(plan):
            if QMessageBox.question(self,'删除大仓分析数据',f'{plan["start"]} 至 {plan["end"]}：{plan["count"]}份快照，{plan["rows"]:,}条商品记录。\n删除本板块数据，并清理无引用原件；补货业务、门店分析不变。')==QMessageBox.StandardButton.Yes:self.entry.start('cleanup',{'plan':plan},lambda _:self.refresh_metadata())
        self.entry.start('delete_preview',self.options(),preview)
    def schedule(self):
        from warehouse_stockout_trigger import load
        plan=load(self.host.repository.folder);d=QDialog(self);d.setWindowTitle('大仓每日采集计划');v=QVBoxLayout(d);enabled=QCheckBox('启用每日采集（仅应用运行时）');enabled.setChecked(plan['enabled']);v.addWidget(enabled);time=QLineEdit(plan.get('at','08:00'));v.addWidget(label('北京时间 HH:MM；使用本板块已保存的仓选择，业务忙时安全让行，跨日不补采。'));v.addWidget(time);v.addWidget(button('保存',d.accept,True));v.addWidget(button('取消',d.reject))
        if d.exec()==QDialog.DialogCode.Accepted:
            def saved(_):
                try:self.entry.set_schedule(enabled.isChecked(),time.text().strip())
                except ValueError as exc:QMessageBox.information(self,'计划未保存',str(exc))
            self.entry.start('select',{'warehouses':self.selected()},saved)
    def export(self):
        if not self.views:return
        d=QDialog(self);d.setWindowTitle('选择导出组合');v=QVBoxLayout(d);listing=QListWidget();v.addWidget(listing)
        for c in self.views['combinations']:item(listing,c['name'],c,True)
        details=QCheckBox('附带全部筛选商品明细');details.setChecked(True);v.addWidget(details);v.addWidget(button('选择保存位置',d.accept,True));v.addWidget(button('取消',d.reject))
        if d.exec()!=QDialog.DialogCode.Accepted:return
        try:combos=combinations(checked(listing))
        except ValueError as exc:QMessageBox.information(self,'请选择组合',str(exc));return
        path,_=QFileDialog.getSaveFileName(self,'导出大仓缺货率视角','大仓缺货率_'+today()+'.xlsx','Excel (*.xlsx)')
        if path:self.entry.start('export',{'options':self.options(),'destination':path,'combinations':combos,'include_detail':details.isChecked()},lambda value:QMessageBox.information(self,'导出完成',value))
