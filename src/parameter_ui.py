"""Complete parameter editor with explanations, versioned presets and per-store use."""
import copy,json
from decimal import Decimal,InvalidOperation
from PySide6.QtGui import QValidator
from business_presenter import value_text,path_title,business_help
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget,QDialog,QVBoxLayout,QHBoxLayout,QTabWidget,QTableWidget,QTableWidgetItem,QDoubleSpinBox,QComboBox,QCheckBox,QLineEdit,QTextBrowser,QMessageBox,QHeaderView,QInputDialog
from ui_controls import label,button
import parameter_schema as schema
import restock_policy as policy

class NumberInput(QDoubleSpinBox):
    def textFromValue(self,value):return f'{value:.8f}'.rstrip('0').rstrip('.') or '0'

class PercentInput(NumberInput):
    """Internal value/range/precision stay fractional; only the editor uses percent."""
    def textFromValue(self,value):
        return format(Decimal(str(value))*100,'f').rstrip('0').rstrip('.') if Decimal(str(value))*100%1 else str(int(Decimal(str(value))*100))
    def valueFromText(self,text):return float(Decimal(text.strip().rstrip('%').strip())/100)
    def validate(self,text,pos):
        try:
            value=Decimal(text.strip().rstrip('%').strip())/100
            state=QValidator.State.Acceptable if value.is_finite() and Decimal(str(self.minimum()))<=value<=Decimal(str(self.maximum())) else QValidator.State.Intermediate
        except InvalidOperation:state=QValidator.State.Intermediate
        return state,text,pos

class ParameterDialog(QDialog):
    def __init__(self,parent,config,preset,trial=None,scope='',readonly=False,store_configs=None,intent='current'):
        super().__init__(parent);self.setWindowTitle('完整参数 · '+scope);self.resize(1050,760);self.config=copy.deepcopy(config);self.preset=copy.deepcopy(preset);self.action=None;self.repository=parent.work_repository
        self.initial_config=copy.deepcopy(config);self.readonly=readonly;self.managing=not scope.startswith('所选') and not readonly;self.store_configs=store_configs or {};self.intent=intent
        v=QVBoxLayout(self);v.addWidget(label(scope+(' · 只读，不应用或保存。' if readonly else ' · 方案保存/停用立即生效，不应用到门店。' if self.managing else ' · 未应用的编辑仅在此窗口；应用整个表单，预算和主从仓保留。多店以第一家显示，应用前逐店核对差异。'),'muted'))
        if intent=='defaults':v.itemAt(0).widget().setText(scope+' · 仅保存后续作业使用的默认；当前作业和历史签发不变。多店以第一家显示，保存前逐店核对完整差异。')
        if len(self.store_configs)>1 and any(c!=config for c in self.store_configs.values()):v.addWidget(label('注意：所选门店参数不完全相同；应用将统一完整参数，请核对每店差异。','section'))
        row=QHBoxLayout();self.choices=QComboBox();self.presets=self.repository.presets()
        for p in self.presets:self.choices.addItem(f"{p['name']} v{p['version']}"+(' · 内置已验收基线' if p['builtin'] else ' · 自定义，效果需试算'),p)
        row.addWidget(self.choices,1);self.load_btn=button('用此方案填充表单',self.load_selected);row.addWidget(self.load_btn);self.retire_btn=button('停用此方案版本',self.archive_selected);row.addWidget(self.retire_btn);self.retire_btn.setVisible(self.managing);self.load_btn.setVisible(not readonly);self.choices.setVisible(not readonly);v.addLayout(row)
        self.choices.currentIndexChanged.connect(lambda:self.retire_btn.setEnabled(bool(self.choices.currentData()) and self.choices.currentData()['id']!='standard'));self.retire_btn.setEnabled(bool(self.choices.currentData()) and self.choices.currentData()['id']!='standard')
        self.source_note=label('','muted');self.source_note.setWordWrap(True);v.addWidget(self.source_note)
        self.tabs=QTabWidget();v.addWidget(self.tabs,1)
        self.param_table=QTableWidget(0,4);self.param_table.setHorizontalHeaderLabels(['参数','当前值','单位','方案默认值']);self.param_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.param_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.param_table,'模型超参')
        self.weight_table=QTableWidget(0,5);self.weight_table.setHorizontalHeaderLabels(['分类方案','趋势敏感度','最近7天权重','此前7天权重','此前16天权重（自动）']);self.tabs.addTab(self.weight_table,'预测权重')
        self.profile_table=QTableWidget(0,4);self.profile_table.setHorizontalHeaderLabels(['分类方案','参数','分类值','使用分类值']);self.profile_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.profile_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.profile_table,'分类超参')
        self.mapping=QTableWidget(0,2);self.mapping.setHorizontalHeaderLabels(['商品分类（可编辑，可在末行新增）','权重方案']);self.mapping.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.mapping,'分类映射')
        self.warehouses=QTableWidget(0,3);self.warehouses.setHorizontalHeaderLabels(['仓','备货天数','到货天数']);self.warehouses.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.warehouses.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch);self.tabs.addTab(self.warehouses,'各仓周期')
        fixed=QTextBrowser();fixed.setPlainText(business_help(schema.FIXED));self.tabs.addTab(fixed,'固定规则')
        self.effective=QTableWidget(0,7);self.effective.setHorizontalHeaderLabels(['参数路径','标准默认','方案值','打开时门店值','当前编辑值','差异所在层','实际用途']);self.effective.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.effective.setColumnWidth(0,300);self.effective.setColumnWidth(5,200);self.effective.setColumnWidth(6,300);self.tabs.addTab(self.effective,'查看覆盖关系')
        self.effective_now=QTableWidget(0,4);self.effective_now.setHorizontalHeaderLabels(['参数','本次表单值（应用后）','适用范围','取值说明']);self.effective_now.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);self.effective_now.setColumnWidth(0,260);self.effective_now.setColumnWidth(1,160);self.effective_now.setColumnWidth(2,160);self.effective_now.horizontalHeader().setStretchLastSection(True);self.tabs.insertTab(0,self.effective_now,'有效值与适用范围')
        self.tabs.currentChanged.connect(self.tab_changed)
        self.help=QTextBrowser();self.help.setMaximumHeight(115);v.addWidget(self.help)
        self.param_table.currentCellChanged.connect(lambda r,*_:self.help.setPlainText(business_help(schema.HELP[list(schema.HELP)[r]][2])) if r>=0 else None)
        self.weight_table.currentCellChanged.connect(lambda r,c,*_:self.help.setPlainText(schema.WEIGHT_HELP.get(['','alpha','w1','w2',''][c], '此前16天权重=100%－最近7天权重－此前7天权重，无分类自动跟随快销；缺货时还按有效有货天数修正。')) if c>=0 else None)
        self.profile_table.currentCellChanged.connect(lambda r,*_:self.help.setPlainText('勾选时使用分类值，未勾选沿用模型超参。快销/日百各一套，两仓共用；无分类跟随快销。\n'+business_help(schema.HELP[self.profile_rows[r][1]][2])) if r>=0 and hasattr(self,'profile_rows') else None)
        self.warehouses.currentCellChanged.connect(lambda *_:self.help.setPlainText('各仓备货、到货天数优先于通用值；按门店实际补货与到货周期维护。修改只在点击应用后生效。'))
        policy_page=QWidget();policy_layout=QVBoxLayout(policy_page);policy_layout.addWidget(label('只维护补货策略开关；资质、拒收、禁用、停采仍是硬排除，不能在这里关闭。','muted'));self.tabs.addTab(policy_page,'补货策略')
        self.exception=QCheckBox('启用一箱例外（仍受准入与门店总预算约束）');policy_layout.addWidget(self.exception)
        self.policy_boxes={}
        for key,name in policy.LABELS.items():
            w=QCheckBox(name);w.setToolTip(policy.HELP[key]);self.policy_boxes[key]=w;policy_layout.addWidget(w)
        policy_layout.addStretch()
        self.note=label('','muted');v.addWidget(self.note)
        if self.managing:
            row=QHBoxLayout();row.addWidget(button('复制另存方案',lambda:self.save_preset(False)));row.addWidget(button('保存方案新版本',lambda:self.save_preset(True)));v.addLayout(row)
        if trial:v.addWidget(button('与本次所选门店配置试算',lambda:self.try_trial(trial)))
        row=QHBoxLayout();cancel=button('关闭' if self.managing or readonly else '放弃未应用编辑',self.reject);cancel.setDefault(True);row.addWidget(cancel);row.addStretch()
        if not self.managing and not readonly:
            if intent=='defaults':row.addWidget(button('保存为门店默认',lambda:self.finish('default'),True))
            elif intent=='plan':row.addWidget(button('采用到计划表单',lambda:self.finish('plan'),True))
            else:
                row.addWidget(button('应用并保存为门店默认',lambda:self.finish('default')))
                row.addWidget(button('应用到本次作业',lambda:self.finish('temporary'),True))
        v.addLayout(row)
        if not readonly:
            restore_row=QHBoxLayout()
            restore_row.addWidget(button('恢复选中项系统默认',lambda:self.restore_standard(False)))
            restore_row.addWidget(button('恢复当前组系统默认',lambda:self.restore_standard(True)))
            restore_row.addWidget(button('撤销本次表单修改',self.undo_form))
            v.addLayout(restore_row)
        self.fill(config)
        if not readonly:
            self.tabs.setTabVisible(self.tabs.indexOf(self.mapping),False)
        if preset.get('import_source'):
            info=preset['import_source'];entries=[['来源','文件',info.get('file','')]]
            for item in info.get('rows',[]):
                entries.extend([[f"{item.get('sheet','')} / 第{item.get('row','')}行",key,str(item['values'].get(key)) if item['values'].get(key) is not None else '空白'] for key in info.get('headers',list(item['values']))])
            entries.extend([['处理依据','说明',text] for text in info.get('decisions',[])])
            source=QTableWidget(len(entries),3);source.setHorizontalHeaderLabels(['来源行','原表字段','原值']);source.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);source.setColumnWidth(0,200);source.setColumnWidth(1,260);source.horizontalHeader().setSectionResizeMode(2,QHeaderView.ResizeMode.Stretch)
            source.setObjectName('parameterImportSource')
            for i,row in enumerate(entries):
                for j,value in enumerate(row):source.setItem(i,j,QTableWidgetItem(value))
            self.tabs.addTab(source,'导入来源（原表留档）')
        if readonly:
            self.exception.setEnabled(False)
            for w in self.policy_boxes.values():w.setEnabled(False)
            for table in (self.param_table,self.weight_table,self.profile_table,self.mapping,self.warehouses):
                table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
                for w in table.findChildren(QDoubleSpinBox)+table.findChildren(QComboBox)+table.findChildren(QCheckBox):w.setEnabled(False)
    def tab_changed(self,index):
        current=self.tabs.widget(index)
        if current in (self.effective,self.effective_now):self.refresh_effective()
        elif current is self.profile_table and hasattr(self,'profile_rows'):
            row=max(0,self.profile_table.currentRow());key=self.profile_rows[row][1]
            self.help.setPlainText('分类值优先于门店通用值；快销/日百各一套，两仓共用，无分类跟随快销。未勾选则沿用门店值。\n'+business_help(schema.HELP[key][2]))
        elif current.objectName()=='parameterImportSource':self.help.setPlainText('这里保留当时完整注入的原表字段和处理依据。后续编辑不会回写此来源；当前实际值请在“生效核对”中查看。')
    def undo_form(self):
        from interaction_ui import confirm
        if confirm(self,'撤销本次表单修改','恢复打开编辑器时的值；不保存门店默认或修改计划。'):
            self.fill(self.initial_config)
    def restore_standard(self,whole):
        from system_standard import restore,VERSION
        from interaction_ui import review_rows
        from interaction_rules import configuration_changes
        current=self.tabs.currentWidget()
        groups={self.param_table:'parameters',self.weight_table:'weight_profiles',self.profile_table:'profile_parameters',self.mapping:'category_profiles',self.warehouses:'warehouses'}
        group=groups.get(current)
        if not group:QMessageBox.information(self,'请选择参数分组','请进入通用参数、分类权重、分类超参、分类映射或各仓周期页。');return
        try:
            before=self.value();key=None;r=current.currentRow()
            if not whole:
                if r<0:raise ValueError('请先选择要恢复的参数行')
                if group=='parameters':key=list(schema.HELP)[r]
                elif group=='profile_parameters':key=self.profile_rows[r]
                elif group=='weight_profiles':
                    col=current.currentColumn()
                    if col not in (1,2,3):raise ValueError('请选择趋势敏感度或可编辑的权重列')
                    key=(current.item(r,0).text(),('alpha','w1','w2')[col-1])
                else:key=current.item(r,0).text().strip()
            after=restore(before,group,key);schema.validate(after)
            rows=configuration_changes({'当前表单':before},after)
            if review_rows(self,'恢复系统默认',['范围','参数','原值','新值'],rows,'系统标准 '+VERSION+'；仅修改当前表单，应用或保存前不影响其他位置。','恢复到表单'):
                index=self.tabs.currentIndex();self.fill(after);self.tabs.setCurrentIndex(index)
                self.editor_baseline=copy.deepcopy(self.initial_config)
        except ValueError as exc:QMessageBox.warning(self,'未恢复',str(exc))
    def is_edited(self):
        try:return self.value()!=self.editor_baseline
        except ValueError:return True
    def reject(self):
        from interaction_ui import confirm
        if not self.readonly and self.is_edited() and not confirm(self,'放弃未应用编辑','只丢弃当前未应用的表单编辑。已单独保存的方案版本仍保留。'):return
        super().reject()
    def spin(self,value,maximum=1000000000,percent=False):
        w=PercentInput() if percent else NumberInput();w.setDecimals(8);w.setRange(0,maximum);w.setSingleStep(.01 if percent else .1);w.setValue(value)
        if percent:w.setSuffix('%')
        return w
    def refresh_effective(self):
        from parameter_view import comparison,display_path
        try:rows=comparison(schema.standard(),self.preset.get('base_config',self.preset['config']),self.initial_config,self.value())
        except ValueError as exc:self.note.setText('当前参数无效，不能应用：'+str(exc));self.effective.setRowCount(0);return
        source=self.preset.get('plan_source')
        title='补货计划：'+source['name'] if source else '参数依据：'+self.preset.get('name','系统标准')
        changed=sum(row[3]!=row[4] for row in rows)
        self.source_note.setText(title+' · '+('当时保存的完整值，只读' if self.readonly else f'本次表单修改 {changed} 项；点击应用后才生效')+'。分类超参两仓共用，周期按仓设置；预算和供货关系分别维护。')
        expanded=[]
        for row in rows:
            if row[0].startswith('warehouses / '):
                for n,part in enumerate(('备货天数','到货天数')):expanded.append([row[0]+' / '+part,*[x[n] if isinstance(x,(list,tuple)) and len(x)>n else x for x in row[1:5]],*row[5:]])
            else:expanded.append(row)
        self.effective.setRowCount(len(expanded));self.effective_now.setRowCount(len(expanded))
        for i,row in enumerate(expanded):
            key=row[0].split(' / ')[-1]
            for j,value in enumerate(row):
                item=QTableWidgetItem(display_path(value) if j==0 else value_text(value,key));item.setToolTip(str(value));item.setData(Qt.ItemDataRole.UserRole,value);self.effective.setItem(i,j,item)
            scope=' / '.join(display_path(row[0]).split(' / ')[:-1])
            from business_presenter import field_unit,PERCENT
            unit='天' if key in ('备货天数','到货天数') else field_unit(key)
            shown=value_text(row[4],key)+((' '+unit) if unit and key not in PERCENT and isinstance(row[4],(int,float)) and not isinstance(row[4],bool) else '')
            for j,value in enumerate([display_path(row[0]).split(' / ')[-1],shown,scope,row[6]]):self.effective_now.setItem(i,j,QTableWidgetItem(value))
        self.note.setText('按当前值比较覆盖层，不推断历史操作人。各仓周期优先；分类权重在主从仓归属后重新应用。未点击应用不会修改门店。')
    def fill(self,config):
        self.config=copy.deepcopy(config);base=self.preset.get('base_config',self.preset['config']);self.param_table.setRowCount(len(schema.HELP))
        for i,(key,(name,unit,helptext)) in enumerate(schema.HELP.items()):
            self.param_table.setRowHidden(i,key in {'depth','lead','extreme_days'})
            helptext=business_help(helptext)
            self.param_table.setItem(i,0,QTableWidgetItem(name+('（在各仓周期页维护）' if key in {'depth','lead'} else '')));w=self.spin(config['parameters'][key],percent=key=='probability');w.setEnabled(key not in {'extreme_days','depth','lead'});self.param_table.setCellWidget(i,1,w);self.param_table.setItem(i,2,QTableWidgetItem('%' if key=='probability' else '库存单位' if key in ('strict_monthly','exit_monthly') else unit));self.param_table.setItem(i,3,QTableWidgetItem(value_text(base['parameters'][key],key)));self.param_table.item(i,0).setToolTip(helptext+'\n原字段：'+key)
            w.valueChanged.connect(lambda _,text=helptext:self.help.setPlainText(text))
        self.weight_table.setRowCount(len(config['weight_profiles']))
        for i,(name,weights) in enumerate(config['weight_profiles'].items()):
            item=QTableWidgetItem(name);item.setFlags(item.flags()&~Qt.ItemFlag.ItemIsEditable);self.weight_table.setItem(i,0,item)
            for j,key in enumerate(['alpha','w1','w2'],1):
                w=self.spin(weights[key],1 if key!='alpha' else 100,percent=key!='alpha');self.weight_table.setCellWidget(i,j,w);w.valueChanged.connect(self.weight_changed)
            derived=QTableWidgetItem('');derived.setFlags(derived.flags()&~Qt.ItemFlag.ItemIsEditable);self.weight_table.setItem(i,4,derived)
        self.weight_changed();mapping=list(config['category_profiles'].items());self.mapping.setRowCount(len(mapping)+1)
        self.profile_rows=[(name,key) for name in config['weight_profiles'] for key in schema.PROFILE_KEYS]
        self.profile_table.setRowCount(len(self.profile_rows))
        for i,(name,key) in enumerate(self.profile_rows):
            values=config.get('profile_parameters',{}).get(name,{})
            self.profile_table.setItem(i,0,QTableWidgetItem(name));self.profile_table.setItem(i,1,QTableWidgetItem(schema.HELP[key][0]))
            w=self.spin(values.get(key,config['parameters'][key]),1 if key=='probability' else 1000000000,percent=key=='probability');w.setEnabled(key in values);self.profile_table.setCellWidget(i,2,w)
            if key=='probability':w.setMinimum(.5);w.setMaximum(.99);w.setSingleStep(.01)
            check=QCheckBox('启用');check.setChecked(key in values);check.toggled.connect(w.setEnabled);self.profile_table.setCellWidget(i,3,check)
        for i in range(len(mapping)+1):
            self.mapping.setItem(i,0,QTableWidgetItem(mapping[i][0] if i<len(mapping) else ''));combo=QComboBox();combo.addItems(list(config['weight_profiles']));combo.setCurrentText(mapping[i][1] if i<len(mapping) else '快销');self.mapping.setCellWidget(i,1,combo)
        self.warehouses.setRowCount(len(config['warehouses']))
        for i,(wh,values) in enumerate(config['warehouses'].items()):
            self.warehouses.setItem(i,0,QTableWidgetItem(wh))
            for j,value in enumerate(values,1):self.warehouses.setCellWidget(i,j,self.spin(max(value,0),365))
        self.exception.setChecked(config['exception']);self.note.setText(f"来源：{self.preset['name']} v{self.preset['version']}。分类映射空白行不保存。")
        for key,value in policy.values(config).items():self.policy_boxes[key].setChecked(value)
        self.param_table.setCurrentCell(0,0);self.editor_baseline=copy.deepcopy(self.value());self.refresh_effective();self.tabs.setCurrentWidget(self.effective_now);self.effective_now.setHorizontalHeaderLabels(['参数','当时值' if self.readonly else '本次表单值（应用后）','适用范围','取值说明'])
    def weight_changed(self):
        for i in range(self.weight_table.rowCount()):
            if self.weight_table.cellWidget(i,2) and self.weight_table.cellWidget(i,3) and self.weight_table.item(i,4):self.weight_table.item(i,4).setText(value_text(round(1-self.weight_table.cellWidget(i,2).value()-self.weight_table.cellWidget(i,3).value(),8),'w3'))
    def value(self):
        c=copy.deepcopy(self.config);c['parameters']={k:self.param_table.cellWidget(i,1).value() for i,k in enumerate(schema.HELP)}
        c['weight_profiles']={self.weight_table.item(i,0).text():{key:self.weight_table.cellWidget(i,j).value() for j,key in enumerate(['alpha','w1','w2'],1)} for i in range(self.weight_table.rowCount())}
        overrides={}
        for i,(name,key) in enumerate(self.profile_rows):
            if self.profile_table.cellWidget(i,3).isChecked():overrides.setdefault(name,{})[key]=self.profile_table.cellWidget(i,2).value()
        if overrides:c['profile_parameters']=overrides
        else:c.pop('profile_parameters',None)
        c['category_profiles']={}
        for i in range(self.mapping.rowCount()):
            name=self.mapping.item(i,0).text().strip()
            if name:
                if name in c['category_profiles']:raise ValueError('分类重复：'+name)
                c['category_profiles'][name]=self.mapping.cellWidget(i,1).currentText()
        c['warehouses']={self.warehouses.item(i,0).text():[self.warehouses.cellWidget(i,j).value() for j in [1,2]] for i in range(self.warehouses.rowCount())};c['exception']=self.exception.isChecked()
        return schema.validate(policy.patch(c,{k:w.isChecked() for k,w in self.policy_boxes.items()}))
    def load_selected(self):
        from interaction_ui import confirm
        if self.is_edited() and not confirm(self,'替换当前表单编辑','将用所选方案替换未应用的编辑；不会立即应用到门店。'):return
        if self.choices.currentData():self.preset=copy.deepcopy(self.choices.currentData());self.fill(self.preset['config'])
    def archive_selected(self):
        p=self.choices.currentData()
        if not self.managing or not p or p['id']=='standard':return
        from interaction_ui import confirm
        if not confirm(self,'停用参数方案',f"停用 {p['name']} v{p['version']}？\n仅从可选方案中停用，已绑定门店和历史快照保留。可在参数设置中恢复停用方案。"):return
        try:self.repository.archive_preset(p['id'],p['version'])
        except Exception as exc:QMessageBox.warning(self,'未归档',str(exc));return
        self.choices.removeItem(self.choices.currentIndex());self.note.setText('已从方案选择列表归档；已绑定的门店和历史快照仍保留该版本。')
    def save_preset(self,update):
        if not self.managing:return
        try:c=self.value()
        except ValueError as exc:QMessageBox.warning(self,'参数未保存',str(exc));return
        if update and self.preset['id']=='standard':QMessageBox.information(self,'标准只读','请使用“复制另存方案”。');return
        name,ok=QInputDialog.getText(self,'保存完整方案','方案名称',text=self.preset['name'] if update else self.preset['name']+' 副本')
        if not ok:return
        try:self.preset=self.repository.save_preset(name,c,self.preset['id'] if update else None)
        except Exception as exc:QMessageBox.warning(self,'未保存',str(exc));return
        self.fill(c);self.note.setText(f"已保存 {self.preset['name']} v{self.preset['version']}；未自动应用到其他门店。")
    def finish(self,action):
        try:edited=self.value()
        except ValueError as exc:QMessageBox.warning(self,'参数需调整',str(exc));return
        if self.store_configs:
            from interaction_rules import configuration_changes
            from interaction_ui import review_rows
            rows=configuration_changes(self.store_configs,edited)
            target='应用并保存为门店默认' if action=='default' else '仅应用到本次门店'
            message=f'{len(self.store_configs)} 家门店；共 {len(rows)} 项变化。预算和主从仓不变。'+('同时保存门店默认；每日草稿需另存。' if action=='default' else '不保存门店默认；每日草稿需另存。')
            if self.intent=='defaults':target='保存为门店默认';message=f'{len(self.store_configs)} 家门店；共 {len(rows)} 项变化。仅供后续作业带入，当前作业和历史签发不变；预算和主从仓保留。'
            if not review_rows(self,'逐店核对完整参数变化',['门店','参数','原值','新值'],rows,message,target):return
        self.config=edited;self.action=action;self.accept()
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
        from parameter_maintenance_ui import edit_stores
        if codes and not self.readonly:edit_stores(self,self,codes,scope='current')
    def apply_parameters(self,codes,config,preset,persist=False):
        schema.validate(config)
        for s in codes:
            self.store_presets[s]={**copy.deepcopy(preset),'base_config':copy.deepcopy(preset.get('base_config',preset['config'])),'config':copy.deepcopy(config)};self.overrides[s]=copy.deepcopy(config['warehouses']);self.refresh_parameter_label(s)
        self.invalidate(codes);self.selection_changed()
        if persist:self.save_store_defaults(codes)
    def refresh_parameter_label(self,s):
        p=self.preset_for(s);changed=json.loads(json.dumps(p['config']))!=json.loads(json.dumps(p.get('base_config',p['config'])))
        import store_defaults
        saved=getattr(self,'saved_store_defaults',{}).get(s)
        state='历史快照' if self.readonly or getattr(self,'_restoring',False) else '已保存默认' if store_defaults.matches(saved,p,self.config_for(s),self.store_ui(s)) else '本次与已保存默认不同' if saved else '仅本次 · 尚无门店默认'
        from parameter_maintenance import common_is_default
        common='默认通用方案' if common_is_default(self.config_for(s)) else '门店自定义通用参数'
        text=f"本次采用：{p['name']}"+(' · 本店调整' if changed else '')+' · '+state+'\n'+common+'；'+policy.summary(self.config_for(s))
        w=self.store_widgets[s];w['params'].setText('编辑参数');w['params'].setToolTip('编辑此店完整参数；不自动保存每日草稿')
        if 'param_status' in w:w['param_status'].setText(text);w['param_status'].setToolTip(text)
    def view_parameters(self,store):
        if store not in self.store_widgets:return
        d=ParameterDialog(self,self.config_for(store),self.preset_for(store),scope='本店实际生效参数 · '+store,readonly=True);d.exec()
    def save_default_dialog(self):
        codes=self.selected_codes()
        if not codes:return
        d=QDialog(self);d.setObjectName('storeDefaultsConfirm');d.setWindowTitle('保存门店主从仓与完整参数');v=QVBoxLayout(d)
        v.addWidget(label(f'将保存所选 {len(codes)} 家门店的主从仓、各仓周期、完整参数方案及本店调整。下次导入相同门店编码时自动带入；不修改历史单。'))
        budget=QCheckBox('同时将本次预算保存为门店默认预算（不勾选则保留原默认预算）');v.addWidget(budget)
        row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button(f'保存为 {len(codes)} 店默认',d.accept,True));v.addLayout(row)
        if d.exec()==QDialog.DialogCode.Accepted:self.save_store_defaults(codes,include_budget=budget.isChecked())
    def save_store_defaults(self,codes=None,*,include_budget=False):
        codes=self.selected_codes() if codes is None or isinstance(codes,bool) else codes
        if not codes:return
        import store_defaults,work_repository as wr
        try:
            old=self.work_repository.stores();stamp=wr.now()
            values={s:store_defaults.record(self.preset_for(s),self.config_for(s),self.store_ui(s),old.get(s),include_budget=include_budget,saved=stamp) for s in codes}
            self.work_repository.save_stores(values);self.saved_store_defaults={**old,**values}
        except Exception as exc:self.failed('门店默认未保存：'+str(exc));return
        for s in codes:self.refresh_parameter_label(s)
        self.status.setText(f'已保存 {len(codes)} 家门店的主从仓与完整参数，下一次自动带入 · {stamp}。'+('预算也已保存。' if include_budget else '默认预算未改变。')+'历史单不变，当前作业需单独保存。')
    def trial_parameters(self,codes,config,dialog):
        if self.worker or self.input_dirty:raise ValueError('请先完成数据检查，等待当前任务结束')
        import parameter_trial
        jobs={s:self.selected_settings(s) for s in codes};dialog.note.setText('正在使用同一份数据试算，不修改当前结果或作业记录…')
        def done(report):
            if not dialog.isVisible():self.status.setText('试算已结束，未应用到正式结果。');return
            dialog.note.setText('试算完成，未修改正式结果。');view=QDialog(dialog);view.setWindowTitle('同快照参数对比 · 未应用');view.resize(1000,720);v=QVBoxLayout(view);text=QTextBrowser();text.setPlainText(report);v.addWidget(text);v.addWidget(button('关闭',view.accept));view.exec()
        self.job(parameter_trial.compare,(self.loaded,jobs,self.profile,config),done)
