from __future__ import annotations
import sys,os,json,threading,traceback,html,logging
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import Qt,Signal,QThread,QAbstractTableModel,QSortFilterProxyModel,QModelIndex,QTimer,QUrl,QStandardPaths
from PySide6.QtGui import QColor,QFont,QDesktopServices,QIcon,QPalette
from PySide6.QtWidgets import (QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QLabel,QPushButton,QFrame,
    QFileDialog,QComboBox,QCheckBox,QDoubleSpinBox,QTableWidget,QTableWidgetItem,QHeaderView,QTableView,QLineEdit,
    QStackedWidget,QSplitter,QTextBrowser,QMessageBox,QDialog,QProgressBar,QAbstractItemView,QScrollArea)
import engine
import priority_list
from version_info import VERSION

STYLE='''
QWidget {font-family:"Microsoft YaHei UI";font-size:10pt;color:#22383C;}
QMainWindow,QDialog {background:#F3F6F7;}
QFrame#card {background:white;border:1px solid #DEE6E8;border-radius:10px;}
QLabel#title {font-size:23pt;font-weight:600;color:#153C43;}
QLabel#section {font-size:15pt;font-weight:600;}
QLabel#muted {color:#647A80;}
QLabel#metric {font-size:20pt;font-weight:600;color:#12665C;}
QPushButton {background:white;border:1px solid #CCDADD;border-radius:6px;padding:8px 16px;}
QPushButton:hover {background:#EAF3F3;border-color:#78AAA4;}
QPushButton:disabled {background:#EDF1F2;color:#9CAEB2;border-color:#E2E8EA;}
QPushButton#primary {background:#176C63;color:white;border:none;font-weight:600;}
QPushButton#primary:hover {background:#12574F;}
QPushButton#primary:disabled {background:#A3C0BC;color:#F8FCFB;}
QLineEdit,QComboBox,QDoubleSpinBox {background:white;border:1px solid #CBDADD;border-radius:5px;padding:6px;min-height:20px;}
QCheckBox {spacing:8px;} QCheckBox::indicator {width:18px;height:18px;}
QTableView,QTableWidget {background:white;alternate-background-color:#F7FAFA;border:1px solid #DFE7E9;gridline-color:#EDF1F2;selection-background-color:#DDEFEA;selection-color:#153C43;}
QHeaderView::section {background:#EAF1F2;border:none;border-bottom:1px solid #D5E2E4;padding:9px;font-weight:600;}
QTextBrowser {background:white;border:1px solid #DFE7E9;border-radius:8px;padding:10px;}
QProgressBar {border:none;background:#DDE8E8;border-radius:4px;height:6px;}
QProgressBar::chunk {background:#258978;border-radius:4px;}
'''

def label(text,name=None):
    x=QLabel(text)
    if name:x.setObjectName(name)
    x.setWordWrap(True);return x
def button(text,fn,primary=False):
    x=QPushButton(text);x.clicked.connect(fn)
    if primary:x.setObjectName('primary')
    return x

class FileCard(QFrame):
    changed=Signal(str)
    def __init__(self,title,description):
        super().__init__();self.path='';self.setObjectName('card');self.setAcceptDrops(True)
        v=QVBoxLayout(self);v.setContentsMargins(20,20,20,20);v.setSpacing(12)
        v.addWidget(label(title,'section'));v.addWidget(label(description,'muted'))
        self.filename=label('拖入 .xlsx 文件，或点击下方选择','muted');self.filename.setMinimumHeight(46);v.addWidget(self.filename)
        self.pick=button('选择 Excel 文件',self.choose);v.addWidget(self.pick,0,Qt.AlignmentFlag.AlignLeft)
    def choose(self):
        p,_=QFileDialog.getOpenFileName(self,'选择Excel文件',str(Path(self.path).parent) if self.path else '', 'Excel 工作簿 (*.xlsx)')
        if p:self.set_path(p)
    def set_path(self,p):
        self.path=str(Path(p));self.filename.setText(Path(p).name);self.filename.setToolTip(self.path);self.pick.setText('更换文件');self.changed.emit(self.path)
    def dragEnterEvent(self,event):
        if event.mimeData().hasUrls() and len(event.mimeData().urls())==1 and event.mimeData().urls()[0].toLocalFile().lower().endswith('.xlsx'):event.acceptProposedAction()
    def dropEvent(self,event):self.set_path(event.mimeData().urls()[0].toLocalFile());event.acceptProposedAction()

class Worker(QThread):
    status=Signal(str);success=Signal(object);failed=Signal(str);cancelled=Signal()
    def __init__(self,fn,args,parent):super().__init__(parent);self.fn=fn;self.args=args;self.cancel=threading.Event()
    def run(self):
        try:self.success.emit(self.fn(*self.args,progress=self.status.emit,cancel=self.cancel))
        except engine.Cancelled:self.cancelled.emit()
        except Exception as e:logging.exception('操作失败');self.failed.emit(str(e))

class GoodsModel(QAbstractTableModel):
    def __init__(self):super().__init__();self.rows=[]
    def set_rows(self,rows):self.beginResetModel();self.rows=rows;self.endResetModel()
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.rows)
    def columnCount(self,parent=QModelIndex()):return len(engine.COLUMNS)
    def headerData(self,section,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole:return engine.COLUMNS[section][0] if orientation==Qt.Orientation.Horizontal else section+1
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        d=self.rows[index.row()];v=engine.COLUMNS[index.column()][1](d)
        if role==Qt.ItemDataRole.DisplayRole:return engine.fmt(v)
        if role==Qt.ItemDataRole.UserRole:return v
        if role==Qt.ItemDataRole.ToolTipRole:return engine.fmt(v)
        if role==Qt.ItemDataRole.TextAlignmentRole and index.column() in range(5,11):return int(Qt.AlignmentFlag.AlignRight|Qt.AlignmentFlag.AlignVCenter)
        if role==Qt.ItemDataRole.ForegroundRole:
            if engine.exclusion_reason(d) and index.column() in (9,10,13,15,16):return QColor('#9B6D16')
            if d.get('_audit',{}).get('applied') and index.column() in (9,11):return QColor('#13796C')
            if d['_source']=='地采商品' and index.column()==2:return QColor('#9B6D16')

class FilterModel(QSortFilterProxyModel):
    def __init__(self):super().__init__();self.mode='全部商品';self.search='';self.warehouse='全部仓';self.category='全部分类';self.setSortRole(Qt.ItemDataRole.UserRole)
    def filterAcceptsRow(self,row,parent):
        d=self.sourceModel().rows[row]
        if self.warehouse!='全部仓' and d.get('AU')!=self.warehouse:return False
        if self.category!='全部分类' and str(d.get('_category') or '未分类')!=self.category:return False
        if self.search and self.search not in (' '.join(str(d.get(k,'')) for k in ['D','E','F'])).lower():return False
        if self.mode=='建议补货':return engine.orderable(d)
        if self.mode=='必卖商品':return bool(d.get('_must_sell'))
        if self.mode=='预算未满足':return d.get('_budget_gap',0)>0
        if self.mode=='必卖未补足':return bool(d.get('_must_sell')) and d.get('_budget_gap',0)>0
        if self.mode=='其他预算暂缓':return not d.get('_must_sell') and d.get('_budget_gap',0)>0
        if self.mode=='最低持有缺口':return bool(d.get('_holding_gap'))
        if self.mode=='已排除':return bool(engine.exclusion_reason(d))
        if self.mode=='商品资料禁用':return str(d.get('S') or '').strip()=='禁用'
        if self.mode=='补货建议停采':return str(d['_meta'].get('export_status') or '').strip()=='停采'
        if self.mode=='资料匹配':return d['_source']=='资料匹配'
        if self.mode=='一箱例外':return bool(d.get('_audit',{}).get('applied'))
        if self.mode=='地采商品':return d['_source']=='地采商品'
        if self.mode=='待处理':return bool(engine.issue_text(d))
        if self.mode=='重点关注':return bool(d.get('BW') or engine.issue_text(d) or any('停采' in n for n in d['_meta']['notes']))
        return True
    def lessThan(self,left,right):
        a=self.sourceModel().data(left,Qt.ItemDataRole.UserRole);b=self.sourceModel().data(right,Qt.ItemDataRole.UserRole)
        return (0,a)<(0,b) if engine.numeric(a) and engine.numeric(b) else (0 if engine.numeric(a) else 1,str(a or ''))<(0 if engine.numeric(b) else 1,str(b or ''))

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__();self.loaded=None;self.result=None;self.worker=None;self.dirty=True;self.initializing=False;self.closing=False;self.last_export=None
        config_dir=os.environ.get('REPLENISHMENT_DATA_DIR') or str(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))/'settings')
        self.repository=priority_list.Repository(config_dir);self.profile=None;self.config_warning=''
        try:self.profile=self.repository.profile()
        except Exception as exc:self.config_warning=f'清单未载入：{exc}。请重新导入。'
        self.setWindowTitle('补货助手 · '+VERSION);self.resize(1380,900);self.setMinimumSize(1080,720)
        root=QWidget();self.setCentralWidget(root);layout=QVBoxLayout(root);layout.setContentsMargins(28,20,28,18);layout.setSpacing(15)
        header=QHBoxLayout();titles=QVBoxLayout();titles.addWidget(label('补货助手','title'));subtitle=label('本地计算  ·  原模型 + 一箱例外  ·  原库存额度口径','muted');subtitle.setWordWrap(False);titles.addWidget(subtitle);header.addLayout(titles);header.addStretch()
        self.nav1=button('01  导入与检查',lambda:self.pages.setCurrentIndex(0));self.nav2=button('02  计算结果',lambda:self.pages.setCurrentIndex(1));self.nav2.setEnabled(False)
        header.addWidget(self.nav1);header.addWidget(self.nav2);layout.addLayout(header)
        self.pages=QStackedWidget();layout.addWidget(self.pages,1)
        self.make_input();self.make_results()
        bottom=QHBoxLayout();self.status=label('请选择补货建议和商品资料。','muted');bottom.addWidget(self.status,1)
        self.progress=QProgressBar();self.progress.setRange(0,0);self.progress.setMaximumWidth(180);self.progress.hide();bottom.addWidget(self.progress)
        self.cancel_btn=button('取消',self.cancel_work);self.cancel_btn.hide();bottom.addWidget(self.cancel_btn);layout.addLayout(bottom)
        self.update_controls()
    def make_input(self):
        self.input_content=QWidget();self.input_content.setObjectName('inputContent');self.input_content.setStyleSheet('QWidget#inputContent {background:#F3F6F7;}');v=QVBoxLayout(self.input_content);v.setContentsMargins(0,0,0,0);v.setSpacing(16)
        files=QHBoxLayout();self.sales=FileCard('补货建议','销量、可用库存、在途与门店信息');self.products=FileCard('商品资料','商品编码、分类、价格、出库箱规与仓归属')
        files.addWidget(self.sales);files.addWidget(self.products);v.addLayout(files)
        self.sales.changed.connect(self.file_changed);self.products.changed.connect(self.file_changed)
        frame=QFrame();frame.setObjectName('card');f=QVBoxLayout(frame);f.setContentsMargins(20,18,20,18)
        row=QHBoxLayout();row.addWidget(label('门店与参数','section'));row.addStretch();row.addWidget(button('查看完整模型参数',self.show_parameters));f.addLayout(row)
        row=QHBoxLayout();row.addWidget(label('计算门店'));self.stores=QComboBox();self.stores.setMinimumWidth(360);self.stores.currentIndexChanged.connect(self.store_changed);row.addWidget(self.stores,1)
        self.exception=QCheckBox('启用一箱例外');self.exception.setChecked(True);self.exception.toggled.connect(self.mark_dirty);row.addWidget(self.exception);f.addLayout(row)
        self.warehouses=QTableWidget(0,3);self.warehouses.setHorizontalHeaderLabels(['所属仓','备货天数','到货天数']);self.warehouses.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);self.warehouses.verticalHeader().hide();self.warehouses.setFixedHeight(142);f.addWidget(self.warehouses)
        f.addWidget(label('预设：河北 8 + 4 天，山东 8 + 5 天。未知仓需填写；其他模型参数保持原值。','muted'))
        f.addWidget(label('一箱例外：有销量、现有可用及在途撑不到到货，且原建议为0时，符合条件可放行一箱；停采、禁用和数据待核不新增例外。','muted'));v.addWidget(frame)
        self.check_label=label('文件选齐后将自动检查。','section');v.addWidget(self.check_label)
        self.check_note=label('商品资料未匹配的标记为地采；缺失值不填成0。','muted');v.addWidget(self.check_note)
        budget_frame=QFrame();budget_frame.setObjectName('card');bv=QVBoxLayout(budget_frame);bv.setContentsMargins(18,14,18,14)
        br=QHBoxLayout();br.addWidget(label('门店资金分配','section'));br.addStretch();self.budget_enabled=QCheckBox('启用总预算');br.addWidget(self.budget_enabled)
        self.budget_amount=QDoubleSpinBox();self.budget_amount.setRange(0,1000000000);self.budget_amount.setDecimals(2);self.budget_amount.setSuffix(' 元');self.budget_amount.setMinimumWidth(180);self.budget_amount.setEnabled(False);br.addWidget(self.budget_amount);bv.addLayout(br)
        self.budget_enabled.toggled.connect(self.budget_amount.setEnabled);self.budget_enabled.toggled.connect(self.mark_dirty);self.budget_amount.valueChanged.connect(self.mark_dirty)
        pr=QHBoxLayout();self.profile_label=label('','muted');pr.addWidget(self.profile_label,1);pr.addWidget(button('查看清单',self.show_priority));pr.addWidget(button('导入 / 更新清单',self.choose_priority));pr.addWidget(button('备份清单',self.backup_priority));bv.addLayout(pr)
        bv.addWidget(label('必卖优先，普通商品用剩余资金；逐个最小出库单位分配，上限为原计算建议。预算为0表示不补，关闭开关则不限总额。','muted'))
        v.insertWidget(1,budget_frame);self.refresh_profile_label()
        actions=QHBoxLayout();self.load_btn=button('重新检查文件',self.start_load);actions.addWidget(self.load_btn)
        self.review_btn=button('查看匹配明细',self.review);actions.addWidget(self.review_btn);actions.addStretch();self.calculate_btn=button('开始计算',self.start_calculation,True);actions.addWidget(self.calculate_btn);v.addStretch()
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);scroll.setWidget(self.input_content)
        outer=QWidget();ov=QVBoxLayout(outer);ov.setContentsMargins(0,0,0,0);ov.addWidget(scroll,1);ov.addLayout(actions);self.pages.addWidget(outer)
    def refresh_profile_label(self):
        if self.profile:self.profile_label.setText(f"已保存必卖清单 {len(self.profile['items']):,} 款 · 版本 {self.profile['revision']}\n{self.profile['name']}")
        else:self.profile_label.setText(self.config_warning or '尚无有效必卖清单，请导入。')
    def restore_budget(self):
        data={}
        try:data=self.repository.budgets().get(self.selected_store(),{})
        except Exception as exc:self.status.setText(f'门店预算未载入，请本次重新填写：{exc}')
        self.budget_enabled.blockSignals(True);self.budget_amount.blockSignals(True)
        self.budget_enabled.setChecked(bool(data.get('enabled',False)));self.budget_amount.setValue(data.get('amount',0))
        self.budget_amount.setEnabled(self.budget_enabled.isChecked());self.budget_enabled.blockSignals(False);self.budget_amount.blockSignals(False)
    def choose_priority(self):
        path,_=QFileDialog.getOpenFileName(self,'导入必卖清单（完整替换现有清单）','','清单文件 (*.xlsx *.csv *.json)')
        if path:self.job(priority_list.import_profile,(path,),self.priority_imported)
    def priority_imported(self,profile):
        try:self.repository.save(profile)
        except Exception as exc:self.failed('清单保存失败，仍保留原清单：'+str(exc));return
        self.profile=profile;self.config_warning='';self.refresh_profile_label();self.mark_dirty()
        self.status.setText(f"必卖清单已保存 {len(profile['items'])} 款，后续自动复用；更新清单后需重新计算。")
        if self.loaded and self.selected_store() and self.result is None:self.preview_store()
    def backup_priority(self):
        if not self.profile:return
        path,_=QFileDialog.getSaveFileName(self,'备份必卖清单，可在其他电脑导入','必卖清单_'+self.profile['revision']+'.json','清单备份 (*.json)',options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.json'):path+='.json'
        try:priority_list.export_profile(self.profile,path);self.status.setText('清单已备份：'+path)
        except Exception as exc:self.failed(str(exc))
    def show_priority(self):
        if not self.profile:return
        dialog=QDialog(self);dialog.setWindowTitle('必卖清单 · 保存后跨批次复用');dialog.resize(960,620);v=QVBoxLayout(dialog)
        v.addWidget(label(f"{self.profile['name']} · {len(self.profile['items'])} 款 · {self.profile['revision']}",'section'))
        v.addWidget(label('仅用于匹配商品和优先分配，不使用此清单历史销量、金额。更新可导入原经营分析，或含商品编码/商品名称的单张清单表；保留长编码为文本。','muted'))
        search=QLineEdit();search.setPlaceholderText('按商品编码或名称查找');v.addWidget(search)
        table=QTableWidget(len(self.profile['items']),4);table.setHorizontalHeaderLabels(['商品编码','商品名称','分类','本次匹配状态']);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        audit={d['sku']:d['state'] for d in self.result.get('must_list_audit',[])} if self.result and not self.dirty else {}
        for i,d in enumerate(self.profile['items']):
            for j,value in enumerate([d['sku'],d.get('name',''),d.get('category',''),audit.get(d['sku'],'计算后显示')]):table.setItem(i,j,QTableWidgetItem(value))
        for i,width in enumerate([200,390,120,200]):table.setColumnWidth(i,width)
        def filter_list(text):
            for i,d in enumerate(self.profile['items']):table.setRowHidden(i,text.lower() not in (d['sku']+' '+d.get('name','')).lower())
        search.textChanged.connect(filter_list);v.addWidget(table,1);v.addWidget(button('关闭',dialog.accept));dialog.exec()
    def make_results(self):
        page=QWidget();v=QVBoxLayout(page);v.setContentsMargins(0,0,0,0);v.setSpacing(12)
        self.result_caption=label('尚未计算','section');v.addWidget(self.result_caption)
        metrics=QHBoxLayout();self.metric_values=[]
        for title in ['执行补货金额（元）','执行补货商品','一箱例外候选 / 候选金额','地采 / 已排除']:
            c=QFrame();c.setObjectName('card');cv=QVBoxLayout(c);cv.setContentsMargins(16,12,16,12);cv.addWidget(label(title,'muted'));x=label('—','metric');cv.addWidget(x);metrics.addWidget(c);self.metric_values.append(x)
        v.addLayout(metrics)
        self.budget_caption=label('','muted');v.addWidget(self.budget_caption)
        tools=QHBoxLayout();self.mode=QComboBox();self.mode.addItems(['全部商品','建议补货','必卖商品','预算未满足','最低持有缺口','已排除','商品资料禁用','补货建议停采','资料匹配','一箱例外','地采商品','待处理','重点关注']);tools.addWidget(self.mode)
        self.warehouse_filter=QComboBox();self.warehouse_filter.addItem('全部仓');tools.addWidget(self.warehouse_filter)
        self.category_filter=QComboBox();self.category_filter.addItem('全部分类');tools.addWidget(self.category_filter)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索商品名称 / 编码 / 条码');self.search.setClearButtonEnabled(True);tools.addWidget(self.search,1)
        self.export_all=button('导出补货清单与明细',lambda:self.export(False),True);tools.addWidget(self.export_all)
        self.export_filtered=button('导出当前筛选',lambda:self.export(True));tools.addWidget(self.export_filtered);v.addLayout(tools)
        self.filtered_caption=label('','muted');v.addWidget(self.filtered_caption)
        splitter=QSplitter();self.model=GoodsModel();self.proxy=FilterModel();self.proxy.setSourceModel(self.model)
        self.table=QTableView();self.table.setModel(self.proxy);self.table.setSortingEnabled(True);self.table.setAlternatingRowColors(True);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection);self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.table.verticalHeader().setDefaultSectionSize(38);self.table.verticalHeader().hide()
        for i,w in enumerate([300,180,105,85,110,70,85,70,70,90,90,90,110,340,110,100,190,90,90]):self.table.setColumnWidth(i,w)
        self.table.horizontalHeader().moveSection(19,2)
        self.table.sortByColumn(10,Qt.SortOrder.DescendingOrder)
        splitter.addWidget(self.table)
        self.detail_panel=QWidget();dv=QVBoxLayout(self.detail_panel);dv.setContentsMargins(8,0,0,0);dr=QHBoxLayout();dr.addWidget(label('商品计算依据','section'));dr.addStretch();dr.addWidget(button('关闭',self.detail_panel.hide));dv.addLayout(dr)
        self.details=QTextBrowser();dv.addWidget(self.details);self.detail_panel.setMinimumWidth(340);splitter.addWidget(self.detail_panel);splitter.setStretchFactor(0,3);splitter.setStretchFactor(1,1);self.detail_panel.hide();v.addWidget(splitter,1)
        self.table.clicked.connect(self.show_detail)
        for x in [self.mode,self.warehouse_filter,self.category_filter]:x.currentTextChanged.connect(self.filter_changed)
        self.search.textChanged.connect(self.filter_changed)
        self.pages.addWidget(page)
    def file_changed(self,*a):
        self.loaded=None;self.mark_dirty();self.check_label.setText('文件已更换，需要重新检查。');self.check_note.setText('')
        self.update_controls();QTimer.singleShot(200,self.auto_load)
    def auto_load(self):
        if self.loaded is None and not self.worker and self.sales.path and self.products.path:self.start_load()
    def mark_dirty(self,*a):
        if self.initializing:return
        self.dirty=True
        if self.result:self.result_caption.setText('上次计算结果 · 输入或参数已变化，请重新计算')
        self.update_controls()
    def selected_store(self):return self.stores.currentData()
    def store_changed(self,*a):
        if self.initializing:return
        self.restore_budget()
        self.mark_dirty()
        if self.loaded and self.selected_store():self.preview_store()
    def settings(self):
        return {self.warehouses.item(i,0).text():(self.warehouses.cellWidget(i,1).value(),self.warehouses.cellWidget(i,2).value()) for i in range(self.warehouses.rowCount())}
    def update_controls(self):
        busy=self.worker is not None
        self.input_content.setEnabled(not busy);self.load_btn.setEnabled(not busy and bool(self.sales.path and self.products.path))
        ready=self.loaded is not None and bool(self.selected_store())
        self.calculate_btn.setEnabled(not busy and ready);self.review_btn.setEnabled(not busy and ready)
        can_export=self.result is not None and not self.dirty and not busy
        self.export_all.setEnabled(can_export);self.export_filtered.setEnabled(can_export)
        self.cancel_btn.setVisible(busy);self.progress.setVisible(busy)
    def job(self,fn,args,success):
        if self.worker:return
        w=Worker(fn,args,self);self.worker=w;self.update_controls();w.status.connect(self.status.setText);w.success.connect(success);w.failed.connect(self.failed);w.cancelled.connect(lambda:self.status.setText('操作已停止。分店导出时已完成文件会保留，请查看目标目录中的部分导出说明。'));w.finished.connect(self.job_finished);w.start()
    def job_finished(self):
        w=self.worker;self.worker=None
        if w:w.deleteLater()
        self.update_controls()
        if self.closing:self.close()
    def failed(self,message):
        self.status.setText('未完成：'+message)
        QMessageBox.warning(self,'操作未完成',message)
    def cancel_work(self):
        if self.worker:self.worker.cancel.set();self.status.setText('正在取消，当前读取步骤结束后返回…')
    def start_load(self):
        if not self.sales.path or not self.products.path:return
        self.status.setText('正在读取文件…');self.job(engine.load_inputs,(self.sales.path,self.products.path),self.loaded_ok)
    def loaded_ok(self,loaded):
        self.loaded=loaded;self.result=None;self.dirty=True;self.initializing=True
        self.stores.clear()
        if len(loaded['stores'])>1:self.stores.addItem('请选择一个门店',None)
        for code,name in loaded['stores'].items():self.stores.addItem(f'{name}  ·  {code}',code)
        self.warehouses.setRowCount(len(loaded['warehouses']))
        for i,wh in enumerate(loaded['warehouses']):
            item=QTableWidgetItem(wh);item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEditable);self.warehouses.setItem(i,0,item)
            for col,value in enumerate(engine.DEFAULT_WAREHOUSES.get(wh,(-1,-1)),1):
                spin=QDoubleSpinBox();spin.setRange(-1,365);spin.setDecimals(1);spin.setSpecialValueText('未设置');spin.setValue(value);spin.valueChanged.connect(self.mark_dirty);self.warehouses.setCellWidget(i,col,spin)
        self.initializing=False;self.nav2.setEnabled(True)
        self.restore_budget()
        self.status.setText('读取完成。请核对门店与参数。')
        if self.selected_store():self.preview_store()
        else:self.model.set_rows([]);self.check_label.setText('已读取多个门店，请选择本次计算门店。')
    def preview_store(self):
        rows=engine.preview(self.loaded,self.selected_store());members={d['sku'] for d in self.profile['items']} if self.profile else set()
        for d in rows:d['_must_sell']=d['E'] in members
        self.result=None;self.dirty=True;self.set_rows(rows,False)
        matched=sum(d['_source']=='资料匹配' for d in rows);local=sum(d['_source']=='地采商品' for d in rows);issues=sum(bool(engine.issue_text(d)) for d in rows)
        self.check_label.setText(f'已读取 {len(rows):,} 行 · 资料匹配 {matched:,} · 地采 {local:,}')
        excluded=sum(bool(engine.exclusion_reason(d)) for d in rows)
        self.check_note.setText(f'数据异常 {issues:,} 行。禁用或停采 {excluded:,} 行默认不补；仍保留其可用计算结果，单列“已排除”。')
        self.calculate_btn.setText('计算并筛选');self.update_controls()
    def review(self):self.pages.setCurrentIndex(1)
    def start_calculation(self):
        if not self.loaded or not self.selected_store():return
        settings=self.settings()
        bad=[w for w,(d,l) in settings.items() if d<0 or l<0 or d+l<=0]
        if bad:QMessageBox.warning(self,'请补齐参数','请设置这些仓的备货/到货天数：'+ '、'.join(bad));return
        if self.budget_enabled.isChecked() and not self.profile:QMessageBox.warning(self,'必卖清单未就绪','请先导入有效必卖清单。');return
        budget=self.budget_amount.value() if self.budget_enabled.isChecked() else None
        self.dirty=True;self.status.setText('开始计算…');self.job(engine.run_calculation,(self.loaded,self.selected_store(),settings,self.exception.isChecked(),budget,self.profile),self.calculated_ok)
    def calculated_ok(self,result):
        self.result=result;self.dirty=False;self.set_rows(result['rows'],True);self.pages.setCurrentIndex(1);self.status.setText('计算完成；原输入文件保持不变。')
        try:self.repository.save_budget(result['store'],self.budget_enabled.isChecked(),self.budget_amount.value())
        except Exception as exc:self.status.setText(f'计算完成，但门店预算未能保存：{exc}')
    def set_rows(self,rows,calculated):
        self.model.set_rows(rows);self.detail_panel.hide();self.search.clear()
        self.warehouse_filter.clear();self.warehouse_filter.addItems(['全部仓']+sorted({str(d.get('AU','')) for d in rows}))
        self.category_filter.clear();self.category_filter.addItems(['全部分类']+sorted({str(d.get('_category') or '未分类') for d in rows}))
        self.mode.setCurrentText('建议补货' if calculated else '全部商品');self.filter_changed()
        s=engine.summarize(rows)
        self.metric_values[0].setText(f"{s['amount']:,.2f}" if calculated else '待计算')
        self.metric_values[1].setText(f"{s['positive']:,}" if calculated else '待计算')
        self.metric_values[2].setText(f"{s['exception']} / +{s['extra']:,.2f}" if calculated else '待计算')
        self.metric_values[3].setText(f"{s['local']:,} / {s['excluded']:,}")
        name=self.loaded['stores'].get(self.selected_store(),'') if self.loaded else ''
        self.result_caption.setText(f'{name} · '+('计算完成' if calculated else '导入预览 · 尚未计算')+ (f" · {s['unpriced']}款补货未计价" if s['unpriced'] else ''))
        b=self.result.get('budget',{}) if calculated and self.result else {}
        if b.get('enabled'):
            self.budget_caption.setText(f"预算 {b['limit']:,.2f} 元 · 已用 {b['spent']:,.2f} · 剩余 {b['remaining']:,.2f} · 必卖 {b['must_spent']:,.2f} / 普通 {b['other_spent']:,.2f} · 未满足 {b['deferred']} 款 · 最低持有缺口 {b['holding_gaps']} 款")
        else:self.budget_caption.setText('总预算未启用；保持原执行建议。' if calculated else '预算分配在原计算和禁用/停采排除之后执行。')
        if calculated and self.result.get('must_list'):
            ml=self.result['must_list'];self.budget_caption.setText(self.budget_caption.text()+f"\n必卖清单 {ml['total']} 款，本店命中 {ml['matched']} 款，{ml['missing']} 款未出现在本店导出；不自动新增补货。")
    def visible_rows(self):return [self.model.rows[self.proxy.mapToSource(self.proxy.index(i,0)).row()] for i in range(self.proxy.rowCount())]
    def filter_changed(self,*a):
        self.proxy.beginFilterChange();self.proxy.mode=self.mode.currentText();self.proxy.warehouse=self.warehouse_filter.currentText() or '全部仓';self.proxy.category=self.category_filter.currentText() or '全部分类';self.proxy.search=self.search.text().strip().lower();self.proxy.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        s=engine.summarize(self.visible_rows());self.filtered_caption.setText(f"当前筛选 {s['rows']:,} 行  ·  执行金额 {s['amount']:,.2f} 元  ·  计算金额 {s['calculated_amount']:,.2f} 元  ·  已排除 {s['excluded']:,} 行  ·  数据异常 {s['issues']:,} 行")
    def show_detail(self,index):
        d=self.model.rows[self.proxy.mapToSource(index).row()];esc=lambda x:html.escape(engine.fmt(x))
        def group(title,items):return '<h3>'+title+'</h3><table width="100%">'+''.join(f'<tr><td style="color:#60777D;padding:5px 0">{html.escape(k)}</td><td align="right">{esc(v)}</td></tr>' for k,v in items)+'</table>'
        content=f'<h2>{esc(d.get("D"))}</h2><p>{esc(d.get("E"))} · {d["_source"]}</p>'
        content+='<p><b>'+esc(engine.order_status(d))+'</b> '+esc(engine.exclusion_reason(d))+'</p>'
        content+='<p>优先清单：'+('必卖商品' if d.get('_must_sell') else '普通商品')+'</p>'
        route=engine.routing(d)
        if route:
            content+=group('需求归属',[('主仓',route['primary']),('从仓',route['secondary'] or '无'),('需求归属仓',route['assigned']),('主仓判断',route['primary_state']),('主仓可用数',route['primary_stock']),('从仓判断',route['secondary_state']),('从仓可用数',route['secondary_stock'])])+'<p>'+esc(route['reason'])+'</p><p>此阶段只确定归属，不拆单、不按仓库存限量或预占；从仓无货仍保留需求。禁用和停采按执行规则处理。</p>'
        if not d.get('_preview'):content+=group('本次执行',[('执行补货',engine.order_quantity(d)),('执行金额',engine.order_amount(d))])+group('计算参考（未按状态排除）',[('计算建议',engine.quantity(d)),('计算金额',engine.amount_value(d)),('按计算建议持有天数',engine.coverage(d))])
        if d.get('_budget_active'):
            content+=group('资金分配',[('预算前执行量',d.get('_candidate_qty')),('预算前金额',d.get('_candidate_amount')),('缺口数量',d.get('_budget_gap')),('最低持有缺口','有' if d.get('_holding_gap') else '无')])+'<p>'+esc(d.get('_budget_reason'))+'</p>'
        content+=group('商品现状',[('所属仓',d.get('AU')),('导出状态',d['_meta'].get('export_status')),('中心仓状态',d.get('S')),('月销 / 周销',f'{engine.fmt(d.get("J"))} / {engine.fmt(d.get("I"))}'),('可用 / 在途',f'{engine.fmt(d.get("K"))} / {engine.fmt(d.get("L"))}'),('箱规',d.get('BD',d.get('O'))),('基本售价',d.get('V'))])
        if d['_source']=='地采商品':content+='<h3>地采商品</h3><p>商品编码未匹配资料。原始销量和库存保留；缺少价格、出库箱规等计算条件，本版不生成补货数量和金额。</p>'
        elif not d.get('_preview'):
            rate=d['AN']*d['AM'] if engine.numeric(d.get('AN')) and engine.numeric(d.get('AM')) else None
            current=(max(d['K'],0)+max(d['L'],0))/rate if rate and engine.numeric(d.get('K')) and engine.numeric(d.get('L')) else None
            content+=group('需求预测与库存额度',[('预测日均',rate),('额度基准日均 BH',d.get('BH')),('增长调整日均 BN',d.get('BN')),('当前可售天数',current),('到货天数',d.get('AH')),('备货天数',d.get('AG')),('安全库存',d.get('AL')),('库存额度总量',d.get('库存额度目标量'))])
            content+='<p style="color:#60777D">库存额度受历史销量约束，未统一为预测日均。可售天数按预测日均折算；无正预测时不显示为0天。</p>'
            content+=group('从需求到计算建议',[('净需求',d.get('AQ')),('向上整箱',d.get('BJ')),('额度后',d.get('AR')),('资金前',d.get('BR')),('原模型建议',d.get('_base_qty')),('原模型金额',d.get('_base_amount')),('计算建议',engine.quantity(d)),('计算金额',engine.amount_value(d)),('按计算建议持有天数',engine.coverage(d))])
            a=d.get('_audit',{});content+='<h3>一箱例外</h3><p>'+esc(a.get('reason','未执行'))+'</p>'
            if a.get('applied'):content+=group('放行依据',[('原拦截',a.get('blocked_by')),('新增金额',a.get('incremental_amount'))])
        content+='<h3>结果说明</h3><p>'+esc(engine.explanation(d))+'</p><p>'+esc('；'.join(d['_meta']['notes']))+'</p>'
        self.details.setHtml(content);self.detail_panel.show()
    def show_parameters(self):
        cfg=self.loaded['config'] if self.loaded else engine.config();dialog=QDialog(self);dialog.setWindowTitle('完整参数 · 只读');dialog.resize(730,650);v=QVBoxLayout(dialog);text=QTextBrowser();v.addWidget(text)
        lines=['<h2>原模型参数</h2><p>备货及到货天数以主界面的分仓设置为准。其他参数首版只读。</p><table width="100%">']
        for k,val in cfg['parameters'].items():lines.append(f'<tr><td>{html.escape(cfg["parameter_labels"].get(k,k))}</td><td>{val}</td></tr>')
        lines.append('</table><h3>分类权重与映射</h3><pre>'+html.escape(json.dumps({k:cfg[k] for k in ['weight_profiles','category_profiles']},ensure_ascii=False,indent=2))+'</pre>');text.setHtml(''.join(lines));v.addWidget(button('关闭',dialog.accept));dialog.exec()
    def export(self,filtered):
        if not self.result or self.dirty:return
        rows=self.visible_rows() if filtered else self.result['rows']
        if not rows:QMessageBox.information(self,'没有可导出商品','请调整筛选条件。');return
        folder=Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation))/'补货助手输出';folder.mkdir(parents=True,exist_ok=True)
        name=f'补货结果_{self.result["store"]}_{datetime.now():%Y%m%d_%H%M%S}.xlsx'
        path,_=QFileDialog.getSaveFileName(self,f'导出{len(rows):,}行 · '+('当前筛选' if filtered else '本次全部结果'),str(folder/name),'Excel 工作簿 (*.xlsx)',options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.xlsx'):path+='.xlsx'
        scope=f'当前筛选：{self.mode.currentText()}；{self.warehouse_filter.currentText()}；{self.category_filter.currentText()}；搜索={self.search.text()}；共{len(rows)}行' if filtered else '本次全部结果'
        self.job(engine.export_xlsx,(self.result,path,rows,scope),self.export_ok)
    def export_ok(self,path):
        self.last_export=path;self.status.setText('导出成功：'+path)
        box=QMessageBox(self);box.setWindowTitle('导出完成');box.setText('已保存本次结果。');box.setInformativeText(path)
        open_file=box.addButton('打开文件',QMessageBox.ButtonRole.ActionRole);open_dir=box.addButton('打开文件夹',QMessageBox.ButtonRole.ActionRole);box.addButton('完成',QMessageBox.ButtonRole.AcceptRole);box.exec()
        if box.clickedButton()==open_file:QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif box.clickedButton()==open_dir:QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))
    def closeEvent(self,event):
        if self.worker:self.closing=True;self.cancel_work();event.ignore()
        else:event.accept()

def main():
    app=QApplication(sys.argv);app.setApplicationName('补货助手');app.setOrganizationName('YoutuanTools');app.setStyle('Fusion')
    palette=app.style().standardPalette()
    for role,color in [(QPalette.ColorRole.Window,'#F3F6F7'),(QPalette.ColorRole.WindowText,'#22383C'),(QPalette.ColorRole.Base,'#FFFFFF'),(QPalette.ColorRole.AlternateBase,'#F7FAFA'),(QPalette.ColorRole.Text,'#22383C'),(QPalette.ColorRole.Button,'#FFFFFF'),(QPalette.ColorRole.ButtonText,'#22383C'),(QPalette.ColorRole.Highlight,'#DDEFEA'),(QPalette.ColorRole.HighlightedText,'#153C43')]:palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE)
    icon=Path(__file__).parent/'app.ico'
    if icon.exists():app.setWindowIcon(QIcon(str(icon)))
    logs=Path(os.environ.get('REPLENISHMENT_DATA_DIR') or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation));logs.mkdir(parents=True,exist_ok=True);logging.basicConfig(filename=logs/'app.log',level=logging.INFO,encoding='utf-8')
    def unexpected(t,v,tb):
        logging.error('界面异常',exc_info=(t,v,tb));QMessageBox.critical(None,'操作未完成',f'{v}\n日志位置：{logs}')
    sys.excepthook=unexpected
    from workbench import Workbench
    win=Workbench();win.show()
    if '--batch-smoke' in sys.argv:
        from batch_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--batch-smoke')+1:])
    if '--warehouse-smoke' in sys.argv:
        from warehouse_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--warehouse-smoke')+1:])
    if '--work-smoke' in sys.argv:
        from work_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--work-smoke')+1:])
    return app.exec()

if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(main())
