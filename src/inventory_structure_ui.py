"""Inventory dashboard: summaries first, background detail and explicit local exports."""
from datetime import date,timedelta,datetime
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QPointF
from PySide6.QtGui import QColor,QPainter,QPen
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QLineEdit,QComboBox,QProgressBar,QTabWidget,QMessageBox,QApplication
from business_ui import grid
from ui_controls import button,label
from ui_tasks import Worker
import inventory_structure as service
import inventory_structure_exports as exports


def fmt(v):
    if v is None:return '待核 / 未提供'
    if isinstance(v,float) or hasattr(v,'as_tuple'):return f'{v:,.2f}'
    return str(v)


def set_grid(model,headers,rows):
    model.headers=headers;model.replace(rows)


class Trend(QWidget):
    def __init__(self,parent):
        super().__init__(parent);self.setMinimumHeight(110);self.setMaximumHeight(145);self.points=[]
    def set_rounds(self,rounds):
        self.points=rounds;self.setToolTip('\n'.join(r['day']+'：'+fmt(r['total']['known_value'])+' 元（已计价部分）' for r in rounds));self.update()
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.setPen(QColor('#51666d'))
        if not self.points:p.drawText(10,30,'双击门店轮次查看该店趋势和商品明细。');return
        peak=max(float(r['total']['known_value']) for r in self.points) or 1;left=90;bottom=self.height()-24;width=max(1,self.width()-120);height=bottom-18
        p.drawText(4,18,f'{peak:,.0f}元');p.drawText(4,bottom,'0');p.drawLine(left,18,left,bottom);p.drawLine(left,bottom,self.width()-10,bottom)
        prev=None
        for i,r in enumerate(self.points):
            point=QPointF(left+i*width/max(1,len(self.points)-1),bottom-float(r['total']['known_value'])/peak*height)
            valid=r['total']['value'] is not None and not r['partial'];p.setPen(QPen(QColor('#176c63' if valid else '#ba6b19'),2))
            if prev is not None and valid:p.drawLine(prev,point)
            p.drawEllipse(point,4,4);p.drawText(int(point.x())-30,bottom+18,r['day'][5:]);prev=point if valid else None


class InventoryWidget(QWidget):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.worker=None;self.report=None;self.close_pending=False;self.details=[];self.page=0;self.drill=None
        v=QVBoxLayout(self);v.addWidget(label('货盘结构 · 看存量、结构和逐轮变化','section'))
        f=QHBoxLayout();self.start=QLineEdit((date.today()-timedelta(days=30)).isoformat());self.end=QLineEdit(date.today().isoformat());self.stores=QLineEdit();self.stores.setPlaceholderText('门店编码，逗号分隔；留空全部')
        for text,edit in [('业务日期',self.start),('至',self.end)]:f.addWidget(label(text));edit.setMaximumWidth(130);f.addWidget(edit)
        f.addWidget(self.stores,1);self.view=QComboBox()
        for k,t in service.VIEWS.items():self.view.addItem(t,k)
        f.addWidget(self.view);self.overview=QComboBox();self.overview.addItem('结束日大盘','end_day');self.overview.addItem('各店最新（非同一时点）','latest');f.addWidget(self.overview)
        self.query_btn=button('查询货盘',self.query,True);f.addWidget(self.query_btn);v.addLayout(f)
        self.scope=label('本次导入范围，非大仓自身库存。禁用、停采、拒收等持有存量仍统计。','muted');self.scope.setWordWrap(True);v.addWidget(self.scope)
        self.cards=label('查询后显示大盘、中心仓、地采和来源待核货值。','section');self.cards.setWordWrap(True);v.addWidget(self.cards)
        self.quality=label('','muted');self.quality.setWordWrap(True);v.addWidget(self.quality)
        self.structure_table,self.structure_model=grid(self,[]);self.structure_table.setMaximumHeight(190);v.addWidget(self.structure_table)
        self.structure_table.doubleClicked.connect(self.group_drill)
        self.trend=Trend(self);v.addWidget(self.trend);self.trend.hide()
        f=QHBoxLayout();self.filters={}
        for key,title,values in [('source','全部来源',service.SOURCES),('warehouse','全部仓',('山东仓','河北仓','地采','旧版未标仓')),('status','全部状态',service.STATUSES),('group','全部必采属性',service.GROUPS)]:
            combo=QComboBox();combo.addItem(title,'');[combo.addItem(x,x) for x in values];combo.currentIndexChanged.connect(self.filter_detail);f.addWidget(combo);self.filters[key]=combo
        self.search=QLineEdit();self.search.setPlaceholderText('明细：商品名称或精确编码片段');self.search.textChanged.connect(self.filter_detail);f.addWidget(self.search,1);f.addWidget(button('清除明细筛选',self.clear_filters));v.addLayout(f)
        self.tabs=QTabWidget();self.table,self.model=grid(self,[]);self.dtable,self.dmodel=grid(self,[]);self.tabs.addTab(self.table,'门店逐轮');self.tabs.addTab(self.dtable,'所选轮次商品与变化');v.addWidget(self.tabs,1)
        self.table.doubleClicked.connect(self.open_round);self.dtable.doubleClicked.connect(self.explain)
        f=QHBoxLayout();f.addWidget(button('◀ 商品明细上一页',lambda:self.turn(-1)));f.addWidget(button('商品明细下一页 ▶',lambda:self.turn(1)));self.page_note=label('');f.addWidget(self.page_note,1)
        self.export_btn=button('导出查询范围汇总',lambda:self.export(False));f.addWidget(self.export_btn);self.detail_btn=button('导出查询范围及全部商品明细',lambda:self.export(True));f.addWidget(self.detail_btn);f.addWidget(button('复制 AI 货盘分析指令',self.copy_prompt));v.addLayout(f)
        self.bar=QProgressBar();self.bar.setMaximumHeight(15);self.bar.setValue(0);v.addWidget(self.bar)
        f=QHBoxLayout();self.note=label('未查询','muted');f.addWidget(self.note,1);self.stop_btn=button('■ 停止',self.cancel);f.addWidget(self.stop_btn);v.addLayout(f)
        for edit in (self.start,self.end,self.stores):edit.textChanged.connect(self.dirty)
        self.view.currentIndexChanged.connect(self.dirty);self.overview.currentIndexChanged.connect(self.dirty);self.controls()
        self.freshness=QTimer(self);self.freshness.setInterval(30000);self.freshness.timeout.connect(self.check_update);self.freshness.start()

    def check_update(self):
        if self.worker or not self.isVisible() or not self.report or self.report.get('preview') or self.close_pending:return
        report=self.report
        def verify(progress=lambda _:None,cancel=None):
            try:service.validate_basis(self.host.work_repository.path,report,cancel=cancel);return True
            except ValueError:return False
        def checked(valid):
            if not valid and self.report is report:
                self.report=None;self.details=[];self.drill=None;self.trend.set_rounds([])
                for model in (self.model,self.dmodel,self.structure_model):set_grid(model,[],[])
                self.cards.setText('签发或删除状态已变化，请重新查询。');self.quality.setText('旧缓存已停止展示。');self.note.setText('分析需刷新。');self.controls()
        self.start_task(verify,(),checked)

    def dirty(self,*_):self.scope.setText('条件已修改，点击查询生效；当前表格与导出仍为上次查询范围。')
    def controls(self):
        busy=self.worker is not None;self.query_btn.setEnabled(not busy);self.stop_btn.setEnabled(busy)
        for b in (self.export_btn,self.detail_btn):b.setEnabled(not busy and bool(self.report) and not self.report.get('preview',False))
    def start_task(self,fn,args,done):
        if self.worker or self.host.closing:return
        w=Worker(fn,args,self);self.worker=w;self.controls();self.bar.setRange(0,0);self._success=False
        w.status.connect(self.note.setText);w.row_progress.connect(lambda n,t:(self.bar.setRange(0,max(1,t) if t else 0),self.bar.setValue(n)))
        def received(result):
            if not self.close_pending and (not w.cancel.is_set() or isinstance(result,dict) and 'path' in result):self._success=True;done(result)
        w.success.connect(received);w.cancelled.connect(lambda:self.note.setText('已停止，保留上次成功结果。'))
        w.failed.connect(lambda text:self.note.setText('未完成：'+text))
        def finish():
            self.worker=None;w.deleteLater();self.bar.setRange(0,100);self.bar.setValue(100 if self._success else 0);self.controls()
            if self.close_pending:
                self.close_pending=False;QTimer.singleShot(0,self.host.close)
        w.finished.connect(finish);w.start()
    def query(self):
        try:spec=service.query_spec({'from':self.start.text().strip(),'to':self.end.text().strip(),'stores':[x.strip() for x in self.stores.text().replace('，',',').split(',') if x.strip()],'view':self.view.currentData(),'overview':self.overview.currentData()})
        except Exception as e:self.note.setText(str(e));return
        self.start_task(service.collect,(self.host.work_repository.path,spec),self.display)
    def display(self,report):
        self.report=report;self.drill=None;self.details=[];self.page=0;self.trend.set_rounds([]);self.trend.hide();set_grid(self.dmodel,[],[])
        o=report['overview'];m=o['total'];prefix='本轮预览 · ' if report.get('preview') else ''
        self.cards.setText(prefix+'大盘 '+fmt(m['known_value'])+'元  |  '+ '  |  '.join(s+' '+fmt(o['sources'][s]['known_value'])+'元' for s in service.SOURCES))
        self.quality.setText(f"以上均为已计价部分；各店持有合计{m['held_skus']}款次，正库存未计价{m['unpriced']}款，数量待核{m['invalid_quantity']}款。日期{'、'.join(o['dates']) or '无结束日记录'}；{o['stores']}店。共同有上轮{o['comparison_stores']}店，已计价部分增减{fmt(o['delta'])}元。")
        q=report['query'];self.scope.setText(f"已查询 {q['from']} 至 {q['to']} · {service.VIEWS[q['view']]} · {'各店最新，非同时点' if q['overview']=='latest' else '结束日大盘'}；本次导入范围（未证明全店）。状态和必采两组不能相加。")
        rs=[]
        for i,status in enumerate(service.STATUSES):
            m=o['statuses'][status];group=service.GROUPS[i] if i<len(service.GROUPS) else '';g=o['groups'].get(group)
            rs.append([status,fmt(m['known_value']),m['held_skus'],m['unpriced'],group,fmt(g['known_value']) if g else '',g['held_skus'] if g else '',g['unpriced'] if g else ''])
        set_grid(self.structure_model,['中心仓状态','已计价货值（元）','持有款数','未计价款数','中心仓必采属性','已计价货值（元）','持有款数','未计价款数'],rs)
        set_grid(self.model,exports.ROUND_HEADERS,[[fmt(v) for v in exports.round_values(r,q['view'])] for r in report['rounds']]);self.table.setColumnWidth(1,220)
        for i in range(len(exports.ROUND_HEADERS)):self.table.setColumnWidth(i,200 if i in (4,5,16,17,18) else 175 if i in (1,6,7,8,9,10,13,14) else 130)
        for i in range(8):self.structure_table.setColumnWidth(i,180 if i in (1,4,5) else 130)
        self.note.setText(f"已完成 {len(report['rounds'])} 个门店轮次；双击一轮展开，原历史不变。");self.controls()
    def open_round(self,index):
        if not self.report or self.worker:return
        r=self.report['rounds'][index.row()];self.drill=r;self.trend.set_rounds([x for x in self.report['rounds'] if x['store']==r['store']]);self.trend.show()
        if self.report.get('preview'):self.note.setText('本轮预览仅供汇总核对；签发后可查正式轮次及来源明细。');return
        def done(rows):self.details=rows;self.page=0;self.filter_detail();self.tabs.setCurrentIndex(1)
        self.start_task(service.detail,(self.host.work_repository.path,self.report,r['store'],r['day']),done)
    def filter_detail(self,*_):
        terms=self.search.text().casefold().split();self.filtered=[r for r in self.details if all(not c.currentData() or r[k]==c.currentData() for k,c in self.filters.items()) and all(t in (r['sku']+' '+r['name']).casefold() for t in terms)]
        self.page=min(self.page,max(0,(len(self.filtered)-1)//100));start=self.page*100
        rows=self.filtered[start:start+100]
        set_grid(self.dmodel,exports.DETAIL_HEADERS,[[*[fmt(r.get(k)) for k in exports.DETAIL_FIELDS],'；'.join(r['issues'])] for r in rows])
        self.page_note.setText(f"所选轮次明细：{len(self.filtered)}行，第{self.page+1}页，每页100行；不改变上方全查询汇总。");self.dtable.setColumnWidth(4,230)
    def clear_filters(self):
        for c in self.filters.values():c.setCurrentIndex(0)
        self.search.clear();self.page=0;self.filter_detail()
    def group_drill(self,index):
        if not self.drill:self.note.setText('先双击门店轮次，再选择状态或必采属性查看商品。');return
        row=self.structure_model.rows[index.row()];group=index.column()>=4;value=row[4] if group else row[0]
        if not value:return
        self.filters['source'].setCurrentText('中心仓');self.filters['group' if group else 'status'].setCurrentText(value);self.tabs.setCurrentIndex(1)
    def turn(self,n):self.page=max(0,self.page+n);self.filter_detail()
    def explain(self,index):
        r=self.filtered[self.page*100+index.row()];text=f"{r['name']}（{r['sku']}）\n{r['day']} · {r['warehouse']} · {r['status']} · {r['group']}\n本轮货值 = {fmt(r['current_quantity'])} {r['unit'] or '单位待核'} × {fmt(r['price'])} 元 = {fmt(r['current_value'])} 元\n上轮数量 {fmt(r['previous_quantity'])}；上轮价格 {fmt(r['previous_price'])}\n数量变化影响 {fmt(r['quantity_effect'])} 元；价格变化影响 {fmt(r['price_effect'])} 元\n{r['change_kind']}；{'；'.join(r['issues']) or '字段有效'}\n来源行：{r['source_row'] or '未提供'}；源表更新时间：{r['observed_at'] or '未提供'}\n不是实时库存，不推断销售或采购原因。"
        QMessageBox.information(self,'当时货盘依据',text)
    def export(self,details):
        if not self.report:return
        import export_locations
        target,_=export_locations.save_file(self.host,'导出货盘结构分析','货盘结构分析_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.xlsx','Excel (*.xlsx)')
        if target:self.start_task(exports.export,(self.host.work_repository.path,self.report,target,details),lambda receipt:self.note.setText('已导出：'+receipt['path']))
    def copy_prompt(self):
        QApplication.clipboard().setText('请先workbench_context核对工作台。用query_inventory_structure查询货盘结构，task_status完成后inventory_structure_results按next_offset分页。先报告未计价、数量待核和每店依据日期，再解释中心仓/地采、启用/备用/禁用、必采/非必采变化。禁用库存不能排除。数量变化不等于销售，价格变化不能说成补货。人员明确要求后export_inventory_structure导出同一快照，不计算、不签发、不改默认。')
        self.note.setText('已复制 AI 货盘分析指令。')
    def cancel(self):
        if self.worker:self.worker.cancel.set();self.note.setText('正在停止，当前读取/保存步骤结束后返回…')
    def prepare_close(self):
        if not self.worker:return True
        self.close_pending=True;self.cancel();return False


def open_preview(host):
    results={s:r for s,r in host.results.items() if host.states.get(s,{}).get('state')=='已完成'}
    if not results:QMessageBox.information(host,'暂无有效结果','请先完成门店计算。');return
    if getattr(host,'input_dirty',False):QMessageBox.information(host,'结果需更新','输入资料已变化，请先重新计算。');return
    host.show_section(3);host.tracking_tabs.setCurrentIndex(2)
    host.inventory.start_task(service.preview,(service.preview_inputs(results),str(host.job_day),'stock'),host.inventory.display)
