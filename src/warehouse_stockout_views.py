"""Warehouse-only combinations; no replenishment parameter changes."""
from stockout_common import digest,read_json,atomic_json
from warehouse_stockout import dimensions

def defaults():
    return {'version':1,'active':['warehouse'],'combinations':[{'name':'大仓总览','dimensions':['warehouse']},
        {'name':'供应商分类','dimensions':['warehouse','supplier','category']},{'name':'大仓商品','dimensions':['warehouse','sku']}]}

def combinations(values):
    if not isinstance(values,list) or not 1<=len(values)<=32:raise ValueError('请选择1至32套组合')
    names=set();result=[]
    for v in values:
        name=v.get('name','').strip()
        if not 1<=len(name)<=40 or name.casefold() in names:raise ValueError('组合名称须为1至40字且不重复')
        names.add(name.casefold());result.append({'name':name,'dimensions':dimensions(v['dimensions'])})
    return result

def validate(value):return {'version':1,'active':dimensions(value['active']),'combinations':combinations(value['combinations'])}

def load(repo):
    path=repo.folder/'views.json';value=validate(read_json(path,256*1024)) if path.exists() else defaults()
    return {**value,'revision':digest(value)}

def save(repo,value,expected):
    value=validate(value)
    if load(repo)['revision']!=expected:raise ValueError('切面配置已变化，请刷新')
    atomic_json(repo.folder/'views.json',value);return load(repo)
