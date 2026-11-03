"""One focused editor in defaults, pre-calculation and pre-issue scenes."""
import copy
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QCheckBox,QTabWidget,QLineEdit,QMessageBox
from ui_controls import label,button
from business_ui import grid,run
import restock_policy as policy
import restock_trial
import store_parameter_ui as imports
import work_repository as wr

class SwitchDialog(QDialog):
    def __init__(self,parent,count,scope,post):
        super().__init__(parent);self.setObjectName('restockSwitchEditor');self.setWindowTitle('调整补货策略');self.resize(740,400)
        v=QVBoxLayout(self);v.addWidget(label(f'所选 {count} 家门店 · 两仓共用 · 只改下面的开关，其余参数各店保留。','section'))
        self.fields={}
        for key,name in policy.LABELS.items():
            row=QHBoxLayout();row.addWidget(label(name));w=QComboBox();w.addItem('保持各店原值',None);w.addItem('开启',True);w.addItem('关闭',False);row.addWidget(w);v.addLayout(row);v.addWidget(label(policy.HELP[key],'muted'));self.fields[key]=w
        self.scope=QComboBox()
        choices=[('仅保存门店默认','defaults')] if scope=='defaults' else [('仅应用本次作业','current'),('应用本次并保存门店默认','both')]
        for text,value in choices:self.scope.addItem(text,value)
        v.addWidget(self.scope);v.addWidget(label('下一步先后台试算，核对商品变化和风险后才应用；取消保持原结果。' if post else '下一步逐店核对原值与新值；应用到本次后，已有结果需重算。','muted'))
        row=QHBoxLayout();cancel=button('取消',self.reject);cancel.setDefault(True);row.addWidget(cancel);row.addWidget(button('预览变化',self.accept,True));v.addLayout(row)
    def changes(self):return {k:w.currentData() for k,w in self.fields.items() if w.currentData() is not None}

class Review(QDialog):
    def __init__(self,parent,prepared,trial):
        super().__init__(parent);self.setObjectName('restockSwitchReview');self.setWindowTitle('核对补货策略变化');self.resize(1180,740)
        v=QVBoxLayout(self);v.addWidget(label('确认后更新本次草稿，历史签发不变。新品关闭会撤销相关正数量安排；预算可能重新分给其他商品。','section'))
        tabs=QTabWidget();v.addWidget(tabs,1)
        for title,headers,rows in [('开关变化',['门店编码','门店名称','设置','原值','新值','来源'],prepared['rows']),('门店金额',['门店','原金额','新金额','处理方式'],trial['amounts']),('商品变化',['门店','编码','名称','原模型量','新模型量','原执行量','新执行量','原金额','新金额'],trial['rows']),('需确认风险',['门店','商品','提示'],[[r.get('store'),r.get('sku'),r['text']] for r in trial['risks']])]:
            page=QDialog();layout=QVBoxLayout(page);table,model=grid(page,headers,rows);search=QLineEdit();search.setPlaceholderText('搜索全部明细，不截断');search.textChanged.connect(model.filter);layout.addWidget(search);layout.addWidget(table);tabs.addTab(page,title)
        self.risk_check=QCheckBox('已核对“需确认风险”页，确认这些商品本次数量');self.risk_check.setVisible(bool(trial['risks']));v.addWidget(self.risk_check)
        v.addWidget(label('；'.join(trial['errors']) if trial['errors'] else f'{len(trial["rows"])} 款数量或金额变化。开启新品不会自动全选。','muted'))
        row=QHBoxLayout();cancel=button('取消，保持原结果',self.reject);cancel.setDefault(True);row.addWidget(cancel);self.go=button('确认应用到草稿',self.accept,True);row.addWidget(self.go);v.addLayout(row)
        self.go.setEnabled(not trial['errors'] and not trial['risks']);self.risk_check.toggled.connect(lambda on:self.go.setEnabled(not trial['errors'] and (on or not trial['risks'])))

def open_policy(parent,host,codes,*,scope='current',post=False):
    if not codes:return
    if host.worker or host.closing or scope!='defaults' and host.readonly:return
    d=SwitchDialog(parent,len(codes),scope,post)
    if d.exec()!=QDialog.DialogCode.Accepted:d.deleteLater();return
    changes=d.changes();scope=d.scope.currentData();d.deleteLater()
    if not changes:return
    data={'sources':{},'rows':[{'code':s,'name':'','values':{(policy.LABELS[k],):'开启' if value else '关闭' for k,value in changes.items()},'source':'补货策略开关'} for s in codes]}
    try:
        snapshot=imports.capture(host,data);prepared=imports.preview(data,snapshot,scope)
        if prepared['errors']:raise ValueError('；'.join(prepared['errors']))
        if not prepared['changed']:QMessageBox.information(parent,'无需修改','所选门店开关已经是该状态。');return
        if not post:
            review=imports.Preview(parent,host,data,snapshot,scope)
            accepted=review.exec()==QDialog.DialogCode.Accepted
            chosen_scope=review.scope.currentData();chosen=review.prepared;review.deleteLater()
            if accepted:imports.commit(host,data,snapshot,chosen_scope,chosen)
            return
        if host.input_dirty or host.job_day!=wr.today():raise ValueError('请先检查今日输入；历史只读单不能调整')
        changed=prepared['changed']
        if any(host.states.get(s,{}).get('state')!='已完成' for s in changed):raise ValueError('仅对有效完成门店进行签发前策略试算')
        jobs={s:host.selected_settings(s) for s in changed};current={s:host.results[s] for s in changed}
        draft_snapshot=copy.deepcopy(host.preissue_drafts)
        trial=run(parent,restock_trial.prepare,(host.loaded,jobs,current,host.new_bases,draft_snapshot,{s:prepared['values'][s]['preset']['config'] for s in changed},host.profile,host.job_day),'正在核算策略变化')
        if host.closing:return
        review=Review(parent,prepared,trial);accepted=review.exec()==QDialog.DialogCode.Accepted;confirmed=[r['id'] for r in trial['risks']] if review.risk_check.isChecked() else [];review.deleteLater()
        if not accepted:return
        if draft_snapshot!=host.preissue_drafts:raise ValueError('待核对调整在预览后变化，请重新核对')
        results=restock_trial.finalize(trial,host.results,confirmed)
        imports.commit(host,data,snapshot,scope,prepared)
        # All calculations, risk checks and persistence succeeded before publishing.
        host.results={**host.results,**results}
        for s in changed:
            host.states[s].update(state='已完成',error='');host.preissue_drafts.pop(s,None)
            if results[s].get('new_products'):host.new_bases[s]=trial['stores'][s]['base']
            else:host.new_bases.pop(s,None)
        host.current_store=changed[0];host.result=host.results[changed[0]];host.dirty=False;host.set_rows(host.result['rows'],True);host.refresh_tables();host.update_controls();host.touch()
        host.status.setText('补货策略已核对并应用；请核对最终清单后签发。历史签发不变。')
    except Exception as exc:
        from runtime_support import Cancelled
        if isinstance(exc,Cancelled):host.status.setText('策略试算已取消，原设置和结果保持不变。')
        else:host.failed(str(exc))
