"""Reusable list maintenance. Shared picker/drop/template, explicit field mapping."""
import copy
from pathlib import Path
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QComboBox,QLineEdit,
    QTableWidget,QTableWidgetItem,QMessageBox,QCheckBox,QProgressBar,QWidget)
from ui_controls import button,label
from ui_tasks import Worker
import stocking_lists as sl
import preissue as rules
import work_repository as wr
import import_ui


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
    def __init__(self,host,apply=True):
        super().__init__(host);self.host=host;self.repository=sl.Repository(host.work_repository.path);self.current=None;self.selected=None;self.applying=apply
        self.setObjectName('stockingLibrary');self.setWindowTitle('铺货清单 · 保存复用 / 用于本次');self.resize(1050,750)
        v=QVBoxLayout(self);self.body=QWidget();v.addWidget(self.body,1);b=QVBoxLayout(self.body)
        top=QHBoxLayout();self.library=QComboBox();top.addWidget(self.library,1);top.addWidget(button('导入清单（支持拖拽）',self.import_files));top.addWidget(button('下载最低模板',lambda:import_ui.download(self,'stocking')));b.addLayout(top)
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
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['商品编码','商品名称','目标持有量（可编辑）','关注','来源行']);b.addWidget(self.table,1)
        actions=QHBoxLayout();actions.addWidget(button('保存为新清单',lambda:self.save(False)));actions.addWidget(button('保存当前清单新版本',lambda:self.save(True)))
        self.use_btn=button('用于本次所选门店',self.use,True);self.use_btn.setVisible(apply);actions.addWidget(self.use_btn);b.addLayout(actions)
        self.setup_async(v);v.addWidget(button('关闭 / 取消读取',self.reject));self.library.currentIndexChanged.connect(self.load);self.reload()
    def reload(self):
        self.values=self.repository.all();self.library.blockSignals(True);self.library.clear();self.library.addItem('选择已保存的清单；也可导入新文件',None)
        for d in self.values:self.library.addItem(f'{d["name"]} · V{d["version"]} · {len(d["items"])}款',d)
        self.library.blockSignals(False)
    def load(self):
        d=self.library.currentData()
        if d:self.show_value(d)
    def show_value(self,d):
        self.current=copy.deepcopy(d);self.name.setText(d['name']);self.kind.setCurrentIndex(self.kind.findData(d['kind']))
        defaults=d.get('defaults',{});self.strategy.setCurrentIndex(max(0,self.strategy.findData(defaults.get('strategy','minimum'))));self.target.setText('' if defaults.get('target') is None else str(defaults['target']))
        for key,c in self.scopes.items():c.setChecked(key in defaults.get('scopes',['zero']))
        self.valid_from.setText(d.get('valid_from',''));self.valid_to.setText(d.get('valid_to',''));self.table.setRowCount(len(d['items']))
        for i,item in enumerate(d['items']):
            for j,value in enumerate([item['sku'],item.get('name',''),item.get('target'),item.get('focused',''),','.join(str(s['row']) for s in item.get('source_rows',[]))]):
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
    def value(self):
        if not self.current:raise ValueError('请先导入或选择清单')
        d=copy.deepcopy(self.current);d['name']=self.name.text().strip();d['kind']=self.kind.currentData()
        target=rules.scalar(rules.decimal(self.target.text(),'统一目标')) if self.target.text().strip() else None
        d['defaults']={'strategy':self.strategy.currentData(),'target':target,'scopes':[k for k,c in self.scopes.items() if c.isChecked()]}
        d.update(valid_from=self.valid_from.text().strip(),valid_to=self.valid_to.text().strip())
        for i,item in enumerate(d['items']):
            text=self.table.item(i,2).text().strip();item['target']=rules.scalar(rules.decimal(text,'商品目标')) if text else None
        d['revision']=sl.revision(d);return sl.validate(d)
    def save(self,existing):
        try:
            d=self.value();expected=self.current['revision'] if existing and self.current.get('version') else None
            if not existing:
                import uuid
                d['id']=uuid.uuid4().hex;d.pop('version',None);d.pop('saved',None);d['revision']=sl.revision(d)
            self.run(self.repository.save,(d,expected),self.saved)
        except Exception as exc:self.message.setText('未保存：'+str(exc))
    def saved(self,d):self.reload();self.show_value(d);self.message.setText(f'已保存 {d["name"]} V{d["version"]}，原文件已留存。')
    def use(self):
        try:
            d=self.value()
            if not d['defaults']['scopes']:raise ValueError('请至少选择一种参与库存状态')
            day=self.host.job_day
            if d.get('valid_from') and day<d['valid_from'] or d.get('valid_to') and day>d['valid_to']:
                if QMessageBox.question(self,'清单不在有效期','本次业务日期不在此清单有效期，仍明确用于本次？',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)!=QMessageBox.StandardButton.Yes:return
                d['outside_period_confirmed_for']=day
            if self.focused.isChecked():
                d['parent_revision']=d['revision'];d['items']=[i for i in d['items'] if str(i.get('focused','')).strip().lower() in sl.new_product_focus.TRUE]
            d['revision']=sl.revision(d);sl.validate(d)
            self.run(sl.retain,(self.repository.path,d),self.used)
        except Exception as exc:self.message.setText('未应用：'+str(exc))
    def used(self,d):self.selected=d;self.accept()
