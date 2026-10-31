"""Grouped whole-document import, history and export; fixed mirror naming."""
import export_locations
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
    path, _ = export_locations.save_file(parent, '下载' + io.TITLES[stage] + '最低标准模板', io.TITLES[stage] + '模板.xlsx', 'Excel (*.xlsx)')
    if path:
        try: io.template(stage, path if path.lower().endswith('.xlsx') else path+'.xlsx'); QMessageBox.information(parent, '模板已保存', path)
        except Exception as exc: warn(parent, exc)

class Files(ImportDialog):
    def __init__(self, parent, stage):
        QDialog.__init__(self, parent); self.stage = stage; self.multiple = stage!='payment'; self.paths = []; self.extensions = ('.xlsx',); self.setAcceptDrops(True); self.resize(780,380); self.setWindowTitle('导入'+io.TITLES[stage]+' · 1/3 选择文件')
        v = QVBoxLayout(self); v.addWidget(label('拖入所选仓单的一份完整付款表，沿用原表数量和商品金额。' if stage=='payment' else '拖入完整单据；门店回传可直接使用修改数量后的签发单。每店每仓独立保存。', 'section')); self.list = QListWidget(); v.addWidget(self.list)
        row = QHBoxLayout(); row.addWidget(button('选择文件', self.choose)); row.addWidget(button('下载'+io.TITLES[stage]+'模板', lambda: template(self,stage))); v.addLayout(row)
        v.addWidget(label('数量空白、缺行在核对确认后按0；未导入整单不是0。付款不记录付款时间，数量不乘箱规。', 'muted'))
        row = QHBoxLayout(); self.go = button('读取并校验', self.accept, True); self.go.setEnabled(False); row.addWidget(self.go); row.addWidget(button('取消', self.reject)); v.addLayout(row)

class Mapping(QDialog):
    def __init__(self,parent,path,stage,info):
        super().__init__(parent); self.path=path; self.stage=stage; self.info=info; self.setWindowTitle('导入'+io.TITLES[stage]+' · 2/3 字段映射'); self.resize(940,760); v=QVBoxLayout(self)
        v.addWidget(label(info['name'],'section')); top=QHBoxLayout(); self.sheet=QComboBox(); self.sheet.addItems([s['sheet'] for s in info['sheets']]); top.addWidget(label('工作表')); top.addWidget(self.sheet)
        self.profiles=service.mappings(path,stage); self.profile=QComboBox(); self.profile.addItems(['选择映射方案',*self.profiles]); top.addWidget(self.profile); top.addWidget(button('应用映射方案',self.apply_profile)); top.addWidget(button('另存映射方案',self.save_profile)); v.addLayout(top)
        self.same_headers=QCheckBox('同时读取本文件表头完全相同的工作表');self.same_headers.setChecked(True);v.addWidget(self.same_headers);self.sheet_scope=label('','muted');v.addWidget(self.sheet_scope);self.same_headers.toggled.connect(self.describe_scope)
        v.addWidget(label('只提供本环节数量；上环节数量和差额由系统读取。按补货单号关联，单张订单详情可自动带入。修订、门店、仓库、单位由系统维护。','muted'))
        scroll=QScrollArea(); scroll.setWidgetResizable(True); body=QWidget(); form=QFormLayout(body); self.fields={}; self.defaults={}
        for f in io.input_fields(stage):
            row=QHBoxLayout(); combo=QComboBox(); self.fields[f]=combo; row.addWidget(combo,1)
            if f in ('门店编码','供货仓','数量单位','币种'):
                e=QLineEdit(); e.setPlaceholderText('未映射时使用此明确固定值'); self.defaults[f]=e; row.addWidget(e)
                if f=='数量单位': e.setText('库存单位')
            form.addRow(f+('（批量必填；单张可带入）' if f=='补货单号' else '（必填）' if f in io.required(stage) else '（选填）'),row)
        scroll.setWidget(body); v.addWidget(scroll,1); self.sample,self.sample_model=grid(self,[]); self.sample.setMaximumHeight(130); v.addWidget(self.sample)
        row=QHBoxLayout(); row.addWidget(button('下一步：核对整单',self.accept,True)); row.addWidget(button('取消',self.reject)); v.addLayout(row); self.sheet.currentIndexChanged.connect(self.populate); self.populate()
    def populate(self):
        s=self.info['sheets'][self.sheet.currentIndex()]; self.headers=s['headers']
        self.compat_mapping=io.auto_mapping(self.stage,self.headers)
        self.compat_defaults={'数量单位':'库存单位'}
        for f,c in self.fields.items():
            c.clear(); c.addItem('不读取',None)
            for i,h in enumerate(self.headers): c.addItem(f'{i+1}列 · {h if h is not None else "（空表头）"}',i)
            index=self.compat_mapping.get(f)
            if index is not None:c.setCurrentIndex(index+1)
        self.sample_model.headers=[str(h) for h in self.headers]; self.sample_model.replace([list(r[:len(self.headers)])+[None]*max(0,len(self.headers)-len(r)) for r in s['sample']])
        self.describe_scope()
    def spec(self):
        return {**{k:self.info[k] for k in ('path','sha','name','properties')},'sheet':self.sheet.currentText(),'headers':self.headers,'mapping':{**self.compat_mapping,**{f:c.currentData() for f,c in self.fields.items()}},'defaults':{**self.compat_defaults,**{f:e.text().strip() for f,e in self.defaults.items() if e.text().strip()}},'order_number_only':True}
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
        self.compat_mapping.update({k:v for k,v in p['mapping'].items() if k not in self.fields})
        self.compat_defaults.update(p.get('defaults',{}))
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

def head_text(h): return f"{h['day']} · {h.get('name',h['store'])}（{h['store']}）· {h['warehouse']} · {h['number']}"+('（历史记录）' if h['revision']!=h['current_revision'] else '')

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
            h=p['head'];payload=p['payload'];basis=payload['basis'];origins=[r['quantity_origin'] for r in payload['rows']]
            basis_text='签发建议' if basis['stage']=='signed' else io.TITLES[basis['stage']]
            summary.append(head_text(h)+f"\n依据：{basis_text}；系统自动读取上环节数量；{len(payload['rows'])}款；明确0 {origins.count('zero')}款、空白转0 {origins.count('blank')}款、缺行转0 {origins.count('missing')}款；清单外 {len(payload['extra'])}款；同值重复 {p['duplicates']}行"+('；上游未就绪，本次仅留存，之后须重新核对。' if payload['pending'] else '')+('；本次明确确认整单为0，不生成原始Excel。' if payload['explicit_zero'] else ''))
            source=next((r['source_order'] for r in payload['raw_rows'] if r.get('source_order')),None)
            if source:summary.append('来源店铺：'+str(source.get('店铺名称'))+'；内部订单：'+str(source.get('内部订单号'))+'；线上订单：'+str(source.get('线上订单号'))+'。请与上方所选店仓单核对。')
            rows.extend(p['differences'])
        details=QPlainTextEdit();details.setReadOnly(True);details.setPlainText('\n'.join(summary));details.setMaximumHeight(150);v.addWidget(details)
        self.search=QLineEdit();self.search.setPlaceholderText('搜索单号 / 商品编码 / 名称 / 处理说明');self.category=QComboBox();self.category.addItems(['全部','数量变化','明确填0','空白按0','缺行按0','清单外商品']);filters=QHBoxLayout();filters.addWidget(self.category);filters.addWidget(self.search);v.addLayout(filters)
        self.all_rows=rows;self.table,self.model=grid(self,['补货单号','商品编码','商品名称','上环节数量（自动）','本环节数量','差额（自动）','处理说明','上次保存数量（自动）','源单商品金额'],[[r[0],*r[2:]] for r in rows]);v.addWidget(self.table,1);self.category.currentTextChanged.connect(self.filter_rows);self.search.textChanged.connect(self.filter_rows)
        self.complete=QCheckBox('我已核对完整读取范围；对照范围内空白/缺行按0，不累计旧版本。'+(' 此表确为已付款商品明细，商品金额即实付。' if plan['plans'][0]['stage']=='payment' else ''));v.addWidget(self.complete)
        self.extra_ack=QCheckBox('清单外商品已逐项核对，确属所选门店和仓单；完全匹配不上时已重新核对订单。');self.extra_ack.setVisible(any(p['payload']['extra'] for p in plan['plans']));self.extra_ack.setChecked(not any(p['payload']['extra'] for p in plan['plans']));v.addWidget(self.extra_ack)
        v.addWidget(label('数量与推荐不同不阻止环节完成。确认后独立留存新版本和原件，不修改签发、计算或其它环节。','muted'))
        row=QHBoxLayout(); cancel=button('取消',self.reject); cancel.setDefault(True); row.addWidget(cancel); self.save=button('确认保存完整单据',self.accept,True); self.save.setEnabled(False); self.save.setAutoDefault(False);self.complete.toggled.connect(self.allow_save);self.extra_ack.toggled.connect(self.allow_save);row.addWidget(self.save);v.addLayout(row)
    def allow_save(self):self.save.setEnabled(self.complete.isChecked() and self.extra_ack.isChecked())
    def filter_rows(self):
        from decimal import Decimal
        kind=self.category.currentText();rows=self.all_rows
        if kind=='数量变化':rows=[r for r in rows if r[6] is not None and Decimal(str(r[6]))!=0]
        elif kind!='全部':rows=[r for r in rows if kind in r[7]]
        self.model.replace([[r[0],*r[2:]] for r in rows]);self.model.filter(self.search.text())

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
    if stage=='payment' and head is None:
        try:
            heads=run(parent,service.current_targets,(host.work_repository.path,),'选择付款对应补货单')
            names=[head_text(h) for h in heads];choice,ok=QInputDialog.getItem(parent,'选择付款对应订单','先选择一张店仓单；本次不做付款批量关联。',names,0,False)
            if not ok or not names:return
            head=heads[names.index(choice)]
        except Exception as exc:warn(parent,exc);return
    d=Files(parent,stage)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    try:
        infos=run(parent,io.inspect,(d.paths,),'识别'+io.TITLES[stage]); specs=[]
        if stage=='payment' and len(infos)!=1:raise ValueError('本次付款仅导入一份完整仓单文件')
        for info in infos:
            paid=io.payment_specs(info) if stage=='payment' else []
            if paid:
                chosen=[s['sheet'] for s in paid];skipped=[s['sheet'] for s in info['sheets'] if s['sheet'] not in chosen]
                if not confirm(parent,'核对付款文件读取范围','已识别现有付款原表。读取：'+'、'.join(chosen)+('；不读取：'+'、'.join(skipped) if skipped else '')+'。数量与回传一致，不乘箱规；商品金额按实付保存。'):return
                specs.extend({**s,'order_number_only':True} for s in paid);continue
            m=Mapping(parent,host.work_repository.path,stage,info)
            if m.exec()!=QDialog.DialogCode.Accepted:return
            specs.extend(m.specs())
        scan=run(parent,io.scan,(stage,specs),'校验完整单据')
        if stage=='payment' and len({service.binding_key(r) for r in scan['rows']})!=1:raise ValueError('付款文件包含多个来源订单，请按一张完整仓单分别导入；不自动合并')
        plan=run(parent,service.prepare_numbered,(host.work_repository.path,scan,head),'按补货单号关联并读取上环节数量'); review=Review(parent,plan)
        if review.exec()!=QDialog.DialogCode.Accepted:return
        receipt=commit(parent,host,plan); QMessageBox.information(parent,'完整单据已保存','\n'.join(r['number']+' · '+io.TITLES[r['stage']]+('（内容相同，无重复保存）' if not r['changed'] else '已更新') for r in receipt)); return receipt
    except Exception as exc:warn(parent,exc)

def import_group(parent,host,head=None,refresh=lambda:None):
    group=QGroupBox('订单生命周期'); row=QHBoxLayout(group)
    def action(stage):
        if import_document(parent,host,stage,head):refresh()
    for s in io.STAGES: row.addWidget(button('导入'+io.TITLES[s],lambda checked=False,k=s:action(k)))
    return group

def review_again(parent,host,head,stage,zero=False):
    try:
        fn=service.zero_plan if zero else service.recheck
        plan=run(parent,fn,(host.work_repository.path,head,stage),'整单为0核对' if zero else '按新上游版本重新核对');d=Review(parent,plan)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        return commit(parent,host,plan)
    except Exception as exc:warn(parent,exc)

def export_selected(parent,host,heads,kind='comparison'):
    if not heads:return
    mode,ok=QInputDialog.getItem(parent,'选择导出方式','仅导出所选完整仓单；不受商品表搜索影响。',['每店每仓每修订一个文件','每仓一表多订单'],0,False)
    if not ok:return
    count=len(heads) if mode.startswith('每店') else len({h['warehouse'] for h in heads})
    if not confirm(parent,'核对导出范围','\n'.join(head_text(h) for h in heads)+f'\n共 {len(heads)} 张仓单，预计 {count} 个文件；'+mode+'。商品搜索不会缩小导出范围。'):return
    folder=export_locations.directory(parent,'选择导出目录（不会覆盖已有文件）')
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
        self.model.replace([[r['sku'],r['name'],r['base_quantity'],r['quantity'],r['unit'],r['amount'],r['currency'],r['document_number'],r['document_date'],'；'.join(s['file']+'/'+s['sheet']+':'+str(s['row']) for s in r['sources']), '；'.join(str(x) for x in (r.get('processing_note'),r.get('note')) if x)] for r in d['payload']['rows']] if d else [])
        if d:
            for f in d['payload']['files']:self.files.addItem(f['name'],f)
    def download(self):
        f=self.files.currentData();d=self.versions.currentData()
        if not f or not d:return
        target,_=export_locations.save_file(self,'下载原文件',f['name'],'Excel (*.xlsx)')
        if target:
            try:run(self,service.attachment,(self.host.work_repository.path,self.head,self.stage,d['version'],f['sha'],target),'下载单据原件')
            except Exception as exc:warn(self,exc)

class LifecycleDialog(QDialog):
    def __init__(self,parent,host,head):
        super().__init__(parent);self.host=host;self.head=head;self.setWindowTitle('订单生命周期 · '+head['number']);self.resize(1400,820);v=QVBoxLayout(self);v.addWidget(label(head_text(head),'section'));self.note=label('');v.addWidget(self.note);self.imports=import_group(self,host,head,self.refresh);v.addWidget(self.imports)
        v.addWidget(label('已签发 → 门店回传 → 门店付款 → 门店实际收货。新单据核对确认后空白/缺行按0；未导入整单不当作0。旧记录按旧口径保留。','muted'));search=QLineEdit();search.setPlaceholderText('筛选商品编码 / 名称 / 资料说明');v.addWidget(search);self.table,self.model=grid(self,service.HEADERS);v.addWidget(self.table,1);search.textChanged.connect(self.model.filter)
        sources=QGroupBox('单据与依据');row=QHBoxLayout(sources);row.addWidget(button('查看签发与计算依据',lambda:host.open_archive(head)));self.views={}
        for s in io.STAGES:self.views[s]=button('查看'+io.TITLES[s]+'版本',lambda checked=False,k=s:self.document(k));row.addWidget(self.views[s])
        v.addWidget(sources);exports=QGroupBox('数据导出');row=QHBoxLayout(exports);self.export_confirmation=button('导出门店确认单',lambda:host.export_order(head));row.addWidget(self.export_confirmation);self.exports={}
        for s in io.STAGES:self.exports[s]=button('导出'+io.TITLES[s],lambda checked=False,k=s:export_selected(self,host,[head],k));row.addWidget(self.exports[s])
        row.addWidget(button('导出生命周期对照',lambda:export_selected(self,host,[head])));v.addWidget(exports)
        corrections=QGroupBox('重新核对（保留原版本）');row=QHBoxLayout(corrections);self.rechecks={}
        for s in io.STAGES:self.rechecks[s]=button('重新核对'+io.TITLES[s],lambda checked=False,k=s:self.recheck(k));row.addWidget(self.rechecks[s])
        v.addWidget(corrections);zeros=QGroupBox('特殊情况：明确确认本环节整单为0');row=QHBoxLayout(zeros);self.zeros={}
        for s in io.STAGES:self.zeros[s]=button('确认'+io.TITLES[s]+'整单为0',lambda checked=False,k=s:self.recheck(k,True));row.addWidget(self.zeros[s])
        v.addWidget(zeros)
        legacy=QGroupBox('历史兼容资料（不计为付款）');row=QHBoxLayout(legacy);row.addWidget(button('查看旧版门店回传',self.legacy_returns));row.addWidget(button('查看旧实际下单资料',self.legacy_purchases));row.addStretch();row.addWidget(button('关闭',self.reject));v.addWidget(legacy);self.refresh()
    def refresh(self):
        self.view=run(self,service.snapshot,(self.host.work_repository.path,self.head),'读取生命周期对照');self.model.replace(self.view['rows']);next_stage=self.view['next'];status='已完成' if self.view['status']=='已收齐' else self.view['status'];self.note.setText(status+' · '+(self.view.get('note') or ('下一步：导入'+io.TITLES[next_stage] if next_stage else '资料已留存'))+('；已有旧版回传资料，原版本继续保留。' if self.view['legacy_return'] else ''))
        for s,b in self.views.items():b.setEnabled(bool(self.view['documents'][s]))
        for s,b in self.exports.items():b.setEnabled(bool(self.view['documents'][s]) and not self.head.get('deleted'))
        for s,b in self.rechecks.items():b.setEnabled(bool(self.view['documents'][s]) and not self.head.get('deleted'))
        for s,b in self.zeros.items():b.setEnabled(not self.head.get('deleted'))
        self.imports.setEnabled(not self.head.get('deleted'));self.export_confirmation.setEnabled(not self.head.get('deleted'))
    def document(self,stage):Document(self,self.host,self.head,stage).exec()
    def recheck(self,stage,zero=False):
        if review_again(self,self.host,self.head,stage,zero):self.refresh()
    def legacy_returns(self):
        from feedback_ui import FeedbackDialog
        FeedbackDialog(self,self.host,self.head,readonly=True).exec();self.refresh()
    def legacy_purchases(self):
        from purchase_ui import PurchaseLibrary
        PurchaseLibrary(self,self.host).exec()
