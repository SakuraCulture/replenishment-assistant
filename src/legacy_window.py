"""Compatibility single-store window; active workbench does not inherit this class."""
from __future__ import annotations
from app_defaults import DEFAULT_WAREHOUSES
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


from ui_controls import FileCard,label,button
from ui_session import SessionWindow

class MainWindow(SessionWindow):
    def __init__(self):
        super().__init__()
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
    def restore_budget(self):
        data={}
        try:data=self.repository.budgets().get(self.selected_store(),{})
        except Exception as exc:self.status.setText(f'门店预算未载入，请本次重新填写：{exc}')
        self.budget_enabled.blockSignals(True);self.budget_amount.blockSignals(True)
        self.budget_enabled.setChecked(bool(data.get('enabled',False)));self.budget_amount.setValue(data.get('amount',0))
        self.budget_amount.setEnabled(self.budget_enabled.isChecked());self.budget_enabled.blockSignals(False);self.budget_amount.blockSignals(False)
    def priority_imported(self,profile):
        try:self.repository.save(profile)
        except Exception as exc:self.failed('清单保存失败，仍保留原清单：'+str(exc));return
        self.profile=profile;self.config_warning='';self.refresh_profile_label();self.mark_dirty()
        self.status.setText(f"必卖清单已保存 {len(profile['items'])} 款，后续自动复用；更新清单后需重新计算。")
        if self.loaded and self.selected_store() and self.result is None:self.preview_store()
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
            for col,value in enumerate(DEFAULT_WAREHOUSES.get(wh,(-1,-1)),1):
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
    def show_parameters(self):
        cfg=self.loaded['config'] if self.loaded else engine.config();dialog=QDialog(self);dialog.setWindowTitle('完整参数 · 只读');dialog.resize(730,650);v=QVBoxLayout(dialog);text=QTextBrowser();v.addWidget(text)
        lines=['<h2>原模型参数</h2><p>备货及到货天数以主界面的分仓设置为准。其他参数首版只读。</p><table width="100%">']
        for k,val in cfg['parameters'].items():lines.append(f'<tr><td>{html.escape(cfg["parameter_labels"].get(k,k))}</td><td>{val}</td></tr>')
        lines.append('</table><h3>分类权重与映射</h3><pre>'+html.escape(json.dumps({k:cfg[k] for k in ['weight_profiles','category_profiles']},ensure_ascii=False,indent=2))+'</pre>');text.setHtml(''.join(lines));v.addWidget(button('关闭',dialog.accept));dialog.exec()
