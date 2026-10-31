"""One continuous workspace for file recognition, association and atomic save."""
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QStackedWidget,QWidget,
    QTableWidget,QTableWidgetItem,QComboBox,QFileDialog,QMessageBox,QPushButton)
from ui_controls import button,label
from business_ui import run
from runtime_support import Cancelled
import lifecycle_batch as batch
import lifecycle_io as io
import order_lifecycle as service


class BatchImport(QDialog):
    def __init__(self,parent,host,stage=None,scope=None):
        super().__init__(parent);self.host=host;self.stage=stage;self.scope=scope
        self.paths=[];self.documents=[];self.issues=[];self.excluded=[];self.heads=[];self.receipt=None;self.review=None;self.busy=False
        self.setObjectName('lifecycleBatchImport');self.setWindowTitle('批量导入单据');self.resize(1240,820);self.setAcceptDrops(True)
        root=QVBoxLayout(self);self.identity=label('限定范围：'+'、'.join(h['number'] for h in scope) if scope is not None else '范围：按文件中的补货单号识别；缺号来源订单需明确关联。','section');root.addWidget(self.identity)
        root.addWidget(label('拖入回传、付款、收货文件，可同批混合。只填写本环节事实；上环节自动读取本批资料或已有资料。','muted'))
        self.pages=QStackedWidget();root.addWidget(self.pages,1);queue=QWidget();v=QVBoxLayout(queue);self.pages.addWidget(queue)
        row=QHBoxLayout();row.addWidget(button('选择文件',self.choose));row.addWidget(button('重新读取全部文件',self.reload))
        from lifecycle_ui import template
        for s in io.STAGES:row.addWidget(button('下载'+io.TITLES[s]+'模板',lambda checked=False,k=s:template(self,k)))
        v.addLayout(row);self.file_note=label('拖入文件后自动识别，原文件不变。');v.addWidget(self.file_note)
        self.table=QTableWidget(0,6);self.table.setHorizontalHeaderLabels(['纳入','来源文件 / 来源订单','环节','商品行数','补货单号（整单关联）','读取范围']);self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        for i,w in enumerate([50,270,130,85,380,260]):self.table.setColumnWidth(i,w)
        v.addWidget(self.table,1)
        self.issue_note=label('需要处理');v.addWidget(self.issue_note)
        self.issue_table=QTableWidget(0,4);self.issue_table.setHorizontalHeaderLabels(['问题文件 / 原因','环节','修正映射','本次不纳入']);self.issue_table.setColumnWidth(0,600);self.issue_table.setMaximumHeight(180);self.issue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers);v.addWidget(self.issue_table)
        self.note=label('同单同环节有多份文件时只能选择一份，不按到达时间覆盖，也不累加。','muted');v.addWidget(self.note)
        row=QHBoxLayout();row.addWidget(button('取消',self.reject));self.preview_btn=button('核对并准备保存',self.preview,True);row.addWidget(self.preview_btn);v.addLayout(row)

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
        self.busy=True
        try:
            data=run(self,batch.read,(self.paths,self.stage),'读取并识别生命周期文件')
            self.heads=self.scope if self.scope is not None else run(self,service.current_targets,(self.host.work_repository.path,),'读取可关联订单')
            self.documents=data['documents'];self.issues=data['issues'];self.excluded=[];self.file_note.setText('\n'.join(f['name']+'：'+f['status']+'；'+ '、'.join(s+' / '+io.TITLES[k] for k,s in f['sheets']) for f in data['files']))
            self.populate()
        except Cancelled:self.note.setText('读取已取消，未保存任何单据。可重新读取。')
        except Exception as exc:self.note.setText('读取未完成：'+str(exc))
        finally:self.busy=False
    def populate(self):
        self.table.setRowCount(len(self.documents));self.targets=[]
        for i,d in enumerate(self.documents):
            rows=d['scan']['rows'];r=rows[0];source=r.get('source_order',{});check=QTableWidgetItem();check.setFlags(Qt.ItemFlag.ItemIsEnabled|Qt.ItemFlag.ItemIsUserCheckable);check.setCheckState(Qt.CheckState.Checked if d.get('selected',True) else Qt.CheckState.Unchecked);self.table.setItem(i,0,check)
            title=r['source']['file']+(' / '+str(source.get('店铺名称'))+' / '+str(source.get('内部订单号')) if source else ' / '+str(r.get('signed_number') or r.get('document_number') or '缺单号'))
            for j,text in ((1,title),(2,io.TITLES[d['scan']['stage']]),(3,str(len(rows))),(5,'、'.join(dict.fromkeys(x['source']['sheet'] for x in rows)))):self.table.setItem(i,j,QTableWidgetItem(text))
            combo=QComboBox();combo.setMinimumWidth(350);combo.addItem('请选择完整来源单对应的补货单号',None)
            number=r.get('signed_number');choices=[h for h in self.heads if not number or h['number']==number]
            for h in choices:combo.addItem(f"{h['number']} · {h.get('name',h['store'])} · {h['warehouse']} · {h['day']}",h)
            if len(choices)==1 and (number or self.scope is not None and len(self.scope)==1):combo.setCurrentIndex(1)
            if d.get('target'):
                index=next((n+1 for n,h in enumerate(choices) if h['number']==d['target']['number']),0);combo.setCurrentIndex(index)
            self.table.setCellWidget(i,4,combo);self.targets.append(combo)
        self.issue_table.setRowCount(len(self.issues))
        for i,issue in enumerate(self.issues):
            self.issue_table.setItem(i,0,QTableWidgetItem(str(issue.get('path') or issue['info']['name'])+'：'+issue['message']))
            c=QComboBox()
            for s in io.STAGES:c.addItem(io.TITLES[s],s)
            c.setCurrentIndex(io.STAGES.index(issue.get('stage') or 'return'));self.issue_table.setCellWidget(i,1,c)
            b=button('修正字段映射',lambda checked=False,n=i:self.map_issue(n));b.setEnabled('info' in issue);self.issue_table.setCellWidget(i,2,b)
            self.issue_table.setCellWidget(i,3,button('本次不读取未识别部分',lambda checked=False,n=i:self.exclude_issue(n)))
        self.issue_note.setVisible(bool(self.issues));self.issue_table.setVisible(bool(self.issues))
        self.note.setText(f'已识别 {len(self.documents)} 份完整来源单；待处理 {len(self.issues)} 项。选择范围将在下一步完整核对。')
    def capture(self):
        for i,d in enumerate(self.documents):d['selected']=self.table.item(i,0).checkState()==Qt.CheckState.Checked;d['target']=self.targets[i].currentData()
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
        if self.issues:self.note.setText('请先处理上方问题文件，或明确选择本次不纳入；不会悄悄跳过。');return
        self.capture();self.busy=True
        try:
            self.plan=run(self,batch.prepare,(self.host.work_repository.path,self.documents,self.scope),'按整单和上下游顺序核对')
            from lifecycle_ui import Review
            if self.review:self.pages.removeWidget(self.review);self.review.deleteLater()
            self.review=Review(self,self.plan);self.review.setWindowFlags(Qt.WindowType.Widget);self.review.save.clicked.disconnect();self.review.save.clicked.connect(self.save)
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
