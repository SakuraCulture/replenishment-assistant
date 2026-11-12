"""Explicit export scopes and saved custom combinations for each metric."""
import copy
from PySide6.QtCore import QDate,Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QCheckBox,QDateEdit,QDialogButtonBox,QFileDialog,QListWidget,QListWidgetItem
from ui_controls import label
from stockout_common import today


def show(page):
    from stockout_repository import Repository
    from warehouse_stockout import Repository as Warehouse
    from stockout_views import load as store_views,defaults as store_defaults
    from warehouse_stockout_views import load as warehouse_views,defaults as warehouse_defaults
    from stockout_hub_service import MODES
    root=page.host.repository.folder;store=Repository(root);warehouse=Warehouse(root)
    queries=copy.deepcopy(getattr(page,'queries',{}))
    child=page.entry.children[page.current]
    if child.page:queries[page.current]=child.page.options()
    active=queries.get(page.current,{})
    dialog=QDialog(page);dialog.setWindowTitle('导出缺货率');v=QVBoxLayout(dialog)
    mode=QComboBox()
    for key,text in MODES.items():mode.addItem('导出'+text.replace('只跑','').replace('一起跑',''),key)
    mode.setCurrentIndex(2);v.addWidget(mode)
    row=QHBoxLayout();dates=[]
    for key in ('start','end'):
        date=QDateEdit(QDate.fromString(active.get(key,today()),'yyyy-MM-dd'));date.setCalendarPopup(True);date.setDisplayFormat('yyyy-MM-dd');dates.append(date);row.addWidget(date)
    v.addLayout(row);v.addWidget(label('沿用两子页各自最近的多维筛选；切面全部分组导出，不受分页影响。'))
    lists={}
    for key,title,config in [('store','门店切面',store_views(store) if store.path.exists() else store_defaults()),('warehouse','大仓切面',warehouse_views(warehouse) if warehouse.path.exists() else warehouse_defaults())]:
        v.addWidget(label(title));listing=QListWidget();listing.setMaximumHeight(130);lists[key]=listing;v.addWidget(listing)
        for combo in config['combinations']:
            item=QListWidgetItem(combo['name']);item.setData(Qt.ItemDataRole.UserRole,combo);item.setFlags(item.flags()|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Checked);listing.addItem(item)
    details=QCheckBox('附带全部匹配商品明细（大范围导出耗时较长）');v.addWidget(details)
    v.addStretch()
    buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel);buttons.accepted.connect(dialog.accept);buttons.rejected.connect(dialog.reject);v.addWidget(buttons)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText('导出');buttons.button(QDialogButtonBox.StandardButton.Cancel).setText('取消')
    from table_readability import fit_window
    fit_window(dialog,900,700)
    if dialog.exec()!=QDialog.DialogCode.Accepted:return
    destination,_=QFileDialog.getSaveFileName(page,'导出缺货率','缺货率_'+today()+'.xlsx','Excel (*.xlsx)')
    if not destination:return
    date_values={k:d.date().toString('yyyy-MM-dd') for k,d in zip(('start','end'),dates)}
    sq={**queries.get('store',{'view':'all','filters':{},'exclude_stopped':True,'exclude_disabled':True}),**date_values}
    wq={**queries.get('warehouse',{'filters':{}}),**date_values}
    def selected(listing):return [listing.item(i).data(Qt.ItemDataRole.UserRole) for i in range(listing.count()) if listing.item(i).checkState()==Qt.CheckState.Checked]
    page.entry.start('hub_export',{'mode':mode.currentData(),'destination':destination,'store_query':sq,'warehouse_query':wq,'store_combinations':selected(lists['store']),'warehouse_combinations':selected(lists['warehouse']),'include_detail':details.isChecked()},lambda path:page.note.setText('已导出：'+path))
