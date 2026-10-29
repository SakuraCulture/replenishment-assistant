"""Complete parameter editor with explanations, versioned presets and per-store use."""
import copy,json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QTabWidget,QTableWidget,QTableWidgetItem,QDoubleSpinBox,QComboBox,QCheckBox,QLineEdit,QTextBrowser,QMessageBox,QHeaderView,QInputDialog
from ui_controls import label,button
import parameter_schema as schema

class NumberInput(QDoubleSpinBox):
    def textFromValue(self,value):return f'{value:.8f}'.rstrip('0').rstrip('.') or '0'

class ParameterDialog(QDialog):
    def __init__(self,parent,config,preset,trial=None,scope=''):
        super().__init__(parent);self.setWindowTitle('完整参数 · '+scope);self.resize(1050,760);self.config=copy.deepcopy(config);self.preset=copy.deepcopy(preset);self.action=None;self.repository=parent.work_repository
        v=QVBoxLayout(self);v.addWidget(label(scope+'。应用整个表单；各店预算和主从仓保留。修改先仅保留在此窗口。','muted'))
        row=QHBoxLayout();self.choices=QComboBox();self.presets=self.repository.presets()
        for p in self.presets:self.choices.addItem(f"{p['name']} v{p['version']}"+(' · 内置已验收基线' if p['builtin'] else ' · 自定义，效果需试算'),p)
        row.addWidget(self.choices,1);row.addWidget(button('载入此方案',self.load_selected));row.addWidget(button('归档所选版本',self.archive_selected));v.addLayout(row)
        self.tabs=QTabWidget();v.addWidget(self.tabs,1)
        self.param_table=QTableWidget(0,4);self.param_table.setHorizontalHeaderLabels(['参数','当前值','单位','方案默认值']);self.param_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.param_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.param_table,'模型超参')
        self.weight_table=QTableWidget(0,5);self.weight_table.setHorizontalHeaderLabels(['分类方案','趋势α','最近7天w1','此前7天w2','此前16天w3（自动）']);self.tabs.addTab(self.weight_table,'预测权重')
        self.mapping=QTableWidget(0,2);self.mapping.setHorizontalHeaderLabels(['商品分类（可编辑，可在末行新增）','权重方案']);self.mapping.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.mapping,'分类映射')
        self.warehouses=QTableWidget(0,3);self.warehouses.setHorizontalHeaderLabels(['仓','备货天数','到货天数']);self.warehouses.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.warehouses.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.warehouses,'各仓周期')
        fixed=QTextBrowser();fixed.setPlainText(schema.FIXED);self.tabs.addTab(fixed,'固定规则')
        self.help=QTextBrowser();self.help.setMaximumHeight(115);v.addWidget(self.help)
        self.param_table.currentCellChanged.connect(lambda r,*_:self.help.setPlainText(schema.HELP[list(schema.HELP)[r]][2]) if r>=0 else None)
        self.weight_table.currentCellChanged.connect(lambda r,c,*_:self.help.setPlainText(schema.WEIGHT_HELP.get(['','alpha','w1','w2',''][c], 'w3=1-w1-w2，无分类自动跟随快销；缺货时还按有效有货天数修正。')) if c>=0 else None)
        self.warehouses.currentCellChanged.connect(lambda *_:self.help.setPlainText('各仓周期优先于通用depth/lead；按门店实际补货与到货周期维护。修改只在点击应用后生效。'))
        self.exception=QCheckBox('启用一箱例外（仍受禁用/停采与门店总预算约束）');v.addWidget(self.exception)
        self.note=label('','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('复制另存方案',lambda:self.save_preset(False)));row.addWidget(button('保存方案新版本',lambda:self.save_preset(True)))
        if trial:row.addWidget(button('与当前配置试算',lambda:self.try_trial(trial)))
        row.addStretch();v.addLayout(row)
        row=QHBoxLayout();row.addWidget(button('取消',self.reject));row.addStretch();row.addWidget(button('仅用于本次',lambda:self.finish('temporary')));row.addWidget(button('保存为本店默认',lambda:self.finish('default'),True));v.addLayout(row)
        if not scope.startswith('所选'):row.itemAt(row.count()-1).widget().setVisible(False);row.itemAt(row.count()-2).widget().setVisible(False)
        self.fill(config)
    def spin(self,value,maximum=1000000000):
        w=NumberInput();w.setDecimals(8);w.setRange(0,maximum);w.setSingleStep(.1);w.setValue(value);return w
    def fill(self,config):
        self.config=copy.deepcopy(config);base=self.preset.get('base_config',self.preset['config']);self.param_table.setRowCount(len(schema.HELP))
        for i,(key,(name,unit,helptext)) in enumerate(schema.HELP.items()):
            self.param_table.setItem(i,0,QTableWidgetItem(name+' · '+key+('（在各仓周期页维护）' if key in {'depth','lead'} else '')));w=self.spin(config['parameters'][key]);w.setEnabled(key not in {'extreme_days','depth','lead'});self.param_table.setCellWidget(i,1,w);self.param_table.setItem(i,2,QTableWidgetItem(unit));self.param_table.setItem(i,3,QTableWidgetItem(str(base['parameters'][key])));self.param_table.item(i,0).setToolTip(helptext)
            w.valueChanged.connect(lambda _,text=helptext:self.help.setPlainText(text))
        self.weight_table.setRowCount(len(config['weight_profiles']))
        for i,(name,weights) in enumerate(config['weight_profiles'].items()):
            item=QTableWidgetItem(name);item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEditable);self.weight_table.setItem(i,0,item)
            for j,key in enumerate(['alpha','w1','w2'],1):
                w=self.spin(weights[key],1 if key!='alpha' else 100);self.weight_table.setCellWidget(i,j,w);w.valueChanged.connect(self.weight_changed)
            derived=QTableWidgetItem('');derived.setFlags(derived.flags()&~Qt.ItemFlag.ItemIsEditable);self.weight_table.setItem(i,4,derived)
        self.weight_changed();mapping=list(config['category_profiles'].items());self.mapping.setRowCount(len(mapping)+1)
        for i in range(len(mapping)+1):
            self.mapping.setItem(i,0,QTableWidgetItem(mapping[i][0] if i<len(mapping) else ''));combo=QComboBox();combo.addItems(list(config['weight_profiles']));combo.setCurrentText(mapping[i][1] if i<len(mapping) else '快销');self.mapping.setCellWidget(i,1,combo)
        self.warehouses.setRowCount(len(config['warehouses']))
        for i,(wh,values) in enumerate(config['warehouses'].items()):
            self.warehouses.setItem(i,0,QTableWidgetItem(wh))
            for j,value in enumerate(values,1):self.warehouses.setCellWidget(i,j,self.spin(max(value,0),365))
        self.exception.setChecked(config['exception']);self.note.setText(f"来源：{self.preset['name']} v{self.preset['version']}。分类映射空白行不保存。")
        self.param_table.setCurrentCell(0,0)
    def weight_changed(self):
        for i in range(self.weight_table.rowCount()):
            if self.weight_table.cellWidget(i,2) and self.weight_table.cellWidget(i,3) and self.weight_table.item(i,4):self.weight_table.item(i,4).setText(f"{1-self.weight_table.cellWidget(i,2).value()-self.weight_table.cellWidget(i,3).value():.8g}")
    def value(self):
        c=copy.deepcopy(self.config);c['parameters']={k:self.param_table.cellWidget(i,1).value() for i,k in enumerate(schema.HELP)}
        c['weight_profiles']={self.weight_table.item(i,0).text():{key:self.weight_table.cellWidget(i,j).value() for j,key in enumerate(['alpha','w1','w2'],1)} for i in range(self.weight_table.rowCount())}
        c['category_profiles']={}
        for i in range(self.mapping.rowCount()):
            name=self.mapping.item(i,0).text().strip()
            if name:
                if name in c['category_profiles']:raise ValueError('分类重复：'+name)
                c['category_profiles'][name]=self.mapping.cellWidget(i,1).currentText()
        c['warehouses']={self.warehouses.item(i,0).text():[self.warehouses.cellWidget(i,j).value() for j in [1,2]] for i in range(self.warehouses.rowCount())};c['exception']=self.exception.isChecked();return schema.validate(c)
    def load_selected(self):
        self.preset=copy.deepcopy(self.choices.currentData());self.fill(self.preset['config'])
    def archive_selected(self):
        p=self.choices.currentData()
        try:self.repository.archive_preset(p['id'],p['version'])
        except Exception as exc:QMessageBox.warning(self,'未归档',str(exc));return
        self.choices.removeItem(self.choices.currentIndex());self.note.setText('已从方案选择列表归档；已绑定的门店和历史快照仍保留该版本。')
    def save_preset(self,update):
        try:c=self.value()
        except ValueError as exc:QMessageBox.warning(self,'参数未保存',str(exc));return
        if update and self.preset['id']=='standard':QMessageBox.information(self,'标准只读','请使用“复制另存方案”。');return
        name,ok=QInputDialog.getText(self,'保存完整方案','方案名称',text=self.preset['name'] if update else self.preset['name']+' 副本')
        if not ok:return
        try:self.preset=self.repository.save_preset(name,c,self.preset['id'] if update else None)
        except Exception as exc:QMessageBox.warning(self,'未保存',str(exc));return
        self.fill(c);self.note.setText(f"已保存 {self.preset['name']} v{self.preset['version']}；未自动应用到其他门店。")
    def finish(self,action):
        try:self.config=self.value()
        except ValueError as exc:QMessageBox.warning(self,'参数需调整',str(exc));return
        self.action=action;self.accept()
    def try_trial(self,callback):
        try:c=self.value();callback(c,self)
        except ValueError as exc:QMessageBox.warning(self,'无法试算',str(exc))

class ParameterUi:
    def preset_for(self,store):
        return copy.deepcopy(self.store_presets.get(store,{'id':'standard','name':'标准补货','version':1,'builtin':True,'config':schema.standard()}))
    def config_for(self,store):
        c=self.preset_for(store)['config'];c['warehouses']=copy.deepcopy(self.overrides.get(store,c['warehouses']))
        for wh,values in self.default_settings(store).items():c['warehouses'].setdefault(wh,values)
        return c
    def show_parameters(self):
        self.open_preset_library()
    def open_preset_library(self):
        if self.worker:return
        dialog=ParameterDialog(self,schema.standard(),self.preset_for('__default__'),scope='方案库 · 查看、复制和保存版本')
        dialog.exec()
    def edit_parameters(self,codes):
        if not codes or self.readonly:return
        preset=self.preset_for(codes[0]);dialog=ParameterDialog(self,self.config_for(codes[0]),preset,trial=lambda c,d:self.trial_parameters(codes,c,d),scope=f'所选 {len(codes)} 家门店')
        if dialog.exec()!=QDialog.DialogCode.Accepted:return
        self.apply_parameters(codes,dialog.config,dialog.preset,dialog.action=='default')
    def apply_parameters(self,codes,config,preset,persist=False):
        schema.validate(config)
        for s in codes:
            self.store_presets[s]={**copy.deepcopy(preset),'base_config':copy.deepcopy(preset.get('base_config',preset['config'])),'config':copy.deepcopy(config)};self.overrides[s]=copy.deepcopy(config['warehouses']);self.refresh_parameter_label(s)
        self.invalidate(codes);self.selection_changed()
        if persist:self.save_store_defaults(codes)
    def refresh_parameter_label(self,s):
        p=self.preset_for(s);changed=json.loads(json.dumps(p['config']))!=json.loads(json.dumps(p.get('base_config',p['config'])));self.store_widgets[s]['params'].setText(f"{p['name']} v{p['version']}"+(' · 本店调整' if changed else ''));self.store_widgets[s]['params'].setToolTip('完整生效配置（本店调整包含在内）：\n'+json.dumps(p['config'],ensure_ascii=False,indent=2))
    def save_store_defaults(self,codes=None):
        codes=self.selected_codes() if codes is None or isinstance(codes,bool) else codes
        if not codes:return
        try:self.work_repository.save_stores({s:{'preset':self.preset_for(s),'ui':self.store_ui(s)} for s in codes})
        except Exception as exc:self.failed('门店默认未保存：'+str(exc));return
        self.status.setText(f'已保存 {len(codes)} 家门店的完整默认配置；作业记录需单独保存。')
    def trial_parameters(self,codes,config,dialog):
        if self.worker or self.input_dirty:raise ValueError('请先完成数据检查，等待当前任务结束')
        import parameter_trial
        jobs={s:self.selected_settings(s) for s in codes};dialog.note.setText('正在使用同一份数据试算，不修改当前结果或作业记录…')
        def done(report):
            if not dialog.isVisible():self.status.setText('试算已结束，未应用到正式结果。');return
            dialog.note.setText('试算完成，未修改正式结果。');view=QDialog(dialog);view.setWindowTitle('同快照参数对比 · 未应用');view.resize(1000,720);v=QVBoxLayout(view);text=QTextBrowser();text.setPlainText(report);v.addWidget(text);v.addWidget(button('关闭',view.accept));view.exec()
        self.job(parameter_trial.compare,(self.loaded,jobs,self.profile,config),done)
