"""Independent, optional shop-name editor. Never submits complete parameters."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,QLineEdit,QComboBox,QFileDialog,QMessageBox
from ui_controls import button,label
from business_ui import run
from runtime_support import Cancelled
import payment_shop_names as service
import payment_shop_names_io as xlsx

class Review(QDialog):
    def __init__(self,parent,plan):
        super().__init__(parent);self.setObjectName('shopNamesReview');self.setWindowTitle('核对店铺名称变更');self.resize(1000,470)
        v=QVBoxLayout(self);v.addWidget(label(f"本次修改 {len(plan['changes'])} 项；仅保存两仓店铺名称，其他参数和历史单据不变。"))
        table=QTableWidget(len(plan['changes']),5);table.setHorizontalHeaderLabels(['门店编码','应用门店名称','仓','原名称','新名称']);table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,c in enumerate(plan['changes']):
            for j,value in enumerate([c['store'],c['display_name'],c['warehouse'],(c['before'] or {}).get('name','') or '未配置',c['name'] or '明确清空']):table.setItem(i,j,QTableWidgetItem(value))
        for j,w in enumerate([150,220,90,250,250]):table.setColumnWidth(j,w)
        v.addWidget(table,1);row=QHBoxLayout();row.addWidget(button('返回，不保存',self.reject));b=button('确认保存名称',self.accept,True);b.setObjectName('confirmShopNames');row.addWidget(b);v.addLayout(row)

class ImportReview(QDialog):
    def __init__(self,parent,imported):
        super().__init__(parent);self.setWindowTitle('选择本次导入的店铺名称');self.setObjectName('shopNamesImport');self.resize(1080,520)
        v=QVBoxLayout(self);bad=sum(bool(r['error']) for r in imported['rows']);v.addWidget(label(f"读取 {len(imported['rows'])} 行，待处理 {bad} 行默认未勾选；仅保存选定有效行。空白名称保持，应用门店名称只核对。"))
        v.addWidget(label('\n'.join(imported['notices']),'muted'));self.table=QTableWidget(len(imported['rows']),5);self.table.setHorizontalHeaderLabels(['纳入','原表行 / 门店编码','应用门店名称','填写的名称','状态']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,r in enumerate(imported['rows']):
            item=QTableWidgetItem();item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Unchecked if r['error'] else Qt.CheckState.Checked);self.table.setItem(i,0,item)
            for j,value in enumerate([str(r['row'])+' / '+r['store'],r['name'],'；'.join(p['warehouse']+'：'+p['value'] for p in r['patches']) or '空白，保持原值','待处理：'+r['error'] if r['error'] else r['status']],1):
                item=QTableWidgetItem(value);item.setToolTip(value);self.table.setItem(i,j,item)
        for j,w in enumerate([50,180,220,330,260]):self.table.setColumnWidth(j,w)
        v.addWidget(self.table,1);row=QHBoxLayout();row.addWidget(button('取消，不导入',self.reject));b=button('预览选定行的变更',self.accept,True);b.setObjectName('previewNamesImport');row.addWidget(b);v.addLayout(row)
    def selected(self):return [i for i in range(self.table.rowCount()) if self.table.item(i,0).checkState()==Qt.CheckState.Checked]

class ShopNames(QDialog):
    def __init__(self,parent,host):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.data=None;self.codes=[];self.busy=False;self.filling=False;self.receipt=None
        self.setObjectName('paymentShopNames');self.setWindowTitle('聚水潭店铺名称维护');self.resize(1180,680)
        v=QVBoxLayout(self);v.addWidget(label('独立维护两仓店铺名称；可不配置，付款仍可识别或手选。','section'))
        v.addWidget(label('从对应仓付款表“店铺名称”列复制。两仓同名允许保存；有多张订单时仍需选单。仅更新明确修改的名称。','muted'))
        row=QHBoxLayout();self.search=QLineEdit();self.search.setObjectName('shopNameSearch');self.search.setPlaceholderText('搜索应用门店名称、编码或聚水潭店铺名称');self.search.textChanged.connect(self.filter);row.addWidget(self.search,1)
        row.addWidget(button('勾选当前筛选',lambda:self.check(True)));row.addWidget(button('清空勾选',lambda:self.check(False)));v.addLayout(row)
        self.table=QTableWidget(0,6);self.table.setObjectName('shopNamesTable');self.table.setHorizontalHeaderLabels(['选择','门店编码','应用门店名称','山东仓聚水潭店铺名称','河北仓聚水潭店铺名称','修改情况'])
        for j,w in enumerate([45,150,220,250,250,180]):self.table.setColumnWidth(j,w)
        self.table.cellChanged.connect(self.changed);v.addWidget(self.table,1)
        row=QHBoxLayout();b=button('导出全部名称 / 专用模板',lambda:self.export(False));b.setObjectName('exportAllShopNames');row.addWidget(b);row.addWidget(button('导出勾选门店',lambda:self.export(True)));b=button('导入专用名称表',self.import_file);b.setObjectName('importShopNames');row.addWidget(b);row.addWidget(button('刷新 / 还原未保存编辑',self.refresh));v.addLayout(row)
        row=QHBoxLayout();self.clear_scope=QComboBox();self.clear_scope.addItems(['山东仓','河北仓','两仓']);row.addWidget(label('明确清空所选门店：'));row.addWidget(self.clear_scope);row.addWidget(button('预览清空',self.clear_selected));row.addStretch();v.addLayout(row)
        self.note=label('正在读取本机门店与名称。','muted');self.note.setWordWrap(True);v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('关闭',self.reject));self.save_btn=button('预览并保存已编辑名称',self.save_edits,True);self.save_btn.setObjectName('saveShopNames');row.addWidget(self.save_btn);v.addLayout(row)
        self.refresh()
    def alive(self):return not getattr(self.host,'closing',False)
    def pending(self):
        if not self.data:return []
        return [{'store':code,'warehouse':wh,'value':self.table.item(i,j).text()} for i,code in enumerate(self.codes) for j,wh in enumerate(service.WAREHOUSES,3)
                if self.table.item(i,j).text()!=(self.data['records'].get(service.key(code,wh)) or {}).get('name','')]
    def refresh(self):
        if self.busy or not self.alive():return
        if self.pending() and QMessageBox.question(self,'还原未保存名称','放弃当前尚未保存的名称编辑并重新读取？')!=QMessageBox.StandardButton.Yes:return
        self.busy=True
        try:
            known={c:v.get('name',c) for c,v in getattr(self.host,'states',{}).items()}
            data=run(self,service.load,(self.path,known),'读取独立店铺名称')
            if not self.alive():return
            self.fill(data);self.note.setText('共 '+str(len(self.codes))+' 家；双击两仓名称编辑。空白保持原值，清空请使用“预览清空”。'+('\n'+'；'.join(data['warnings']) if data['warnings'] else ''))
        except Cancelled:self.note.setText('已取消读取，未修改名称。')
        except Exception as exc:self.note.setText(str(exc))
        finally:self.busy=False
    def fill(self,data):
        self.filling=True
        try:
            self.data=data;self.codes=sorted(data['names']);self.table.setRowCount(len(self.codes))
            for i,code in enumerate(self.codes):
                check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);check.setCheckState(Qt.CheckState.Unchecked);self.table.setItem(i,0,check)
                for j,value in enumerate([code,data['names'][code],*[(data['records'].get(service.key(code,w)) or {}).get('name','') for w in service.WAREHOUSES],'已保存'],1):
                    item=QTableWidgetItem(value);item.setToolTip(value)
                    if j not in (3,4):item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEditable)
                    self.table.setItem(i,j,item)
        finally:self.filling=False
        self.filter()
    def changed(self,i,j):
        if self.filling or j not in (3,4):return
        self.table.item(i,5).setText('已编辑，尚未保存');self.note.setText('名称尚未保存，请预览新旧差异后确认。空白不会清空已有名称。')
    def filter(self,*_):
        terms=self.search.text().split()
        for i in range(len(self.codes)):self.table.setRowHidden(i,not all(t in ' '.join(self.table.item(i,j).text() for j in (1,2,3,4)) for t in terms))
    def check(self,value):
        for i in range(len(self.codes)):
            if not value or not self.table.isRowHidden(i):self.table.item(i,0).setCheckState(Qt.CheckState.Checked if value else Qt.CheckState.Unchecked)
    def selected(self):return [c for i,c in enumerate(self.codes) if self.table.item(i,0).checkState()==Qt.CheckState.Checked]
    def confirm(self,plan):
        if not self.alive():return
        if not plan['changes']:self.note.setText('没有需要保存的名称变更；模板空白保持原值。');return
        d=Review(self,plan)
        try:
            if d.exec()!=QDialog.DialogCode.Accepted or not self.alive():return
        finally:d.deleteLater()
        self.receipt=run(self,service.save,(self.path,plan),'保存店铺名称（不修改其他参数）')
        if not self.alive():return
        try:data=run(self,service.load,(self.path,self.data['names']),'读回已保存名称')
        except Exception as exc:
            self.data=None;self.note.setText('名称已保存，但读回未完成：'+str(exc)+'；请点击刷新核对，不必重复提交。');return
        if self.alive():self.fill(data);self.note.setText(f"已保存 {self.receipt['changed']} 项名称；其他参数、签发和数量金额未改动。")
    def save_edits(self):
        if self.busy or not self.data or not self.alive():return
        self.busy=True
        try:self.confirm(service.prepare(self.data,self.pending()))
        except Cancelled:self.note.setText('已取消，未保存；编辑内容仍保留。')
        except Exception as exc:self.note.setText(str(exc)+'；编辑内容仍保留，可刷新后重核。')
        finally:self.busy=False
    def saved_first(self):
        if not self.data or not self.alive():return False
        if self.pending():self.note.setText('请先保存名称编辑，或点击“刷新 / 还原未保存编辑”，再进行导入、导出或清空。');return False
        return True
    def clear_selected(self):
        if self.busy or not self.saved_first():return
        codes=self.selected()
        if not codes:self.note.setText('请先勾选要明确清空的门店。');return
        warehouses=service.WAREHOUSES if self.clear_scope.currentText()=='两仓' else [self.clear_scope.currentText()]
        self.busy=True
        try:self.confirm(service.prepare(self.data,[{'store':c,'warehouse':w,'value':''} for c in codes for w in warehouses],clear=True,origin='explicit_clear'))
        except Cancelled:self.note.setText('已取消，原名称保留。')
        except Exception as exc:self.note.setText(str(exc))
        finally:self.busy=False
    def export(self,selected):
        if self.busy or not self.saved_first():return
        codes=self.selected() if selected else None
        if selected and not codes:self.note.setText('请先勾选要导出的门店。');return
        path,_=QFileDialog.getSaveFileName(self,'导出专用店铺名称表（请选择新文件）','聚水潭店铺名称维护.xlsx','Excel (*.xlsx)')
        if not path:return
        self.busy=True
        try:
            file=run(self,xlsx.export,(path,self.data,codes),'导出独立店铺名称表')
            if self.alive():self.note.setText('已导出：'+file+'；仅含两仓名称，不包含补货参数。')
        except Cancelled:self.note.setText('已取消导出。')
        except Exception as exc:self.note.setText(str(exc))
        finally:self.busy=False
    def import_file(self):
        if self.busy or not self.saved_first():return
        path,_=QFileDialog.getOpenFileName(self,'读取专用店铺名称表','','Excel (*.xlsx)')
        if not path:return
        self.import_path(path)
    def import_path(self,path):
        if self.busy or not self.saved_first():return
        self.busy=True
        try:
            imported=run(self,xlsx.read,(path,self.data),'读取专用名称表（暂不保存）')
            if not self.alive():return
            d=ImportReview(self,imported)
            try:
                if d.exec()!=QDialog.DialogCode.Accepted or not self.alive():return
                selected=d.selected()
            finally:d.deleteLater()
            plan=run(self,xlsx.prepare,(self.data,imported,selected),'核对选定名称变更');self.confirm(plan)
        except Cancelled:self.note.setText('已取消，原名称保留。')
        except Exception as exc:self.note.setText(str(exc))
        finally:self.busy=False
    def reject(self):
        if self.busy:return
        if self.pending() and self.alive() and QMessageBox.question(self,'名称尚未保存','放弃未保存的名称编辑并关闭？')!=QMessageBox.StandardButton.Yes:return
        super().reject()
    def closeEvent(self,event):
        if self.busy:event.ignore();return
        super().closeEvent(event)

def open_names(parent,host):
    if getattr(host,'closing',False):return
    d=ShopNames(parent,host)
    try:
        if not getattr(host,'closing',False):d.exec()
    finally:d.deleteLater()
