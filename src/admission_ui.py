"""Business forms for store qualification and refusal; save only after a diff."""
import copy
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QComboBox,QLineEdit,QTableWidget,QTableWidgetItem,QMessageBox,QInputDialog,QTabWidget,QWidget,QCompleter)
from ui_controls import label,button
from interaction_ui import review_rows
from business_ui import run
import admission as service

def invalidate(host,codes):
    affected=[c for c in codes if c in host.store_widgets]
    if affected:host.invalidate(affected)
    host.status.setText('门店准入已保存；受影响门店需重新计算。历史签发、回传、付款、收货不变。')

def changes(before,after):
    from parameter_view import flatten
    from history_forms import LABELS
    a,b=dict(flatten(before)),dict(flatten(after));out=[]
    for key in dict.fromkeys([*a,*b]):
        if a.get(key)!=b.get(key):out.append([' / '.join(LABELS.get(k,k) for k in key.split(' / ')),a.get(key,'未记录'),b.get(key,'未记录')])
    return out

class Editor(QDialog):
    def __init__(self,parent,host,code):
        super().__init__(parent);self.host=host;self.code=code;self.setWindowTitle('门店准入 · '+code);self.resize(950,650)
        self.before=service.snapshot(host.work_repository.path,code)['record'];self.value=copy.deepcopy(self.before)
        v=QVBoxLayout(self);v.addWidget(label('维护本店二类医疗资质与拒收商品；保存后影响后续安排，两仓共用。','section'))
        tabs=QTabWidget();v.addWidget(tabs,1)
        qpage=QWidget();qform=QFormLayout(qpage);q=self.value.get('qualification',{})
        self.state=QComboBox();self.state.addItems(service.STATES);self.state.setCurrentText(q.get('state','待核验'));qform.addRow('资质状态',self.state)
        self.qfields={}
        for key,title in [('reviewer','核验人（已核验必填）'),('verified_on','核验日期（已核验必填）'),('expires','到期日（可选）'),('note','备注（可选）')]:
            w=QLineEdit(str(q.get(key) or ''));self.qfields[key]=w;qform.addRow(title,w)
            if key in ('verified_on','expires'):w.setPlaceholderText('YYYY-MM-DD')
        tabs.addTab(qpage,'二类医疗资质')
        page=QWidget();pv=QVBoxLayout(page);pv.addWidget(label('到期当天仍有效；不填到期日则持续到手动解除。解除不会自动给旧结果加量。','muted'))
        self.search=QLineEdit();self.search.setPlaceholderText('搜索现有拒收商品名称或编码');pv.addWidget(self.search)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['商品编码','商品名称','拒收原因','到期日','当前状态']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.table.setColumnWidth(0,190);self.table.setColumnWidth(2,250);pv.addWidget(self.table,1)
        self.search.textChanged.connect(self.filter);line=QHBoxLayout();line.addWidget(button('搜索并添加拒收商品',self.add));line.addWidget(button('解除选中商品拒收',self.release));pv.addLayout(line);tabs.addTab(page,'门店拒收清单');self.refresh()
        row=QHBoxLayout();row.addWidget(button('取消，不保存',self.reject));row.addWidget(button('核对并保存本店准入',self.save,True));v.addLayout(row)
    def refresh(self):
        self.skus=list(self.value.get('refusals',{}));self.table.setRowCount(len(self.skus))
        for i,sku in enumerate(self.skus):
            r=self.value['refusals'][sku];state='已解除' if not r.get('active',True) else '已到期' if r.get('expires') and r['expires']<service.today() else '拒收中'
            for j,x in enumerate([sku,r.get('name',''),r['reason'],r.get('expires') or '手动解除',state]):self.table.setItem(i,j,QTableWidgetItem(str(x)))
        self.filter()
    def filter(self):
        words=self.search.text().lower().split()
        for i in range(self.table.rowCount()):self.table.setRowHidden(i,any(x not in ' '.join(self.table.item(i,j).text() for j in range(5)).lower() for x in words))
    def add(self):
        from warehouse_catalog import Repository
        try:catalogs=Repository(self.host.work_repository.path.parent).catalogs()
        except Exception as exc:QMessageBox.warning(self,'未能读取商品',str(exc));return
        items={}
        for c in catalogs.values():
            for sku,r in c['items'].items():items.setdefault(sku,r.get('商品名称') or '')
        d=QDialog(self);d.setWindowTitle('添加本店拒收商品');f=QFormLayout(d);sku=QLineEdit();sku.setPlaceholderText('输入编码，或输入名称后选择完整商品')
        options={k+' · '+str(n):k for k,n in items.items()};completer=QCompleter(list(options),sku);completer.setFilterMode(Qt.MatchFlag.MatchContains);completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive);sku.setCompleter(completer)
        reason=QLineEdit();expires=QLineEdit();expires.setPlaceholderText('可空；YYYY-MM-DD');f.addRow('商品（必填）',sku);f.addRow('拒收原因（必填）',reason);f.addRow('到期日',expires);f.addRow(button('添加到待保存清单',d.accept),button('取消',d.reject))
        if d.exec()!=QDialog.DialogCode.Accepted:return
        code=options.get(sku.text(),sku.text().strip())
        try:
            from purchase_io import code as check_code
            check_code(code)
            r={'name':items.get(code,''),'reason':reason.text().strip(),'expires':service.date_value(expires.text()),'active':True}
            candidate=copy.deepcopy(self.value);candidate.setdefault('refusals',{})[code]=r;service.validate(candidate)
        except ValueError as exc:QMessageBox.warning(self,'未添加',str(exc));return
        self.value=candidate;self.refresh()
    def release(self):
        r=self.table.currentRow()
        if r<0:return
        self.value['refusals'][self.skus[r]]['active']=False;self.refresh()
    def save(self):
        try:
            self.value['qualification']={'state':self.state.currentText(),**{k:(service.date_value(w.text()) if k in ('expires','verified_on') else w.text().strip()) for k,w in self.qfields.items()}}
            service.validate(self.value);delta=changes(self.before,self.value)
            if not delta:self.accept();return
            if not review_rows(self,'核对本店准入变化',['项目','原值','新值'],delta,'只保存门店 '+self.code+'；受影响的本次结果需重算，历史事实保留。','保存本店准入'):return
            run(self,save_records,(self.host.work_repository.path,{self.code:self.value},{self.code:self.before}),'保存门店准入',cancellable=False)
            invalidate(self.host,[self.code]);self.accept()
        except Exception as exc:QMessageBox.warning(self,'未保存',str(exc))

def save_records(path,values,expected,progress=lambda _:None,cancel=None):
    from runtime_support import checkpoint
    checkpoint(cancel);progress('正在原子保存门店准入…');service.save(path,values,expected)

def open_editor(parent,host,code=None):
    if host.worker or host.readonly:QMessageBox.information(parent,'暂不能维护','请结束当前任务；历史记录只读。');return
    if not code:
        saved=host.work_repository.stores();codes=list(dict.fromkeys([*host.store_widgets,*saved]))
        if not codes:QMessageBox.information(parent,'尚无门店','请先导入门店补货文件或门店默认参数。');return
        choices=[(host.states.get(c,{}).get('name') or saved.get(c,{}).get('name') or c)+' · '+c for c in codes]
        value,ok=QInputDialog.getItem(parent,'选择门店','维护哪家门店的准入',choices,0,False)
        if not ok:return
        code=codes[choices.index(value)]
    Editor(parent,host,code).exec()

def read_import(path,kind,progress=lambda _:None,cancel=None):
    from openpyxl import load_workbook
    from store_parameter_import import digest,code_value,checkpoint
    from import_templates import TEMPLATES
    before=digest(path);wb=load_workbook(path,read_only=True,data_only=False);result=[];seen=set();required=set(TEMPLATES[kind][2])
    try:
        for ws in wb:
            ws.reset_dimensions();headers=None
            for rn,cells in enumerate(ws.iter_rows(),1):
                checkpoint(cancel)
                if headers is None:
                    if rn>20:break
                    h=[str(c.value or '').strip() for c in cells]
                    if not required<=set(h):continue
                    if len([s for s in h if s])!=len(set(s for s in h if s)):raise ValueError('重复表头：'+ws.title)
                    headers=h;continue
                if all(c.value in (None,'') for c in cells):continue
                source=f'{Path(path).name} / {ws.title} / 第{rn}行'
                try:
                    if any(c.data_type in ('f','e') for c in cells):raise ValueError('禁止公式或错误值')
                    raw={k:cells[i].value if i<len(cells) else None for i,k in enumerate(headers) if k}
                    store=code_value(cells[headers.index('门店编码')]);sku=code_value(cells[headers.index('商品编码')]) if kind=='store_refusals' else None
                    key=(store,sku)
                    if key in seen:raise ValueError('重复业务键，请合并后导入')
                    seen.add(key)
                    if sku:value={'name':str(raw.get('商品名称') or ''),'reason':str(raw.get('拒收原因') or '').strip(),'expires':service.date_value(raw.get('到期日')),'active':True}
                    else:value={'state':str(raw.get('资质状态') or '').strip(),'reviewer':str(raw.get('核验人') or '').strip(),'verified_on':service.date_value(raw.get('核验日期')),'expires':service.date_value(raw.get('到期日')),'note':str(raw.get('备注') or '')}
                    value['source']={'file':Path(path).name,'sha256':before,'sheet':ws.title,'row':rn,'original':{k:(v.isoformat() if hasattr(v,'isoformat') else v) for k,v in raw.items()}}
                    service.validate({'refusals':{sku:value}} if sku else {'qualification':value})
                    result.append({'store':store,'sku':sku,'value':value,'source':source})
                except Exception as exc:raise ValueError(source+'：'+str(exc)) from exc
    finally:wb.close()
    if before!=digest(path):raise ValueError('读取期间文件变化，请重新导入')
    if not result:raise ValueError('没有找到可导入的记录，请使用本功能模板')
    return {'path':path,'sha':before,'rows':result}

def import_records(parent,host,kind):
    from import_ui import ImportDialog
    from store_parameter_import import digest
    d=ImportDialog(parent,kind)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    try:
        data=run(parent,read_import,(d.paths[0],kind),'读取门店准入');old={};new={};rows=[]
        for item in data['rows']:
            s=item['store']
            if s not in old:old[s]=service.snapshot(host.work_repository.path,s)['record'];new[s]=copy.deepcopy(old[s])
            if item['sku']:new[s].setdefault('refusals',{})[item['sku']]=item['value']
            else:new[s]['qualification']=item['value']
        for s in new:rows.extend([[s,*r] for r in changes(old[s],new[s])])
        if not review_rows(parent,'核对准入导入',['门店','项目','原值','新值'],rows,'缺行不解除；仅更新所列记录，历史订单不变。','保存门店准入'):return
        if digest(data['path'])!=data['sha']:raise ValueError('预览后文件变化，请重新导入')
        run(parent,save_records,(host.work_repository.path,new,old),'保存门店准入',cancellable=False);invalidate(host,list(new))
    except Exception as exc:QMessageBox.warning(parent,'未导入',str(exc))
