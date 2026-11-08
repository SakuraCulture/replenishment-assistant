"""Business-first whole-document review; viewing filters never alter commit scope."""
from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QVBoxLayout,QSplitter,QTabWidget,QTreeWidget,QTreeWidgetItem,QLabel
from ui_controls import label
import lifecycle_io as io


def arrange(d,plan,details,disclosure,filters,footer,correction_table=None):
    root=d.layout();notices=[]
    while root.count():
        item=root.takeAt(0);w=item.widget()
        if isinstance(w,QLabel):notices.append(w.text());w.hide()
    filters.setParent(None);footer.setParent(None)
    disclosure.hide();disclosure.toggled.disconnect()
    if notices:details.appendPlainText('\n'+'\n'.join(notices))
    d.scope_numbers=None;d.review_count=len(plan['plans'])
    splitter=QSplitter(Qt.Orientation.Horizontal,d);d.review_splitter=splitter
    d.scope_tree=QTreeWidget();d.scope_tree.setHeaderLabels(['门店 / 仓单 / 环节（仅切换查看）']);d.scope_tree.setMinimumWidth(180);splitter.addWidget(d.scope_tree)
    all_item=QTreeWidgetItem([f'本批全部 · {len(plan["plans"])}份']);all_item.setData(0,Qt.ItemDataRole.UserRole,(None,None));d.scope_tree.addTopLevelItem(all_item)
    stores={}
    for p in plan['plans']:
        h=p['head'];title=io.TITLES[p['stage']]
        key=(h['store'],h['name']);store=stores.get(key)
        if store is None:
            store=QTreeWidgetItem([h['name']+'\n'+h['store']]);stores[key]=store;all_item.addChild(store);store.setData(0,Qt.ItemDataRole.UserRole,([],None))
        numbers=list(store.data(0,Qt.ItemDataRole.UserRole)[0]);numbers.append(h['number']);store.setData(0,Qt.ItemDataRole.UserRole,(numbers,None))
        text=h['warehouse']+' · '+title+'\n'+h['number']
        if p['stage']=='payment':
            values=[r['amount'] for r in p['payload']['rows'] if Decimal(str(r['base_quantity']))>0]
            text+='\n金额 '+(f'{sum((Decimal(str(v)) for v in values),Decimal(0)):,.2f} 元' if all(v is not None for v in values) else '待核')
        leaf=QTreeWidgetItem([text]);leaf.setData(0,Qt.ItemDataRole.UserRole,([h['number']],title));leaf.setToolTip(0,text);store.addChild(leaf);store.setExpanded(True)
    all_item.setExpanded(True)
    d.review_tabs=QTabWidget();splitter.addWidget(d.review_tabs)
    goods=QWidget();g=QVBoxLayout(goods);g.setContentsMargins(0,0,0,0);g.addLayout(filters);g.addWidget(d.table,1);d.review_tabs.addTab(goods,'商品明细')
    details.setMaximumHeight(16777215);d.review_tabs.addTab(details,'读取依据与说明')
    statistics=QWidget();s=QVBoxLayout(statistics);s.setContentsMargins(0,0,0,0)
    s.addWidget(label('本批全部保存范围；点击单号或统计可筛选商品。','muted'))
    for w in (d.overview,d.stats):w.setMaximumHeight(16777215);s.addWidget(w,1)
    d.review_tabs.addTab(statistics,'批次范围与统计')
    if correction_table is not None:
        correction_table.setMaximumHeight(16777215);d.review_tabs.addTab(correction_table,'与上次保存的差异')
    splitter.setSizes([300,900]);splitter.setStretchFactor(0,0);splitter.setStretchFactor(1,1);splitter.setChildrenCollapsible(False);root.addWidget(splitter,1)
    d.view_scope=label('','muted');root.addWidget(d.view_scope)
    root.addWidget(d.complete);root.addWidget(d.extra_ack)
    root.addWidget(label('确认保存本批全部完整单据，筛选仅用于查看；不累计旧版本，不修改签发和其它环节。','muted'))
    root.addLayout(footer)
    def selected():
        item=d.scope_tree.currentItem()
        if item is None:return
        numbers,stage=item.data(0,Qt.ItemDataRole.UserRole);d.scope_numbers=numbers
        d.search.clear();d.stage_filter.setCurrentText(stage or '全部环节');d.filter_rows()
    d.scope_tree.currentItemChanged.connect(lambda *_:selected());d.scope_tree.setCurrentItem(all_item)
