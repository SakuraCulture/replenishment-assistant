"""Standalone native dashboard. All query/export work stays off the GUI thread."""
from datetime import date, timedelta, datetime
from pathlib import Path
import json
from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTabWidget,
    QProgressBar,QMessageBox,QInputDialog,QTableWidget,QTableWidgetItem,QAbstractItemView)
from ui_controls import button,label
from business_ui import grid
from ui_tasks import Worker
import order_analytics as service
import order_analytics_exports as exports
import export_locations


def formatted(v):
    if v is None:return '未确认'
    if isinstance(v,(int,float)) or hasattr(v,'as_tuple'):return f'{v:,.2f}'
    return str(v)


class AnalyticsWidget(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.worker=None;self.report=None;self.page=0;self.drill=None;self.selected=set();self.close_pending=False
        self.views_path=Path(host.repository.folder)/'analytics_views.json'
        v=QVBoxLayout(self);v.addWidget(label('经营分析 · 五段漏斗','section'))
        v.addWidget(label('模型建议 → 签发 → 门店确认 → 实际下单 → 实际收货；按签发价比较，付款实际金额另列。','muted'))
        f=QHBoxLayout();self.start=QLineEdit(date.today().isoformat());self.end=QLineEdit(date.today().isoformat())
        for title,edit in [('业务日期',self.start),('至',self.end)]:f.addWidget(label(title));edit.setMaximumWidth(130);f.addWidget(edit)
        for days,text in ((1,'今天'),(7,'近7天'),(30,'近30天')):f.addWidget(button(text,lambda checked=False,n=days:self.set_dates(n)))
        self.views=QComboBox();self.views.addItem('常用视图',None);f.addWidget(self.views,1);self.views.activated.connect(self.load_view);v.addLayout(f)
        f=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('搜索门店名称、订单号、商品名称或编码');self.search.setClearButtonEnabled(True);f.addWidget(self.search,2)
        self.stores=QLineEdit();self.stores.setPlaceholderText('门店编码，逗号分隔；留空全部');f.addWidget(self.stores,1)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓','河北仓','山东仓']);f.addWidget(self.warehouse)
        self.group=QComboBox();self.group.addItems(['全部属性',*service.GROUPS]);f.addWidget(self.group)
        self.query_btn=button('查询',self.query,True);f.addWidget(self.query_btn);v.addLayout(f)
        self.applied=label('先查询；无需计算本次补货，也无需启用 AI。','muted');v.addWidget(self.applied)
        for edit in (self.start,self.end,self.search,self.stores):edit.textChanged.connect(self.dirty);edit.returnPressed.connect(self.query)
        self.warehouse.currentIndexChanged.connect(self.dirty);self.group.currentIndexChanged.connect(self.dirty)
        self.bar=QProgressBar();self.bar.setRange(0,100);self.bar.setValue(0);self.bar.setMaximumHeight(16);v.addWidget(self.bar)
        self.note=label('未查询','muted');v.addWidget(self.note)
        self.funnel=QTableWidget(3,6);self.funnel.setHorizontalHeaderLabels(['必采属性',*[service.NAMES[s] for s in service.STAGES]])
        self.funnel.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.funnel.setMaximumHeight(195)
        self.funnel.verticalHeader().hide();self.funnel.horizontalHeader().setStretchLastSection(True)
        for i in range(6):self.funnel.setColumnWidth(i,185)
        v.addWidget(self.funnel)
        self.crumb=label('全部查询结果','section');v.addWidget(self.crumb)
        self.tabs=QTabWidget();self.tables={};self.models={}
        for key,title in [('store','门店汇总'),('warehouse','门店分仓'),('order','订单漏斗'),('detail','商品明细'),('pending','资料待核')]:
            table,model=grid(self,[]);self.tables[key]=table;self.models[key]=model;self.tabs.addTab(table,title)
            table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            if key in ('store','warehouse','order'):table.doubleClicked.connect(lambda idx,k=key:self.open_detail(k,idx.row()))
        self.tables['order'].selectionModel().selectionChanged.connect(self.select_orders)
        self.tabs.currentChanged.connect(self.change_tab);v.addWidget(self.tabs,1)
        row=QHBoxLayout();row.addWidget(button('← 返回全部',self.back));row.addWidget(button('◀ 上一页',lambda:self.turn(-1)));row.addWidget(button('下一页 ▶',lambda:self.turn(1)));self.page_note=label('');row.addWidget(self.page_note,1)
        row.addWidget(button('清空订单勾选',self.clear_selected));v.addLayout(row)
        row=QHBoxLayout();self.export_kind=QComboBox()
        for title,k in [('门店经营对比','store'),('订单漏斗明细','order'),('当前商品明细','detail')]:self.export_kind.addItem(title,k)
        row.addWidget(self.export_kind);self.export_btn=button('导出当前范围',lambda:self.export(False),True);row.addWidget(self.export_btn)
        self.selected_btn=button('导出所选订单',lambda:self.export(True));row.addWidget(self.selected_btn)
        row.addWidget(button('保存常用视图',self.save_view));self.cancel_btn=button('■ 停止当前任务',self.cancel);row.addWidget(self.cancel_btn);v.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.heartbeat);self.ticks=0
        self.load_views();self.controls()

    def spec(self):
        return service.query_spec({'from':self.start.text().strip(),'to':self.end.text().strip(),
            'stores':[s.strip() for s in self.stores.text().replace('，',',').split(',') if s.strip()],
            'warehouse':self.warehouse.currentText() if self.warehouse.currentIndex() else '',
            'group':self.group.currentText() if self.group.currentIndex() else '', 'search':self.search.text().strip()})

    def set_dates(self,days):
        self.end.setText(date.today().isoformat());self.start.setText((date.today()-timedelta(days=days-1)).isoformat())

    def dirty(self,*_):
        self.applied.setText('筛选条件尚未查询；当前表格与导出仍为上次成功查询的范围。' if self.report else '条件已设置，点击查询。')

    def controls(self):
        busy=self.worker is not None
        self.query_btn.setEnabled(not busy);self.export_btn.setEnabled(not busy and self.report is not None)
        self.selected_btn.setEnabled(not busy and bool(self.selected));self.cancel_btn.setEnabled(busy)

    def start_task(self,fn,args,done):
        if self.worker or self.host.closing:return
        w=Worker(fn,args,self);self.worker=w;self.ticks=0;self.bar.setRange(0,0);self.controls();self.timer.start()
        w.status.connect(self.note.setText);w.row_progress.connect(self.progress)
        def received(value):
            if self.worker is w and not self.close_pending and (not w.cancel.is_set() or isinstance(value,dict) and 'path' in value):done(value)
        w.success.connect(received);w.cancelled.connect(lambda:self.note.setText('已停止，保留上次成功结果。'))
        w.failed.connect(lambda message:QMessageBox.warning(self,'分析未完成',message) if not self.close_pending else None)
        def finish():
            self.timer.stop();self.worker=None;w.deleteLater();self.controls();self.bar.setRange(0,100);self.bar.setValue(100)
            if self.close_pending:QTimer.singleShot(0,self.host.close)
        w.finished.connect(finish);w.start()

    def heartbeat(self):
        self.ticks+=1;self.cancel_btn.setText(f'■ 停止当前任务（{self.ticks}秒）')

    def progress(self,n,total):self.bar.setRange(0,max(total,1) if total else 0);self.bar.setValue(n)

    def cancel(self):
        if self.worker:self.worker.cancel.set();self.note.setText('正在安全停止，当前读取步骤结束后返回…');self.cancel_btn.setEnabled(False)

    def prepare_close(self):
        if self.worker:self.close_pending=True;self.cancel();return False
        return True

    def query(self):
        try:spec=self.spec()
        except Exception as exc:QMessageBox.warning(self,'核对筛选',str(exc));return
        self.start_task(service.collect,(self.host.work_repository.path,spec),self.show_report)

    def show_report(self,report):
        self.report=report;self.drill=None;self.selected.clear();self.page=0
        self.applied.setText(f"已查询 {report['query']['from']} 至 {report['query']['to']} · 数据截至 {report['as_of']} · 按当时资料与清单")
        m=report['metrics'];self.note.setText(f"{len(report['heads'])} 张店仓单；纳入 {m['included']} 条，排除 {m['excluded']} 条，资料待核 {m['pending']} 条。未导入不算0。")
        for i,g in enumerate(service.GROUPS):
            self.funnel.setItem(i,0,QTableWidgetItem(g))
            for j,s in enumerate(service.STAGES,1):
                metric=report['groups'][g]['stages'][s]
                self.funnel.setItem(i,j,QTableWidgetItem(f"{formatted(metric['value'])} 元\n{metric['skus']}款 / {metric['orders']}单"))
                self.funnel.item(i,j).setToolTip(f"已计价部分；缺价{metric['unpriced']}条；不同环节有效范围可能不同。")
            self.funnel.setRowHeight(i,46)
        self.render();self.controls()

    def key(self):return ('store','warehouse','order','detail','pending')[self.tabs.currentIndex()]

    def visible_rows(self):
        rows=self.report['rows'] if self.report else []
        if self.drill:rows=[r for r in rows if all(r[k]==v for k,v in self.drill.items())]
        return rows

    def render(self):
        key=self.key();rows=self.visible_rows();self.display_items=[]
        self.crumb.setText('全部查询结果' if not self.drill else ' → '.join(str(v) for v in self.drill.values()))
        if key in ('store','warehouse','order'):
            items=self.report[{'store':'stores','warehouse':'warehouses','order':'orders'}[key]] if self.report else []
            if self.drill:items=[i for i in items if all(i[k]==value for k,value in self.drill.items() if k in i)]
            self.display_items=items
            headers=['门店','供货仓','补货单号',*[service.NAMES[s]+'货值' for s in service.STAGES],'必采签发货值','非必采签发货值','待核条目']
            values=[[i['name'],i['warehouse'],i['order'],*[i['metrics']['stages'][s]['value'] for s in service.STAGES],i['groups']['必采']['stages']['signed']['value'],i['groups']['非必采']['stages']['signed']['value'],i['metrics']['pending']] for i in items]
        else:
            scoped=[r for r in rows if r['scope']==('待核' if key=='pending' else '纳入')]
            headers=exports.DETAIL_HEADERS;values=scoped
        self.page=min(self.page,max(0,(len(values)-1)//100));model=self.models[key];model.headers=headers
        blocker=QSignalBlocker(self.tables[key].selectionModel())
        page=values[self.page*100:(self.page+1)*100]
        model.replace(list(exports.detail_rows(page)) if key in ('detail','pending') else page);del blocker
        self.page_note.setText(f'{len(values)}条 · 第{self.page+1}/{max(1,(len(values)+99)//100)}页 · 已选{len(self.selected)}单；双击汇总下钻')
        self.controls()

    def change_tab(self,*_):self.page=0;self.render()
    def turn(self,step):self.page=max(0,self.page+step);self.render()
    def back(self):self.drill=None;self.page=0;self.tabs.setCurrentIndex(0);self.render()

    def open_detail(self,key,index):
        if not self.report:return
        item=self.display_items[self.page*100+index]
        self.drill={'store':item['store']}
        if key in ('warehouse','order'):self.drill['warehouse']=item['warehouse']
        if key=='order':self.drill['order']=item['order']
        self.tabs.setCurrentIndex({'store':1,'warehouse':2,'order':3}[key]);self.page=0;self.render()

    def select_orders(self,selected,deselected):
        if self.key()!='order':return
        for selection,add in ((selected,True),(deselected,False)):
            for index in {i.row() for i in selection.indexes()}:
                item=self.display_items[self.page*100+index];number=item['order']
                self.selected.add(number) if add else self.selected.discard(number)
        self.controls();self.page_note.setText(f'已选 {len(self.selected)} 单；支持跨页选择')

    def clear_selected(self):self.selected.clear();self.tables['order'].clearSelection();self.controls()

    def export(self,selected):
        if not self.report:return
        ordered=[h['number'] for h in self.report['heads']]
        orders=[n for n in ordered if n in self.selected] if selected else list(dict.fromkeys(r['order'] for r in self.visible_rows())) if self.drill else None
        if (selected or self.drill) and not orders:QMessageBox.information(self,'暂无订单','当前范围没有可导出的订单。');return
        name='补货经营分析_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx'
        try:
            import export_settings
            configured=export_settings.load(export_locations.config_path(self))
            if configured:target=str(Path(export_settings.validate(configured,True))/name)
            else:target,_=export_locations.save_file(self,'保存经营分析报表',name,'Excel (*.xlsx)')
            if not target:return
            def complete(receipt):
                from readable_ui import file_results
                file_results(self,[receipt['path']],'分析报表已导出',f"纳入{receipt['rows']}条，资料待核{receipt['pending']}条。")
            self.start_task(exports.export,(self.host.work_repository.path,self.report,target,self.export_kind.currentData(),orders),complete)
        except Exception as exc:QMessageBox.warning(self,'未能导出',str(exc))

    def load_views(self):
        self.saved_views=[]
        try:
            if self.views_path.exists():self.saved_views=json.loads(self.views_path.read_text('utf-8'))
            if not isinstance(self.saved_views,list):raise ValueError('视图设置格式错误')
            self.views.clear();self.views.addItem('常用视图',None)
            for view in self.saved_views:self.views.addItem(view['name'],view)
        except Exception as exc:self.note.setText('常用视图读取失败，可继续查询：'+str(exc))

    def save_view(self):
        try:spec=self.spec()
        except ValueError as exc:QMessageBox.warning(self,'无法保存视图',str(exc));return
        name,ok=QInputDialog.getText(self,'保存常用视图','名称（只保存筛选，不修改业务参数）')
        if not ok or not name.strip():return
        mode,ok=QInputDialog.getItem(self,'日期方式','下次使用时日期如何取值？',['滚动到当天，保留相同天数','固定当前日期范围'],0,False)
        if not ok:return
        view={'name':name.strip(),'query':spec,'rolling_days':(date.fromisoformat(spec['to'])-date.fromisoformat(spec['from'])).days+1 if mode.startswith('滚动') else None}
        from priority_list import atomic_json
        values=[x for x in self.saved_views if x['name']!=view['name']]+[view];atomic_json(self.views_path,values);self.load_views()

    def load_view(self,index):
        view=self.views.itemData(index)
        if not view:return
        q=view['query'];self.start.setText(q['from']);self.end.setText(q['to'])
        if view.get('rolling_days'):self.set_dates(view['rolling_days'])
        self.stores.setText(','.join(q.get('stores',[])));self.search.setText(q.get('search',''))
        self.warehouse.setCurrentText(q.get('warehouse') or '全部仓');self.group.setCurrentText(q.get('group') or '全部属性')


def open_analytics(host):
    host.show_section(3);host.tracking_tabs.setCurrentIndex(1)
