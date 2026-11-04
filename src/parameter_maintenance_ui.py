"""Business forms for selected fields, optional common values, and shared mapping."""
import copy
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QTableWidget,QTableWidgetItem,
    QComboBox,QLineEdit,QMessageBox,QCheckBox,QHeaderView)
from ui_controls import button,label
from interaction_ui import review_rows
from business_ui import run
from business_presenter import value_text
import parameter_maintenance as pm
import parameter_schema as schema
import store_parameter_ui as store_ui
import store_parameter_import as spi


class PatchDialog(QDialog):
    def __init__(self,parent,configs,section='store',selected=None,allow_empty=False):
        super().__init__(parent);self.setWindowTitle('门店通用参数' if section=='common' else '门店补货参数');self.resize(1060,720)
        self.configs=configs;self.section=section;self.changes={};self.allow_empty=allow_empty
        v=QVBoxLayout(self)
        v.addWidget(label('仅修改勾选字段；未勾选项保留各店原值。分类参数两仓共用；主从仓、预算和策略在各自入口维护。','muted'))
        if section=='common':
            v.addWidget(label('不需要自定义时，采用默认通用方案即可。分类专用值仍优先；这里不修改分类值和仓周期。','muted'))
        self.search=QLineEdit();self.search.setPlaceholderText('搜索参数名称、分类或仓');v.addWidget(self.search)
        self.table=QTableWidget(0,5);self.table.setHorizontalHeaderLabels(['修改','参数','当前值 / 来源','设置方式','设置值'])
        self.table.setColumnWidth(0,55);self.table.setColumnWidth(1,285);self.table.setColumnWidth(2,220);self.table.setColumnWidth(3,180)
        self.table.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeMode.Stretch)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(self.table,1)
        self.fields=pm.fields(configs.values(),section);self.controls={};self.table.setRowCount(len(self.fields))
        for i,f in enumerate(self.fields):
            check=QCheckBox();check.setChecked(f in set(map(tuple,selected or [])));self.table.setCellWidget(i,0,check)
            self.table.setItem(i,1,QTableWidgetItem(pm.title(f)))
            vals=[]
            for c in configs.values():
                try:x=pm.value(c,f)
                except KeyError:x='本店未配置此分类'
                if x not in vals:vals.append(x)
            percent=f[-1] in ('w1','w2','probability')
            def fmt(x,p=percent):return f'{x*100:g}%' if p and isinstance(x,(int,float)) else value_text(x)
            text=fmt(vals[0]) if len(vals)==1 else '多种值（各店保留）'
            if len(vals)==1:
                try:text+=' · 系统默认' if vals[0]==pm.default_value(f) else ' · 已自定义'
                except KeyError:pass
            item=QTableWidgetItem(text);item.setToolTip('\n'.join(fmt(x) for x in vals));self.table.setItem(i,2,item)
            mode=QComboBox();mode.addItem('指定值','set');mode.addItem('恢复系统默认' if f[0]!='profile_parameters' else '沿用门店通用值','default')
            if len(vals)==1 and vals[0]=='沿用门店':mode.setCurrentIndex(1)
            edit=QLineEdit();edit.setPlaceholderText('填写百分比或小数' if percent else '填写非负数字')
            if len(vals)==1 and isinstance(vals[0],(int,float)):edit.setText(fmt(vals[0]))
            mode.currentIndexChanged.connect(lambda _,m=mode,e=edit,ch=check:e.setEnabled(ch.isChecked() and m.currentData()=='set'))
            check.toggled.connect(lambda on,m=mode,e=edit:(m.setEnabled(on),e.setEnabled(on and m.currentData()=='set')))
            mode.setEnabled(check.isChecked());edit.setEnabled(check.isChecked());self.table.setCellWidget(i,3,mode);self.table.setCellWidget(i,4,edit)
            if f[0]=='parameters' or f[0]=='profile_parameters':item.setToolTip(schema.HELP[f[-1]][2])
            elif f[0]=='weight_profiles':item.setToolTip(schema.WEIGHT_HELP[f[-1]])
            self.controls[f]=(check,mode,edit)
        self.search.textChanged.connect(lambda t:[self.table.setRowHidden(i,t.lower() not in pm.title(f).lower()) for i,f in enumerate(self.fields)])
        row=QHBoxLayout();row.addWidget(button('清空勾选',self.clear))
        if section=='common':row.addWidget(button('全部采用默认通用方案',self.defaults))
        row.addStretch();row.addWidget(button('取消',self.reject));row.addWidget(button('核对修改',self.finish,True));v.addLayout(row)

    def clear(self):
        for check,_,_ in self.controls.values():check.setChecked(False)
    def defaults(self):
        for check,mode,_ in self.controls.values():check.setChecked(True);mode.setCurrentIndex(1)
    def finish(self):
        try:
            from parameter_tables import numeric
            changes={}
            for f,(check,mode,edit) in self.controls.items():
                if not check.isChecked():continue
                if mode.currentData()=='default':changes[f]=pm.default_value(f)
                else:
                    raw=edit.text().strip()
                    if not raw:raise ValueError(pm.title(f)+'：请填写设置值，或选择恢复默认')
                    changes[f]=numeric(raw,f[-1])
            if not changes and not self.allow_empty:raise ValueError('请勾选需要修改的参数')
            for c in self.configs.values():pm.patch(c,changes)
            self.changes=changes;self.accept()
        except (ValueError,KeyError) as exc:QMessageBox.warning(self,'参数未应用',str(exc))


def edit_stores(parent,host,codes,section='store',scope='defaults'):
    if not codes:return
    try:
        skeleton={'rows':[{'code':c} for c in codes]}
        snapshot=store_ui.capture(host,skeleton)
        bases=snapshot['saved'] if scope=='defaults' else snapshot['current']
        if scope!='defaults' and (host.readonly or host.worker is not None or set(codes)-set(bases)):
            raise ValueError('请先载入所选门店并结束计算；历史作业只读')
        configs={c:(bases.get(c) or spi.base())['preset']['config'] for c in codes}
        form=PatchDialog(parent,configs,section)
        if form.exec()!=QDialog.DialogCode.Accepted:return
        data=pm.selection_data(codes,form.changes,snapshot['names'])
        check=store_ui.Preview(parent,host,data,snapshot,scope)
        check.setWindowTitle('核对所选参数变化')
        if check.exec()!=QDialog.DialogCode.Accepted:return
        store_ui.commit(host,data,snapshot,check.scope.currentData(),check.prepared,
            persist=lambda values,expected:run(parent,store_ui.save_values,(host.work_repository,values,expected),'保存所选门店参数',cancellable=False))
        if hasattr(parent,'refresh'):parent.refresh()
        QMessageBox.information(parent,'参数已应用',check.note.text())
    except Exception as exc:QMessageBox.warning(parent,'参数未应用',str(exc))


class MappingDialog(QDialog):
    def __init__(self,parent,host):
        super().__init__(parent);self.host=host;self.setWindowTitle('全局分类映射 · 所有门店共用');self.resize(900,670)
        self.snapshot=pm.mapping_snapshot(host.work_repository.path)
        v=QVBoxLayout(self);v.addWidget(label('只决定商品分类使用哪个分类组；各店分类权重和超参保持独立。保存只供新作业使用，历史签发不变。','muted'))
        self.note=label('旧门店映射存在差异。请核对下方完整差异，选择起点后编辑并保存。' if self.snapshot['conflict'] else '所有门店只维护这一份映射。','section');self.note.setWordWrap(True);v.addWidget(self.note)
        row=QHBoxLayout();self.source=QComboBox();self.source.addItem('选择映射起点…',None)
        if self.snapshot['mapping'] is not None:self.source.addItem('当前统一映射',self.snapshot['mapping'])
        self.source.addItem('系统默认映射',schema.standard()['category_profiles'])
        for var in self.snapshot['variants']:
            self.source.addItem('旧门店：'+'、'.join(var['stores'][:3])+f"（共{len(var['stores'])}店）",var['mapping'])
        row.addWidget(self.source,1);row.addWidget(button('用所选映射填充',self.fill_selected));row.addWidget(button('查看旧门店映射差异',self.show_conflicts));v.addLayout(row)
        self.table=QTableWidget(0,2);self.table.setHorizontalHeaderLabels(['商品分类','目标分类组（如快销、日百）']);self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch);v.addWidget(self.table,1)
        self.fill(self.snapshot['mapping'] or {})
        row=QHBoxLayout();row.addWidget(button('添加一行',lambda:self.table.insertRow(self.table.rowCount())));row.addWidget(button('删除选中行',self.remove));row.addWidget(button('导入全局分类映射',self.import_file));row.addWidget(button('下载映射模板',self.template));row.addWidget(button('导出当前映射',self.export_file));v.addLayout(row)
        row=QHBoxLayout();row.addWidget(button('关闭',self.reject));row.addWidget(button('应用已保存映射到本次全部门店',self.apply_current));row.addWidget(button('核对并保存全局映射',self.save,True));v.addLayout(row)
    def fill(self,mapping):
        self.table.setRowCount(len(mapping))
        for i,pair in enumerate(mapping.items()):
            for j,x in enumerate(pair):self.table.setItem(i,j,QTableWidgetItem(x))
    def fill_selected(self):
        if self.source.currentData() is not None:self.fill(self.source.currentData())
    def remove(self):
        for i in sorted({x.row() for x in self.table.selectedIndexes()},reverse=True):self.table.removeRow(i)
    def value(self):
        result={}
        for i in range(self.table.rowCount()):
            a,b=[self.table.item(i,j).text().strip() if self.table.item(i,j) else '' for j in (0,1)]
            if not a and not b:continue
            if a in result:raise ValueError('商品分类重复：'+a)
            result[a]=b
        return pm.validate_mapping(result,[r['preset']['config'] for r in self.snapshot['stores'].values()])
    def show_conflicts(self):
        rows=[]
        keys=list(dict.fromkeys(k for v in self.snapshot['variants'] for k in v['mapping']))
        for v in self.snapshot['variants']:
            for code in v['stores']:
                for k in keys:rows.append([code,k,v['mapping'].get(k,'未映射，沿用无分类')])
        review_rows(self,'旧门店分类映射明细',['门店','商品分类','原目标分类组'],rows,'仅供核对，不修改旧参数和历史快照。','查看完毕')
    def save(self):
        try:
            mapping=self.value();old=self.snapshot['mapping'] or {}
            rows=[[k,old.get(k,'未统一'),mapping.get(k,'未映射')] for k in dict.fromkeys([*old,*mapping]) if old.get(k)!=mapping.get(k)]
            if self.snapshot['conflict']:
                rows=[[code,k,var['mapping'].get(k,'未映射'),mapping.get(k,'未映射')] for var in self.snapshot['variants'] for code in var['stores'] for k in dict.fromkeys([*var['mapping'],*mapping]) if var['mapping'].get(k)!=mapping.get(k)]
                columns=['门店','商品分类','原值','统一后']
            else:columns=['商品分类','原值','新值']
            if not review_rows(self,'核对全局分类映射',columns,rows,'保存供新作业使用；当前作业需单独应用。历史签发不变。','保存全局映射'):return
            run(self,pm.save_mapping,(self.host.work_repository.path,mapping,self.snapshot),'保存全局分类映射',cancellable=False)
            self.snapshot=pm.mapping_snapshot(self.host.work_repository.path);self.note.setText('已保存统一映射。当前作业尚未改变；需要时点击“应用已保存映射到本次全部门店”。')
        except Exception as exc:QMessageBox.warning(self,'映射未保存',str(exc))
    def apply_current(self):
        h=self.host
        try:
            if h.readonly or h.worker is not None or not h.store_widgets:raise ValueError('请载入本次门店并结束计算；历史作业不能修改')
            snap=pm.mapping_snapshot(h.work_repository.path)
            if snap['current'] is None:raise ValueError('请先保存全局映射')
            if self.value()!=snap['mapping']:raise ValueError('表单有未保存的修改，请先保存或重新打开')
            proposed={};rows=[]
            before={s:h.current_parameter_record(s) for s in h.store_widgets}
            for s,record in before.items():
                proposed[s]=pm.bind(record['preset'],snap)
                for name,a,b in spi.changes(record,{**record,'preset':proposed[s]}):rows.append([s,name,a,b])
            if not review_rows(self,'核对本次映射变化',['门店','参数','原值','新值'],rows,'本次全部门店统一采用；变化门店结果需重算，历史和门店其他参数不变。','应用本次映射'):return
            if snap!=pm.mapping_snapshot(h.work_repository.path) or before!={s:h.current_parameter_record(s) for s in h.store_widgets}:raise ValueError('核对期间配置变化，请重新核对')
            changed=[s for s in proposed if proposed[s]['config']!=before[s]['preset']['config']]
            h.store_presets.update(proposed);h._mapping_error='';h.invalidate(changed)
            for s in proposed:h.refresh_parameter_label(s)
            h.selection_changed();self.note.setText('本次已采用全局映射；变化门店请重算。')
        except Exception as exc:QMessageBox.warning(self,'本次映射未应用',str(exc))
    def import_file(self):
        from import_ui import ImportDialog
        from global_mapping_io import read
        d=ImportDialog(self,'global_category_mapping',multiple=False)
        if d.exec()==QDialog.DialogCode.Accepted:
            try:self.fill(run(self,read,(d.paths[0],),'读取全局映射'));self.note.setText('已填充表单，尚未保存；请核对并保存。')
            except Exception as exc:QMessageBox.warning(self,'未导入',str(exc))
    def template(self):
        from import_ui import download
        download(self,'global_category_mapping')
    def export_file(self):
        import export_locations
        from global_mapping_io import write
        try:
            mapping=self.value();path,_=export_locations.save_file(self,'导出全局映射','全局分类映射.xlsx','Excel (*.xlsx)')
            if path:run(self,write,(path,mapping),'导出全局分类映射')
        except Exception as exc:QMessageBox.warning(self,'未导出',str(exc))


def open_mapping(parent,host):
    try:MappingDialog(parent,host).exec()
    except Exception as exc:QMessageBox.warning(parent,'映射未打开',str(exc))
