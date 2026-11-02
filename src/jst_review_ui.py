"""Store-first shared preview/history view; no writes and lazy SKU rendering."""
from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QWidget,QVBoxLayout,QHBoxLayout,QStackedWidget,QComboBox,QLineEdit,QLabel,QSizePolicy
from ui_controls import label,button
from business_ui import grid
import jst_review as review


class SummaryLabel(QLabel):
    """Keep all summary lines visible; let the result table use remaining height."""
    def __init__(self,text=''):
        super().__init__(text);self.setWordWrap(True)
        self.setStyleSheet('font-size:11pt;font-weight:600;')
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Minimum)
    def fit_height(self):
        self.ensurePolished()
        self.setMinimumHeight(max(self.fontMetrics().lineSpacing(),self.heightForWidth(max(1,self.width()))))
    def setText(self,text):
        super().setText(text);self.fit_height()
    def resizeEvent(self,event):
        super().resizeEvent(event);self.fit_height()


class AllocationReview(QWidget):
    def __init__(self,parent=None):
        super().__init__(parent);self.groups=[];self.current=None;self.entries=[]
        v=QVBoxLayout(self);v.setContentsMargins(0,0,0,0)
        self.total=SummaryLabel('尚未读取分配结果');v.addWidget(self.total)
        self.definition=label('金额仅含可计价行；待核不算0。同一门店同SKU去重，跨门店按款次计；有减量可与已安排交叉。','muted');v.addWidget(self.definition)
        self.pages=QStackedWidget();v.addWidget(self.pages,1)
        overview=QWidget();ov=QVBoxLayout(overview);ov.setContentsMargins(0,0,0,0)
        ov.addWidget(label('点击门店查看商品；仅查看，不改变分配顺序或数量。','muted'))
        self.store_table,self.store_model=grid(self,['门店','仓单数','回传需求（元）','已分配（元）','减少（元）','金额满足率','涉及商品款数','减量款数','金额待核款数'])
        for i,w in enumerate((210,65,115,115,115,95,80,80,100)):self.store_table.setColumnWidth(i,w)
        ov.addWidget(self.store_table,1);self.pages.addWidget(overview);self.store_table.clicked.connect(self.open_store)
        detail=QWidget();dv=QVBoxLayout(detail);dv.setContentsMargins(0,0,0,0);dv.addWidget(button('← 返回门店汇总',lambda:self.pages.setCurrentIndex(0)))
        self.detail_title=SummaryLabel();dv.addWidget(self.detail_title)
        filters=QHBoxLayout();self.warehouse=QComboBox();self.kind=QComboBox();self.kind.addItems(['全部商品','有减量','完全未安排','已安排','资料待核'])
        self.search=QLineEdit();self.search.setPlaceholderText('商品编码 / 名称 / 原因 / 补货单号');self.search.setClearButtonEnabled(True)
        filters.addWidget(self.warehouse);filters.addWidget(self.kind);filters.addWidget(self.search,1);dv.addLayout(filters)
        self.detail_note=label('','muted');dv.addWidget(self.detail_note)
        self.table,self.model=grid(self,['仓','补货单号','商品编码','商品名称','回传量','分配量','减少量','单价','需求金额','分配金额','减少金额','原因','箱规','资料可用','分配前余额','分配后余额'])
        for i,w in enumerate((75,215,135,200,75,75,75,80,100,100,100,160,65,80,95,95)):self.table.setColumnWidth(i,w)
        dv.addWidget(self.table,1);self.pages.addWidget(detail)
        self.pages.currentChanged.connect(lambda i:self.total.setVisible(i==0))
        self.warehouse.currentIndexChanged.connect(self.filter);self.kind.currentIndexChanged.connect(self.filter);self.search.textChanged.connect(self.filter)
    def replace(self,plans,scope='本次分配预览'):
        self.groups=review.stores(plans);all_entries=[entry for g in self.groups for entry in g['entries']]
        self.total.setText(f'{scope} · {len(self.groups)} 家门店 / {len(plans)} 张仓单\n'+review.caption(review.metrics(all_entries)))
        rows=[]
        for g in self.groups:
            m=g['metrics'];rows.append([g['name']+'（'+g['store']+'）',len(g['plans']),review.amount_text(m,'demand'),review.amount_text(m,'allocated'),review.amount_text(m,'reduced'),f"{m['rate']:.1%}" if m['rate'] is not None else '—',m['sku'],m['reduced_sku'],m['unknown']])
        self.store_model.replace(rows);self.current=None;self.entries=[];self.model.replace([]);self.pages.setCurrentIndex(0)
    def open_store(self,index):
        if not index.isValid():return
        g=self.groups[self.store_model.indices[index.row()]];self.current=g;self.entries=g['entries']
        self.detail_title.setText(g['name']+'（'+g['store']+'）\n'+review.caption(g['metrics']))
        blockers=[QSignalBlocker(w) for w in (self.warehouse,self.kind,self.search)]
        self.warehouse.clear();self.warehouse.addItem('全部仓','');self.warehouse.addItems(list(dict.fromkeys(h['warehouse'] for h,r in self.entries)))
        self.kind.setCurrentIndex(0);self.search.clear();del blockers
        self.filter();self.pages.setCurrentIndex(1)
    def filter(self,*_):
        wh=self.warehouse.currentText() if self.warehouse.currentIndex()>0 else '';kind=self.kind.currentText();terms=self.search.text().lower().split();rows=[]
        for h,r in self.entries:
            if wh and h['warehouse']!=wh:continue
            q=review.number(r.get('quantity'));want=review.number(r.get('demand'));price=review.number(r.get('price'));amount=review.number(r.get('amount'))
            reduced=want-q if want is not None and q is not None else None
            unknown=any(x is None for x in (want,q,price,amount))
            if kind=='有减量' and not (reduced is not None and reduced>0):continue
            if kind=='完全未安排' and q!=0:continue
            if kind=='已安排' and not (q is not None and q>0):continue
            if kind=='资料待核' and not (r.get('issue') or unknown):continue
            text=' '.join(str(x) for x in (r['sku'],r.get('name'),r.get('reason'),h['number'])).lower()
            if not all(t in text for t in terms):continue
            needed=want*price if want is not None and price is not None else None
            cut=needed-amount if needed is not None and amount is not None else None
            fmt=lambda n:'待核' if n is None else review.money(n)
            rows.append([h['warehouse'],h['number'],r['sku'],r.get('name'),r.get('demand'),r.get('quantity'),str(reduced) if reduced is not None else '待核',r.get('price') if price is not None else '待核',fmt(needed),fmt(amount),fmt(cut),r.get('reason'),r.get('step'),r.get('stock'),r.get('before'),r.get('after')])
        self.model.replace(rows);self.detail_note.setText(f'当前筛选 {len(rows)} 行；上方金额为本店全部仓单合计，筛选不改变分配和导出。')
