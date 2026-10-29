"""Guided native desktop workbench; business calculations remain in engine."""
from __future__ import annotations
import copy,json,os,threading
from collections import Counter
from datetime import datetime
from decimal import Decimal,InvalidOperation
from pathlib import Path
from PySide6.QtCore import Qt,Signal,QThread,QStandardPaths,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget,QMainWindow,QVBoxLayout,QHBoxLayout,QLabel,QFrame,QPushButton,
    QFileDialog,QComboBox,QCheckBox,QDoubleSpinBox,QSpinBox,QTableWidget,QTableWidgetItem,QHeaderView,
    QLineEdit,QStackedWidget,QMessageBox,QDialog,QProgressBar,QAbstractItemView,QScrollArea,QListWidget,QApplication)
from ui_controls import FileCard,SalesFiles,label,button
from ui_tasks import BatchWorker,BackgroundJobs
from ui_session import SessionWindow
import engine,priority_list,batch_service
import warehouse_catalog
from warehouse_ui import WarehouseUi
from version_info import VERSION,MODEL_VERSION,diagnostic

class BatchWindow(WarehouseUi,SessionWindow):
    def __init__(self):
        super().__init__(initializing=True)
        self.results={};self.states={};self.store_widgets={};self.overrides={};self.default_by_store={};self.run_codes=[];self.current_store=None;self.config_warning='';self.profile=None;self.input_dirty=True
        folder=self.repository.folder
        self.catalog_repository=warehouse_catalog.Repository(folder);self.catalogs={}
        try:self.catalogs=self.catalog_repository.catalogs()
        except Exception as exc:self.config_warning='仓资料未载入：'+str(exc)
        try:self.profile=self.repository.profile()
        except Exception as exc:self.config_warning=str(exc)
        self.setWindowTitle(f'补货助手 {VERSION} · 多店工作台');self.resize(1380,900);self.setMinimumSize(1080,720)
        root=QWidget();self.setCentralWidget(root);v=QVBoxLayout(root);v.setContentsMargins(24,18,24,16);v.setSpacing(12)
        head=QHBoxLayout();titles=QVBoxLayout();titles.addWidget(label('补货助手','title'));subtitle=label('多店一起算，预算各店用 · 必卖优先 · 全程本地处理','muted');subtitle.setWordWrap(False);titles.addWidget(subtitle);head.addLayout(titles);head.addStretch();head.addWidget(button('版本信息',self.about));v.addLayout(head)
        nav=QHBoxLayout();self.nav=[]
        for i,text in enumerate(['① 准备数据','② 门店与预算','③ 计算进度','④ 查看与导出']):
            b=button(text,lambda checked=False,n=i:self.navigate(n));self.nav.append(b);nav.addWidget(b)
        v.addLayout(nav);self.pages=QStackedWidget();v.addWidget(self.pages,1)
        self.make_import();self.make_stores();self.make_progress();self.make_overview();self.make_results()
        detail=self.pages.widget(4).layout();detail.insertWidget(0,button('← 返回门店汇总',lambda:self.navigate(3)))
        self.mode.addItems(['必卖未补足','其他预算暂缓'])
        bottom=QHBoxLayout();self.status=label('先添加补货建议和商品资料，检查后选择本次门店。','muted');bottom.addWidget(self.status,1)
        self.progress=QProgressBar();self.progress.setRange(0,0);self.progress.setMaximumWidth(160);bottom.addWidget(self.progress)
        self.cancel_btn=button('停止',self.cancel_work);bottom.addWidget(self.cancel_btn);version_label=label(f'应用 {VERSION} · 模型 {MODEL_VERSION}','muted');version_label.setWordWrap(False);bottom.addWidget(version_label);v.addLayout(bottom)
        self.initializing=False;self.restore_master();self.update_controls()
        self.store_table.verticalHeader().setDefaultSectionSize(56)
        for table in [self.task_table,self.overview,self.file_report]:table.verticalHeader().setDefaultSectionSize(46)
    def page(self):
        p=QWidget();v=QVBoxLayout(p);v.setContentsMargins(0,0,0,0);v.setSpacing(12);self.pages.addWidget(p);return p,v
    def make_import(self):
        self.import_page,outer=self.page();content=QWidget();v=QVBoxLayout(content);v.setContentsMargins(0,0,0,0)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setFrameShape(QFrame.Shape.NoFrame);scroll.setWidget(content);outer.addWidget(scroll,1)
        files=QHBoxLayout();self.sales=SalesFiles();warehouses=self.make_warehouse_panel()
        files.addWidget(self.sales,3);files.addWidget(warehouses,3);v.addLayout(files)
        self.sales.changed.connect(self.file_changed)
        card=QFrame();card.setObjectName('card');cv=QVBoxLayout(card);row=QHBoxLayout();row.addWidget(label('必卖清单','section'));row.addStretch();row.addWidget(button('查看清单',self.show_priority));row.addWidget(button('导入 / 更新',self.choose_priority));row.addWidget(button('备份',self.backup_priority));cv.addLayout(row)
        self.profile_label=label('','muted');cv.addWidget(self.profile_label);self.refresh_profile_label();v.addWidget(card)
        self.import_note=label('分仓资料分别导入后检查。需求按门店主从仓归属，不拆单、不预占仓库存；禁用或停采仍按执行规则排除。','muted');v.addWidget(self.import_note)
        self.file_report=QTableWidget(0,4);self.file_report.setHorizontalHeaderLabels(['文件','商品行数','门店数','读取结果 / 处理办法']);self.file_report.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch);self.file_report.setColumnWidth(0,300);self.file_report.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);v.addWidget(self.file_report,1)
        actions=QHBoxLayout();self.load_btn=button('检查文件',self.start_load);actions.addWidget(self.load_btn);self.conflict_btn=button('处理重复冲突',self.resolve_conflicts);actions.addWidget(self.conflict_btn);actions.addStretch();self.next_btn=button('下一步：选择门店',lambda:self.navigate(1),True);actions.addWidget(self.next_btn);outer.addLayout(actions)
    def make_stores(self):
        self.store_page,v=self.page();v.addWidget(label('选择本次门店，分别设置可用资金','section'));v.addWidget(label('限额补货优先保必卖；不限预算按模型建议补货。金额为0表示本次不安排。各店资金不互相挪用。','muted'))
        row=QHBoxLayout();self.store_search=QLineEdit();self.store_search.setPlaceholderText('搜索门店名称或编码');self.store_search.textChanged.connect(self.filter_stores);row.addWidget(self.store_search,1)
        self.only_issues=QCheckBox('只看需处理');self.only_issues.toggled.connect(self.filter_stores);row.addWidget(self.only_issues);row.addWidget(button('全选可计算门店',lambda:self.select_all(True)));row.addWidget(button('取消全选',lambda:self.select_all(False)));v.addLayout(row)
        row=QHBoxLayout();row.addWidget(button('批量设置主从仓',self.bulk_routes));row.addWidget(button('批量设置每店预算',self.bulk_budget));row.addWidget(button('批量设置仓参数',self.bulk_parameters));row.addStretch();row.addWidget(button('查看完整模型参数',self.show_parameters));v.addLayout(row)
        self.store_table=QTableWidget(0,9);self.store_table.setHorizontalHeaderLabels(['选择','门店','数据检查','主仓','从仓','预算方式','本次预算（元）','参数','配置提示']);self.store_table.verticalHeader().hide();self.store_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.store_table.setAlternatingRowColors(True)
        for i,w in enumerate([50,235,165,140,140,140,145,110,190]):self.store_table.setColumnWidth(i,w)
        self.store_table.horizontalHeader().setSectionResizeMode(8,QHeaderView.ResizeMode.Stretch);v.addWidget(self.store_table,1)
        options=QHBoxLayout();self.exception=QCheckBox('启用已验收的一箱例外');self.exception.setChecked(True);self.exception.toggled.connect(self.mark_dirty);options.addWidget(self.exception);options.addStretch()
        options.addWidget(label('同时计算门店数'));self.concurrency=QSpinBox();self.concurrency.setRange(1,4);self.concurrency.setValue(2);options.addWidget(self.concurrency);v.addLayout(options)
        self.selection_note=label('请先导入数据。','muted');v.addWidget(self.selection_note)
        row=QHBoxLayout();row.addWidget(button('上一步：准备数据',lambda:self.navigate(0)));row.addStretch();self.calculate_btn=button('开始计算',self.start_calculation,True);row.addWidget(self.calculate_btn);v.addLayout(row)
    def make_progress(self):
        self.run_page,v=self.page();v.addWidget(label('各店独立计算，完成一家即可查看','section'));self.run_note=label('尚未开始','muted');v.addWidget(self.run_note)
        self.run_progress=QProgressBar();self.run_progress.setRange(0,1);self.run_progress.setValue(0);v.addWidget(self.run_progress)
        self.task_table=QTableWidget(0,4);self.task_table.setHorizontalHeaderLabels(['门店','状态','安排金额','提示']);self.task_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeMode.Stretch);self.task_table.setColumnWidth(0,280);self.task_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);v.addWidget(self.task_table,1)
        row=QHBoxLayout();self.stop_btn=button('停止剩余任务',self.cancel_work);row.addWidget(self.stop_btn);self.retry_btn=button('重试失败门店',self.retry_failed);row.addWidget(self.retry_btn);row.addStretch();row.addWidget(button('查看已完成结果',lambda:self.navigate(3),True));v.addLayout(row)
    def make_overview(self):
        self.overview_page,v=self.page();v.addWidget(label('本次投入多少，还有哪些需求没满足？','section'));self.overview_note=label('计算完成后显示。','muted');v.addWidget(self.overview_note)
        row=QHBoxLayout();self.metrics=[]
        for title in ['预算前可补需求（元）','本次安排金额（元）','未满足需求（元）']:
            c=QFrame();c.setObjectName('card');cv=QVBoxLayout(c);cv.addWidget(label(title,'muted'));x=label('—','metric');cv.addWidget(x);row.addWidget(c);self.metrics.append(x)
        v.addLayout(row);v.addWidget(label('金额只统计有效完成门店；地采及缺价单列，不视为零成本。点击门店或缺口列查看商品。','muted'))
        self.overview=QTableWidget(0,12);self.overview.setHorizontalHeaderLabels(['门店','状态','预算','可补需求','本次安排','必卖金额','其他金额','未满足需求','剩余预算','最低持有缺口','地采 / 已排除','提示'])
        self.overview.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);self.overview.setAlternatingRowColors(True);self.overview.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);self.overview.setColumnWidth(0,250);self.overview.setColumnWidth(11,330);self.overview.cellClicked.connect(self.open_overview);v.addWidget(self.overview,1)
        row=QHBoxLayout();row.addWidget(button('返回门店设置',lambda:self.navigate(1)));row.addStretch();self.batch_export=button('导出本次补货结果',self.export_dialog,True);row.addWidget(self.batch_export);v.addLayout(row)
    def about(self):
        text=json.dumps(diagnostic(),ensure_ascii=False,indent=2);box=QMessageBox(self);box.setWindowTitle('版本信息');box.setText(f'补货助手 {VERSION}\n模型 {MODEL_VERSION}\n多文件、多店独立预算、并发计算。');box.setDetailedText(text)
        copy_btn=box.addButton('复制诊断信息',QMessageBox.ButtonRole.ActionRole);box.addButton('关闭',QMessageBox.ButtonRole.AcceptRole);box.exec()
        if box.clickedButton()==copy_btn:QApplication.clipboard().setText(text)
    def restore_master(self):
        p=self.repository.folder/'workbench.json'
        try:
            if p.exists():
                path=json.loads(p.read_text(encoding='utf-8')).get('products','')
                if path and Path(path).is_file():self.products.set_path(path);self.status.setText('旧混合资料路径已保留在兼容模式；分仓使用请分别导入河北仓、山东仓资料。')
        except Exception as exc:self.status.setText('上次资料路径未恢复，请重新选择：'+str(exc))
    def file_changed(self,*args):
        if self.initializing:return
        self.input_dirty=True;self.mark_dirty();self.import_note.setText('文件列表已变化，请重新检查。上次结果已标记待重算。');self.update_controls()
    def start_load(self):
        if self.worker:return
        products=self.products.path if self.master_legacy.isChecked() else self.catalogs
        if self.sales.paths and products:
            self.input_dirty=True;self.mark_dirty()
            self.job(batch_service.load,(self.sales.paths,products),self.loaded_ok)
    def loaded_ok(self,loaded):
        self.loaded=loaded;self.input_dirty=False;self.results={};self.result=None;self.current_store=None;self.overrides={};self.dirty=True;self.run_codes=[]
        self.default_by_store={s:{w:engine.DEFAULT_WAREHOUSES[w] for w in loaded.get('catalogs',{})} for s in loaded['stores']}
        for d,m in loaded['normalized']:
            if d['_source']=='资料匹配':self.default_by_store[d['B']][d['AU']]=engine.DEFAULT_WAREHOUSES.get(d['AU'],(-1,-1))
        data=loaded['intake'];self.file_report.setRowCount(len(data['files']))
        for i,f in enumerate(data['files']):
            for j,value in enumerate([Path(f['path']).name,f['rows'],f['stores'],f['error'] or '读取成功']):self.file_report.setItem(i,j,QTableWidgetItem(str(value)))
        conflicts=sum(c['choice'] is None for c in data['conflicts']);errors=sum(bool(f['error']) for f in data['files'])
        self.import_note.setText(f"识别 {len(loaded['stores'])} 家门店；一致重复已去重 {data['duplicates']} 行；冲突 {conflicts} 组；失败文件 {errors} 个。未处理冲突的门店不能计算，其他门店可继续。")
        self.states={s:{'name':n,'state':'未运行','error':''} for s,n in loaded['stores'].items()};self.populate_stores();self.refresh_tables()
        try:
            if data['products']:priority_list.atomic_json(self.repository.folder/'workbench.json',{'products':data['products']})
        except Exception as exc:self.status.setText('读取完成；资料路径未保存：'+str(exc));return
        self.status.setText('检查完成。处理冲突后，进入门店与预算设置。');self.update_controls()
    def populate_stores(self):
        self.initializing=True;self.store_widgets={};self.store_table.setRowCount(len(self.loaded['stores']))
        try:budgets=self.repository.budgets()
        except Exception as exc:budgets={};self.status.setText('历史预算无效，请重新填写：'+str(exc))
        try:routes=self.catalog_repository.routes()
        except Exception as exc:routes={};self.status.setText('主从仓设置未载入，请重新选择：'+str(exc))
        counts=Counter(d['B'] for d,m in self.loaded['normalized']);local=Counter(d['B'] for d,m in self.loaded['normalized'] if d['_source']=='地采商品')
        for i,(s,name) in enumerate(self.loaded['stores'].items()):
            blocked=self.loaded['blocked'].get(s,0);check=QCheckBox();check.setChecked(not blocked);check.setEnabled(not blocked);check.toggled.connect(self.selection_changed);self.store_table.setCellWidget(i,0,check)
            self.store_table.setItem(i,1,QTableWidgetItem(name+'\n'+s));self.store_table.setItem(i,2,QTableWidgetItem(f'{blocked} 组冲突待处理' if blocked else f'{counts[s]} 行 · 地采 {local[s]}'))
            mode=QComboBox();mode.addItems(['请选择预算方式','不限预算','限额补货']);amount=QLineEdit();amount.setPlaceholderText('限额时必填，0表示不补')
            saved=budgets.get(s)
            if saved:mode.setCurrentIndex(2 if saved['enabled'] else 1);amount.setText(f"{saved['amount']:.2f}")
            amount.setEnabled(mode.currentIndex()==2)
            self.store_table.setCellWidget(i,5,mode);self.store_table.setCellWidget(i,6,amount)
            p=button('默认参数',lambda checked=False,code=s:self.edit_parameters([code]));self.store_table.setCellWidget(i,7,p)
            self.store_table.setItem(i,8,QTableWidgetItem('沿用上次预算' if saved else '请选择预算方式'))
            self.store_widgets[s]={'row':i,'check':check,'mode':mode,'amount':amount,'params':p,'saved':bool(saved)}
            self.install_route_widgets(s,i,routes)
            mode.currentIndexChanged.connect(lambda value,code=s:self.budget_changed(code));amount.textChanged.connect(lambda value,code=s:self.budget_changed(code))
        self.initializing=False;self.selection_changed()
    def selected_store(self):return self.current_store
    def selected_codes(self):return [s for s,w in self.store_widgets.items() if w['check'].isChecked()]
    def selected_settings(self,s):
        w=self.store_widgets[s];mode=w['mode'].currentIndex()
        if mode==0:raise ValueError('请选择预算方式')
        budget=None
        if mode==2:
            try:
                raw=w['amount'].text().strip()
                if not raw:raise ValueError('请填写本次预算')
                value=Decimal(raw)
                if not value.is_finite() or value<0 or value>1000000000 or value!=value.quantize(Decimal('.01')):raise ValueError('预算须为0至10亿元，最多两位小数')
                budget=float(value)
            except InvalidOperation:raise ValueError('请输入有效预算金额')
        warehouses=self.overrides.get(s,self.default_settings(s))
        if any(d<0 or l<0 or d+l<=0 for d,l in warehouses.values()):raise ValueError('请填写未知仓的备货和到货天数')
        if budget is not None and not self.profile:raise ValueError('限额补货需要有效必卖清单')
        result={'warehouses':warehouses,'exception':self.exception.isChecked(),'budget':budget}
        if self.loaded.get('catalogs'):result['supply_route']=self.route_for(s)
        return result
    def default_settings(self,s):
        return self.default_by_store.get(s,{})
    def budget_changed(self,s):
        if self.initializing:return
        w=self.store_widgets[s];w['amount'].setEnabled(w['mode'].currentIndex()==2);w['saved']=False;self.invalidate([s]);self.selection_changed()
    def selection_changed(self,*args):
        if self.initializing:return
        selected=self.selected_codes();total=0;limited=unlimited=invalid=0
        for s,w in self.store_widgets.items():
            try:
                settings=self.selected_settings(s);b=settings['budget'];message='本店本次不安排补货' if b==0 else '沿用上次预算' if w['saved'] else '设置就绪'
                if s in selected:
                    if b is None:unlimited+=1
                    else:limited+=1;total+=b
            except ValueError as exc:
                message=str(exc)
                if s in selected:invalid+=1
            self.store_table.item(w['row'],8).setText(message)
        self.selection_note.setText(f'已选 {len(selected)} 家｜限额 {limited} 家，预算合计 ¥{total:,.2f}｜不限预算 {unlimited} 家｜需补设置 {invalid} 家\n预算合计仅统计限额门店；未选门店不参与本次计算。')
        self.ready=bool(selected) and not invalid and not self.input_dirty
        self.calculate_btn.setText(f'开始计算 {len(selected)} 家门店');self.update_controls()
    def select_all(self,on):
        for w in self.store_widgets.values():
            if w['check'].isEnabled():w['check'].setChecked(on)
    def filter_stores(self,*args):
        text=self.store_search.text().strip().lower()
        for s,w in self.store_widgets.items():
            invalid=bool(self.loaded['blocked'].get(s))
            try:self.selected_settings(s)
            except ValueError:invalid=True
            self.store_table.setRowHidden(w['row'],text not in (s+' '+self.loaded['stores'][s]).lower() or (self.only_issues.isChecked() and not invalid))
    def bulk_budget(self):
        codes=self.selected_codes()
        if not codes:QMessageBox.information(self,'先选择门店','请先勾选需要设置的门店。');return
        dialog=QDialog(self);dialog.setWindowTitle('批量设置每店预算');v=QVBoxLayout(dialog);v.addWidget(label(f'将选中的 {len(codes)} 家门店分别设置为以下预算；不是共用一笔钱。'))
        mode=QComboBox();mode.addItems(['限额补货','不限预算']);v.addWidget(mode);value=QDoubleSpinBox();value.setRange(0,1000000000);value.setDecimals(2);value.setSuffix(' 元 / 店');v.addWidget(value);mode.currentIndexChanged.connect(lambda i:value.setEnabled(i==0))
        v.addWidget(button('应用到所选门店',dialog.accept,True));v.addWidget(button('取消',dialog.reject))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            for s in codes:
                w=self.store_widgets[s];w['mode'].setCurrentIndex(2 if mode.currentIndex()==0 else 1);w['amount'].setText(f'{value.value():.2f}')
    def bulk_parameters(self):self.edit_parameters(self.selected_codes())
    def edit_parameters(self,codes):
        if not codes:return
        dialog=QDialog(self);dialog.setWindowTitle('设置门店仓参数');dialog.resize(600,380);v=QVBoxLayout(dialog);v.addWidget(label(f'作用于 {len(codes)} 家门店；原模型其他参数保持不变。'))
        settings={}
        for s in codes:settings.update(self.overrides.get(s,self.default_settings(s)))
        table=QTableWidget(len(settings),3);table.setHorizontalHeaderLabels(['仓','备货天数','到货天数']);table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);v.addWidget(table)
        for i,(wh,values) in enumerate(settings.items()):
            table.setItem(i,0,QTableWidgetItem(wh))
            for j,value in enumerate(values,1):
                spin=QDoubleSpinBox();spin.setRange(-1,365);spin.setDecimals(1);spin.setSpecialValueText('未设置');spin.setValue(value);table.setCellWidget(i,j,spin)
        row=QHBoxLayout();row.addWidget(button('保存',dialog.accept,True));row.addWidget(button('取消',dialog.reject));v.addLayout(row)
        def reset():
            for s in codes:self.overrides.pop(s,None);self.store_widgets[s]['params'].setText('默认参数')
            self.invalidate(codes);self.selection_changed();dialog.reject()
        v.addWidget(button('所选门店恢复默认参数',reset))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            values={table.item(i,0).text():(table.cellWidget(i,1).value(),table.cellWidget(i,2).value()) for i in range(table.rowCount())}
            for s in codes:self.overrides[s]={w:values[w] for w in self.default_settings(s)};self.store_widgets[s]['params'].setText('自定义参数')
            self.invalidate(codes);self.selection_changed()
    def invalidate(self,codes):
        for s in codes:
            if s in self.results:self.states[s].update(state='待重算',error='设置或输入变化，上次结果已过期')
        if self.current_store in codes:self.dirty=True;self.result_caption.setText('上次结果已过期，请重新计算后导出')
        self.refresh_tables();self.update_controls()
    def mark_dirty(self,*args):
        if self.initializing:return
        self.invalidate(list(self.states));self.selection_changed()
    def priority_imported(self,profile):
        try:self.repository.save(profile)
        except Exception as exc:self.failed('清单保存失败，原清单保留：'+str(exc));return
        self.profile=profile;self.refresh_profile_label();self.mark_dirty();self.status.setText('必卖清单已保存；已有结果需重新计算。')
    def resolve_conflicts(self):
        if not self.loaded:return
        data=copy.deepcopy(self.loaded['intake']);conflicts=data['conflicts']
        if not conflicts:return
        dialog=QDialog(self);dialog.setWindowTitle('处理重复冲突 · 选择保留来源');dialog.resize(1060,650);v=QVBoxLayout(dialog);v.addWidget(label('同店同商品内容不同，不叠加。查看字段差异后选择保留哪条；未选择的门店暂不能计算。','muted'))
        table=QTableWidget(len(conflicts),3);table.setHorizontalHeaderLabels(['门店 / 商品','差异及来源','保留记录']);table.setColumnWidth(0,200);table.setColumnWidth(1,560);table.setColumnWidth(2,250);table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers);v.addWidget(table,1);combos=[]
        for i,c in enumerate(conflicts):
            table.setItem(i,0,QTableWidgetItem(c['store']+'\n'+c['sku']));variants=c['variants'];fields=set().union(*(r['values'] for r in variants));different=[k for k in sorted(fields) if len({str(r['values'].get(k)) for r in variants})>1]
            lines=[];combo=QComboBox();combo.addItem('暂不处理',None)
            for n,r in enumerate(variants):
                source=f"{n+1}. {Path(r['path']).name} 第{r['row']}行";combo.addItem(source,n);lines.append(source+'：'+ '；'.join(k+'='+str(r['values'].get(k)) for k in different))
            table.setItem(i,1,QTableWidgetItem('\n'.join(lines)));table.item(i,1).setToolTip('\n'.join(lines));combo.setCurrentIndex(c['choice']+1 if c['choice'] is not None else 0);table.setCellWidget(i,2,combo);table.setRowHeight(i,100);combos.append(combo)
        v.addWidget(button('应用选择并重新检查',dialog.accept,True));v.addWidget(button('取消',dialog.reject))
        if dialog.exec()==QDialog.DialogCode.Accepted:
            for c,combo in zip(conflicts,combos):c['choice']=combo.currentData()
            self.job(batch_service.prepare,(data,),self.loaded_ok)
    def start_calculation(self,codes=None):
        if self.worker or self.input_dirty or not self.loaded:return
        codes=self.selected_codes() if codes is None or isinstance(codes,bool) else codes
        try:jobs={s:self.selected_settings(s) for s in codes}
        except ValueError as exc:self.failed(str(exc));return
        if not jobs:return
        self.run_codes=list(jobs)
        for s,settings in jobs.items():
            self.states[s].update(state='排队中',error='');self.results.pop(s,None)
            if s==self.current_store:self.dirty=True;self.result=None
            try:
                if not getattr(self,'explicit_defaults',False):
                    self.repository.save_budget(s,settings['budget'] is not None,settings['budget'] or 0)
                    if settings.get('supply_route'):self.catalog_repository.save_route(s,settings['supply_route'])
            except Exception as exc:self.states[s]['error']='本次设置未能保存：'+str(exc)
        worker=BatchWorker(self.loaded,jobs,self.profile,self.concurrency.value(),self);self.worker=worker
        worker.changed.connect(self.task_changed);worker.failed.connect(self.batch_failed);worker.finished.connect(self.job_finished)
        self.pages.setCurrentIndex(2);self.refresh_tables();self.update_controls();worker.start()
    def task_changed(self,store,state,result,error):
        self.states[store].update(state=state,error=error)
        if result:self.results[store]=result
        self.refresh_tables();self.update_controls()
    def batch_failed(self,error):
        for s,state in self.states.items():
            if state['state'] in {'排队中','计算中'}:state.update(state='计算失败',error=error)
        self.status.setText('批次遇到问题，已完成结果保留：'+error);self.refresh_tables()
    def retry_failed(self):self.start_calculation([s for s,state in self.states.items() if state['state']=='计算失败'])
    def cancel_work(self):
        if self.worker:
            self.worker.cancel.set();self.status.setText('已停止启动后续任务，正在计算的门店完成后结束。' if isinstance(self.worker,BatchWorker) else '正在取消文件操作…')
    def job_finished(self):
        batch=isinstance(self.worker,BatchWorker);BackgroundJobs.job_finished(self)
        if batch:self.status.setText('本轮任务结束。可查看结果，或调整设置后只重算需要的门店。')
        self.refresh_tables()
    def refresh_tables(self):
        if not hasattr(self,'overview'):return
        counts=Counter(self.states[s]['state'] for s in self.run_codes);task_total=len(self.run_codes)
        self.run_note.setText(f"本轮完成 {counts['已完成']} / {task_total} 家 · 计算中 {counts['计算中']} · 排队 {counts['排队中']} · 失败 {counts['计算失败']} · 停止 {counts['已停止']}")
        self.run_progress.setRange(0,max(task_total,1));self.run_progress.setValue(sum(counts[k] for k in ['已完成','计算失败','已停止']))
        keys=list(self.states);finished=sum(s['state']=='已完成' for s in self.states.values());total=len(keys);self.task_table.setRowCount(task_total)
        for i,s in enumerate(self.run_codes):
            state=self.states[s];r=self.results.get(s) if state['state']=='已完成' else None
            for j,val in enumerate([state['name'],state['state'],f"¥{r['summary']['amount']:,.2f}" if r else '—',state['error']]):self.task_table.setItem(i,j,QTableWidgetItem(val))
        self.overview_codes=sorted(keys,key=lambda s:(0 if self.states[s]['state'] in {'计算失败','待重算'} else 1 if self.results.get(s,{}).get('summary',{}).get('holding_gaps',0) else 2,s));self.overview.setRowCount(len(keys))
        totals=[0,0,0];unpriced=0
        for i,s in enumerate(self.overview_codes):
            state=self.states[s];r=self.results.get(s) if state['state']=='已完成' else None
            if r:
                a=r['summary'];b=r['budget'];gap=round(a['candidate_amount']-a['amount'],2);totals=[totals[0]+a['candidate_amount'],totals[1]+a['amount'],totals[2]+gap];unpriced+=a['unpriced']
                values=[state['name'],state['state'],engine.fmt(b['limit']) if b['enabled'] else '不限预算',engine.fmt(a['candidate_amount']),engine.fmt(a['amount']),engine.fmt(a['must_amount']),engine.fmt(a['amount']-a['must_amount']),engine.fmt(gap),engine.fmt(b.get('remaining')),str(a['holding_gaps']),f"{a['local']} / {a['excluded']}",state['error'] or f"待核 {a['issues']}；补货未计价 {a['unpriced']}"]
            else:values=[state['name'],state['state']]+['—']*9+[state['error']]
            for j,val in enumerate(values):self.overview.setItem(i,j,QTableWidgetItem(val));self.overview.item(i,j).setToolTip(s+'\n'+str(val))
        for widget,value in zip(self.metrics,totals):widget.setText(f'{value:,.2f}' if finished else '—')
        self.overview_note.setText(f'有效完成 {finished} / {total} 家。'+('部分完成，未完成或过期门店不计入合计。' if finished<total else '全部门店结果就绪。')+f' 补货未计价 {unpriced} 款。')
    def open_overview(self,row,column):
        s=self.overview_codes[row]
        if s not in self.results:
            self.status.setText(self.states[s]['error'] or '该店尚无结果，请先计算。');return
        self.current_store=s;self.result=self.results[s];self.dirty=self.states[s]['state']!='已完成';self.set_rows(self.result['rows'],True)
        if self.dirty:self.result_caption.setText(self.result['store_name']+' · 上次结果已过期，请重算')
        modes={5:'必卖商品',7:'预算未满足',9:'最低持有缺口',10:'地采商品'}
        self.mode.setCurrentText(modes.get(column,'建议补货'));self.pages.setCurrentIndex(4);self.update_controls()
    def navigate(self,index):
        self.pages.setCurrentIndex(index)
        for i,b in enumerate(self.nav):b.setStyleSheet('font-weight:600;border:2px solid #176C63;' if i==index else '')
    def update_controls(self):
        if not hasattr(self,'cancel_btn'):return
        busy=self.worker is not None;batch=isinstance(self.worker,BatchWorker)
        self.import_page.setEnabled(not busy);self.store_page.setEnabled(not busy)
        products_ready=bool(self.products.path) if self.master_legacy.isChecked() else bool(self.catalogs)
        self.load_btn.setEnabled(not busy and bool(self.sales.paths) and products_ready);self.next_btn.setEnabled(not busy and self.loaded is not None and not self.input_dirty)
        self.conflict_btn.setEnabled(not busy and bool(self.loaded and self.loaded['intake']['conflicts']) and not self.input_dirty)
        self.calculate_btn.setEnabled(not busy and getattr(self,'ready',False) and not self.input_dirty)
        self.nav[1].setEnabled(self.loaded is not None and not self.input_dirty and not busy);self.nav[0].setEnabled(not busy)
        self.nav[2].setEnabled(bool(self.states));self.nav[3].setEnabled(bool(self.states))
        valid=any(state['state']=='已完成' for state in self.states.values());self.batch_export.setEnabled(valid and not busy)
        can_export=self.result is not None and not self.dirty and not busy;self.export_all.setEnabled(can_export);self.export_filtered.setEnabled(can_export)
        self.stop_btn.setEnabled(batch);self.retry_btn.setEnabled(not busy and not self.input_dirty and any(s['state']=='计算失败' for s in self.states.values()))
        self.cancel_btn.setVisible(busy);self.cancel_btn.setText('停止剩余任务' if batch else '取消');self.progress.setVisible(busy and not batch)
    def export_dialog(self):
        dialog=QDialog(self);dialog.setWindowTitle('导出本次补货结果');dialog.resize(660,520);v=QVBoxLayout(dialog);v.addWidget(label('勾选导出范围；未完成门店保留状态，只有有效结果生成补货明细。','muted'))
        checks={};scroll=QScrollArea();content=QWidget();cv=QVBoxLayout(content)
        for s,state in self.states.items():
            c=QCheckBox(f"{state['name']} · {s} · {state['state']}");c.setChecked(True);cv.addWidget(c);checks[s]=c
        scroll.setWidgetResizable(True);scroll.setWidget(content);v.addWidget(scroll,1)
        note=label('','muted');v.addWidget(note)
        def refresh():
            selected=[s for s,c in checks.items() if c.isChecked()];valid=[s for s in selected if self.states[s]['state']=='已完成'];amount=sum(self.results[s]['summary']['amount'] for s in valid)
            note.setText(f'选中 {len(selected)} 家，完成 {len(valid)} 家，未完成/待重算 {len(selected)-len(valid)} 家；安排金额 ¥{amount:,.2f}')
        for c in checks.values():c.toggled.connect(refresh)
        refresh();mode=QComboBox();mode.addItems(['汇总为一个 Excel','每店一个 Excel，同时生成汇总']);v.addWidget(mode);v.addWidget(button('选择保存位置',dialog.accept,True));v.addWidget(button('取消',dialog.reject))
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        states={s:copy.deepcopy(self.states[s]) for s,c in checks.items() if c.isChecked()};results={s:self.results[s] for s in states if s in self.results}
        if not any(st['state']=='已完成' for st in states.values()):self.failed('所选门店没有可导出的有效结果。');return
        stamp=datetime.now().strftime('%Y%m%d_%H%M%S');split=mode.currentIndex()==1
        if split:
            parent=QFileDialog.getExistingDirectory(self,'选择保存文件夹')
            if not parent:return
            path=str(Path(parent)/('补货结果_'+stamp))
        else:
            path,_=QFileDialog.getSaveFileName(self,'保存汇总结果','补货结果_'+stamp+'.xlsx','Excel (*.xlsx)',options=QFileDialog.Option.DontConfirmOverwrite)
            if not path:return
            if not path.lower().endswith('.xlsx'):path+='.xlsx'
        self.job(batch_service.export_batch,(results,states,path,split),self.export_ok)
