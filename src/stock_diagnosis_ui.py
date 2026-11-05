"""One bounded, explicit diagnosis session over the current editable batch."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QListWidget,QListWidgetItem,QComboBox,QTabWidget,QLineEdit,QCheckBox,QMessageBox
from business_ui import grid,run
from ui_controls import button,label
import stock_diagnosis as service
import stock_diagnosis_adoption as adoption

def fmt(v):return '待核' if v is None else f'{v:,.2f}' if isinstance(v,(int,float)) or hasattr(v,'as_tuple') else str(v)

class DiagnosisDialog(QDialog):
    def __init__(self,host):
        super().__init__(host);self.host=host;self.gateway=host.automation;self.session=None;self.resize(1180,780);self.setWindowTitle('安全库存与资金诊断 · 仅本次作业')
        v=QVBoxLayout(self);v.addWidget(label('先查执行与多轮缺货证据，再比较资金与覆盖。试算不会改变当前清单。','section'))
        v.addWidget(label('仅调整分类库存目标分位概率；两仓共用分类参数，完整重算整店预算。已有人工改量/新品安排须先核对，不自动清除。','muted'))
        top=QHBoxLayout();self.stores=QListWidget();self.categories=QListWidget();top.addWidget(self.stores,2);top.addWidget(self.categories,1);self.stores.setMaximumHeight(135);self.categories.setMaximumHeight(135);v.addLayout(top)
        selected=set(host.selected_codes());groups=[]
        for code,name in (host.loaded or {}).get('stores',{}).items():
            item=QListWidgetItem(name+' · '+code);item.setData(Qt.ItemDataRole.UserRole,code);item.setCheckState(Qt.CheckState.Checked if code in selected else Qt.CheckState.Unchecked);self.stores.addItem(item)
            groups.extend(host.config_for(code)['weight_profiles'])
        for name in dict.fromkeys(groups):
            item=QListWidgetItem(name);item.setData(Qt.ItemDataRole.UserRole,name);item.setCheckState(Qt.CheckState.Checked if name=='快销' else Qt.CheckState.Unchecked);self.categories.addItem(item)
        row=QHBoxLayout();self.steps=QComboBox()
        for title,steps in [('上下各2.5个百分点',[-.025,.025]),('下调2.5 / 5个百分点',[-.025,-.05]),('上调2.5 / 5个百分点',[.025,.05])]:self.steps.addItem(title,steps)
        row.addWidget(self.steps);self.start=button('冻结本次依据并试算',self.calculate,True);row.addWidget(self.start);self.candidate=QComboBox();self.candidate.currentIndexChanged.connect(self.render);row.addWidget(self.candidate,1);v.addLayout(row)
        self.note=label('合法范围50%～99%；触界截断并去重。默认不选任何候选，不以最便宜为默认。','muted');v.addWidget(self.note)
        self.tabs=QTabWidget();self.models={}
        for key,title in [('stores','门店资金'),('changes','参数原值与新值'),('distribution','箱规与覆盖'),('details','全部商品影响'),('risks','缺货风险')]:
            table,model=grid(self,[]);self.models[key]=model;self.tabs.addTab(table,title)
        v.addWidget(self.tabs,1)
        self.reason=QLineEdit();self.reason.setPlaceholderText('采用原因（必填）；不写入永久默认');v.addWidget(self.reason)
        self.risk=QCheckBox('已核对本候选全部风险，接受所列逐商品保护期/最低持有缺口增加');v.addWidget(self.risk)
        row=QHBoxLayout();self.adopt_btn=button('采用选定完整候选到本次作业',self.adopt);row.addWidget(self.adopt_btn);self.undo_btn=button('撤回本次采用',self.undo);row.addWidget(self.undo_btn);row.addWidget(button('关闭',self.accept));v.addLayout(row)
        self.session=getattr(host,'_native_diagnosis',None)
        if self.session:self.fill_candidates()
        self.render()

    def fill_candidates(self):
        self.candidate.clear();self.candidate.addItem('请选择候选，默认保持当前',None)
        for i,o in enumerate(self.session['options']):self.candidate.addItem(f"候选{i+1}：{o['step']*100:+g}个百分点",i)

    def chosen(self,widget):return [widget.item(i).data(Qt.ItemDataRole.UserRole) for i in range(widget.count()) if widget.item(i).checkState()==Qt.CheckState.Checked]

    def calculate(self):
        try:
            self.gateway.idle()
            if self.host.readonly:raise ValueError('历史作业只读，回到本次可编辑工作台试算')
            stores=self.chosen(self.stores)
            if not stores:raise ValueError('至少选择一家门店')
            args={'stores':stores,'categories':self.chosen(self.categories),'steps':self.steps.currentData()}
            snapshot=self.gateway.snapshot({});current=dict(self.host.results)
            self.session=run(self,service.run,(snapshot,args,current),'冻结依据并试算安全库存')
            self.host._native_diagnosis=self.session
            self.fill_candidates()
            self.render()
        except Exception as exc:QMessageBox.warning(self,'未完成试算',str(exc))

    def show_table(self,key,headers,rows):
        model=self.models[key];model.headers=headers;model.replace([[fmt(v) for v in row] for row in rows])

    def render(self,*_):
        index=self.candidate.currentData();valid=self.session is not None and index is not None
        self.adopt_btn.setEnabled(valid and not self.session.get('adopted'));self.undo_btn.setEnabled(bool(self.session and self.session.get('undo')))
        if not valid:return
        o=self.session['options'][index];m=o['comparison'];self.risk.setChecked(False)
        self.note.setText(f"{len(m['hard_issues'])}条关键待核（不能采用），{len(m['risks'])}条风险。"+m['note'])
        self.adopt_btn.setEnabled(not self.session['adopted'] and not m['hard_issues'])
        self.show_table('stores',['门店','原执行货值','候选执行货值','增减','原模型建议货值','候选模型建议货值','原必采货值','候选必采货值','改量款数'],[[s['name'],s['before'],s['after'],s['delta'],s['model_before'],s['model_after'],s['must_before'],s['must_after'],s['changed']] for s in m['stores']])
        self.show_table('changes',['门店编码','分类组','原概率','候选概率','原覆盖来源'],[[r['store'],r['category'],f"{r['old']:.1%}",f"{r['new']:.1%}",r['source']] for r in o['changes']])
        self.show_table('distribution',['门店','箱规类型','方案','≤保护期','至保护期+2天','+2至+4天','超过+4天','无预测参考','>21天','>30天','>60天'],[[s['name'],kind,'原方案' if stage=='before' else '候选',*d['bins'].values(),*d['over_days'].values()] for s in m['stores'] for kind,both in s['distributions'].items() for stage,d in both.items()])
        headers=['门店编码','商品编码','商品名称','仓','必采','箱规','原执行量','候选量','货值增减','原覆盖天数','候选覆盖天数','原保护期缺口','候选缺口','现货覆盖','现货加在途覆盖','到货前缺口（不计未定时在途）','风险']
        for key in ('details','risks'):
            self.show_table(key,headers,[[r[k] for k in ('store','sku','name','warehouse','must_sell','box','before','after','amount_delta','coverage_before','coverage_after','deficit_before','deficit_after','available_cover','position_cover','arrival_gap_without_timed_inbound')]+['、'.join(r['risks'])] for r in m[key]])

    def adopt(self):
        try:
            index=self.candidate.currentData()
            if not self.reason.text().strip():raise ValueError('请填写采用原因')
            box=QMessageBox.question(self,'确认本次采用',f'采用候选{index+1}的整店结果及概率参数？\n仅本次作业，尚未保存或签发。永久默认不变。',QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.Cancel,QMessageBox.StandardButton.Cancel)
            if box!=QMessageBox.StandardButton.Yes:return
            result=adoption.adopt(self.gateway,self.session,index,self.reason.text(),self.risk.isChecked());self.render();self.note.setText(result['message'])
        except Exception as exc:QMessageBox.warning(self,'未采用',str(exc))

    def undo(self):
        try:result=adoption.undo(self.gateway,self.session);self.render();self.note.setText(result['message'])
        except Exception as exc:QMessageBox.warning(self,'未撤回',str(exc))

def open_diagnosis(host):
    try:
        host.automation.idle()
        if not host.loaded or host.input_dirty:raise ValueError('先在补货出单检查本次输入，再进行诊断')
        dialog=DiagnosisDialog(host);dialog.exec();dialog.deleteLater()
    except Exception as exc:QMessageBox.warning(host,'暂不能诊断',str(exc))
