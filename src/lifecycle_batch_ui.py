"""One continuous workspace for file recognition, association and atomic save."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QStackedWidget,QWidget,
    QTableWidget,QTableWidgetItem,QComboBox,QFileDialog,QMessageBox,QPushButton,QCompleter,QCheckBox,QLineEdit,QPlainTextEdit)
from ui_controls import button,label
from business_ui import run
from runtime_support import Cancelled
import lifecycle_batch as batch
import lifecycle_io as io
import order_lifecycle as service
import lifecycle_diagnostics as diagnostics


class BatchImport(QDialog):
    def __init__(self,parent,host,stage=None,scope=None):
        super().__init__(parent);self.host=host;self.stage=stage;self.scope=scope
        self.paths=[];self.documents=[];self.issues=[];self.excluded=[];self.heads=[];self.receipt=None;self.review=None;self.busy=False
        self.payment_index=None;self.read_ready=False
        self.setObjectName('lifecycleBatchImport');self.setWindowTitle('批量导入单据');self.resize(1240,820);self.setAcceptDrops(True)
        root=QVBoxLayout(self);self.identity=label(f'限定范围：所选 {len(scope)} 张仓单；付款表自动认单，识别后核对保存。' if scope is not None else '付款表自动认单：有签发单号按单号核对，缺号按完整商品明细识别门店和仓。','section');root.addWidget(self.identity)
        if scope is not None:self.identity.setToolTip('\n'.join(h['number'] for h in scope))
        root.addWidget(label('拖入回传、付款、收货文件，可同批混合。只填写本环节事实；上环节自动读取本批资料或已有资料。','muted'))
        self.pages=QStackedWidget();root.addWidget(self.pages,1);queue=QWidget();v=QVBoxLayout(queue);self.pages.addWidget(queue)
        row=QHBoxLayout();row.addWidget(button('选择文件',self.choose));row.addWidget(button('重新读取全部文件',self.reload))
        from lifecycle_ui import template
        for s in io.STAGES:row.addWidget(button('下载'+io.TITLES[s]+'模板',lambda checked=False,k=s:template(self,k)))
        v.addLayout(row);self.file_note=label('拖入文件后自动识别，原文件不变。');v.addWidget(self.file_note)
        self.match_bar=QWidget();match_layout=QVBoxLayout(self.match_bar);match_layout.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();row.addWidget(label('自动识别完整付款单；门店名称略有不同、同店两仓均按实际分配明细核对。'))
        self.source_company=QComboBox();self.source_company.addItem('自动识别（无需选择来源企业）',None)
        self.match_mode=QComboBox()
        for title,value in [('首次付款优先','first'),('重复导入 / 付款更正优先','correction'),('全部有效生命周期','all')]:self.match_mode.addItem(title,value)
        row.addWidget(self.match_mode);self.remember_matching=QCheckBox('保存本次认单对应');self.remember_matching.setChecked(True);row.addWidget(self.remember_matching)
        self.match_more=QCheckBox('更多候选筛选');row.addWidget(self.match_more);match_layout.addLayout(row)
        self.match_filters=QWidget();filters_layout=QVBoxLayout(self.match_filters);filters_layout.setContentsMargins(0,0,0,0)
        row=QHBoxLayout();row.addWidget(label('指定来源企业（仅需要时设置）'));row.addWidget(self.source_company,1);filters_layout.addLayout(row)
        row=QHBoxLayout();self.match_from=QLineEdit();self.match_from.setPlaceholderText('候选起始日 YYYY-MM-DD，选填');self.match_to=QLineEdit();self.match_to.setPlaceholderText('候选结束日 YYYY-MM-DD，选填');row.addWidget(self.match_from);row.addWidget(self.match_to)
        row.addWidget(button('筛选候选日期',self.filter_candidates));row.addWidget(button('扩大到全部日期',self.expand_candidates));row.addWidget(button('刷新索引并重新识别',self.refresh_payment_index));row.addWidget(button('维护已确认门店对应',self.maintain_aliases));filters_layout.addLayout(row)
        self.match_filters.hide();self.match_more.toggled.connect(self.match_filters.setVisible);match_layout.addWidget(self.match_filters)
        filters_layout.addWidget(label('指定企业时，本批文件须来自该企业。自动模式支持同店两仓混合；不猜测或回写企业身份。日期只筛候选显示。','muted'));self.match_bar.hide();v.addWidget(self.match_bar)
        self.source_company.currentIndexChanged.connect(self.rematch);self.match_mode.currentIndexChanged.connect(self.rematch)
        self.table=QTableWidget(0,7);self.table.setHorizontalHeaderLabels(['纳入','来源文件 / 来源订单','环节','商品行数','补货单号（整单关联）','读取范围','识别状态与下一步']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,w in enumerate([50,270,130,85,380,260]):self.table.setColumnWidth(i,w)
        v.addWidget(self.table,1)
        self.selection_note=QPlainTextEdit('选择一行查看完整来源、目标签发单及识别原因。');self.selection_note.setReadOnly(True)
        self.selection_note.setMinimumHeight(76);self.selection_note.setMaximumHeight(130);v.addWidget(self.selection_note)
        self.table.currentCellChanged.connect(lambda *_:self.describe_selection())
        row=QHBoxLayout();row.addWidget(button('划分选中来源组并绑定签发单',self.split_selected));row.addWidget(button('查看识别依据与候选',self.explain_binding));row.addWidget(button('查看文件关联信息',self.file_identity));v.addLayout(row)
        self.table.setColumnWidth(6,420)
        self.issue_note=label('需要处理');v.addWidget(self.issue_note)
        self.issue_table=QTableWidget(0,4);self.issue_table.setHorizontalHeaderLabels(['问题文件 / 原因','环节','修正映射','本次不纳入']);self.issue_table.setColumnWidth(0,600);self.issue_table.setMaximumHeight(180);self.issue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(self.issue_table)
        self.note=label('同单同环节有多份文件时只能选择一份，不按到达时间覆盖，也不累加。','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('取消',self.reject));self.preview_btn=button('核对并准备保存',self.preview,True);row.addWidget(self.preview_btn);v.addLayout(row)
        from table_readability import fit_window,install
        fit_window(self,1240,820)
        for table in (self.table,self.issue_table):install(table)
        self.table.reading_controller.apply()
        # Cell widgets may shrink with columns; never paint over the reason.
        for i,w in enumerate([50,240,105,75,340,90,390]):self.table.setColumnWidth(i,w)

    def choose(self):
        paths,_=QFileDialog.getOpenFileNames(self,'选择完整单据（可跨订单、跨环节）','','Excel (*.xlsx)')
        if paths:self.add_paths(paths)
    def dragEnterEvent(self,event):
        urls=event.mimeData().urls()
        if not self.busy and urls and all(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower()=='.xlsx' for u in urls):event.acceptProposedAction()
    def dropEvent(self,event):
        if not self.busy:self.add_paths([u.toLocalFile() for u in event.mimeData().urls()]);event.acceptProposedAction()
    def add_paths(self,paths):
        if self.pages.currentIndex()!=0:
            QMessageBox.information(self,'请先返回文件范围','预览已固定，先返回再加入文件。');return
        self.paths=list(dict.fromkeys([*self.paths,*[str(Path(p).resolve()) for p in paths]]));self.reload()
    def reload(self):
        if not self.paths or self.busy:return
        self.busy=True;self.read_ready=False;self.preview_btn.setEnabled(False)
        self.documents=[];self.issues=[];self.heads=[];self.payment_index=None;self.targets=[];self.table.setRowCount(0);self.issue_table.setRowCount(0)
        try:
            data=run(self,batch.read,(self.paths,self.stage),'读取并识别生命周期文件')
            self.documents=data['documents'];self.issues=data['issues'];self.excluded=[];self.file_note.setText('\n'.join(f['name']+'：'+f['status']+'；'+ '、'.join(s+' / '+io.TITLES[k] for k,s in f['sheets']) for f in data['files']))
            paid=any(d['scan']['stage']=='payment' and d['scan']['rows'][0].get('adapter')=='paid_order' for d in self.documents)
            self.match_bar.setVisible(paid)
            if paid:
                from payment_matching import build
                self.payment_index=run(self,build,(self.host.work_repository.path,),'按需建立付款识别索引（不读取旧修订）')
                self.heads=self.scope if self.scope is not None else self.payment_index['heads']
                self.populate_sources()
                self.identify_payments()
            else:
                self.payment_index=None;self.heads=self.scope if self.scope is not None else run(self,service.current_targets,(self.host.work_repository.path,),'读取可关联订单')
            self.populate()
            self.read_ready=True;self.preview_btn.setEnabled(True)
        except Cancelled:self.note.setText('读取已取消，未保存任何单据。可重新读取。')
        except Exception as exc:self.note.setText('读取未完成：'+str(exc))
        finally:self.busy=False
    def populate(self):
        self.table.setRowCount(len(self.documents));self.targets=[]
        for i,d in enumerate(self.documents):
            rows=d['scan']['rows'];r=rows[0];source=r.get('source_order',{});check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);check.setCheckState(Qt.CheckState.Checked if d.get('selected',True) else Qt.CheckState.Unchecked);self.table.setItem(i,0,check)
            title=r['source']['file']+(' / '+str(source.get('店铺名称') or '原表未提供店名')+' / '+str(source.get('内部订单号')) if source else ' / '+str(r.get('signed_number') or r.get('document_number') or '缺单号'))
            for j,text in ((1,title),(2,io.TITLES[d['scan']['stage']]),(3,str(len(rows))),(5,'、'.join(dict.fromkeys(x['source']['sheet'] for x in rows)))):self.table.setItem(i,j,QTableWidgetItem(text))
            combo=QComboBox();combo.setMinimumWidth(0);combo.setMinimumContentsLength(12);combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon);combo.addItem('尚未自动对应订单',None)
            explanation=diagnostics.binding(d,self.heads,scoped=self.scope is not None);choices=explanation['candidates']
            automatic_head=choices[0] if choices and explanation['automatic'] else None
            if d.get('payment_match') is not None and (self.match_from.text().strip() or self.match_to.text().strip()):
                start,end=self.match_from.text().strip(),self.match_to.text().strip()
                choices=[h for h in choices if (not start or h['day']>=start) and (not end or h['day']<=end)]
                preserve=d.get('target') or automatic_head
                if preserve and not any(h['number']==preserve['number'] for h in choices):choices=[preserve,*choices]
            for h in choices:combo.addItem(f"{h['day']} · {h.get('name',h['store'])} ({h['store']}) · {h['warehouse']} · {h['number']} · V{h['revision']:03d}",h)
            combo.setEditable(True);combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert);combo.completer().setFilterMode(Qt.MatchFlag.MatchContains);combo.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
            if automatic_head:combo.setCurrentIndex(next((n+1 for n,h in enumerate(choices) if h['number']==automatic_head['number']),0))
            if d.get('target'):
                index=next((n+1 for n,h in enumerate(choices) if h['number']==d['target']['number']),0);combo.setCurrentIndex(index)
            self.table.setCellWidget(i,4,combo);self.targets.append(combo)
            combo.currentIndexChanged.connect(lambda *_:self.describe_selection())
            cell=QTableWidgetItem(explanation['state']+'：'+explanation['reason']);cell.setToolTip(cell.text());self.table.setItem(i,6,cell)
        self.issue_table.setRowCount(len(self.issues))
        for i,issue in enumerate(self.issues):
            cell=QTableWidgetItem(str(issue.get('path') or issue['info']['name'])+'：'+diagnostics.issue(issue['message'])+'\n'+issue['message']);cell.setToolTip(cell.text());self.issue_table.setItem(i,0,cell)
            c=QComboBox()
            for s in io.STAGES:c.addItem(io.TITLES[s],s)
            c.setCurrentIndex(io.STAGES.index(issue.get('stage') or 'return'));self.issue_table.setCellWidget(i,1,c)
            b=button('修正字段映射',lambda checked=False,n=i:self.map_issue(n));b.setEnabled('info' in issue);self.issue_table.setCellWidget(i,2,b)
            self.issue_table.setCellWidget(i,3,button('本次不读取未识别部分',lambda checked=False,n=i:self.exclude_issue(n)))
        self.issue_note.setVisible(bool(self.issues));self.issue_table.setVisible(bool(self.issues))
        matched=sum(bool(c.currentData()) for c in self.targets)
        self.note.setText(f'已读取 {len(self.documents)} 份来源单；已对应订单 {matched} 份；未对应 {len(self.documents)-matched} 份；文件问题 {len(self.issues)} 项。下一步核对付款明细并保存。')
        self.table.verticalHeader().setDefaultSectionSize(68);self.issue_table.verticalHeader().setDefaultSectionSize(72)
        if self.documents and self.table.currentRow()<0:self.table.setCurrentCell(0,1)
        self.describe_selection()
    def describe_selection(self):
        i=self.table.currentRow()
        if i<0 or i>=len(self.documents) or i>=len(getattr(self,'targets',[])):return
        source=self.table.item(i,1);status=self.table.item(i,6);target=self.targets[i].currentData()
        text=('来源：'+source.text() if source else '')+'\n目标签发：'+(f"{target['day']} · {target.get('name',target['store'])} ({target['store']}) · {target['warehouse']} · {target['number']}" if target else '尚未确认')
        if status:text+='\n'+status.text()
        self.selection_note.setPlainText(text)
    def explain_binding(self):
        index=self.table.currentRow()
        if index<0 or index>=len(self.documents):self.note.setText('请先选择一个来源订单。');return
        result=diagnostics.binding(self.documents[index],self.heads,scoped=self.scope is not None)
        from readable_ui import details
        rows=[]
        for h in result['candidates']:rows.append([h['day'],h['store'],h.get('name',''),h['warehouse'],h['number'],f"V{h['revision']:03d}",(result.get('candidate_reasons') or {}).get(h['number'],'已有来源单号对应' if result['state']=='已识别原内部单号' else '候选；下一步完整核对数量和金额')])
        for item in result['rejected']:
            h=item['head'];rows.append([h['day'],h['store'],h.get('name',''),h['warehouse'],h['number'],f"V{h['revision']:03d}",item['reason']])
        trace='；'.join(str(k)+' '+str(v) for k,v in result.get('trace',[]))
        details(self,'识别依据与原单候选',['业务日期','门店编码','门店名称','仓','补货单号','签发修订','匹配依据 / 排除原因'],rows,result['state']+'：'+result['reason']+('\n'+trace if trace else ''))

    def identify_payments(self):
        if not self.payment_index:return
        from payment_matching import analyze
        source_scope,mode=self.source_company.currentData(),self.match_mode.currentData()
        def operation(progress,cancel):
            values=[]
            for d in self.documents:
                if d['scan']['stage']=='payment' and d['scan']['rows'][0].get('adapter')=='paid_order':
                    values.append(analyze(self.host.work_repository.path,d,self.payment_index,self.scope,source_scope,mode,progress,cancel))
                else:values.append(None)
            return values
        values=run(self,operation,(),'按来源、生命周期、时间及明细识别付款仓单')
        for d,result in zip(self.documents,values):
            if result is not None:d['payment_match']=result

    def populate_sources(self):
        previous=self.source_company.currentData();self.source_company.blockSignals(True)
        try:
            self.source_company.clear();self.source_company.addItem('自动识别（无需选择来源企业）',None)
            for source in self.payment_index['accounts']['choices']:self.source_company.addItem(source['name']+' · '+' / '.join(source['warehouses']),source['coid'])
            self.source_company.setCurrentIndex(next((i for i in range(self.source_company.count()) if self.source_company.itemData(i)==previous),0))
        finally:self.source_company.blockSignals(False)

    def maintain_aliases(self):
        if self.busy:return
        from payment_matching_ui import maintain_aliases
        if maintain_aliases(self,self.host.work_repository.path):self.refresh_payment_index()

    def rematch(self,*_):
        if self.busy or not self.payment_index:return
        self.capture();self.busy=True
        try:
            for d in self.documents:
                if d.get('payment_match') is not None:d['target']=None
            self.identify_payments();self.populate();self.read_ready=True;self.preview_btn.setEnabled(True)
        except Cancelled:self.read_ready=False;self.preview_btn.setEnabled(False);self.note.setText('识别已取消，未保存付款或对应关系；请重新识别后核对')
        except Exception as exc:self.read_ready=False;self.preview_btn.setEnabled(False);self.note.setText('识别未完成：'+str(exc))
        finally:self.busy=False

    def filter_candidates(self):
        if not self.read_ready:return
        try:
            from datetime import date
            for value in (self.match_from.text().strip(),self.match_to.text().strip()):
                if value:date.fromisoformat(value)
            if self.match_from.text().strip() and self.match_to.text().strip() and self.match_from.text().strip()>self.match_to.text().strip():raise ValueError('起始日不能晚于结束日')
            self.capture();self.populate()
        except ValueError as exc:self.note.setText('候选日期无效：'+str(exc))

    def expand_candidates(self):
        if not self.read_ready:return
        self.capture();self.match_from.clear();self.match_to.clear();self.populate()

    def refresh_payment_index(self):
        if self.busy or not self.payment_index:return
        self.capture();self.busy=True
        try:
            from payment_matching import build
            self.payment_index=run(self,build,(self.host.work_repository.path,),'刷新付款索引，仅读取变更快照')
            self.populate_sources()
            for d in self.documents:
                if d.get('payment_match') is not None:d['target']=None
            self.heads=self.scope if self.scope is not None else self.payment_index['heads'];self.identify_payments();self.populate();self.read_ready=True;self.preview_btn.setEnabled(True)
        except Cancelled:self.read_ready=False;self.preview_btn.setEnabled(False);self.note.setText('索引刷新已取消，未保存付款；请刷新后继续')
        except Exception as exc:self.read_ready=False;self.preview_btn.setEnabled(False);self.note.setText('刷新未完成：'+str(exc))
        finally:self.busy=False
    def file_identity(self):
        index=self.table.currentRow()
        if index>=0 and index<len(self.documents):path=self.documents[index]['scan']['files'][0]['path']
        else:
            path,_=QFileDialog.getOpenFileName(self,'只读查看文件关联信息','','Excel (*.xlsx)')
        if not path:return
        try:
            info=run(self,io.inspect,([path],),'只读核对文件关联信息')[0]
            rows=[[e['sheet'],e['number'],e['store'],e['warehouse'] or '未分仓',e['day'],f"V{e['revision']:03d}",e['created']] for e in info.get('identity',[])]
            note='隐藏关联页完整，导入时仍须与当前资料目录中的原签发核对。' if rows else '没有系统关联页。文件属性：'+str({k:v for k,v in info.get('properties',{}).items() if k in ('补货单号','修订')})+'；缺少明确身份时，需要人工核对完整原单。'
            from readable_ui import details
            details(self,'文件关联信息（只读）',['业务工作表','补货单号','门店编码','仓','业务日期','签发修订','签发保存时间'],rows,note)
        except Cancelled:pass
        except Exception as exc:
            from readable_ui import failure
            failure(self,'文件关联信息未通过',exc)
    def split_selected(self):
        i=self.table.currentRow()
        if i<0 or i>=len(self.documents):self.note.setText('请先选择需要划分的来源组。');return
        self.capture();document=self.documents[i];rows=document['scan']['rows'];d=QDialog(self);d.setWindowTitle('选择记录范围与目标签发单');d.resize(1000,600);v=QVBoxLayout(d)
        v.addWidget(label('勾选属于同一完整订单的记录，再选择签发单。余下记录保留待绑定；不按商品、时间或行位置猜归属。','muted'))
        table=QTableWidget(len(rows),6);table.setHorizontalHeaderLabels(['纳入该订单','原表 / 行','商品编码','商品名称','本环节数量','门店 / 仓']);table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(table,1)
        for n,r in enumerate(rows):
            item=QTableWidgetItem();item.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);item.setCheckState(Qt.CheckState.Unchecked);table.setItem(n,0,item)
            for j,value in enumerate([r['source']['sheet']+' / '+str(r['source']['row']),r['sku'],r['name'],r['quantity'],r.get('store','')+' / '+r.get('warehouse','')],1):table.setItem(n,j,QTableWidgetItem('空白' if value is None else str(value)))
        target=QComboBox();target.addItem('请选择签发单',None)
        for h in self.heads:target.addItem(f"{h['day']} · {h.get('name',h['store'])} · {h['warehouse']} · {h['number']}",h)
        target.setEditable(True);target.setInsertPolicy(QComboBox.InsertPolicy.NoInsert);target.completer().setFilterMode(Qt.MatchFlag.MatchContains);v.addWidget(target)
        row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button('划分并绑定选中记录',d.accept,True));v.addLayout(row)
        if d.exec()!=QDialog.DialogCode.Accepted:return
        try:
            if not target.currentData():raise ValueError('请选择明确的签发单')
            parts=batch.split_document(document,[n for n in range(len(rows)) if table.item(n,0).checkState()==Qt.CheckState.Checked],target.currentData())
            self.documents[i:i+1]=parts;self.populate()
        except Exception as exc:self.note.setText('未划分：'+str(exc))
    def capture(self):
        for i,d in enumerate(self.documents):
            combo=self.targets[i];d['selected']=self.table.item(i,0).checkState()==Qt.CheckState.Checked
            d['target']=combo.currentData() if combo.currentText()==combo.itemText(combo.currentIndex()) else None
    def exclude_issue(self,index):
        self.capture();self.excluded.append(self.issues.pop(index));self.populate()
        self.note.setText('已明确排除未识别部分；其余已识别工作表仍在上方，是否纳入由勾选决定。重新读取可恢复排除项。')
    def map_issue(self,index):
        from lifecycle_ui import Mapping
        issue=self.issues[index];stage=self.issue_table.cellWidget(index,1).currentData();m=Mapping(self,self.host.work_repository.path,stage,issue['info'])
        if m.exec()!=QDialog.DialogCode.Accepted:return
        try:
            scan=run(self,io.scan,(stage,m.specs()),'校验映射后的完整单据');self.capture();sha=issue['info']['sha']
            self.documents=[d for d in self.documents if d['scan']['files'][0]['sha']!=sha]+batch.group_scan(scan)
            self.issues.pop(index);self.populate()
        except Exception as exc:self.note.setText('映射未通过：'+str(exc))
    def preview(self):
        if self.busy:return
        if not self.read_ready:self.note.setText('文件读取或识别尚未完成，请重新读取后再核对；未保存任何单据');return
        if self.issues:self.note.setText('请先处理上方问题文件，或明确选择本次不纳入；不会悄悄跳过。');return
        self.capture();self.busy=True
        try:
            if self.payment_index:
                from payment_matching import prepare
                self.plan=run(self,prepare,(self.host.work_repository.path,self.documents,self.scope,self.payment_index,self.source_company.currentData(),self.remember_matching.isChecked()),'按完整付款单核对并冻结识别对应')
            else:self.plan=run(self,batch.prepare,(self.host.work_repository.path,self.documents,self.scope),'按整单和上下游顺序核对')
            from lifecycle_ui import Review
            if self.review:self.pages.removeWidget(self.review);self.review.deleteLater()
            self.review=Review(self,self.plan);self.review.setWindowFlags(Qt.WindowType.Widget);self.review.save.clicked.disconnect();self.review.save.clicked.connect(self.save)
            if self.plan.get('payment_matching'):
                scope=self.source_company.currentData();name=self.source_company.currentText()
                self.review.layout().insertWidget(0,label(('本批指定付款来源：'+name if scope else '本批按文件证据自动认单，不修改企业身份及门店资料。')+'；'+('确认保存时同时保留本次认单对应，供重复导入和金额更正使用。' if self.remember_matching.isChecked() else '本次不新增认单对应关系。'),'muted'))
            names=[str(x.get('path') or x['info']['name'])+'：'+x['message'] for x in self.excluded]
            omitted=[d['scan']['files'][0]['name']+' / '+io.TITLES[d['scan']['stage']] for d in self.documents if not d['selected']]
            self.review.layout().insertWidget(0,label(f"本批拟保存 {len(self.plan['plans'])} 份；仅留存待核 {sum(p['payload']['pending'] for p in self.plan['plans'])} 份；未纳入 {len(names)+len(omitted)} 项。"+('\n未纳入：'+'；'.join(names+omitted) if names or omitted else ''),'section'))
            for b in self.review.findChildren(QPushButton):
                if b.text()=='取消':b.setText('返回文件范围');b.clicked.disconnect();b.clicked.connect(lambda:self.pages.setCurrentIndex(0))
            self.pages.addWidget(self.review);self.pages.setCurrentWidget(self.review)
        except Cancelled:self.note.setText('预览已取消，未保存。')
        except Exception as exc:self.note.setText('需要处理：'+str(exc))
        finally:self.busy=False
    def save(self):
        from lifecycle_ui import commit
        try:
            self.busy=True;self.receipt=commit(self,self.host,self.plan)
            self.pages.setCurrentIndex(0);self.preview_btn.setEnabled(False);self.table.setEnabled(False)
            self.note.setText('本批已保存：'+ '；'.join(r['number']+' / '+io.TITLES[r['stage']]+(' 已保存' if r['changed'] else ' 内容相同') for r in self.receipt))
            self.accept()
        except Cancelled:self.pages.setCurrentIndex(0);self.note.setText('已安全取消，未保存；可重新核对。')
        except Exception as exc:self.pages.setCurrentIndex(0);self.note.setText('整批未保存：'+str(exc)+'；请重新核对。')
        finally:self.busy=False
    def reject(self):
        if not self.busy:super().reject()


def import_batch(parent,host,stage=None,scope=None,paths=None):
    dialog=BatchImport(parent,host,stage,scope)
    if paths:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0,lambda:dialog.add_paths(paths))
    dialog.exec();result=dialog.receipt;dialog.deleteLater();return result
