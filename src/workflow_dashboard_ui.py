"""Lightweight native ratio dashboard, instantiated only inside analytics."""
from datetime import date,timedelta
from PySide6.QtCore import Qt,QRectF,QPointF,Signal
from PySide6.QtGui import QPainter,QColor,QPen,QPainterPath
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QHBoxLayout,QComboBox,QTableWidget,QTableWidgetItem,
    QSplitter,QDialog,QListWidget,QListWidgetItem,QLineEdit,QDialogButtonBox,QMessageBox,QTabWidget)
from ui_controls import button,label
import workflow_analysis as wf

def display(v,percent=False):
    return '—' if v is None else f'{v:.2%}' if percent else f'{v:,.2f}'

class Chart(QWidget):
    picked=Signal(str)
    def __init__(self,parent=None):
        super().__init__(parent);self.points=[];self.setMinimumHeight(140);self.setMouseTracking(True)
    def set_points(self,points):self.points=points;self.update()
    def geometry_for(self):
        rect=QRectF(64,14,max(1,self.width()-85),max(1,self.height()-48))
        xs=[date.fromisoformat(p['period']).toordinal() for p in self.points]
        ys=[float(p['value']) for p in self.points if p['value'] is not None]
        return rect,xs,min(xs,default=0),max(xs,default=0),max([1.,*ys])
    def point_at(self,index):
        rect,xs,lo,hi,top=self.geometry_for();v=self.points[index]['value']
        return (rect.left()+rect.width()*(xs[index]-lo)/max(1,hi-lo),rect.bottom()-rect.height()*float(v or 0)/top)
    def paintEvent(self,event):
        p=QPainter(self);p.setRenderHint(QPainter.RenderHint.Antialiasing);p.fillRect(self.rect(),QColor('#ffffff'))
        rect,xs,lo,hi,top=self.geometry_for();p.setPen(QColor('#b8c9d0'));p.drawLine(rect.bottomLeft(),rect.bottomRight())
        for i in range(5):
            y=rect.bottom()-rect.height()*i/4;p.setPen(QColor('#e3ebee'));p.drawLine(QPointF(rect.left(),y),QPointF(rect.right(),y))
            p.setPen(QColor('#48616c'));p.drawText(QRectF(0,y-9,58,20),Qt.AlignmentFlag.AlignRight,f'{top*i/4:.0%}')
        path=QPainterPath();started=False
        for i,r in enumerate(self.points):
            if r['value'] is None:started=False;continue
            x,y=self.point_at(i)
            if started:path.lineTo(x,y)
            else:path.moveTo(x,y);started=True
        p.setPen(QPen(QColor('#176b65'),2));p.drawPath(path)
        for i,r in enumerate(self.points):
            if r['value'] is not None:
                x,y=self.point_at(i);p.drawEllipse(QRectF(x-3,y-3,6,6))
        p.setPen(QColor('#48616c'))
        if self.points:
            p.drawText(QRectF(rect.left(),rect.bottom()+7,140,22),self.points[0]['period'])
            p.drawText(QRectF(rect.right()-140,rect.bottom()+7,140,22),Qt.AlignmentFlag.AlignRight,self.points[-1]['period'])
        else:p.drawText(rect,Qt.AlignmentFlag.AlignCenter,'当前范围尚无可比记录')
    def nearest(self,event):
        if not self.points:return None
        return min(range(len(self.points)),key=lambda i:abs(self.point_at(i)[0]-event.position().x()))
    def mouseMoveEvent(self,event):
        i=self.nearest(event)
        if i is not None:
            r=self.points[i];self.setToolTip(r.get('tooltip',r['period']))
    def mousePressEvent(self,event):
        i=self.nearest(event)
        if i is not None:self.picked.emit(self.points[i]['period'])

class Dashboard(QWidget):
    def __init__(self,owner):
        super().__init__(owner);self.owner=owner;self.report=None;self.filters={};self.period_filter=None;self.trend_page=0
        v=QVBoxLayout(self);v.setContentsMargins(2,2,2,2);row=QHBoxLayout()
        self.grain=QComboBox();self.grain.addItems(['日','周（周一开始）','月']);row.addWidget(self.grain)
        self.mode=QComboBox()
        for k,name in wf.METRICS.items():self.mode.addItem(name,k)
        self.mode.setCurrentIndex(2);row.addWidget(self.mode)
        self.ratio=QComboBox();self.ratio.addItems(wf.RATIOS);row.addWidget(self.ratio)
        self.dimension=QComboBox()
        for name,key in [('不分组',''),('门店','store'),('供货仓','warehouse'),('上游供应商','upstream_supplier'),('商品类目','product_category'),('必采属性','group')]:self.dimension.addItem(name,key)
        row.addWidget(self.dimension);v.addLayout(row);row=QHBoxLayout();row.addWidget(button('多维筛选',self.filter_dialog));row.addWidget(button('清空趋势筛选',self.clear))
        row.addWidget(button('节点概览',lambda:self.visual_tabs.setCurrentWidget(self.nodes)));row.addWidget(button('导出本视角',self.export,True));v.addLayout(row)
        self.note=label('先查询订单；缺失周期断线，未导入不算0。','muted');self.note.setWordWrap(True);self.note.setMaximumHeight(46);v.addWidget(self.note)
        self.nodes=QTableWidget(1,7);self.nodes.setHorizontalHeaderLabels([wf.NAMES[s] for s in wf.STAGES])
        self.nodes.horizontalHeader().setStretchLastSection(True);self.nodes.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.visual_tabs=QTabWidget();upper=QWidget();uv=QVBoxLayout(upper);uv.setContentsMargins(0,0,0,0)
        self.chart=Chart();self.chart.picked.connect(self.drill);self.visual_tabs.addTab(self.chart,'比率趋势图')
        self.table=QTableWidget();self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.cellDoubleClicked.connect(lambda r,c:self.drill(self.table.item(r,0).text()) if self.table.item(r,0) else None);uv.addWidget(self.table)
        row=QHBoxLayout();row.addWidget(button('上200组',lambda:self.turn_trends(-1)));row.addWidget(button('下200组',lambda:self.turn_trends(1)));self.trend_note=label('');row.addWidget(self.trend_note,1);uv.addLayout(row)
        self.visual_tabs.addTab(upper,'趋势数据');lower=QWidget();lv=QVBoxLayout(lower);lv.setContentsMargins(0,0,0,0)
        self.details=QTableWidget();self.details.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);lv.addWidget(self.details)
        self.page=0;row=QHBoxLayout();row.addWidget(button('上100条',lambda:self.turn(-1)));row.addWidget(button('下100条',lambda:self.turn(1)));self.detail_note=label('');row.addWidget(self.detail_note,1);row.addWidget(button('返回全部周期',self.clear_period));lv.addLayout(row)
        self.visual_tabs.addTab(lower,'商品明细');self.visual_tabs.addTab(self.nodes,'节点概览');v.addWidget(self.visual_tabs,1)
        row=QHBoxLayout();row.addWidget(button('联看留存缺货率趋势',self.load_stockout));self.stockout_note=label('只读已留存日报；不会采集商品或自动加载分析库。','muted');self.stockout_note.setWordWrap(True);self.stockout_note.setMaximumHeight(40);row.addWidget(self.stockout_note,1);v.addLayout(row)
        for control in (self.grain,self.mode,self.ratio,self.dimension):control.currentIndexChanged.connect(self.render)
    def set_report(self,report):
        self.report=report;self.period_filter=None;self.page=0;self.trend_page=0
    def rows(self):return wf.selected(self.report['rows'],self.filters) if self.report else []
    def clear(self):self.filters={};self.clear_period()
    def clear_period(self):self.period_filter=None;self.page=0;self.render()
    def turn(self,step):self.page=max(0,self.page+step);self.render_details()
    def turn_trends(self,step):self.trend_page=max(0,self.trend_page+step);self.render_trends()
    def render_trends(self):
        self.trend_page=min(self.trend_page,max(0,(len(self.trend_values)-1)//200))
        self.fill(self.table,self.trend_headers,self.trend_values[self.trend_page*200:(self.trend_page+1)*200])
        self.trend_note.setText(f'{len(self.trend_values)}个周期分组，第{self.trend_page+1}页；导出包含全部组')
    def filter_dialog(self):
        if not self.report:return
        d=QDialog(self);d.setWindowTitle('筛选趋势与导出范围');d.resize(720,550);v=QVBoxLayout(d)
        dims=QComboBox()
        fields=[('门店','store'),('供货仓','warehouse'),('上游供应商','upstream_supplier'),('商品类目','product_category'),('必采属性','group'),('库存单位','unit')]
        for name,key in fields:dims.addItem(name,key)
        v.addWidget(dims);search=QLineEdit();search.setPlaceholderText('搜索选项；多选为或，不同维度为且');v.addWidget(search);items=QListWidget();v.addWidget(items,1)
        pending={k:set(vals) for k,vals in self.filters.items()};prior=[None]
        def store():
            if prior[0] is not None:pending[prior[0]]={items.item(i).data(Qt.ItemDataRole.UserRole) for i in range(items.count()) if items.item(i).checkState()==Qt.CheckState.Checked}
        def fill():
            store();key=dims.currentData();items.clear();prior[0]=key
            values=sorted({r.get(key) for r in self.report['rows']},key=lambda x:str(x or ''))
            for value in values:
                title=str(value or '待核')
                if key=='store':title=next((r['store_name']+' · '+value for r in self.report['rows'] if r['store']==value),title)
                item=QListWidgetItem(title);item.setData(Qt.ItemDataRole.UserRole,value);item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked if value in pending.get(key,set()) else Qt.CheckState.Unchecked);items.addItem(item)
            search.clear()
        dims.currentIndexChanged.connect(fill);search.textChanged.connect(lambda text:[items.item(i).setHidden(text.casefold() not in items.item(i).text().casefold()) for i in range(items.count())]);fill()
        row=QHBoxLayout();row.addWidget(button('全选本维度',lambda:[items.item(i).setCheckState(Qt.CheckState.Checked) for i in range(items.count()) if not items.item(i).isHidden()]));row.addWidget(button('清空本维度',lambda:[items.item(i).setCheckState(Qt.CheckState.Unchecked) for i in range(items.count())]));v.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(d.accept);buttons.rejected.connect(d.reject);v.addWidget(buttons)
        try:
            if d.exec():store();self.filters={k:list(v) for k,v in pending.items() if v};self.clear_period()
        finally:d.deleteLater()
    def render(self,*_):
        if not self.report:return
        rows=self.rows();mode=self.mode.currentData();grain=('day','week','month')[self.grain.currentIndex()];index=self.ratio.currentIndex()
        self.periods=wf.trends(rows,self.report['query']['from'],self.report['query']['to'],grain,mode)
        m=wf.summary(rows,mode)
        for col,s in enumerate(wf.STAGES):self.nodes.setItem(0,col,QTableWidgetItem(display(m['stages'][s])));self.nodes.setColumnWidth(col,145)
        points=[]
        for r in self.periods:
            p=r['pairs'][index];points.append({'period':r['period'],'value':p['rate'],'tooltip':f"{r['period']} · {wf.RATIOS[index]} {display(p['rate'],True)}\n分子 {display(p['after'])} / 分母 {display(p['before'])}\n可比{p['orders']}单、{p['skus']}款；"+('非完整周期' if r['partial'] else '完整周期')})
        self.chart.set_points(points)
        key=self.dimension.currentData();groups={};values=[]
        for r in rows:groups.setdefault(str(r.get(key) or '待核') if key else '全部',[]).append(r)
        for group,rs in sorted(groups.items()):
            for r in wf.trends(rs,self.report['query']['from'],self.report['query']['to'],grain,mode):
                values.append([r['period'],group,*[display(p['rate'],True) for p in r['pairs']],display(r['delta_pp'][index]),str(r['pairs'][index]['orders']),str(r['pairs'][index]['skus']),'非完整周期' if r['partial'] else '完整周期'])
        self.trend_headers=['周期','分组',*wf.RATIOS,'所选比率变化(pp)','可比订单','可比商品','周期范围'];self.trend_values=values;self.render_trends()
        self.note.setText(f"按业务日期归属；{len(rows)}条、{m['orders']}单。比率使用同一批可比明细的分子合计÷分母合计；缺失及分母0留空。货值按签发价。数量单位：{'、'.join(m['units']) or '无'}。图为当前筛选合计；分组见“趋势数据”。")
        self.report['_workflow_options']={'filters':self.filters,'grain':grain,'metric':mode,'dimension':key}
        self.render_details()
    @staticmethod
    def fill(table,headers,values):
        table.clear();table.setColumnCount(len(headers));table.setHorizontalHeaderLabels(headers);table.setRowCount(len(values))
        for i,row in enumerate(values):
            for j,value in enumerate(row):
                item=QTableWidgetItem(str(value if value is not None else '—'));item.setToolTip(item.text());table.setItem(i,j,item)
        for j in range(len(headers)):table.setColumnWidth(j,240 if '名称' in headers[j] or '单号' in headers[j] else 150)
    def drill(self,day):self.period_filter=day;self.page=0;self.render_details();self.visual_tabs.setCurrentIndex(2)
    def render_details(self):
        from analytics_identifiers import HEADERS,cells
        rows=self.rows();grain=('day','week','month')[self.grain.currentIndex()]
        if self.period_filter:rows=[r for r in rows if wf.period(r['day'],grain)==self.period_filter]
        self.page=min(self.page,max(0,(len(rows)-1)//100));values=[]
        for r in rows[self.page*100:(self.page+1)*100]:values.append([r['day'],r['store'],r['store_name'],r['warehouse'],r['sku'],r['name'],r.get('upstream_supplier'),r.get('product_category'),r['group'],*[display(r['quantities'].get(s)) if r['valid'].get(s) else '未留存／待核' for s in wf.STAGES],*cells([r])])
        self.fill(self.details,['业务日期','门店编码','门店名称','供货仓','商品编码','商品名称','上游供应商','商品类目','必采属性',*[wf.NAMES[s]+'数量' for s in wf.STAGES],*HEADERS],values)
        self.detail_note.setText(f"{self.period_filter or '全部周期'} · {len(rows)}条 · 第{self.page+1}页；单号在末列")
    def export(self):
        if not self.report:return
        self.owner.export_kind.setCurrentIndex(self.owner.export_kind.findData('workflow'));self.owner.export(False)
    def load_stockout(self):
        if not self.report:return
        import workflow_stockout
        def done(data):
            self.report['linked_stockout']=data;self.stockout_note.setText(f"门店{len(data['store'])}个日报；大仓{len(data['warehouse'])}个仓日。"+'；'.join(data['notes']))
            d=QDialog(self);d.setWindowTitle('留存缺货率趋势（独立观测）');d.resize(1080,640);v=QVBoxLayout(d);v.addWidget(label(data['basis'],'muted'))
            choice=QComboBox();choice.addItem('门店缺货率','store')
            for wh in ('山东仓','河北仓'):choice.addItem(wh+'缺货率',wh)
            v.addWidget(choice);chart=Chart(d);chart.setMaximumHeight(230);v.addWidget(chart)
            def show_line():
                key=choice.currentData();lookup={}
                for r in data['store'] if key=='store' else [r for r in data['warehouse'] if r['warehouse']==key]:lookup[r['day']]=r.get('total',r)
                day=date.fromisoformat(data['query']['start']);end=date.fromisoformat(data['query']['end']);points=[]
                while day<=end:
                    text=day.isoformat();m=lookup.get(text);points.append({'period':text,'value':m['rate'] if m else None,'tooltip':f"{text} · "+(f"缺货{m['zero']}/基数{m['base']}；缺货率{display(m['rate'],True)}" if m else '未采集／无留存')});day+=timedelta(days=1)
                chart.set_points(points)
            choice.currentIndexChanged.connect(show_line);show_line()
            table=QTableWidget();values=[]
            for kind,series in [('门店',data['store']),('大仓',data['warehouse'])]:
                for r in series:
                    x=r.get('total',r);values.append([kind,r['day'],r.get('warehouse','所选门店范围'),x['zero'],x['base'],display(x['rate'],True),x.get('bad',x.get('pending',0))])
            self.fill(table,['观测对象','观测日期','范围','缺货数','统计基数','缺货率','指标待核'],values);v.addWidget(table,1)
            v.addWidget(button('关闭',d.accept))
            try:d.exec()
            finally:d.deleteLater()
        self.owner.start_task(workflow_stockout.read,(self.owner.host.work_repository.path,self.report['query'],self.filters),done)
