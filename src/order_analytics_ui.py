"""Standalone native dashboard. All query/export work stays off the GUI thread."""
from datetime import date, timedelta, datetime
from pathlib import Path
import json
from PySide6.QtCore import Qt, QTimer, QSignalBlocker
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QTabWidget,
    QProgressBar,QMessageBox,QInputDialog,QTableWidget,QTableWidgetItem,QAbstractItemView,QSpinBox)
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
        super().__init__(host);self.host=host;self.worker=None;self.basis_worker=None;self.stale=False;self.report=None;self.page=0;self.drill=None;self.selected=set();self.close_pending=False
        self.views_path=Path(host.repository.folder)/'analytics_views.json'
        v=QVBoxLayout(self);heading=label('五段经营分析 · 按签发价比较，付款实际金额另列','section');heading.setToolTip('模型建议 → 签发 → 门店确认 → 实际下单 → 实际收货；分析不改变历史。');v.addWidget(heading)
        f=QHBoxLayout();self.start=QLineEdit(date.today().isoformat());self.end=QLineEdit(date.today().isoformat())
        for title,edit in [('业务日期',self.start),('至',self.end)]:f.addWidget(label(title));edit.setMaximumWidth(130);f.addWidget(edit)
        for days,text in ((1,'今天'),(7,'近7天'),(30,'近30天'),(90,'多轮复盘·近90天')):f.addWidget(button(text,lambda checked=False,n=days:self.set_dates(n)))
        self.views=QComboBox();self.views.addItem('常用视图',None);f.addWidget(self.views,1);self.views.activated.connect(self.load_view);v.addLayout(f)
        f=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('搜索门店名称、订单号、商品名称或编码');self.search.setClearButtonEnabled(True);f.addWidget(self.search,2)
        self.stores=QLineEdit();self.stores.setPlaceholderText('门店编码，逗号分隔；留空全部');f.addWidget(self.stores,1)
        self.warehouse=QComboBox();self.warehouse.addItems(['全部仓','河北仓','山东仓']);f.addWidget(self.warehouse)
        self.group=QComboBox();self.group.addItems(['全部属性',*service.GROUPS]);f.addWidget(self.group)
        self.rounds=QSpinBox();self.rounds.setRange(1,30);self.rounds.setValue(8);self.rounds.setSuffix('轮复盘');f.addWidget(self.rounds);self.rounds.valueChanged.connect(self.dirty)
        self.query_btn=button('查询',self.query,True);f.addWidget(self.query_btn);v.addLayout(f)
        self.more=QWidget();extra=QHBoxLayout(self.more);extra.setContentsMargins(0,0,0,0);extra.addWidget(button('按补货计划选择门店',self.choose_plan));self.category=QLineEdit();self.category.setPlaceholderText('分类组精确名称（选填，如快销）；留空全部');extra.addWidget(self.category,1);self.category.textChanged.connect(self.dirty);v.addWidget(self.more);self.more.hide()
        toggle=button('更多筛选',lambda:self.more.setVisible(not self.more.isVisible()));f.insertWidget(f.count()-1,toggle)
        self.applied=label('先查询；无需计算本次补货，也无需启用 AI。','muted');v.addWidget(self.applied)
        from stock_diagnosis_ui import open_diagnosis
        extra.addWidget(button('诊断本次工作台',lambda:open_diagnosis(self.host)))
        for edit in (self.start,self.end,self.search,self.stores):edit.textChanged.connect(self.dirty);edit.returnPressed.connect(self.query)
        self.warehouse.currentIndexChanged.connect(self.dirty);self.group.currentIndexChanged.connect(self.dirty)
        metric_row=QHBoxLayout();metric_row.addWidget(label('漏斗指标'))
        self.metric_choice=QComboBox()
        for title,key in [('商品货值','value'),('商品款数','skus'),('商品数量','quantity'),('预计可售天数','coverage')]:self.metric_choice.addItem(title,key)
        metric_row.addWidget(self.metric_choice);self.metric_choice.currentIndexChanged.connect(self.change_metric)
        metric_row.addWidget(label('天数共同对比至'))
        self.coverage_through=QComboBox()
        for stage in service.STAGES:self.coverage_through.addItem(service.NAMES[stage],stage)
        self.coverage_through.setCurrentIndex(1);self.coverage_through.currentIndexChanged.connect(self.dirty);metric_row.addWidget(self.coverage_through)
        metric_row.addWidget(label('选择后查询；固定当时预测，实收新增天数不是当前库存。','muted'),1);v.addLayout(metric_row)
        self.coverage_note=label('','muted');self.coverage_note.setWordWrap(True);v.addWidget(self.coverage_note)
        self.bar=QProgressBar();self.bar.setRange(0,100);self.bar.setValue(0);self.bar.setMaximumHeight(16);v.addWidget(self.bar)
        self.note=label('未查询','muted');v.addWidget(self.note)
        self.funnel=QTableWidget(3,6);self.funnel.setHorizontalHeaderLabels(['必采属性',*[service.NAMES[s] for s in service.STAGES]])
        self.funnel.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.funnel.setFixedHeight(136)
        self.funnel.verticalHeader().hide();self.funnel.horizontalHeader().setStretchLastSection(True)
        for i in range(6):self.funnel.setColumnWidth(i,185)
        v.addWidget(self.funnel)
        self.crumb=label('全部查询结果','section');v.addWidget(self.crumb)
        self.tabs=QTabWidget();self.tables={};self.models={}
        for key,title in [('store','门店汇总'),('warehouse','门店分仓'),('order','订单漏斗'),('detail','商品明细'),('pending','资料待核'),('history','多轮复盘'),('rounds','门店逐轮天数')]:
            table,model=grid(self,[]);self.tables[key]=table;self.models[key]=model;self.tabs.addTab(table,title)
            table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            if key in ('store','warehouse','order'):table.doubleClicked.connect(lambda idx,k=key:self.open_detail(k,idx.row()))
        self.tables['detail'].doubleClicked.connect(self.coverage_explanation)
        self.tables['order'].selectionModel().selectionChanged.connect(self.select_orders)
        self.tabs.currentChanged.connect(self.change_tab);v.addWidget(self.tabs,1)
        row=QHBoxLayout();row.addWidget(button('← 返回全部',self.back));row.addWidget(button('◀ 上一页',lambda:self.turn(-1)));row.addWidget(button('下一页 ▶',lambda:self.turn(1)));self.page_note=label('');row.addWidget(self.page_note,1)
        row.addWidget(button('清空订单勾选',self.clear_selected));v.addLayout(row)
        row=QHBoxLayout();self.export_kind=QComboBox()
        for title,k in [('门店经营分析（含商品切面与天数）','store'),('订单经营分析（含商品切面与天数）','order'),('当前商品明细','detail'),('门店逐轮天数','coverage_rounds')]:self.export_kind.addItem(title,k)
        row.addWidget(self.export_kind);self.export_btn=button('导出当前范围',lambda:self.export(False),True);row.addWidget(self.export_btn)
        self.selected_btn=button('导出所选订单',lambda:self.export(True));row.addWidget(self.selected_btn)
        row.addWidget(button('保存常用视图',self.save_view));row.addWidget(button('复制 AI 分析指令',self.copy_prompt));self.cancel_btn=button('■ 停止当前任务',self.cancel);row.addWidget(self.cancel_btn);v.addLayout(row)
        self.timer=QTimer(self);self.timer.setInterval(1000);self.timer.timeout.connect(self.heartbeat);self.ticks=0
        self.update_timer=QTimer(self);self.update_timer.setInterval(30000);self.update_timer.timeout.connect(self.check_update);self.update_timer.start()
        self.load_views();self.controls()

    def check_update(self):
        if not self.isVisible() or not self.report or self.worker or self.basis_worker or self.close_pending:return
        report=self.report;w=Worker(service.validate_basis,(self.host.work_repository.path,report),self);self.basis_worker=w
        def failed(message):
            if self.report is report and not self.close_pending:
                self.stale=True;self.note.setText('数据有更新或核验未完成，请重新查询后导出。'+message);self.controls()
        w.failed.connect(failed)
        def finish():
            self.basis_worker=None;w.deleteLater()
            if self.close_pending:QTimer.singleShot(0,self.host.close)
        w.finished.connect(finish);w.start()

    def spec(self):
        return service.query_spec({'from':self.start.text().strip(),'to':self.end.text().strip(),
            'stores':[s.strip() for s in self.stores.text().replace('，',',').split(',') if s.strip()],
            'warehouse':self.warehouse.currentText() if self.warehouse.currentIndex() else '',
            'group':self.group.currentText() if self.group.currentIndex() else '', 'search':self.search.text().strip(),'category':self.category.text().strip(),'rounds':self.rounds.value(),'coverage_through':self.coverage_through.currentData()})

    def choose_plan(self):
        try:
            from replenishment_plans import Repository
            plans=Repository(self.host.work_repository.path).list()
            if not plans:QMessageBox.information(self,'暂无计划','可直接填写门店编码，或到参数设置维护补货计划。');return
            names=[p['name']+' · '+p['id'][:8] for p in plans];chosen,ok=QInputDialog.getItem(self,'选择查询门店','仅复制计划门店范围，不应用参数或修改历史',names,0,False)
            if ok:self.stores.setText(','.join(s['code'] for s in plans[names.index(chosen)]['stores']))
        except Exception as exc:QMessageBox.warning(self,'无法选择',str(exc))

    def set_dates(self,days):
        self.end.setText(date.today().isoformat());self.start.setText((date.today()-timedelta(days=days-1)).isoformat())

    def dirty(self,*_):
        self.applied.setText('筛选条件尚未查询；当前表格与导出仍为上次成功查询的范围。' if self.report else '条件已设置，点击查询。')

    def controls(self):
        busy=self.worker is not None
        self.query_btn.setEnabled(not busy);self.export_btn.setEnabled(not busy and self.report is not None and not self.stale)
        self.selected_btn.setEnabled(not busy and bool(self.selected) and not self.stale);self.cancel_btn.setEnabled(busy)

    def start_task(self,fn,args,done):
        if self.worker or self.host.closing:return
        w=Worker(fn,args,self);self.worker=w;self.ticks=0;self.task_succeeded=False;self.bar.setRange(0,0);self.controls();self.timer.start()
        w.status.connect(self.note.setText);w.row_progress.connect(self.progress)
        def received(value):
            if self.worker is w and not self.close_pending and (not w.cancel.is_set() or isinstance(value,dict) and 'path' in value):self.task_succeeded=True;done(value)
        w.success.connect(received);w.cancelled.connect(lambda:self.note.setText('已停止，保留上次成功结果。'))
        w.failed.connect(lambda message:QMessageBox.warning(self,'分析未完成',message) if not self.close_pending else None)
        def finish():
            self.timer.stop();self.worker=None;w.deleteLater();self.controls();self.bar.setRange(0,100);self.bar.setValue(100 if self.task_succeeded else 0)
            if self.close_pending:QTimer.singleShot(0,self.host.close)
        w.finished.connect(finish);w.start()

    def heartbeat(self):
        self.ticks+=1;self.cancel_btn.setText(f'■ 停止当前任务（{self.ticks}秒）')

    def progress(self,n,total):self.bar.setRange(0,max(total,1) if total else 0);self.bar.setValue(n)

    def cancel(self):
        if self.worker:self.worker.cancel.set();self.note.setText('正在安全停止，当前读取步骤结束后返回…');self.cancel_btn.setEnabled(False)

    def prepare_close(self):
        if self.worker or self.basis_worker:
            self.close_pending=True;self.update_timer.stop();self.cancel()
            if self.basis_worker:self.basis_worker.cancel.set()
            return False
        return True

    def query(self):
        try:spec=self.spec()
        except Exception as exc:QMessageBox.warning(self,'核对筛选',str(exc));return
        self.start_task(service.collect,(self.host.work_repository.path,spec),self.show_report)

    def show_report(self,report):
        self.report=report;self.drill=None;self.selected.clear();self.page=0;self.stale=False
        self.applied.setText(f"已查询 {report['query']['from']} 至 {report['query']['to']} · 数据截至 {report['as_of']} · 按当时资料与清单")
        m=report['metrics'];self.note.setText(f"查询{len(report['heads'])}单，纳入{m['included_orders']}单，五段完整{m['complete_orders']}单；纳入 {m['included']} 条，排除 {m['excluded']} 条，待核 {m['pending']} 条。未导入不算0。")
        self.render_funnel();self.render();self.controls()

    def change_metric(self,*_):
        if not hasattr(self,'funnel'):return
        self.render_funnel();self.render()

    def render_funnel(self):
        if not self.report:return
        report=self.report;mode=self.metric_choice.currentData();coverage=report['coverage']
        groups=[g for g in service.GROUPS if g!='属性待核' or report['groups'][g]['included'] or report['groups'][g]['pending']]
        self.funnel.setRowCount(len(groups));self.funnel.setFixedHeight(44+46*len(groups))
        for i,g in enumerate(groups):
            self.funnel.setItem(i,0,QTableWidgetItem(g));eligible=[r for r in report['rows'] if r['group']==g and r['scope']=='纳入']
            for j,stage in enumerate(service.STAGES,1):
                metric=report['groups'][g]['stages'][stage]
                if mode=='coverage':
                    from coverage_exports import summary_note
                    m=coverage['groups'][g];value=m['stage_days'][stage]
                    text=('未纳入比较' if stage not in service.STAGES[:service.STAGES.index(coverage['through'])+1] else ('待核' if value is None else f'{value:.1f}天')+f"\n{m['orders']}/{m['round_orders']}单")
                    tip=summary_note(m,stage)
                elif mode=='quantity':
                    from funnel_compact_export import metrics
                    value=metrics(eligible,stage)[1];text=formatted(value)+' 个库存单位';tip='不同商品单位合计，仅作执行数量统计。'
                elif mode=='skus':text=str(metric['skus'])+'款';tip='本环节数量大于0的去重商品编码款数。'
                else:
                    text=f"{formatted(metric['value'])} 元\n{metric['skus']}款 / {metric['orders']}单";tip=f"已计价部分；缺价{metric['unpriced']}条；不同环节有效范围可能不同。"
                cell=QTableWidgetItem(text);cell.setToolTip(tip);self.funnel.setItem(i,j,cell)
            self.funnel.setRowHeight(i,46)
        m=coverage['overview']
        self.coverage_note.setText((f"天数依据：{m['basis_day'] or '无'}；共同对比至{service.NAMES[coverage['through']]}；有效{m['eligible_rows']}/{m['round_rows']}条、{m['orders']}/{m['round_orders']}单。按货值加权，跨轮不累计；下表订单显示各自轮次，未完成留空。" if mode=='coverage' else ''))

    def coverage_explanation(self,index):
        if self.metric_choice.currentData()!='coverage' or not self.report:return
        from coverage_metrics import item
        from coverage_exports import explanation
        rows=[r for r in self.visible_rows() if r['scope']=='纳入'];pos=self.page*100+index.row()
        if pos>=len(rows):return
        r=rows[pos];d=item(r)
        text=f"{r['name']}（{r['sku']}）\n依据业务日：{r['day']}\n预测日均：{formatted(d['forecast_daily'])} {r['unit']}/天\n可用：{formatted(d['available'])}；在途：{formatted(d['inbound'])}\n"
        for title,key in [('现货可售','available_days'),('含在途覆盖','position_days'),('签发后预计覆盖','signed_position_days'),('当时保护期','protection_days')]:text+=title+'：'+('待核' if d[key] is None else f'{d[key]:.1f}天')+'\n'
        text+='\n各段新增天数＝本环节数量÷当时预测日均\n'
        for stage in service.STAGES:text+=service.NAMES[stage]+'：'+('未取得或待核' if d['stage_days'][stage] is None else f"{d['stage_days'][stage]:.1f}天")+'\n'
        QMessageBox.information(self,'预计可售天数依据',text+'\n'+explanation(d))

    def key(self):return ('store','warehouse','order','detail','pending','history','rounds')[self.tabs.currentIndex()]

    def visible_rows(self):
        rows=self.report['rows'] if self.report else []
        if self.drill:rows=[r for r in rows if all(r[k]==v for k,v in self.drill.items())]
        return rows

    def render(self):
        from analytics_identifiers import HEADERS as ID_HEADERS,cells
        key=self.key();rows=self.visible_rows();self.display_items=[]
        self.crumb.setText('全部查询结果' if not self.drill else ' → '.join(str(v) for v in self.drill.values()))
        if key in ('store','warehouse','order'):
            items=self.report[{'store':'stores','warehouse':'warehouses','order':'orders'}[key]] if self.report else []
            if self.drill:items=[i for i in items if all(i[k]==value for k,value in self.drill.items() if k in i)]
            self.display_items=items
            headers=['门店','供货仓','补货单号',*[service.NAMES[s]+'货值' for s in service.STAGES],'必采签发货值','非必采签发货值','待核条目']
            values=[[i['name'],i['warehouse'],i['order'],*[i['metrics']['stages'][s]['value'] for s in service.STAGES],i['groups']['必采']['stages']['signed']['value'],i['groups']['非必采']['stages']['signed']['value'],i['metrics']['pending']] for i in items]
        elif key=='rounds':
            from coverage_metrics import STAGES
            headers=['门店','业务日期','供货仓','建议新增天数','签发新增天数','确认新增天数','下单新增天数','实收新增天数','有效商品条数','待核说明']
            items=self.report['coverage']['rounds'] if self.report else []
            if self.drill:items=[i for i in items if all(i.get(k)==v for k,v in self.drill.items() if k!='order')]
            values=[[i['name'],i['day'],i['warehouse'],*[i['coverage']['stage_days'][s] for s in STAGES],i['coverage']['eligible_rows'],'；'.join(i['coverage']['excluded_reasons'])] for i in items]
        elif key=='history':
            from history_diagnostics import HEADERS,table
            headers=HEADERS;review=self.report['history'] if self.report else {'items':[]}
            items=review['items']
            if self.drill:items=[i for i in items if all(i.get(k)==value for k,value in self.drill.items() if k!='order')]
            values=table({'items':items})
        else:
            scoped=[r for r in rows if r['scope']==('待核' if key=='pending' else '纳入')]
            headers=exports.DETAIL_HEADERS;values=scoped
        coverage_mode=self.metric_choice.currentData()=='coverage'
        if coverage_mode and key in ('store','warehouse','order'):
            kind={'store':'stores','warehouse':'warehouses','order':'orders'}[key]
            fields=('store',) if key=='store' else ('store','warehouse') if key=='warehouse' else ('store','warehouse','order')
            lookup={tuple(i[f] for f in fields):i['coverage'] for i in self.report['coverage'][kind]} if self.report else {}
            headers=['门店','供货仓','补货单号','天数依据业务日',*['建议新增天数','签发新增天数','确认新增天数','下单新增天数','实收新增天数'],'有效商品条数','待核说明']
            values=[]
            for i in items:
                m=lookup[tuple(i[f] for f in fields)]
                values.append([i['name'],i['warehouse'],i['order'],m['basis_day'],*[None if m['stage_days'][s] is None else f"{m['stage_days'][s]:.1f}" for s in service.STAGES],m['eligible_rows'],'；'.join(dict.fromkeys(list(m['excluded_reasons'])+[v for s in service.STAGES for v in m['stage_reasons'][s]]))])
        if coverage_mode and key in ('detail','pending'):
            from coverage_exports import DETAIL_HEADERS
            headers=DETAIL_HEADERS
        if key in ('store','warehouse','order'):
            headers=headers[:2]+ID_HEADERS+headers[3:]
            values=[value[:2]+cells(i.get('order_links',[]))+value[3:] for value,i in zip(values,items)]
        self.page=min(self.page,max(0,(len(values)-1)//100));model=self.models[key];model.headers=headers
        blocker=QSignalBlocker(self.tables[key].selectionModel())
        page=values[self.page*100:(self.page+1)*100]
        if key in ('detail','pending') and coverage_mode:
            from coverage_exports import detail_rows
            display=list(detail_rows(page))
        else:display=list(exports.detail_rows(page)) if key in ('detail','pending') else page
        if (coverage_mode and key!='history') or key=='rounds':
            display=[['待核' if value is None else value for value in row] for row in display]
        model.replace(display)
        del blocker
        if coverage_mode and key in ('store','warehouse','order'):
            for column,width in enumerate([240,110,360,260,260,160,160,160,160,160,160,140,300]):self.tables[key].setColumnWidth(column,width)
        elif coverage_mode and key in ('detail','pending'):
            for column in range(len(headers)):self.tables[key].setColumnWidth(column,260 if column in (2,4,6,22) else 180)
        if key=='rounds':
            for column,width in enumerate([180,150,120,160,160,160,160,160,140,360]):self.tables[key].setColumnWidth(column,width)
        if key in ('store','warehouse','order'):
            for column,width in enumerate((240,110,360,260,260)):self.tables[key].setColumnWidth(column,width)
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

    def copy_prompt(self):
        from PySide6.QtWidgets import QApplication
        gateway=getattr(self.host,'automation',None)
        if not gateway or not gateway.enabled:
            QMessageBox.information(self,'请先启用本机连接','经营分析无需 AI 即可使用；复制 AI 指令前请到“AI 协助”启用本机连接。');return
        if not self.report:QMessageBox.information(self,'先明确范围','请先成功查询需要的业务日期和门店范围。');return
        scope={'instance_id':gateway.instance,'business_date':self.host.job_day,'context_token':gateway.token(),'query':self.report['query']}
        prompt='请使用补货助手 MCP 查询下列范围的五段经营漏斗，分别说明必采、非必采、属性待核的表现，并导出门店经营对比 Excel 到软件已配置目录。\n'+json.dumps(scope,ensure_ascii=False,indent=2)+'\n先核对 workbench_context 的实例、业务日期和 context_token，不一致就停止。用新的 operation_key 调用 query_order_funnel；task_status 确认完成后调用 order_funnel_results，按 next_offset 读完，每页不超过100。区分未导入和0，不把不同可比范围直接相减。读取coverage及definitions说明五段预计可售天数、依据日期、有效范围与缺失原因；固定制单预测，实收新增天数不是当前库存。再用返回的 task_id 和 snapshot_id 调用 export_order_funnel（level=store），等待任务完成后给出文件和实际范围。只读与写本地报表，不计算、不采用、不签发、不修改参数、不采购、不删除。连接不可用不读本机数据库绕过。'
        QApplication.clipboard().setText(prompt);self.note.setText('已复制 AI 分析与导出指令；尚未执行或发送任何任务。')

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
        self.rounds.setValue(q.get('rounds',8))
        self.category.setText(q.get('category',''))
        self.coverage_through.setCurrentIndex(service.STAGES.index(q.get('coverage_through','signed')))


def open_analytics(host):
    host.show_section(3);host.tracking_tabs.setCurrentIndex(1)
