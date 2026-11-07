"""Small analysis-only view preferences. No business settings or snapshot writes."""
from stockout_common import atomic_json,read_json,digest
from stockout_metrics import dimension_keys


def combinations(value):
    if not isinstance(value,list) or not 1<=len(value)<=32:raise ValueError('请选择1至32套切面组合')
    result=[];names=set()
    for item in value:
        if not isinstance(item,dict):raise ValueError('切面组合格式无效')
        name=item.get('name','')
        if not isinstance(name,str) or not name.strip() or len(name.strip())>40:raise ValueError('组合名称需为1至40字')
        name=name.strip()
        if name.casefold() in names:raise ValueError('组合名称不能重复')
        names.add(name.casefold());result.append({'name':name,'dimensions':list(dimension_keys(item.get('dimensions')))})
    return result


def defaults():
    return {'version':1,'active':['store'],'combinations':[
        {'name':'门店总览','dimensions':['store']},
        {'name':'门店供货结构','dimensions':['store','warehouse','supplier','category','mandatory']},
        {'name':'门店商品','dimensions':['store','sku']}]}


def validate(value):
    if not isinstance(value,dict) or value.get('version')!=1:raise ValueError('切面配置版本无效')
    return {'version':1,'active':list(dimension_keys(value.get('active'))),'combinations':combinations(value.get('combinations'))}


def load(repo):
    path=repo.folder/'views.json'
    value=validate(read_json(path,256*1024)) if path.exists() else defaults()
    return {**value,'revision':digest(value)}


def save(repo,value,expected):
    value=validate(value)
    if load(repo)['revision']!=expected:raise ValueError('切面组合已由其他窗口更新，请刷新记录后重新编辑')
    atomic_json(repo.folder/'views.json',value)
    return {**value,'revision':digest(value)}
