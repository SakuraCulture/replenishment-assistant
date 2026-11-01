"""Reusable list maintenance. Shared picker/drop/template, explicit field mapping."""
import copy
from pathlib import Path
from PySide6.QtCore import Qt,QTimer,QAbstractTableModel,QSortFilterProxyModel,QModelIndex
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QComboBox,QLineEdit,
    QTableWidget,QTableWidgetItem,QMessageBox,QCheckBox,QProgressBar,QWidget,QTableView,QAbstractItemView)
from ui_controls import button,label
from ui_tasks import Worker
import stocking_lists as sl
import preissue as rules
import work_repository as wr
import import_ui
import stocking_selection as selection


class AsyncDialog(QDialog):
    def setup_async(self,layout):
        self.task=None;self.close_pending=False;self.accept_pending=False;self.queued=None;self.message=label('','muted');layout.addWidget(self.message)
        self.bar=QProgressBar();self.bar.hide();layout.addWidget(self.bar)
    def run(self,fn,args,callback):
        if self.task:self.queued=(fn,args,callback);return
        task=Worker(fn,args,self);self.task=task;self.bar.show();self.bar.setRange(0,0);self.body.setEnabled(False)
        task.status.connect(self.message.setText)
        task.row_progress.connect(lambda n,total:(self.bar.setRange(0,total or 0),self.bar.setValue(n)))
        task.success.connect(lambda value:callback(value) if not self.close_pending and not task.cancel.is_set() else None)
        task.failed.connect(lambda text:self.message.setText('未完成：'+text))
        task.cancelled.connect(lambda:self.message.setText('已取消，原记录保留'))
        def finish():
            self.task=None;self.body.setEnabled(True);self.bar.hide();task.deleteLater()
            if self.close_pending:super(AsyncDialog,self).reject()
            elif self.accept_pending:super(AsyncDialog,self).accept()
            elif self.queued:
                fn,args,callback=self.queued;self.queued=None;QTimer.singleShot(0,lambda:self.run(fn,args,callback))
        task.finished.connect(finish);task.start()
    def reject(self):
        if self.task:
            self.close_pending=True;self.task.cancel.set();self.message.setText('正在取消，等待当前安全步骤结束…');return
        super().reject()
    def accept(self):
        if self.task:self.accept_pending=True;return
        super().accept()
    def closeEvent(self,event):
        if self.task:self.reject();event.ignore()
        else:super().closeEvent(event)


class MappingDialog(QDialog):
    def __init__(self,parent,infos):
        super().__init__(parent);self.setWindowTitle('铺货清单 · 确认工作表和字段');self.resize(1000,390);self.infos=infos;self.widgets=[]
        v=QVBoxLayout(self);v.addWidget(label('按表头匹配，可换列顺序。商品编码必填，其余可不选；重复表头必须先处理。','muted'))
        self.table=QTableWidget(len(infos),7);self.table.setHorizontalHeaderLabels(['文件','工作表','商品编码（必填）','名称（选填）','目标（选填）','备注（选填）','关注（选填）']);v.addWidget(self.table)
        for i,info in enumerate(infos):
            self.table.setItem(i,0,QTableWidgetItem(Path(info['path']).name));sheet=QComboBox();sheet.addItems(info['sheets']);self.table.setCellWidget(i,1,sheet)
            combos=[QComboBox() for _ in range(5)];self.widgets.append((sheet,combos))
            for col,combo in enumerate(combos,2):self.table.setCellWidget(i,col,combo)
            sheet.currentTextChanged.connect(lambda _,row=i:self.refresh(row));self.refresh(i)
        row=QHBoxLayout();row.addWidget(button('按此映射读取',self.accept,True));row.addWidget(button('取消',self.reject));v.addLayout(row)
    def refresh(self,i):
        sheet,combos=self.widgets[i];headers=self.infos[i]['sheets'][sheet.currentText()]
        for combo,name in zip(combos,['商品编码','商品名称','目标持有量','备注','关注']):
            combo.clear();combo.addItem('不使用此列','');
            for h in headers:combo.addItem(h,h)
            combo.setCurrentIndex(max(0,combo.findData(name)))
    def specs(self):
        return [{**info,'sheet':sheet.currentText(),'mapping':{k:c.currentData() for k,c in zip(['sku','name','target','note','focused'],combos)}} for info,(sheet,combos) in zip(self.infos,self.widgets)]


class StockingDialog(AsyncDialog):
    def __init__(self,host,apply=True,codes=None):
        super().__init__(host);self.host=host;self.repository=sl.Repository(host.work_repository.path);self.current=None;self.selected=None;self.applying=apply
        self.codes=codes;self.saved_revision=None
        self.setObjectName('stockingLibrary');self.setWindowTitle('铺货清单 · 保存复用 / 用于本次');self.resize(1050,750)
        v=QVBoxLayout(self);self.body=QWidget();v.addWidget(self.body,1);b=QVBoxLayout(self.body)
        top=QHBoxLayout();self.library=QComboBox();top.addWidget(self.library,1);self.search_btn=button('搜索选商品',self.search_products,True);top.addWidget(self.search_btn);top.addWidget(button('导入清单（支持拖拽）',self.import_files));top.addWidget(button('下载最低模板',lambda:import_ui.download(self,'stocking')));b.addLayout(top)
        form=QFormLayout();self.name=QLineEdit();form.addRow('清单名称',self.name);self.kind=QComboBox()
        for value,text in [('new','新品铺货'),('plan','计划补货'),('promotion','活动备货')]:self.kind.addItem(text,value)
        form.addRow('清单用途',self.kind);self.strategy=QComboBox();self.strategy.addItem('有货即可：双零补1个出库单位','minimum');self.strategy.addItem('补足目标持有量','target');form.addRow('补足策略',self.strategy)
        self.target=QLineEdit();self.target.setPlaceholderText('库存单位；商品行已有目标时优先使用行目标');form.addRow('统一目标持有量',self.target)
        dates=QHBoxLayout();self.valid_from=QLineEdit();self.valid_to=QLineEdit()
        for w in (self.valid_from,self.valid_to):w.setPlaceholderText('选填 YYYY-MM-DD')
        dates.addWidget(self.valid_from);dates.addWidget(label('至'));dates.addWidget(self.valid_to);form.addRow('使用日期范围',dates);b.addLayout(form)
        opts=QHBoxLayout();self.scopes={}
        for key,title in [('zero','可用/在途双零'),('inbound','有在途无可用'),('available','已有可用')]:
            c=QCheckBox(title);c.setChecked(key=='zero');self.scopes[key]=c;opts.addWidget(c)
        b.addLayout(opts);self.focused=QCheckBox('本次仅使用关注行（不改变必卖优先级）');b.addWidget(self.focused)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['商品编码','商品名称','目标持有量（可编辑）','关注','选品来源']);self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows);b.addWidget(self.table,1)
        b.addWidget(button('移除选中商品',self.remove_products))
        actions=QHBoxLayout();actions.addWidget(button('保存为新清单',lambda:self.save(False)));actions.addWidget(button('保存当前清单新版本',lambda:self.save(True)))
        self.use_btn=button('预览本次补足',self.use,True);self.use_btn.setVisible(apply);self.use_btn.setEnabled(bool(getattr(host,'results',{})) and not getattr(host,'input_dirty',True));self.use_btn.setToolTip('先完成门店计算；预览后仍需核对数量、预算和风险，才会应用。');actions.addWidget(self.use_btn);b.addLayout(actions)
        self.setup_async(v);v.addWidget(button('关闭 / 取消读取',self.reject));self.library.currentIndexChanged.connect(self.load);self.reload()
    def reload(self):
        self.values=self.repository.all();self.library.blockSignals(True);self.library.clear();self.library.addItem('使用已保存清单；也可搜索选品或导入Excel',None)
        for d in self.values:self.library.addItem(f'{d["name"]} · V{d["version"]} · {len(d["items"])}款',d)
        self.library.blockSignals(False)
    def load(self):
        d=self.library.currentData()
        if d:self.show_value(d)
    def show_value(self,d,keep_saved=False):
        if not keep_saved:self.saved_revision=d['revision'] if d.get('version') else None
        self.current=copy.deepcopy(d);self.name.setText(d['name']);self.kind.setCurrentIndex(self.kind.findData(d['kind']))
        defaults=d.get('defaults',{});self.strategy.setCurrentIndex(max(0,self.strategy.findData(defaults.get('strategy','minimum'))));self.target.setText('' if defaults.get('target') is None else str(defaults['target']))
        for key,c in self.scopes.items():c.setChecked(key in defaults.get('scopes',['zero']))
        self.valid_from.setText(d.get('valid_from',''));self.valid_to.setText(d.get('valid_to',''));self.table.setRowCount(len(d['items']))
        for i,item in enumerate(d['items']):
            origin='搜索选择：'+ ' / '.join(dict.fromkeys(str(s.get('仓库') or '混合资料') for s in item['selection_sources'])) if item.get('selection_sources') else '文件行 '+','.join(str(s['row']) for s in item.get('source_rows',[]))
            for j,value in enumerate([item['sku'],item.get('name',''),item.get('target'),'是' if sl.focused(item.get('focused')) else '否',origin]):
                cell=QTableWidgetItem('' if value is None else str(value))
                if j!=2:cell.setFlags(cell.flags()&~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(i,j,cell)
        self.table.resizeColumnsToContents();self.message.setText(f'已读取{len(d["items"])}款；保存清单不等于应用或签发。')
    def import_files(self):
        paths=import_ui.choose(self,'stocking','导入新品 / 计划 / 活动清单',True)
        if paths:self.run(sl.inspect,(paths,),self.mapped)
    def mapped(self,infos):
        d=MappingDialog(self,infos)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        specs=d.specs();name=Path(infos[0]['path']).stem
        QTimer.singleShot(0,lambda:self.run(sl.import_list,(specs,name,self.kind.currentData()),self.show_value))
    def value(self,validate=True):
        if not self.current and validate:raise ValueError('请先搜索选商品、导入文件或选择已保存清单')
        d=copy.deepcopy(self.current) if self.current else selection.empty('',self.kind.currentData())
        d['name']=self.name.text().strip() or (f'临时铺货清单 {self.host.job_day}' if not self.current else '');d['kind']=self.kind.currentData()
        target=rules.scalar(rules.decimal(self.target.text(),'统一目标')) if self.target.text().strip() else None
        d['defaults']={'strategy':self.strategy.currentData(),'target':target,'scopes':[k for k,c in self.scopes.items() if c.isChecked()]}
        d.update(valid_from=self.valid_from.text().strip(),valid_to=self.valid_to.text().strip())
        for i,item in enumerate(d['items']):
            text=self.table.item(i,2).text().strip();item['target']=rules.scalar(rules.decimal(text,'商品目标')) if text else None
        d['revision']=sl.revision(d);return sl.validate(d) if validate else d
    def search_products(self):
        try:draft=self.value(False)
        except ValueError as exc:self.message.setText('请先修正清单：'+str(exc));return
        records=[];seen=set()
        for result in getattr(self.host,'results',{}).values():
            for row in result['rows']:
                if self.codes is None or row.get('B') in self.codes:
                    records.append(row);seen.add((row.get('B'),row.get('E')))
        for row,meta in (getattr(self.host,'loaded',None) or {}).get('normalized',[]):
            if (self.codes is None or row.get('B') in self.codes) and (row.get('B'),row.get('E')) not in seen:
                records.append({**row,'_meta':meta})
        picker=ProductPicker(self,getattr(self.host,'catalogs',{}),records,[i['sku'] for i in draft['items']],self.use_btn.isEnabled() and self.applying)
        if picker.exec()==QDialog.DialogCode.Accepted:
            value=selection.append_products(draft,picker.selected);added=len(value['items'])-len(draft['items'])
            self.show_value(value,keep_saved=True);self.message.setText(f'已加入{added}款，现有目标和商品顺序保留。可直接预览，或保存复用。')
            if picker.preview:self.use()
        picker.deleteLater()
    def remove_products(self):
        rows={i.row() for i in self.table.selectionModel().selectedRows()}
        if not rows:self.message.setText('请先选中要从清单移除的商品行。');return
        try:d=self.value(False)
        except ValueError as exc:self.message.setText('请先修正清单：'+str(exc));return
        d['items']=[item for i,item in enumerate(d['items']) if i not in rows];d['revision']=sl.revision(d);self.show_value(d,keep_saved=True)
        self.message.setText(f'已从当前编辑清单移除{len(rows)}款；尚未保存或应用。')
    def save(self,existing):
        try:
            d=self.value();expected=self.saved_revision if existing and self.current.get('version') else None
            if not existing:
                import uuid
                d['id']=uuid.uuid4().hex;d.pop('version',None);d.pop('saved',None);d['revision']=sl.revision(d)
            self.run(self.repository.save,(d,expected),self.saved)
        except Exception as exc:self.message.setText('未保存：'+str(exc))
    def saved(self,d):self.reload();self.show_value(d);self.message.setText(f'已保存 {d["name"]} V{d["version"]}，选品依据已保留。'+('原文件已留存。' if d['sources'] else ''))
    def use(self):
        try:
            d=self.value()
            if not d['defaults']['scopes']:raise ValueError('请至少选择一种参与库存状态')
            day=self.host.job_day
            if d.get('valid_from') and day<d['valid_from'] or d.get('valid_to') and day>d['valid_to']:
                if QMessageBox.question(self,'清单不在有效期','本次业务日期不在此清单有效期，仍明确用于本次？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
                d['outside_period_confirmed_for']=day
            if self.focused.isChecked():
                d['parent_revision']=d['revision'];d['items']=[i for i in d['items'] if sl.focused(i.get('focused'))]
            d['revision']=sl.revision(d);sl.validate(d)
            self.run(sl.retain,(self.repository.path,d),self.used)
        except Exception as exc:self.message.setText('未应用：'+str(exc))
    def used(self,d):self.selected=d;self.accept()


class ProductModel(QAbstractTableModel):
    HEADERS=['选择','商品编码','商品名称','资料仓','分类','商品状态','本次门店记录']
    def __init__(self,items,existing,changed,parent=None):
        super().__init__(parent);self.items=items;self.positions={d['sku']:i for i,d in enumerate(items)};self.existing=set(existing);self.checked={};self.changed=changed
    def rowCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.items)
    def columnCount(self,parent=QModelIndex()):return 0 if parent.isValid() else len(self.HEADERS)
    def headerData(self,n,orientation,role=Qt.ItemDataRole.DisplayRole):
        if role==Qt.ItemDataRole.DisplayRole and orientation==Qt.Orientation.Horizontal:return self.HEADERS[n]
    def flags(self,index):
        flags=Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsSelectable
        if index.isValid() and index.column()==0 and self.items[index.row()]['sku'] not in self.existing:flags|=Qt.ItemFlag.ItemIsUserCheckable
        return flags
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        d=self.items[index.row()];col=index.column();sku=d['sku']
        if role==Qt.ItemDataRole.CheckStateRole and col==0 and sku not in self.existing:return Qt.CheckState.Checked if sku in self.checked else Qt.CheckState.Unchecked
        if role==Qt.ItemDataRole.ToolTipRole:return '\n'.join(f'{v["warehouse"]}：{v["category"]} / {v["status"]}' for v in d['variants'])+'\n选品不指定供货仓；应用时按各店主从仓和禁用/停采规则核对。'
        if role!=Qt.ItemDataRole.DisplayRole:return None
        return [('已在清单' if sku in self.existing else ''),sku,d['name'],
            ' / '.join(dict.fromkeys(v['warehouse'] for v in d['variants'])),
            ' / '.join(dict.fromkeys(v['category'] for v in d['variants'])),
            ' / '.join(f'{v["warehouse"]}：{v["status"]}' for v in d['variants']),
            f'{len(d["stores"])}家有记录' if d['stores'] else '缺门店记录，应用时待核'][col]
    def setData(self,index,value,role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column()!=0 or role!=Qt.ItemDataRole.CheckStateRole:return False
        d=self.items[index.row()];sku=d['sku']
        if sku in self.existing:return False
        if value in (Qt.CheckState.Checked,Qt.CheckState.Checked.value):self.checked[sku]=d
        else:self.checked.pop(sku,None)
        self.dataChanged.emit(index,index,[role]);self.changed();return True


class ProductFilter(QSortFilterProxyModel):
    def __init__(self,parent=None):super().__init__(parent);self.needle='';self.warehouse='';self.category='';self.only_selected=False
    def filterAcceptsRow(self,i,parent):
        model=self.sourceModel();d=model.items[i]
        return (not self.only_selected or d['sku'] in model.checked) and selection.matches(d,self.needle,self.warehouse,self.category)


class ProductPicker(AsyncDialog):
    def __init__(self,parent,catalogs,records,existing,can_preview):
        super().__init__(parent);self.setObjectName('stockingProductPicker');self.setWindowTitle('搜索选商品 · 加入铺货清单');self.resize(1250,760)
        self.selected=[];self.preview=False;self.model=None;self.existing=existing
        v=QVBoxLayout(self);self.body=QWidget();v.addWidget(self.body,1);b=QVBoxLayout(self.body)
        b.addWidget(label('搜索并勾选商品；两仓相同编码只选一次。供货仓、箱规和禁用/停采在补足预览中按门店核对。','muted'))
        filters=QHBoxLayout();self.search=QLineEdit();self.search.setPlaceholderText('搜索商品名称 / 商品编码；空格可组合关键词');self.warehouse=QComboBox();self.category=QComboBox();self.only_selected=QCheckBox('只看已勾选')
        self.warehouse.setMinimumWidth(150);self.category.setMinimumWidth(150)
        for w in (self.search,self.warehouse,self.category,self.only_selected):filters.addWidget(w,1 if w is self.search else 0)
        b.addLayout(filters);self.table=QTableView();self.table.setSortingEnabled(True);self.table.setAlternatingRowColors(True);self.table.verticalHeader().hide();b.addWidget(self.table,1)
        actions=QHBoxLayout();actions.addWidget(button('勾选当前筛选',self.check_visible));actions.addWidget(button('清空全部勾选',self.clear_checks));self.count=label('','muted');actions.addWidget(self.count,1);b.addLayout(actions)
        footer=QHBoxLayout();self.add_btn=button('加入清单',lambda:self.finish(False));self.preview_btn=button('选好并预览补足',lambda:self.finish(True),True);self.preview_btn.setVisible(can_preview)
        for btn in (self.add_btn,self.preview_btn):btn.setEnabled(False);btn.setAutoDefault(False);footer.addWidget(btn)
        b.addLayout(footer);self.setup_async(v);v.addWidget(button('取消选品',self.reject))
        self.search.textChanged.connect(self.filter);self.warehouse.currentIndexChanged.connect(self.filter);self.category.currentIndexChanged.connect(self.filter);self.only_selected.toggled.connect(self.filter)
        QTimer.singleShot(0,lambda:self.run(selection.products,(catalogs,records),self.ready))
    def ready(self,items):
        self.model=ProductModel(items,self.existing,self.changed,self);self.proxy=ProductFilter(self);self.proxy.setSourceModel(self.model);self.table.setModel(self.proxy)
        for combo,key,title in ((self.warehouse,'warehouse','全部资料仓'),(self.category,'category','全部分类')):
            combo.blockSignals(True);combo.addItem(title,'')
            for value in dict.fromkeys(v[key] for d in items for v in d['variants']):combo.addItem(value,value)
            combo.blockSignals(False)
        for i,width in enumerate((90,185,260,130,140,195,185)):self.table.setColumnWidth(i,width)
        self.search.setFocus();self.changed()
        if not items:self.message.setText('暂无可选商品，请先维护仓库商品资料，或检查已匹配的门店数据。')
    def filter(self,*_):
        if self.model is None:return
        self.proxy.needle=self.search.text().strip();self.proxy.warehouse=self.warehouse.currentData() or '';self.proxy.category=self.category.currentData() or '';self.proxy.only_selected=self.only_selected.isChecked();self.proxy.invalidateFilter();self.changed(False)
    def changed(self,refresh=True):
        if self.model is None:return
        if refresh:self.proxy.invalidateFilter()
        hidden=sum(not self.proxy.mapFromSource(self.model.index(self.model.positions[sku],0)).isValid() for sku in self.model.checked)
        self.count.setText(f'当前{self.proxy.rowCount()}款 · 已勾选{len(self.model.checked)}款（筛选外{hidden}款） · 清单已有{len(self.existing)}款')
        self.add_btn.setEnabled(bool(self.model.checked));self.preview_btn.setEnabled(bool(self.model.checked))
    def check_visible(self):
        if self.model is None:return
        for i in range(self.proxy.rowCount()):
            d=self.model.items[self.proxy.mapToSource(self.proxy.index(i,0)).row()]
            if d['sku'] not in self.model.existing:self.model.checked.setdefault(d['sku'],d)
        self.model.layoutChanged.emit();self.changed()
    def clear_checks(self):
        if self.model is None:return
        self.model.checked.clear();self.model.layoutChanged.emit();self.changed()
    def finish(self,preview):
        if self.task or self.model is None or not self.model.checked:return
        self.selected=list(self.model.checked.values());self.preview=preview;self.accept()
