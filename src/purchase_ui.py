"""Explicit purchase import, version review and allocation forms; no model writes."""
import copy
from contextlib import closing
from pathlib import Path
from decimal import Decimal
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QFormLayout,QComboBox,QLineEdit,QFileDialog,QMessageBox,QListWidget,QScrollArea,QWidget,QInputDialog,QAbstractItemView
from ui_controls import button,label
from business_ui import run,grid
from interaction_ui import confirm,review_rows
from runtime_support import Cancelled
from import_ui import ImportDialog
import purchase_io as io,purchases as service

def warn(parent,exc):
    if isinstance(exc,Cancelled):QMessageBox.information(parent,'已取消','已停止当前读取/准备，未提交本次采购修改。')
    else:QMessageBox.warning(parent,'未完成',str(exc))

def download_template(parent):
    p,_=QFileDialog.getSaveFileName(parent,'下载实际下单最低标准模板','实际下单明细模板.xlsx','Excel (*.xlsx)')
    if p:
        try:io.template(p if p.lower().endswith('.xlsx') else p+'.xlsx');QMessageBox.information(parent,'已下载','请按填写说明录入；模板中不包含示例订单。')
        except Exception as exc:warn(parent,exc)

class PurchaseFiles(ImportDialog):
    def __init__(self,parent):
        QDialog.__init__(self,parent);self.multiple=True;self.paths=[];self.extensions=('.xlsx',);self.setAcceptDrops(True);self.resize(780,380);self.setWindowTitle('导入实际下单资料 · 1/3 选择文件')
        v=QVBoxLayout(self);v.addWidget(label('拖入一份或多份 Excel。每个订单须提供完整本版明细，更新时不会当增量累加。','section'));self.list=QListWidget();v.addWidget(self.list)
        row=QHBoxLayout();row.addWidget(button('选择文件',self.choose));row.addWidget(button('下载最低标准模板',lambda:download_template(self)));v.addLayout(row)
        v.addWidget(label('精确使用门店编码、供货仓、采购单号及采购明细号。没有补货单号或修订时，由你明确选择关联。','muted'))
        row=QHBoxLayout();self.go=button('读取表头与样例',self.accept,True);self.go.setEnabled(False);row.addWidget(self.go);row.addWidget(button('取消',self.reject));v.addLayout(row)

class Mapping(QDialog):
    def __init__(self,parent,path,info):
        super().__init__(parent);self.path=path;self.info=info;self.setWindowTitle('导入实际下单资料 · 2/3 字段映射');self.resize(900,750);v=QVBoxLayout(self);v.addWidget(label(info['name'],'section'))
        top=QHBoxLayout();self.sheet=QComboBox();self.sheet.addItems([s['sheet'] for s in info['sheets']]);top.addWidget(label('工作表'));top.addWidget(self.sheet);self.profile=QComboBox();self.profile.addItem('选择已保存映射');self.profiles=service.mappings(path);self.profile.addItems(list(self.profiles));top.addWidget(self.profile);top.addWidget(button('应用所选映射',self.apply_profile));top.addWidget(button('另存映射方案',self.save_profile));v.addLayout(top)
        v.addWidget(label('带 * 为必填。相同表头按列序号区分；固定值仅在未映射源列时使用。','muted'))
        scroll=QScrollArea();scroll.setWidgetResizable(True);body=QWidget();form=QFormLayout(body);self.fields={};self.defaults={}
        for f in io.FIELDS:
            line=QHBoxLayout();c=QComboBox();line.addWidget(c,1);self.fields[f]=c
            if f in ('来源系统','供货仓','数量单位','币种'):
                e=QLineEdit();e.setPlaceholderText('可明确填写固定值');line.addWidget(e);self.defaults[f]=e
            form.addRow(f+(' *' if f in io.REQUIRED else ''),line)
        scroll.setWidget(body);v.addWidget(scroll,1);self.sample,self.sample_model=grid(self,[]);self.sample.setMaximumHeight(135);v.addWidget(self.sample)
        r=QHBoxLayout();r.addWidget(button('确认映射，读取明细',self.accept,True));r.addWidget(button('取消',self.reject));v.addLayout(r);self.sheet.currentIndexChanged.connect(self.populate);self.populate()
    def populate(self):
        s=self.info['sheets'][self.sheet.currentIndex()];self.headers=s['headers']
        for f,c in self.fields.items():
            c.clear();c.addItem('不读取',None)
            for i,h in enumerate(self.headers):c.addItem(f'{i+1}列 · {h if h is not None else "（空表头）"}',i)
            hits=[i for i,h in enumerate(self.headers) if h==f]
            if len(hits)==1:c.setCurrentIndex(hits[0]+1)
        self.sample_model.headers=[f'{i+1} · {h}' for i,h in enumerate(self.headers)];self.sample_model.replace([list(r)+[None]*max(0,len(self.headers)-len(r)) for r in s['sample']])
    def spec(self):return {**{k:self.info[k] for k in ('path','sha','name')},'sheet':self.sheet.currentText(),'headers':self.headers,'mapping':{f:c.currentData() for f,c in self.fields.items()},'defaults':{f:e.text().strip() for f,e in self.defaults.items() if e.text().strip()}}
    def apply_profile(self):
        p=self.profiles.get(self.profile.currentText())
        if not p:return
        if p['headers']!=self.headers:QMessageBox.warning(self,'表头不同','此方案的表头或列顺序不同，请重新映射；不会猜测套用。');return
        for f,c in self.fields.items():c.setCurrentIndex(c.findData(p['mapping'].get(f)))
        for f,e in self.defaults.items():e.setText(p.get('defaults',{}).get(f,''))
    def save_profile(self):
        name,ok=QInputDialog.getText(self,'另存映射方案','方案名称（不保存本次业务文件路径）')
        if not ok or not name.strip():return
        name=name.strip()
        if name in self.profiles and not confirm(self,'覆盖映射方案',f'将覆盖“{name}”的字段映射，是否确认？'):return
        try:
            s=self.spec();self.profiles=run(self,service.mappings,(self.path,name,{k:s[k] for k in ('headers','mapping','defaults')}),'保存字段映射方案',cancellable=False);self.profile.clear();self.profile.addItem('选择已保存映射');self.profile.addItems(list(self.profiles));self.profile.setCurrentText(name)
        except Exception as exc:warn(self,exc)

def head_text(h):return f"{h['day']} · {h.get('name',h['store'])} ({h['store']}) · {h['warehouse']} · {h['number']} · V{h['revision']:03d}"+('（历史修订）' if h['revision']!=h['current_revision'] else '')
def links_text(row):return '；'.join(l['head']['number']+f" V{l['head']['revision']:03d}：{l['quantity']}"+('（额外采购）' if l.get('extra') else '') for l in row['links']) or '未关联'

class LinkDialog(QDialog):
    def __init__(self,parent,row,heads):
        super().__init__(parent);self.row=row;self.links=copy.deepcopy(row['links']);self.heads=[h for h in heads if h['store']==row['store'] and h['warehouse']==row['warehouse']];self.setWindowTitle('分配实际下单数量 · 不改变签发');self.resize(1050,610);v=QVBoxLayout(self)
        v.addWidget(label(f"采购单 {row['number']} · 明细 {row['line_id']} · {row['sku']} {row['name']}\n实际 {row['quantity']} {row['unit']} · 折合库存单位 {row['base_quantity']}（可分摊到多张签发单）",'section'))
        self.target=QComboBox();self.target.addItem('请选择确切签发修订',None)
        for h in self.heads:self.target.addItem(head_text(h),h)
        v.addWidget(self.target);r=QHBoxLayout();self.quantity=QLineEdit(row['base_quantity'] or '');self.quantity.setPlaceholderText('本单关联数量，库存单位');r.addWidget(self.quantity);r.addWidget(button('加入此签发修订',self.add));v.addLayout(r)
        self.table,self.model=grid(self,['签发单 / 修订','分配数量','类型']);v.addWidget(self.table,1);v.addWidget(button('移除选中关联',self.remove));self.note=label('');v.addWidget(self.note)
        r=QHBoxLayout();r.addWidget(button('保存本行关联到预览',self.accept,True));r.addWidget(button('取消本行修改',self.reject));v.addLayout(r);self.refresh()
    def refresh(self):
        self.model.replace([[head_text(l['head']),l['quantity'],'额外采购' if l.get('extra') else '签发内商品'] for l in self.links]);total=sum((Decimal(l['quantity']) for l in self.links),Decimal(0));self.note.setText('已分摊 '+str(total)+'；未分摊 '+str(Decimal(self.row['base_quantity'] or 0)-total)+'。保存到预览后仍需最终确认提交。')
    def add(self):
        try:
            h=self.target.currentData()
            if not h:raise ValueError('请先选定签发修订')
            if self.row['base_quantity'] is None or self.row['status']=='已取消':raise ValueError('单位待核/已取消记录不能关联')
            if any((l['head']['oid'],l['head']['revision'],l['head']['warehouse'])==(h['oid'],h['revision'],h['warehouse']) for l in self.links):raise ValueError('此修订已在列表中；修改请先移除原关联')
            q=io.number(self.quantity.text());total=sum((Decimal(l['quantity']) for l in self.links),Decimal(0))+Decimal(q)
            if total>Decimal(self.row['base_quantity']):raise ValueError('分摊总量不能超过实际下单量')
            extra=self.row['sku'] not in h['products']
            if extra and not confirm(self,'确认额外采购','该商品不在此仓签发表内，是否明确记录为额外采购？不会增加原签发建议。'):return
            self.links.append(service.link(h,q,extra));self.refresh()
        except Exception as exc:warn(self,exc)
    def remove(self):
        n=self.table.currentIndex().row()
        if n>=0:self.links.pop(self.model.indices[n]);self.refresh()

def retain_links(scan,docs):
    return service.retain_links(scan,docs)

class PurchaseEditor(QDialog):
    def __init__(self,parent,path,scan,heads,docs,editing=None):
        super().__init__(parent);self.path=path;self.scan=copy.deepcopy(scan);self.heads=heads;self.docs=docs;self.editing=editing;self.saved=False;self.setWindowTitle('实际下单资料 · 3/3 关联核对');self.resize(1320,760);v=QVBoxLayout(self)
        v.addWidget(label('采购数量独立保存。每单完整新版本替换当前版本，旧版保留；不会重写签发或加盟商确认数。','section'))
        v.addWidget(label('选中商品行后设置关联；多选可一次关联到同一签发单。未关联量保持待核，不当作零；单位不明确时只暂存。','muted'));self.search=QLineEdit();self.search.setPlaceholderText('筛选采购单 / 门店 / 仓 / 商品 / 来源');v.addWidget(self.search)
        self.table,self.model=grid(self,['采购单','明细号','门店','仓','商品编码','商品名称','实际量','单位','折合库存量','实际金额','关联单 / 修订 / 数量','原始来源']);self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection);self.search.textChanged.connect(self.model.filter);v.addWidget(self.table,1)
        actions=QHBoxLayout();actions.addWidget(button('分摊选中一行',self.single_link));actions.addWidget(button('关联所选行至同一签发单',self.bulk_link));actions.addWidget(button('确认选中行单位换算',self.convert));actions.addWidget(button('确认选中行身份（无明细号更新）',self.identify));v.addLayout(actions);self.note=label('');v.addWidget(self.note)
        actions=QHBoxLayout();actions.addWidget(button('预览差异并确认保存',self.save,True));actions.addWidget(button('取消本次修改',self.reject));v.addLayout(actions);self.refresh()
    def indices(self):return [self.model.indices[i.row()] for i in sorted(self.table.selectionModel().selectedRows(),key=lambda i:i.row())]
    def one(self):
        ids=self.indices()
        if len(ids)!=1:raise ValueError('请只选中一行')
        return self.scan['rows'][ids[0]]
    def refresh(self):
        rows=self.scan['rows'];self.model.replace([[r['number'],r['line_id'],r['store'],r['warehouse'],r['sku'],r['name'],r['quantity'],r['unit'],r['base_quantity'] if r['base_quantity'] is not None else '单位待核',r['amount'] if r['amount'] is not None else '未提供',links_text(r),f"{r['source']['file']} / {r['source']['sheet']} / 第{r['source']['row']}行"] for r in rows]);self.model.filter(self.search.text());self.note.setText(f"共 {len(rows)} 行；未关联 {sum(not r['links'] for r in rows)} 行；单位待核 {sum(r['base_quantity'] is None for r in rows)} 行。下方保存前将完整显示新旧差异。")
    def single_link(self):
        try:
            r=self.one();d=LinkDialog(self,r,self.heads)
            if d.exec()==QDialog.DialogCode.Accepted:r['links']=d.links;self.refresh()
        except Exception as exc:warn(self,exc)
    def bulk_link(self):
        try:
            rows=[self.scan['rows'][i] for i in self.indices()]
            if not rows:raise ValueError('请先选中商品行')
            if len({(r['store'],r['warehouse']) for r in rows})!=1:raise ValueError('批量关联仅支持同门店同仓的所选行')
            heads=[h for h in self.heads if (h['store'],h['warehouse'])==(rows[0]['store'],rows[0]['warehouse']) and all(r['sku'] in h['products'] for r in rows)]
            if not heads:raise ValueError('没有包含全部所选商品的签发单；额外采购请逐行明确关联')
            choice,ok=QInputDialog.getItem(self,'选择签发修订','严格绑定下列修订；将替换所选行的当前预览关联。',[head_text(h) for h in heads],0,False)
            if not ok:return
            h=heads[[head_text(x) for x in heads].index(choice)]
            if any(r['base_quantity'] is None or r['status']=='已取消' for r in rows):raise ValueError('包含单位待核/已取消行，不能批量关联')
            if not confirm(self,'核对批量关联',head_text(h)+f'\n将 {len(rows)} 行各自的全部实际下单量关联至此修订。是否确认？'):return
            for r in rows:r['links']=[service.link(h,r['base_quantity'])]
            self.refresh()
        except Exception as exc:warn(self,exc)
    def convert(self):
        try:
            r=self.one()
            if r['unit']=='库存单位':raise ValueError('本行已明确按库存单位，无需换算')
            factor,ok=QInputDialog.getText(self,'确认单位换算',f"1 {r['unit']} 等于多少库存单位？不得按当前箱规猜测。",text=r['factor'] or '')
            if not ok:return
            factor=io.number(factor)
            if Decimal(factor)<=0:raise ValueError('换算系数必须大于0')
            basis,ok=QInputDialog.getText(self,'填写换算依据','例如采购单注明每箱24包；会随本次资料留存。',text=r['conversion_basis'])
            if not ok:return
            if not basis.strip():raise ValueError('换算依据不能为空')
            if r['links'] and not confirm(self,'重新核对关联','换算改变后会清空本行预览关联，请重新分配。是否继续？'):return
            r.update(factor=factor,conversion_basis=basis.strip(),base_quantity=str(Decimal(r['quantity'])*Decimal(factor)),links=[]);self.refresh()
        except Exception as exc:warn(self,exc)
    def identify(self):
        try:
            r=self.one()
            if r['external_line_id']:raise ValueError('本行已有外部明细号，按该精确编号识别')
            old=next((d for d in self.docs if d['docid']==service.docid(r)),None)
            candidates=old['payload']['rows'] if old else [];options=['明确作为新增行']+[f"对应旧明细 {x['line_id']} · {x['sku']} {x['name']} · {x['quantity']} {x['unit']}" for x in candidates]
            text,ok=QInputDialog.getItem(self,'确认明细身份','不按位置或相似度猜测；本版缺失的旧行将显示为移除。',options,0,False)
            if not ok:return
            n=options.index(text)
            if n:r['line_id']=candidates[n-1]['line_id']
            else:r['line_id']='local:'+io.digest((r['source']['sha']+'\0'+r['source']['sheet']+'\0'+str(r['source']['row'])).encode())[:24]
            r['identity_reviewed']=True;self.refresh()
        except Exception as exc:warn(self,exc)
    def save(self):
        try:
            if self.editing:
                for r in self.scan['rows']:r['identity_reviewed']=True
                plan=run(self,service.revise,(self.path,self.editing['docid'],self.editing['version'],self.scan['rows']),'核对关联变更')
            else:plan=run(self,service.prepare,(self.path,self.scan),'校验并生成采购差异')
            details=[]
            for p in plan['plans']:
                for d in p['differences']:
                    a,b=d['before'],d['after'];fields=[('商品编码','sku'),('商品名称','name'),('下单日期','day'),('下单数量','quantity'),('单位','unit'),('换算系数','factor'),('换算依据','conversion_basis'),('折合库存量','base_quantity'),('实际单价','price'),('实际金额','amount'),('币种','currency'),('状态','status'),('备注','note')]
                    for title,k in fields:
                        av=a.get(k) if a else None;bv=b.get(k) if b else None
                        if a is None or b is None or av!=bv:details.append([p['identity'][1],d['line'],d['sku'],title,'未有该行' if a is None else '未提供' if av is None else av,'移除此行' if b is None else '未提供' if bv is None else bv])
                    if a is None or b is None or a['links']!=b['links']:details.append([p['identity'][1],d['line'],d['sku'],'签发关联',links_text(a) if a else '未有该行',links_text(b) if b else '移除此行'])
                    def source_text(r):return '；'.join(f"{s['file']} / {s['sheet']} / {s['row']}行 / SHA256 {s['sha']}" for s in r.get('sources') or [r['source']]) if r else '无此行'
                    if a is None or b is None or source_text(a)!=source_text(b):details.append([p['identity'][1],d['line'],d['sku'],'原始来源',source_text(a),source_text(b)])
            summary='\n'.join(f"{p['identity'][1]} · {p['identity'][2]} · {p['identity'][3]}：采购V{p['expected'] or 0:03d} → V{(p['expected'] or 0)+int(p['changed']):03d}；{len(p['differences'])} 行变化" for p in plan['plans'])
            if not review_rows(self,'确认采购资料版本',['采购单','明细','商品','字段','原值','新值'],details or [['无业务差异','','','','','重复导入不增加数量']],summary+'\n全量快照替换当前采购版本；原签发和回传不变。未关联量继续待核。\n以下为完整差异，可滚动查看；默认取消。','确认保存采购资料'):return
            receipts=run(self,service.commit,(self.path,plan),'提交独立采购记录（完成前不可中断）',cancellable=False);self.saved=True;QMessageBox.information(self,'采购资料已保存','\n'.join(f"{r['number']} · V{r['version']:03d} · 未关联 {r['unlinked']} 行" for r in receipts));self.accept()
        except Exception as exc:warn(self,exc)

def import_purchases(parent,host):
    path=host.work_repository.path;dialog=PurchaseFiles(parent)
    if dialog.exec()!=QDialog.DialogCode.Accepted:return False
    try:
        infos=run(parent,io.inspect,(dialog.paths,),'读取采购文件表头');specs=[]
        for info in infos:
            d=Mapping(parent,path,info)
            if d.exec()!=QDialog.DialogCode.Accepted:return False
            specs.append(d.spec())
        scan=run(parent,io.scan,(specs,),'读取实际下单明细');scopes=list(dict.fromkeys((r['store'],r['warehouse']) for r in scan['rows']));heads=run(parent,service.targets,(path,scopes),'读取对应门店的签发修订');docs=run(parent,service.documents,(path,),'读取已有采购版本');scan=retain_links(service.auto_links(scan,heads),docs)
        d=PurchaseEditor(parent,path,scan,heads,docs);d.exec();return d.saved
    except Exception as exc:warn(parent,exc);return False

class PurchaseLibrary(QDialog):
    def __init__(self,parent,host):
        super().__init__(parent);self.host=host;self.path=host.work_repository.path;self.setWindowTitle('实际下单资料 · 独立版本');self.resize(1200,720);v=QVBoxLayout(self);v.addWidget(label('采购版本保留原件与全部明细；选中一单后查看版本或调整关联。','section'));self.search=QLineEdit();self.search.setPlaceholderText('查采购单 / 门店 / 仓 / 日期');v.addWidget(self.search);self.table,self.model=grid(self,['来源','采购单','门店','仓','日期','当前采购版本','明细行','未关联行','保存时间']);self.search.textChanged.connect(self.model.filter);v.addWidget(self.table,1)
        r=QHBoxLayout();r.addWidget(button('导入实际下单资料',self.import_more));r.addWidget(button('查看选中采购单版本',self.view));r.addWidget(button('修改当前采购版本关联',self.edit));r.addWidget(button('刷新',self.refresh));r.addWidget(button('关闭',self.reject));v.addLayout(r);self.refresh()
    def refresh(self):
        try:
            self.docs=run(self,service.documents,(self.path,),'读取采购目录');self.model.replace([[r[k] for k in ('system','number','store','warehouse','day')]+[d['version'],len(d['payload']['rows']),sum(not x['links'] for x in d['payload']['rows']),d['saved']] for d in self.docs for r in [d['payload']['rows'][0]]]);self.model.filter(self.search.text())
        except Exception as exc:warn(self,exc)
    def selected(self):
        n=self.table.currentIndex().row()
        if n<0:raise ValueError('请先选中一张采购单')
        return self.docs[self.model.indices[n]]
    def import_more(self):
        if import_purchases(self,self.host):self.refresh()
    def edit(self):
        try:
            d=self.selected();rows=d['payload']['rows'];heads=run(self,service.targets,(self.path,[(rows[0]['store'],rows[0]['warehouse'])]),'读取签发修订');editor=PurchaseEditor(self,self.path,{'rows':rows,'files':[]},heads,self.docs,editing=d);editor.exec()
            if editor.saved:self.refresh()
        except Exception as exc:warn(self,exc)
    def view(self):
        try:
            d=self.selected();version,ok=QInputDialog.getItem(self,'选择采购版本','历史版本只读，不自动移动到新签发。',[f'V{i:03d}' for i in range(d['version'],0,-1)],0,False)
            if not ok:return
            with closing(service.connect(self.path)) as db:record=service.current(db,d['docid'],int(version[1:]))
            PurchaseView(self,self.host,record).exec()
        except Exception as exc:warn(self,exc)

class PurchaseView(QDialog):
    def __init__(self,parent,host,record):
        super().__init__(parent);self.record=record;self.host=host;self.setWindowTitle('采购版本明细 · 只读');self.resize(1280,700);v=QVBoxLayout(self);v.addWidget(label(f"采购版本 V{record['version']:03d} · 保存 {record['saved']} · 实际金额与模型估算金额独立",'section'))
        fields=[('来源','system'),('采购单','number'),('明细号','line_id'),('日期','day'),('门店','store'),('仓','warehouse'),('商品编码','sku'),('商品','name'),('数量','quantity'),('单位','unit'),('换算系数','factor'),('换算依据','conversion_basis'),('折合库存量','base_quantity'),('实际单价','price'),('实际金额','amount'),('币种','currency'),('状态','status'),('备注','note')]
        table,model=grid(self,[f for f,k in fields]+['关联签发修订','原文件/页/行'],[[r.get(k) if r.get(k) is not None else '未提供' for f,k in fields]+[links_text(r),f"{r['source']['file']} / {r['source']['sheet']} / {r['source']['row']}"] for r in record['payload']['rows']]);v.addWidget(table,1);r=QHBoxLayout();r.addWidget(button('下载此版本采购原件',self.attachment));r.addWidget(button('关闭',self.reject));v.addLayout(r)
    def attachment(self):
        files=self.record['payload']['files'];names=[f['name']+' · '+f['sha'][:10] for f in files]
        if not files:QMessageBox.information(self,'无原件','此记录没有留存原件引用。');return
        text,ok=QInputDialog.getItem(self,'选择原件','按原始字节下载，不重新生成表格。',names,0,False)
        if not ok:return
        f=files[names.index(text)];p,_=QFileDialog.getSaveFileName(self,'原件另存为新文件',f['name'],'Excel (*.xlsx)')
        if p:
            try:run(self,service.attachment,(self.host.work_repository.path,f['sha'],p),'下载采购原件');QMessageBox.information(self,'已下载',p)
            except Exception as exc:warn(self,exc)

class ComparisonDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.setWindowTitle('签发 → 回传 → 实际下单 · 执行对照');self.resize(1250,740);v=QVBoxLayout(self);v.addWidget(label(head_text(head),'section'));v.addWidget(label('未关联不等于未下单；仅比较已关联范围。收货尚未接入。原签发、回传、采购量分别保存。','muted'));self.search=QLineEdit();self.search.setPlaceholderText('查商品或采购来源');v.addWidget(self.search);self.table,self.model=grid(self,service.COMPARE_HEADERS);self.search.textChanged.connect(self.model.filter);v.addWidget(self.table,1);r=QHBoxLayout();r.addWidget(button('查看门店回传',self.feedback));r.addWidget(button('导入实际下单资料',self.import_more));r.addWidget(button('采购资料与版本',self.library));r.addWidget(button('导出此单执行对照',self.export));r.addWidget(button('刷新',self.refresh));r.addWidget(button('关闭',self.reject));v.addLayout(r);self.refresh()
    def refresh(self):
        try:
            data=run(self,service.comparison,(self.host.work_repository.path,self.head),'读取执行对照');self.model.replace([[('未回传' if i==3 else '未关联' if i==4 else '不适用') if x is None else x for i,x in enumerate(r)] for r in data['rows']]);self.model.filter(self.search.text())
        except Exception as exc:warn(self,exc)
    def feedback(self):
        from feedback_ui import FeedbackDialog
        try:FeedbackDialog(self,self.host,self.head).exec();self.refresh()
        except Exception as exc:warn(self,exc)
    def import_more(self):
        if import_purchases(self,self.host):self.refresh()
    def library(self):PurchaseLibrary(self,self.host).exec();self.refresh()
    def export(self):export_selected(self,self.host,[self.head])

def export_selected(parent,host,heads):
    if not heads:QMessageBox.warning(parent,'尚未勾选','请先勾选需要导出的订单。');return
    mode,ok=QInputDialog.getItem(parent,'导出执行对照','只导出勾选订单的此修订；不受商品关键词过滤影响。',['每店每仓每修订一个文件','合并为一张多订单表'],0,False)
    if not ok:return
    folder=QFileDialog.getExistingDirectory(parent,'选择输出目录（新文件，不覆盖旧导出）')
    if folder:
        try:
            receipt=run(parent,service.export_comparisons,(host.work_repository.path,heads,folder,'separate' if mode.startswith('每店') else 'combined'),'导出独立执行对照');message=f"已完成 {len(receipt['files'])} 个文件。"+('已取消剩余导出，已完成文件保留。' if receipt['cancelled'] else '')+('后续写出失败：'+receipt['error'] if receipt['error'] else '')+'\n'+'\n'.join(receipt['files']);QMessageBox.information(parent,'执行对照已导出' if not receipt['cancelled'] and not receipt['error'] else '导出未全部完成',message)
        except Exception as exc:warn(parent,exc)
