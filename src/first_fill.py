"""Optional warehouse first-fill evidence; never a recurring stock floor."""
from decimal import Decimal,DecimalException
import result_semantics as sem

FIELD='新店新品首铺数'

def basis(catalog,warehouse,sku):
    row=catalog['items'].get(sku) or {}
    return {'field':FIELD,'known':FIELD in row,'raw':row.get(FIELD),'issue':row.get('_first_fill_issue'),'warehouse':warehouse,
            'catalog_revision':catalog['revision'],'source_sha256':catalog.get('source_sha256'),
            'sheet':catalog.get('sheet'),'row':row.get('_row')}

def suggestion(row):
    info=row.get('_meta',{}).get('first_fill',{});warehouse=row.get('AU')
    if warehouse not in ('山东仓','河北仓') or info.get('warehouse')!=warehouse or info.get('field')!=FIELD:
        return None,'首铺来源未明确对应归属仓，请更新该仓资料并重新检查/计算'
    if not info.get('catalog_revision') or info['catalog_revision']!=row.get('_meta',{}).get('product_revision'):
        return None,'首铺来源与本次仓资料修订不一致，请重新检查/计算'
    if info.get('issue'):return None,warehouse+'：'+str(info['issue'])+'；首铺数量待维护'
    if not info.get('known'):return None,warehouse+'资料缺少“'+FIELD+'”，首铺数量待维护'
    raw=info.get('raw')
    if raw is None or isinstance(raw,str) and not raw.strip():return None,warehouse+'“'+FIELD+'”为空，首铺数量待维护'
    from input_adapter import numeric
    value=numeric(raw)
    if not sem.numeric(value) or value<=0 or value>1e9:
        return None,warehouse+'“'+FIELD+'”须为正数且不超过数量上限，原值'+repr(raw)+'；需维护或不选择'
    return float(value),warehouse+'商品资料“'+FIELD+'”='+sem.fmt(value,8)+'（库存单位）；最小出库单位仍须校验'

def quantity_issue(row,value):
    if not sem.numeric(value) or value<=0:return '首铺数量必须为正数'
    step=row.get('BD')
    if not sem.numeric(step) or step<=0:return '最小出库单位待核'
    try:
        if Decimal(str(value))%Decimal(str(step))!=0:return '首铺数量须为最小出库单位倍数（'+sem.fmt(step,8)+'）'
    except DecimalException:return '首铺数量或箱规超出可校验范围，请核对'
    return ''
