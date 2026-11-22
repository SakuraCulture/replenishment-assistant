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
from app_defaults import WAREHOUSES
from version_info import VERSION


from ui_controls import label,button
from ui_tasks import BackgroundJobs
from ui_results import ResultsView

class SessionWindow(BackgroundJobs,ResultsView,QMainWindow):
    def __init__(self,*,initializing=False):
        super().__init__()
        self.loaded=None;self.result=None;self.worker=None;self.dirty=True
        self.saved_job_model_version=None
        self.initializing=initializing;self.closing=False;self.last_export=None
        self.profile=None;self.config_warning=""
        from data_location import root
        folder=root()
        self.repository=priority_list.Repository(folder)

    def refresh_profile_label(self):
        if priority_list.is_catalog_profile(self.profile):
            warehouses=self.profile['warehouses'];parts=[]
            for warehouse in WAREHOUSES:
                snapshot=warehouses.get(warehouse)
                if not snapshot:
                    parts.append(warehouse+'待同步')
                    continue
                message=f"{warehouse} {snapshot['count']:,} 款"
                if snapshot['missing_tag_fields']:
                    message+=f"（{snapshot['missing_tag_fields']:,} 款标签字段待核）"
                parts.append(message)
            self.profile_label.setText('从各仓商品资料自动获取「必卖清单」标签\n'+' · '.join(parts))
        elif self.profile:
            self.profile_label.setText(f"保存的必卖清单 {len(self.profile['items']):,} 款 · 版本 {self.profile['revision']}\n{self.profile['name']}")
        else:self.profile_label.setText(self.config_warning or '尚无必卖清单。')

    def choose_priority(self):
        from import_ui import choose
        paths=choose(self,'priority','导入必卖清单（完整替换现有清单）');path=paths[0] if paths else None
        if path:self.job(priority_list.import_profile,(path,),self.priority_imported)

    def backup_priority(self):
        if not self.profile:return
        path,_=export_locations.save_file(self,'备份当前显示的必卖清单','必卖清单_'+self.profile['revision']+'.json','清单备份 (*.json)',options=QFileDialog.Option.DontConfirmOverwrite)
        if not path:return
        if not path.lower().endswith('.json'):path+='.json'
        try:priority_list.export_profile(self.profile,path);self.status.setText('清单已备份：'+path)
        except Exception as exc:self.failed(str(exc))

    def show_priority(self):
        if not self.profile:return
        per_warehouse=priority_list.is_catalog_profile(self.profile)
        dialog=QDialog(self);dialog.setWindowTitle('必卖清单');dialog.resize(1080,620);v=QVBoxLayout(dialog)
        v.addWidget(label(f"{self.profile['name']} · {len(self.profile['items']):,} 款次 · {self.profile['revision']}",'section'))
        warehouse_filter=QComboBox();warehouse_filter.addItem('全部仓')
        if per_warehouse:
            for warehouse in WAREHOUSES:
                snapshot=self.profile['warehouses'].get(warehouse)
                if snapshot:
                    v.addWidget(label(f"{warehouse} · {snapshot['count']:,} 款 · 资料时间 {snapshot['imported_at'] or '未提供'}",'muted'))
                    warehouse_filter.addItem(warehouse)
                else:v.addWidget(label(warehouse+' · 待同步商品资料','muted'))
            v.addWidget(warehouse_filter)
        else:v.addWidget(label('该清单随作业保存，供回看时核对。','muted'))
        search=QLineEdit();search.setPlaceholderText('按商品编码或名称查找');v.addWidget(search)
        headers=(['供货仓'] if per_warehouse else [])+['商品编码','商品名称','分类','本次匹配状态']
        table=QTableWidget(len(self.profile['items']),len(headers));table.setHorizontalHeaderLabels(headers);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        audit={}
        if self.result and not self.dirty and (self.result.get('must_list') or {}).get('revision')==self.profile['revision']:
            audit={(d.get('warehouse'),d['sku']):d['state'] for d in self.result.get('must_list_audit',[])}
        for i,d in enumerate(self.profile['items']):
            values=([d['warehouse']] if per_warehouse else [])+[d['sku'],d.get('name',''),d.get('category',''),audit.get((d.get('warehouse'),d['sku']),'—')]
            for j,value in enumerate(values):table.setItem(i,j,QTableWidgetItem(value))
        for i,width in enumerate(([90] if per_warehouse else [])+[205,355,120,220]):table.setColumnWidth(i,width)
        def filter_list(*_):
            text=search.text().strip().lower();warehouse=warehouse_filter.currentText()
            for i,d in enumerate(self.profile['items']):
                table.setRowHidden(i,(warehouse!='全部仓' and d.get('warehouse')!=warehouse) or text not in (d['sku']+' '+d.get('name','')).lower())
        search.textChanged.connect(filter_list);warehouse_filter.currentIndexChanged.connect(filter_list)
        v.addWidget(table,1);v.addWidget(button('关闭',dialog.accept));dialog.exec()

