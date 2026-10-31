"""Explicit shared session state. Does not build pages or call virtual UI methods."""
from __future__ import annotations
import export_locations
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


from ui_controls import label,button
from ui_tasks import BackgroundJobs
from ui_results import ResultsView

class SessionWindow(BackgroundJobs,ResultsView,QMainWindow):
    def __init__(self,*,initializing=False):
        super().__init__()
        self.loaded=None;self.result=None;self.worker=None;self.dirty=True
        self.initializing=initializing;self.closing=False;self.last_export=None
        self.profile=None;self.config_warning=""
        folder=os.environ.get("REPLENISHMENT_DATA_DIR") or str(Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))/"settings")
        self.repository=priority_list.Repository(folder)

    def refresh_profile_label(self):
        if self.profile:self.profile_label.setText(f"已保存必卖清单 {len(self.profile['items']):,} 款 · 版本 {self.profile['revision']}\n{self.profile['name']}")
        else:self.profile_label.setText(self.config_warning or '尚无有效必卖清单，请导入。')

    def choose_priority(self):
        from import_ui import choose
        paths=choose(self,'priority','导入必卖清单（完整替换现有清单）');path=paths[0] if paths else None
        if path:self.job(priority_list.import_profile,(path,),self.priority_imported)

    def backup_priority(self):
        if not self.profile:return
        path,_=export_locations.save_file(self,'备份必卖清单，可在其他电脑导入','必卖清单_'+self.profile['revision']+'.json','清单备份 (*.json)',options=QFileDialog.Option.DontConfirmOverwrite)
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

