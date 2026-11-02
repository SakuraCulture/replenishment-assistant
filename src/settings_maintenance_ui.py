"""Business-readable future-default maintenance, separate from current work."""
import copy,re
from PySide6.QtWidgets import QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,QMessageBox,QCheckBox,QHeaderView
from ui_controls import button,label
from business_ui import grid
from parameter_view import flatten,display_path
import settings_history as history
import work_repository as wr

def fit_diff_grid(table):
    # Keep both values visible at the default dialog width, including high DPI.
    # Long parameter paths wrap instead of pushing values out of the viewport.
    for column,width in ((0,145),(2,150),(3,150)):table.setColumnWidth(column,width)
    table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
    table.setWordWrap(True)
    table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)


def diffs(plan):
    rows=[]
    names={'preset':'参数方案','config':'完整参数','ui':'门店设置','primary':'主仓','secondary':'从仓','mode':'预算方式','amount':'预算金额','selected':'默认参与','name':'名称','id':'方案标识','version':'方案版本','builtin':'内置方案','saved':'保存时间'}
    def fields(value):
        result={}
        for key,v in flatten(value or {}):
            if ' / warehouses / ' in key and isinstance(v,(list,tuple)) and len(v)==2:
                result[key+' / 备货天数']=v[0];result[key+' / 到货天数']=v[1]
            else:result[key]=v
        return result
    def show(key,value):
        if key.endswith(' / mode') and value in (0,1,2):return ['未指定','不限预算','限定预算'][value]
        if isinstance(value,bool):return '是' if value else '否'
        return value
    for code,after in plan['values'].items():
        before=plan['expected'].get(code)
        a=fields(before);b=fields(after)
        for k in dict.fromkeys([*a,*b]):
            if a.get(k)!=b.get(k):rows.append([code,' / '.join(names.get(p,p) for p in display_path(k).split(' / ')),show(k,a.get(k,'未设置')),show(k,b.get(k,'未设置'))])
    return rows

def confirm_plan(parent,host,plan):
    d=QDialog(parent);d.setWindowTitle('核对门店默认变更');d.resize(1050,650);v=QVBoxLayout(d)
    v.addWidget(label(f"影响 {len(plan['values'])} 家门店的后续默认。当前作业和已签发历史均不改变。",'section'))
    if plan['conflicts']:v.addWidget(label('以下门店后来又修改过，本次不撤销：'+'、'.join(plan['conflicts'])))
    search=QLineEdit();search.setPlaceholderText('搜索门店或参数');v.addWidget(search)
    table,model=grid(d,['门店编码','设置项','现有默认','修改后默认'],diffs(plan));fit_diff_grid(table);search.textChanged.connect(model.filter);v.addWidget(table,1)
    row=QHBoxLayout();row.addWidget(button('取消',d.reject));go=button('确认保存默认',d.accept,True);go.setEnabled(bool(plan['values']));row.addWidget(go);v.addLayout(row)
    if d.exec()!=QDialog.DialogCode.Accepted:return False
    try:history.apply(host.work_repository,plan)
    except Exception as exc:QMessageBox.warning(parent,'未保存',str(exc));return False
    host.saved_store_defaults=host.work_repository.stores()
    for code in plan['values']:
        if code in host.store_widgets:host.refresh_parameter_label(code)
    QMessageBox.information(parent,'默认设置已保存','以后重新检查文件时，沿用默认的门店将带入新值。当前明确修改/历史恢复的配置保留；要改本次，请使用“编辑本次作业参数”并重算。')
    return True

def open_history(parent,host):
    d=QDialog(parent);d.setWindowTitle('默认设置变更记录');d.resize(900,620);v=QVBoxLayout(d)
    v.addWidget(label('撤销只还原未被后续修改的门店；恢复旧配置会先比较当前值。升级前未留存的旧值不能恢复。'))
    choices=QComboBox();v.addWidget(choices)
    table,model=grid(d,['门店编码','设置项','变更前','变更后']);fit_diff_grid(table);v.addWidget(table,1)
    def load():
        choices.clear()
        for e in history.history(host.work_repository):choices.addItem(f"{e['at']} · {re.sub(r'[0-9a-f]{32}',lambda m:m[0][:8],e['action'])} · {len(e['after'])}店 · {e['id'][:8]}",e)
    def show():
        e=choices.currentData();model.replace(diffs({'values':e['after'],'expected':e['before']}) if e else [])
    choices.currentIndexChanged.connect(show)
    def apply(restore):
        e=choices.currentData()
        if e and confirm_plan(d,host,history.plan(host.work_repository,e,restore=restore)):load()
    row=QHBoxLayout();row.addWidget(button('撤销此批变更',lambda:apply(False)));row.addWidget(button('恢复为此批保存后的配置',lambda:apply(True)));row.addWidget(button('关闭',d.accept));v.addLayout(row);load();d.exec()

def reset_defaults(parent,host,codes):
    if not codes:return
    old=host.work_repository.stores();d=QDialog(parent);d.setWindowTitle('重置勾选门店默认');v=QVBoxLayout(d)
    v.addWidget(label('只处理勾选范围。参数方案包含分类覆盖、全部超参和仓周期。'))
    p=QComboBox()
    for preset in host.work_repository.presets():p.addItem(f"{preset['name']} v{preset['version']}",preset)
    v.addWidget(p);params=QCheckBox('替换完整参数方案（清除门店分类覆盖）');params.setChecked(True);v.addWidget(params)
    route=QCheckBox('清除默认主从仓（下次必须重新指定）');budget=QCheckBox('清除默认预算（下次重新选择）');v.addWidget(route);v.addWidget(budget)
    row=QHBoxLayout();row.addWidget(button('取消',d.reject));row.addWidget(button('下一步：核对差异',d.accept));v.addLayout(row)
    if d.exec()!=QDialog.DialogCode.Accepted:return
    values={}
    for s in codes:
        x=copy.deepcopy(old.get(s) or {'preset':p.currentData(),'ui':{'selected':True},'saved':''})
        if params.isChecked():x['preset']=copy.deepcopy(p.currentData())
        if route.isChecked():x['ui'].update(primary=None,secondary=None)
        if budget.isChecked():x['ui'].update(mode=0,amount='')
        x['saved']=wr.now();values[s]=x
    confirm_plan(parent,host,{'values':values,'expected':{s:old.get(s) for s in codes},'conflicts':[],'action':'重置指定门店默认'})

def restore_presets(parent,host):
    d=QDialog(parent);d.setWindowTitle('恢复已停用参数方案');v=QVBoxLayout(d);choices=QComboBox();v.addWidget(choices)
    for p in host.work_repository.presets(include_archived=True):
        if p.get('archived'):choices.addItem(f"{p['name']} v{p['version']}",p)
    v.addWidget(label('恢复后重新出现在可选方案中；不会替换任何门店或历史单。'))
    row=QHBoxLayout();row.addWidget(button('取消',d.reject));go=button('恢复所选方案',d.accept);go.setEnabled(choices.count()>0);row.addWidget(go);v.addLayout(row)
    if d.exec()==QDialog.DialogCode.Accepted:
        p=choices.currentData();host.work_repository.restore_preset(p['id'],p['version'])
