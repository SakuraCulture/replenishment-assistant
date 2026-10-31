"""Grouped whole-document import, history and export; fixed mirror naming."""
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QComboBox, QLineEdit, QFileDialog, QMessageBox, QListWidget, QScrollArea, QWidget, QInputDialog, QGroupBox, QCheckBox, QPlainTextEdit
from ui_controls import button, label
from business_ui import run, grid, Task
from interaction_ui import confirm, review_rows
from import_ui import ImportDialog
from runtime_support import Cancelled
import lifecycle_io as io
import order_lifecycle as service

def warn(parent, exc):
    if not isinstance(exc, Cancelled): QMessageBox.warning(parent, '未完成', str(exc))

def template(parent, stage):
    path, _ = QFileDialog.getSaveFileName(parent, '下载' + io.TITLES[stage] + '最低标准模板', io.TITLES[stage] + '模板.xlsx', 'Excel (*.xlsx)')
    if path:
        try: io.template(stage, path if path.lower().endswith('.xlsx') else path+'.xlsx'); QMessageBox.information(parent, '模板已保存', path)
        except Exception as exc: warn(parent, exc)

class Files(ImportDialog):
    def __init__(self, parent, stage):
        QDialog.__init__(self, parent); self.stage = stage; self.multiple = True; self.paths = []; self.extensions = ('.xlsx',); self.setAcceptDrops(True); self.resize(780,380); self.setWindowTitle('导入'+io.TITLES[stage]+' · 1/3 选择文件')
        v = QVBoxLayout(self); v.addWidget(label('拖入一份或多份完整单据。每店、每仓、每签发修订独立保存，不改门店推荐数。', 'section')); self.list = QListWidget(); v.addWidget(self.list)
        row = QHBoxLayout(); row.addWidget(button('选择文件', self.choose)); row.addWidget(button('下载'+io.TITLES[stage]+'模板', lambda: template(self,stage))); v.addLayout(row)
        v.addWidget(label('付款必须是带商品明细的已付款单；收货必须是实际收货单。后环节先到可先存，首页仍提示最早缺少的单据。', 'muted'))
        row = QHBoxLayout(); self.go = button('读取并校验', self.accept, True); self.go.setEnabled(False); row.addWidget(self.go); row.addWidget(button('取消', self.reject)); v.addLayout(row)

class Mapping(QDialog):
    def __init__(self,parent,path,stage,info):
        super().__init__(parent); self.path=path; self.stage=stage; self.info=info; self.setWindowTitle('导入'+io.TITLES[stage]+' · 2/3 字段映射'); self.resize(940,760); v=QVBoxLayout(self)
        v.addWidget(label(info['name'],'section')); top=QHBoxLayout(); self.sheet=QComboBox(); self.sheet.addItems([s['sheet'] for s in info['sheets']]); top.addWidget(label('工作表')); top.addWidget(self.sheet)
        self.profiles=service.mappings(path,stage); self.profile=QComboBox(); self.profile.addItems(['选择映射方案',*self.profiles]); top.addWidget(self.profile); top.addWidget(button('应用映射方案',self.apply_profile)); top.addWidget(button('另存映射方案',self.save_profile)); v.addLayout(top)
        self.same_headers=QCheckBox('同时读取本文件表头完全相同的工作表');self.same_headers.setChecked(True);v.addWidget(self.same_headers);self.sheet_scope=label('','muted');v.addWidget(self.sheet_scope);self.same_headers.toggled.connect(self.describe_scope)
        v.addWidget(label('数量按库存单位；其它单位须提供换算系数和依据。单号/修订缺少时，下一步必须明确选择。* 为必需字段。','muted'))
        scroll=QScrollArea(); scroll.setWidgetResizable(True); body=QWidget(); form=QFormLayout(body); self.fields={}; self.defaults={}
        for f in io.fields(stage):
            row=QHBoxLayout(); combo=QComboBox(); self.fields[f]=combo; row.addWidget(combo,1)
            if f in ('门店编码','供货仓','数量单位','币种'):
                e=QLineEdit(); e.setPlaceholderText('未映射时使用此明确固定值'); self.defaults[f]=e; row.addWidget(e)
                if f=='数量单位': e.setText('库存单位')
            form.addRow(f+(' *' if f in io.required(stage) else ''),row)
        scroll.setWidget(body); v.addWidget(scroll,1); self.sample,self.sample_model=grid(self,[]); self.sample.setMaximumHeight(130); v.addWidget(self.sample)
        row=QHBoxLayout(); row.addWidget(button('下一步：核对整单',self.accept,True)); row.addWidget(button('取消',self.reject)); v.addLayout(row); self.sheet.currentIndexChanged.connect(self.populate); self.populate()
    def populate(self):
        s=self.info['sheets'][self.sheet.currentIndex()]; self.headers=s['headers']
        aliases={'门店确认数量':['门店确认数量','补货数量'], '供货仓':['供货仓','所属仓','仓']}
        for f,c in self.fields.items():
            c.clear(); c.addItem('不读取',None)
            for i,h in enumerate(self.headers): c.addItem(f'{i+1}列 · {h if h is not None else "（空表头）"}',i)
            hits=[i for i,h in enumerate(self.headers) if h==f]
            if not hits: hits=[i for i,h in enumerate(self.headers) if h in aliases.get(f,[])]
            if len(hits)==1: c.setCurrentIndex(hits[0]+1)
        self.sample_model.headers=[str(h) for h in self.headers]; self.sample_model.replace([list(r[:len(self.headers)])+[None]*max(0,len(self.headers)-len(r)) for r in s['sample']])
        self.describe_scope()
    def spec(self):
        return {**{k:self.info[k] for k in ('path','sha','name','properties')},'sheet':self.sheet.currentText(),'headers':self.headers,'mapping':{f:c.currentData() for f,c in self.fields.items()},'defaults':{f:e.text().strip() for f,e in self.defaults.items() if e.text().strip()}}
    def specs(self):
        selected=self.spec()
        return [{**selected,'sheet':s['sheet']} for s in self.info['sheets'] if s['headers']==self.headers] if self.same_headers.isChecked() else [selected]
    def describe_scope(self):
        if not hasattr(self,'headers'):return
        chosen=[s['sheet'] for s in self.info['sheets'] if s['headers']==self.headers] if self.same_headers.isChecked() else [self.sheet.currentText()]
        skipped=[s['sheet'] for s in self.info['sheets'] if s['sheet'] not in chosen]
        self.sheet_scope.setText('本次读取：'+'、'.join(chosen)+('；不读取：'+'、'.join(skipped) if skipped else ''))
    def apply_profile(self):
        p=self.profiles.get(self.profile.currentText())
        if not p:return
        if p['headers']!=self.headers: QMessageBox.warning(self,'表头不匹配','已保存的表头/列序不同，请重新映射。'); return
        for f,c in self.fields.items(): c.setCurrentIndex(c.findData(p['mapping'].get(f)))
        for f,e in self.defaults.items(): e.setText(p.get('defaults',{}).get(f,''))
    def save_profile(self):
        name,ok=QInputDialog.getText(self,'保存字段映射','方案名称（只保存映射，不保存业务文件）')
        if not ok or not name.strip():return
        name=name.strip()
        if name in self.profiles and not confirm(self,'覆盖映射方案',name+' 已存在，是否覆盖？'):return
        try:
            s=self.spec(); self.profiles=run(self,service.mappings,(self.path,self.stage,name,{k:s[k] for k in ('headers','mapping','defaults')}),'保存映射方案',False); self.profile.clear(); self.profile.addItems(['选择映射方案',*self.profiles]); self.profile.setCurrentText(name)
        except Exception as exc:warn(self,exc)

def head_text(h): return f"{h['day']} · {h.get('name',h['store'])}（{h['store']}）· {h['warehouse']} · {h['number']} / V{h['revision']:03d}"+('（历史修订）' if h['revision']!=h['current_revision'] else '')

class Bindings(QDialog):
    def __init__(self,parent,scan,heads,scope=None):
        super().__init__(parent); self.bindings={}; self.combos={}; self.setWindowTitle('导入'+io.TITLES[scan['stage']]+' · 3/3 绑定整单'); self.resize(1100,650); v=QVBoxLayout(self)
        v.addWidget(label('每组完整单据只关联一张店仓单的一个签发修订。未提供修订时必须手动选择。','section')); scroll=QScrollArea(); scroll.setWidgetResizable(True); body=QWidget(); form=QVBoxLayout(body)
        groups={}
        for r in scan['rows']: groups.setdefault(service.binding_key(r),[]).append(r)
        for k,rows in groups.items():
            r=rows[0]; candidates=service.matching(r,heads)
            if scope: candidates=[h for h in candidates if service.key(h)==service.key(scope)]
            form.addWidget(label(f"源单 {r['document_number'] or r['signed_number'] or '未提供单号'} · {r['store'] or '未填门店'} · {r['warehouse'] or '未填仓'} · {len(rows)}行")); c=QComboBox(); c.addItem('请选择确切店仓单及签发修订',None)
            for h in candidates:c.addItem(head_text(h),h)
            if len(candidates)==1 and ((r['signed_number'] and r['revision']) or scope): c.setCurrentIndex(1)
            self.combos[k]=c; form.addWidget(c)
            if not candidates: form.addWidget(label('无符合条件的签发单；请核对单号、门店、仓与修订。不能跨仓或猜测关联。','muted'))
        scroll.setWidget(body); v.addWidget(scroll,1); row=QHBoxLayout(); cancel=button('取消',self.reject); cancel.setDefault(True); row.addWidget(cancel); self.go=button('预览完整单据差异',self.accept,True); row.addWidget(self.go); v.addLayout(row)
        for c in self.combos.values():c.currentIndexChanged.connect(self.update)
        self.update()
    def update(self):
        self.bindings={k:c.currentData() for k,c in self.combos.items()}; self.go.setEnabled(all(self.bindings.values()))

class Review(QDialog):
    def __init__(self,parent,plan):
        super().__init__(parent); self.setObjectName('lifecycleReview'); self.setWindowTitle('确认完整'+io.TITLES[plan['plans'][0]['stage']]); self.resize(1200,740); v=QVBoxLayout(self); summary=[]; rows=[]
        for p in plan['plans']:
            h=p['head']; summary.append(head_text(h)+f"\n单据版本 {p['expected'] or 0} → {(p['expected'] or 0)+int(p['changed'])}；{len(p['payload']['rows'])}款；未列出 {len(p['payload']['missing'])}款；额外 {len(p['payload']['extra'])}款；同值重复 {p['duplicates']}行")
            rows.extend(p['differences'])
            rows.extend([[h['number'],f"V{h['revision']:03d}",s,h['products'][s]['name'],None,None,'本次未列出（不当作0）',None,None] for s in p['payload']['missing'] if not any(r[2]==s for r in p['differences'])])
        details=QPlainTextEdit();details.setReadOnly(True);details.setPlainText('\n'.join(summary));details.setMaximumHeight(140);v.addWidget(details); search=QLineEdit(); search.setPlaceholderText('筛选单号 / 商品 / 未列出 / 额外商品'); v.addWidget(search); self.table,self.model=grid(self,['补货单号','签发修订','商品编码','商品名称','上版数量','本版数量','范围说明','上版实际金额','本版实际金额'],rows); v.addWidget(self.table,1); search.textChanged.connect(self.model.filter)
        self.complete=QCheckBox('我已核对：这是本环节完整单据；未列出的商品不填0，按实保留差异。'+(' 此表确为已付款商品明细。' if plan['plans'][0]['stage']=='payment' else '')); v.addWidget(self.complete)
        v.addWidget(label('数量与推荐不同不阻止环节完成。确认后独立留存新版本和原件，不修改签发、计算或其它环节。','muted'))
        row=QHBoxLayout(); cancel=button('取消',self.reject); cancel.setDefault(True); row.addWidget(cancel); self.save=button('确认保存完整单据',self.accept,True); self.save.setEnabled(False); self.save.setAutoDefault(False); self.complete.toggled.connect(self.save.setEnabled); row.addWidget(self.save); v.addLayout(row)

def commit(parent,host,plan):
    task=Task(parent,service.commit,(host.work_repository.path,plan),'保存完整单据')
    def boundary(message):
        if message==service.COMMIT_BOUNDARY:task.cancellable=False;task.cancel_btn.setEnabled(False);task.cancel_btn.setText('正在提交，暂不能取消')
    task.worker.status.connect(boundary); previous=getattr(host,'_large_io_dialog',None);host._large_io_dialog=task
    try:task.worker.start();task.exec();task.worker.wait();result,error=task.result,task.error
    finally:host._large_io_dialog=previous;task.deleteLater()
    if host.closing:QTimer.singleShot(0,host.close)
    if error:raise error
    return result

def import_document(parent,host,stage,head=None):
    d=Files(parent,stage)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    try:
        infos=run(parent,io.inspect,(d.paths,),'识别'+io.TITLES[stage]); specs=[]
        for info in infos:
            m=Mapping(parent,host.work_repository.path,stage,info)
            if m.exec()!=QDialog.DialogCode.Accepted:return
            specs.extend(m.specs())
        scan=run(parent,io.scan,(stage,specs),'校验完整单据'); heads=run(parent,service.available,(host.work_repository.path,scan),'读取可关联签发修订')
        b=Bindings(parent,scan,heads,head)
        if b.exec()!=QDialog.DialogCode.Accepted:return
        plan=run(parent,service.prepare,(host.work_repository.path,scan,b.bindings),'核对整单数量与来源'); review=Review(parent,plan)
        if review.exec()!=QDialog.DialogCode.Accepted:return
        receipt=commit(parent,host,plan); QMessageBox.information(parent,'完整单据已保存','\n'.join(r['number']+f" / 签发V{r['revision']:03d} · {io.TITLES[r['stage']]} 第{r['version']}版"+('（相同内容未增加版本）' if not r['changed'] else '') for r in receipt)); return receipt
    except Exception as exc:warn(parent,exc)

def import_group(parent,host,head=None,refresh=lambda:None):
    group=QGroupBox('订单生命周期'); row=QHBoxLayout(group)
    def action(stage):
        if import_document(parent,host,stage,head):refresh()
    for s in io.STAGES: row.addWidget(button('导入'+io.TITLES[s],lambda checked=False,k=s:action(k)))
    return group

def export_selected(parent,host,heads,kind='comparison'):
    if not heads:return
    mode,ok=QInputDialog.getItem(parent,'选择导出方式','仅导出所选完整仓单；不受商品表搜索影响。',['每店每仓每修订一个文件','每仓一表多订单'],0,False)
    if not ok:return
    folder=QFileDialog.getExistingDirectory(parent,'选择导出目录（不会覆盖已有文件）')
    if not folder:return
    try:
        result=run(parent,service.export,(host.work_repository.path,heads,folder,kind,'separate' if mode.startswith('每店') else 'combined'),'导出生命周期资料')
        QMessageBox.information(parent,'导出结果',f"完成 {len(result['files'])} 个文件"+('；已取消剩余项' if result['cancelled'] else '')+('；失败：'+result['error'] if result['error'] else '')+'\n'+'\n'.join(result['files']))
        return result
    except Exception as exc:warn(parent,exc)

class Document(QDialog):
    def __init__(self,parent,host,head,stage):
        super().__init__(parent); self.host=host; self.head=head; self.stage=stage; self.setWindowTitle(io.TITLES[stage]+' · '+head['number']); self.resize(1200,730); v=QVBoxLayout(self)
        v.addWidget(label(head_text(head),'section')); self.versions=QComboBox(); v.addWidget(self.versions); self.table,self.model=grid(self,['商品编码','商品名称','本版数量（库存单位）','源单数量','源单单位','实际金额','币种','外部单号','日期','源表 / 行','备注']); v.addWidget(self.table,1); self.files=QComboBox(); v.addWidget(self.files); row=QHBoxLayout(); row.addWidget(button('下载此版本原文件',self.download)); row.addWidget(button('关闭',self.reject)); v.addLayout(row)
        self.docs=run(self,service.history,(host.work_repository.path,head,stage),'读取单据版本')
        for d in self.docs:self.versions.addItem(f"第{d['version']}版 · {d['saved']}",d)
        self.versions.currentIndexChanged.connect(self.show_version); self.show_version()
    def show_version(self):
        d=self.versions.currentData(); self.files.clear()
        self.model.replace([[r['sku'],r['name'],r['base_quantity'],r['quantity'],r['unit'],r['amount'],r['currency'],r['document_number'],r['document_date'],'；'.join(s['file']+'/'+s['sheet']+':'+str(s['row']) for s in r['sources']),r['note']] for r in d['payload']['rows']] if d else [])
        if d:
            for f in d['payload']['files']:self.files.addItem(f['name'],f)
    def download(self):
        f=self.files.currentData();d=self.versions.currentData()
        if not f or not d:return
        target,_=QFileDialog.getSaveFileName(self,'下载原文件',f['name'],'Excel (*.xlsx)')
        if target:
            try:run(self,service.attachment,(self.host.work_repository.path,self.head,self.stage,d['version'],f['sha'],target),'下载单据原件')
            except Exception as exc:warn(self,exc)

class LifecycleDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.setWindowTitle('订单生命周期 · '+head['number']);self.resize(1400,820);v=QVBoxLayout(self);v.addWidget(label(head_text(head),'section'));self.note=label('');v.addWidget(self.note);self.imports=import_group(self,host,head,self.refresh);v.addWidget(self.imports)
        v.addWidget(label('已签发 → 门店回传 → 门店付款 → 门店实际收货。以下四个数量独立保存；空白/未列出不等于0。','muted'));search=QLineEdit();search.setPlaceholderText('筛选商品编码 / 名称 / 资料说明');v.addWidget(search);self.table,self.model=grid(self,service.HEADERS);v.addWidget(self.table,1);search.textChanged.connect(self.model.filter)
        sources=QGroupBox('单据与依据');row=QHBoxLayout(sources);row.addWidget(button('查看签发与计算依据',lambda:host.open_archive(head)));self.views={}
        for s in io.STAGES:self.views[s]=button('查看'+io.TITLES[s]+('回传版本' if s=='return' else '版本'),lambda checked=False,k=s:self.document(k));row.addWidget(self.views[s])
        v.addWidget(sources);exports=QGroupBox('数据导出');row=QHBoxLayout(exports);self.export_confirmation=button('导出门店确认单',lambda:host.export_order(head));row.addWidget(self.export_confirmation);self.exports={}
        for s in ('payment','receipt'):self.exports[s]=button('导出'+io.TITLES[s],lambda checked=False,k=s:export_selected(self,host,[head],k));row.addWidget(self.exports[s])
        row.addWidget(button('导出生命周期对照',lambda:export_selected(self,host,[head])));v.addWidget(exports)
        legacy=QGroupBox('历史兼容资料（不计为付款）');row=QHBoxLayout(legacy);row.addWidget(button('查看旧版门店回传',self.legacy_returns));row.addWidget(button('查看旧实际下单资料',self.legacy_purchases));row.addStretch();row.addWidget(button('关闭',self.reject));v.addWidget(legacy);self.refresh()
    def refresh(self):
        self.view=run(self,service.snapshot,(self.host.work_repository.path,self.head),'读取生命周期对照');self.model.replace(self.view['rows']);next_stage=self.view['next'];self.note.setText(self.view['status']+' · '+('下一步：导入'+io.TITLES[next_stage] if next_stage else '四个环节的资料均已留存')+('；已有旧版回传资料，原版本继续保留。' if self.view['legacy_return'] else ''))
        for s,b in self.views.items():b.setEnabled(bool(self.view['documents'][s]))
        for s,b in self.exports.items():b.setEnabled(bool(self.view['documents'][s]) and not self.head.get('deleted'))
        self.imports.setEnabled(not self.head.get('deleted'));self.export_confirmation.setEnabled(not self.head.get('deleted'))
    def document(self,stage):Document(self,self.host,self.head,stage).exec()
    def legacy_returns(self):
        from feedback_ui import FeedbackDialog
        FeedbackDialog(self,self.host,self.head,readonly=True).exec();self.refresh()
    def legacy_purchases(self):
        from purchase_ui import PurchaseLibrary
        PurchaseLibrary(self,self.host).exec()
