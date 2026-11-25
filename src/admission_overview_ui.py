"""Two focused business views for current qualifications and refusal lists."""
from PySide6.QtCore import QDate,QTimer,Qt,Signal
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QDateEdit,QFormLayout,QLineEdit,QMessageBox,QPlainTextEdit
from ui_controls import label,button
from business_ui import grid,run,GridModel
import admission_overview as overview


class RefusalModel(GridModel):
    checked_changed=Signal()
    def __init__(self,parent):
        self.checked=set();self.enabled=True
        super().__init__(['勾选',*overview.REF_HEADERS],parent=parent)
    def replace(self,rows):
        self.checked.clear();super().replace(rows);self.checked_changed.emit()
    def key(self,index):
        r=self.rows[self.indices[index.row()]];return r[0],r[2]
    def data(self,index,role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():return None
        if index.column()==0:
            if role==Qt.ItemDataRole.CheckStateRole:return Qt.CheckState.Checked if self.key(index) in self.checked else Qt.CheckState.Unchecked
            if role==Qt.ItemDataRole.ToolTipRole:return '勾选要维护的门店商品'
            return None
        return super().data(self.index(index.row(),index.column()-1),role)
    def flags(self,index):
        f=super().flags(index)
        if index.isValid() and index.column()==0 and self.enabled:f|=Qt.ItemFlag.ItemIsUserCheckable
        return f
    def setData(self,index,value,role=Qt.ItemDataRole.EditRole):
        if not index.isValid() or index.column()!=0 or role!=Qt.ItemDataRole.CheckStateRole or not self.flags(index)&Qt.ItemFlag.ItemIsUserCheckable:return False
        key=self.key(index)
        if value in (Qt.CheckState.Checked,Qt.CheckState.Checked.value):self.checked.add(key)
        else:self.checked.discard(key)
        self.dataChanged.emit(index,index,[role]);self.checked_changed.emit();return True
    def select_all(self,checked):
        self.checked={(r[0],r[2]) for r in self.rows} if checked and self.enabled else set()
        if self.rowCount():self.dataChanged.emit(self.index(0,0),self.index(self.rowCount()-1,0),[Qt.ItemDataRole.CheckStateRole])
        self.checked_changed.emit()
    def keys(self):return [(r[0],r[2]) for r in self.rows if (r[0],r[2]) in self.checked]


def known_names(host):
    return {s:host.states.get(s,{}).get('name','') for s in host.store_widgets}


class BulkRefusalFilters(QDialog):
    def __init__(self, parent, codes):
        super().__init__(parent)
        self.setObjectName('bulkRefusalFilters')
        self.setWindowTitle('批量筛选拒收清单')
        self.resize(590, 355)
        form = QFormLayout(self)
        note = label('可粘贴 Excel 的编码列，也可用空格、逗号或换行分隔。同组匹配任一编码，两组同时匹配。')
        note.setWordWrap(True)
        form.addRow(note)
        self.stores = QPlainTextEdit('\n'.join(sorted(codes[0])))
        self.products = QPlainTextEdit('\n'.join(sorted(codes[1])))
        form.addRow('门店编码', self.stores)
        form.addRow('商品编码', self.products)
        form.addRow(button('取消', self.reject), button('应用批量筛选', self.apply, True))

    def apply(self):
        try:
            self.codes = tuple(overview.selection_codes(widget.toPlainText()) for widget in (self.stores, self.products))
        except ValueError as exc:
            QMessageBox.warning(self, '编码无法使用', str(exc))
            return
        self.accept()


class BulkRefusalEdit(QDialog):
    def __init__(self, parent, count, day):
        super().__init__(parent)
        self.setObjectName('bulkRefusalEdit')
        self.setWindowTitle('批量修改拒收记录')
        self.resize(550, 230)
        form = QFormLayout(self)
        form.addRow(label(f'已勾选 {count} 条。填写需要改动的字段。'))
        self.reason = QLineEdit()
        self.reason.setPlaceholderText('留空沿用各条原原因')
        form.addRow('拒收原因', self.reason)
        self.expiry = QComboBox()
        for title, value in [('沿用原到期日', None), ('设置统一到期日', 'set'), ('清除到期日', 'clear')]:
            self.expiry.addItem(title, value)
        self.date = QDateEdit(QDate.fromString(day, 'yyyy-MM-dd'))
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat('yyyy-MM-dd')
        form.addRow('到期日', self.expiry)
        form.addRow('新到期日', self.date)
        self.next = button('核对改动', self.accept, True)
        form.addRow(button('取消', self.reject), self.next)
        self.expiry.currentIndexChanged.connect(self.update_fields)
        self.reason.textChanged.connect(self.update_fields)
        self.update_fields()

    def update_fields(self, *_):
        action = self.expiry.currentData()
        self.date.setEnabled(action == 'set')
        self.next.setEnabled(bool(self.reason.text().strip()) or action is not None)

    def fields(self):
        values = {}
        if reason := self.reason.text().strip():
            values['reason'] = reason
        if action := self.expiry.currentData():
            values['expires'] = self.date.date().toString('yyyy-MM-dd') if action == 'set' else None
        return values


class Overview(QDialog):
    def __init__(self,parent,host,kind='qualification',focus=None):
        super().__init__(parent);self.host=host;self.kind=kind;self.focus=focus;self.data=None;self.filtered=[]
        self.setObjectName('admissionOverview');self.title='二类医疗资质' if kind=='qualification' else '门店拒收清单'
        self.setWindowTitle(self.title+' · 查看现状');self.resize(1240,760)
        v=QVBoxLayout(self);v.addWidget(label(self.title,'title'))
        v.addWidget(label('两仓共用。查看和导出不修改资料；维护后影响后续安排，历史单保留。','muted'))
        self.summary=label('正在准备查询…','section');self.summary.setWordWrap(True);v.addWidget(self.summary)
        row=QHBoxLayout()
        if kind=='refusals':
            fields=QHBoxLayout()
            self.search_fields=[]
            for title,hint in [('门店','门店名称或编码'),('商品','商品名称或编码'),('拒收原因','原因关键词')]:
                field=QLineEdit();field.setPlaceholderText(hint);field.setAccessibleName(title)
                self.search_fields.append(field);fields.addWidget(label(title));fields.addWidget(field,1)
            v.addLayout(fields)
            self.match_codes=(frozenset(),frozenset())
            row.addWidget(label('状态'))
        else:
            self.search=QLineEdit();self.search.setPlaceholderText('搜索门店名称 / 编码 / 核验人');row.addWidget(self.search,1)
        self.state=QComboBox();self.state.addItems(['全部','待核验','无资质','有效','已到期'] if kind=='qualification' else ['拒收中','全部','已到期','已解除']);row.addWidget(self.state)
        if kind=='refusals':
            self.batch_filter_btn=button('批量筛选',self.batch_filter);row.addWidget(self.batch_filter_btn)
            row.addWidget(button('清空筛选',self.reset_filters));row.addStretch()
        row.addWidget(button('查询 / 刷新',self.reload));self.clear_focus=button('显示全部记录',self.show_all);row.addWidget(self.clear_focus);v.addLayout(row)
        self.scope_note=label('','muted');self.scope_note.setWordWrap(True);v.addWidget(self.scope_note)
        self.table,self.model=grid(self,overview.QUAL_HEADERS if kind=='qualification' else overview.REF_HEADERS)
        widths=[145,200,115,95,95,115,115,105,190] if kind=='qualification' else [140,180,150,180,220,115,100]
        for i,width in enumerate(widths):self.table.setColumnWidth(i,width)
        self.table.doubleClicked.connect(lambda _:self.details());v.addWidget(self.table,1)
        row=QHBoxLayout();row.addWidget(button('查看所选详情',self.details));self.edit_btn=button('维护所选门店' if kind=='qualification' else '维护所选记录',self.edit);row.addWidget(self.edit_btn)
        if kind=='refusals':
            self.add_btn=button('选择门店添加拒收',self.add);row.addWidget(self.add_btn)
        self.import_btn=button('导入'+self.title,self.import_file);row.addWidget(self.import_btn)
        row.addWidget(button('下载导入模板',self.template))
        row.addWidget(button('导出资质维护表' if kind=='qualification' else '导出当前筛选',self.export_filtered))
        row.addWidget(button('关闭',self.reject));v.addLayout(row)
        self.debounce=QTimer(self);self.debounce.setSingleShot(True);self.debounce.setInterval(150);self.debounce.timeout.connect(self.filter)
        for field in self.search_fields if kind=='refusals' else (self.search,):
            field.textChanged.connect(lambda _:self.debounce.start());field.returnPressed.connect(self.filter)
        self.state.currentIndexChanged.connect(self.filter)
        self._filter_signature=None
        if kind=='refusals':
            self.model=RefusalModel(self);self.table.setModel(self.model);self.table.setColumnWidth(0,55)
            for i,width in enumerate(widths,1):self.table.setColumnWidth(i,width)
            line=QHBoxLayout();line.addWidget(button('勾选当前筛选',lambda:self.select_all(True)));line.addWidget(button('清空勾选',lambda:self.select_all(False)))
            self.selection_note=label('');self.selection_note.setWordWrap(True);line.addWidget(self.selection_note,1)
            self.edit_checked_btn=button('批量修改',lambda:self.change_checked('edit'));line.addWidget(self.edit_checked_btn)
            self.delete_btn=button('批量删除',lambda:self.change_checked('delete'));line.addWidget(self.delete_btn)
            self.release_btn=button('批量解除勾选拒收',self.release_checked);line.addWidget(self.release_btn);v.insertLayout(v.count()-1,line)
            self.model.checked_changed.connect(self.update_selection);self.update_selection()

    def reload(self):
        try:
            data=run(self,overview.load,(self.host.work_repository.path,known_names(self.host)),'查询门店准入现状')
            self.data=data;self.filter();self.update_controls()
        except Exception as exc:
            self.data=None;self.filtered=[];self.model.replace([]);self.summary.setText('查询未完成，不能把空列表视为无记录。')
            QMessageBox.warning(self,'现状未能读取',str(exc))

    def update_controls(self):
        enabled=not self.host.readonly and not self.host.worker
        self.edit_btn.setEnabled(enabled);self.import_btn.setEnabled(enabled)
        if hasattr(self,'add_btn'):self.add_btn.setEnabled(enabled)
        if self.kind=='refusals':self.model.enabled=enabled;self.update_selection()

    def filter(self,*_):
        if not self.data:return
        allrows=self.data[self.kind];state=self.state.currentText()
        conditions=tuple(w.text() for w in self.search_fields)+self.match_codes if self.kind=='refusals' else (self.search.text(),)
        signature=(id(self.data),state,conditions,frozenset(self.focus) if self.focus is not None else None)
        if signature!=self._filter_signature:
            if self.kind=='refusals':self.filtered=overview.select_refusals(allrows,state,self.focus,*conditions)
            else:self.filtered=overview.select_rows(allrows,self.kind,state,*conditions,focus=self.focus)
            self.model.replace(self.filtered);self._filter_signature=signature
            if self.filtered:self.table.selectRow(0)
        self.clear_focus.setVisible(self.focus is not None)
        col=2 if self.kind=='qualification' else 6;states=['有效','待核验','无资质','已到期'] if self.kind=='qualification' else ['拒收中','已到期','已解除']
        counts=' · '.join(f'{s} {sum(r[col]==s for r in allrows)}'+(' 家' if self.kind=='qualification' else ' 条') for s in states)
        self.summary.setText(counts)
        scope='本次导入涉及记录' if self.focus is not None else '全部已知门店' if self.kind=='qualification' else '全部拒收记录'
        self.scope_note.setText(f'查询日 {self.data["day"]} · {scope} · 当前显示 {len(self.filtered)} / {len(allrows)} 条。'+(' 未登记按待核验展示。' if self.kind=='qualification' else ' 默认只看拒收中，可切换已到期、已解除。')+'\n'+'\n'.join(self.data['warnings']))

    def update_selection(self):
        keys=self.model.keys();self.selection_note.setText(f'已勾选 {len(keys)} 条 · {len({s for s,_ in keys})} 家门店；筛选或刷新后清空勾选。')
        enabled=bool(keys) and not self.host.worker and not self.host.readonly
        self.edit_checked_btn.setEnabled(enabled);self.delete_btn.setEnabled(enabled)
        can_release=all(row[6]!='已解除' for row in self.filtered if (row[0],row[2]) in self.model.checked)
        self.release_btn.setEnabled(enabled and can_release)

    def select_all(self,checked):
        self.debounce.stop();self.filter();self.model.select_all(checked)

    def release_checked(self):
        self.change_checked('release')

    def change_checked(self, action):
        if self.host.worker or self.host.readonly:return
        self.debounce.stop();self.filter();keys=self.model.keys()
        if not keys:QMessageBox.information(self,'请重新勾选','请先勾选需要处理的记录。');return
        label_text=overview.REFUSAL_ACTIONS[action]
        fields=None
        if action=='edit':
            form=BulkRefusalEdit(self,len(keys),self.data['day'])
            if form.exec()!=QDialog.DialogCode.Accepted:return
            fields=form.fields()
        from interaction_ui import review_rows
        from admission_ui import invalidate
        try:
            prepared=run(self,overview.prepare_refusal_change,(self.data,keys,action,fields),f'准备批量{label_text}预览')
            if not prepared['count']:
                self.selection_note.setText('勾选记录的字段已与填写值一致。');return
            headers=overview.REFUSAL_EDIT_HEADERS if action=='edit' else overview.REFUSAL_END_HEADERS
            note=f'勾选 {len(keys)} 条，将{label_text} {prepared["count"]} 条，涉及 {len(prepared["stores"])} 家门店。'
            if action=='delete':note+='删除后从清单移除，维护日志保留原记录。'
            else:note+='请核对下方每条记录的变动。'
            if not review_rows(self,f'核对批量{label_text}拒收',headers,prepared['rows'],note,f'确认批量{label_text}'):return
            result=run(self,overview.commit_refusal_change,(self.host.work_repository.path,prepared),f'保存批量{label_text}',cancellable=False)
        except Exception as exc:QMessageBox.warning(self,'未完成批量'+label_text,str(exc));return
        invalidate(self.host,result['stores']);self.reload()
        self.selection_note.setText(f'已{label_text} {result["count"]} 条 · {len(result["stores"])} 家门店。')

    def batch_filter(self):
        dialog=BulkRefusalFilters(self,self.match_codes)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            self.set_batch_codes(dialog.codes)
            self.filter()

    def set_batch_codes(self,codes):
        self.match_codes=codes
        stores,products=map(len,codes)
        self.batch_filter_btn.setText(f'批量筛选（{stores} 店 / {products} 品）' if stores or products else '批量筛选')

    def clear_queries(self):
        if self.kind=='refusals':
            for field in self.search_fields:field.clear()
            self.set_batch_codes((frozenset(),frozenset()))
        else:self.search.clear()
        self.debounce.stop()

    def reset_filters(self):
        self.clear_queries();self.state.setCurrentIndex(0);self.filter();self.model.select_all(False)

    def show_all(self):self.focus=None;self.clear_queries();self.filter()
    def selected(self):
        index=self.table.currentIndex().row()
        if 0<=index<len(self.filtered):return self.filtered[index]
        QMessageBox.information(self,'请选择记录','请先选择要查看或维护的门店记录。')
        return None
    def details(self):self.open_selected(True)
    def edit(self):self.open_selected(False)
    def open_selected(self,readonly):
        row=self.selected()
        if row is None:return
        if not readonly and (self.host.worker or self.host.readonly):
            QMessageBox.information(self,'暂不能维护','请结束当前任务；历史作业只读。');return
        from admission_ui import Editor
        Editor(self,self.host,row[0],readonly=readonly,section=self.kind,name=row[1],sku=row[2] if self.kind=='refusals' else None).exec()
        if not readonly:self.reload()
    def add(self):
        from admission_ui import open_editor
        open_editor(self,self.host,section='refusals');self.reload()
    def template(self):
        from import_ui import download
        download(self,'store_qualification' if self.kind=='qualification' else 'store_refusals')
    def import_file(self):
        from admission_ui import import_records
        result=import_records(self,self.host,'store_qualification' if self.kind=='qualification' else 'store_refusals',show_saved=False)
        if result:
            self.focus=set(result['keys']);self.clear_queries();self.state.setCurrentText('全部');self.reload()
    def export_filtered(self):
        if not self.data:return
        self.debounce.stop();self.filter()  # Include the last keystroke, not stale debounce output.
        if not self.filtered:QMessageBox.information(self,'没有可导出记录','当前筛选为空，请调整条件。');return
        import export_locations
        path,_=export_locations.save_file(self,'导出'+self.title,self.title+'_'+self.data['day']+'.xlsx','Excel (*.xlsx)')
        if not path:return
        if not path.lower().endswith('.xlsx'):path+='.xlsx'
        scope={'查询日期':self.data['day'],'状态筛选':self.state.currentText(),'记录范围':'本次导入涉及记录' if self.focus is not None else '全部记录','条数':len(self.filtered)}
        if self.kind=='refusals':
            scope.update({title:field.text() for title,field in zip(('门店搜索','商品搜索','拒收原因搜索'),self.search_fields)})
            scope.update({title:'、'.join(sorted(codes)) for title,codes in zip(('批量门店编码','批量商品编码'),self.match_codes)})
        else:scope['搜索条件']=self.search.text()
        try:
            qualification=self.kind=='qualification'
            run(self,overview.export,(path,self.kind,[r[:] for r in self.filtered],scope),'导出资质维护表' if qualification else '导出准入查询结果')
            message=f'已导出当前筛选 {len(self.filtered)} 条。'
            self.scope_note.setText(message+('可修改「门店资质」页后直接导回。' if qualification else f'查询日 {self.data["day"]}。'))
        except Exception as exc:QMessageBox.warning(self,'未完成导出',str(exc))


def open_overview(parent,host,kind='qualification',focus=None):
    d=Overview(parent,host,kind,focus)
    if focus is not None:d.state.setCurrentText('全部')
    d.reload();d.exec()
