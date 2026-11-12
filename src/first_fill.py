"""Optional warehouse first-fill evidence; never a recurring stock floor."""
from decimal import Decimal,DecimalException,ROUND_CEILING
import result_semantics as sem

FIELD='新店新品首铺数'
FIELDS=(FIELD,'新店新品首铺数量')

def basis(catalog,warehouse,sku):
    row=catalog['items'].get(sku) or {}
    return {'field':FIELD,'source_field':row.get('_first_fill_source_field',FIELD if FIELD in row else None),'known':FIELD in row,'raw':row.get(FIELD),'issue':row.get('_first_fill_issue'),'warehouse':warehouse,
            'catalog_revision':catalog['revision'],'source_sha256':catalog.get('source_sha256'),
            'sheet':catalog.get('sheet'),'row':row.get('_row')}

def suggestion(row):
    info=row.get('_meta',{}).get('first_fill',{});warehouse=row.get('AU')
    step=row.get('BD')
    if not sem.numeric(step) or not 0<step<=1e9:return None,'最小出库单位无效，首铺数量待维护'
    def minimum(reason):return float(step),reason+'；默认使用最小出库单位'+sem.fmt(step,8)+'（库存单位）'
    if warehouse not in ('山东仓','河北仓') or info.get('warehouse')!=warehouse or info.get('field')!=FIELD:
        return minimum('首铺来源未明确对应归属仓，未借用其他仓首铺数')
    if not info.get('catalog_revision') or info['catalog_revision']!=row.get('_meta',{}).get('product_revision'):
        return minimum('首铺来源与本次仓资料修订不一致，未采用该首铺数')
    if info.get('issue'):return minimum(warehouse+'：'+str(info['issue']))
    if not info.get('known'):return minimum(warehouse+'资料缺少“'+FIELD+'”')
    raw=info.get('raw')
    if raw is None or isinstance(raw,str) and not raw.strip():return minimum(warehouse+'“'+FIELD+'”为空')
    from input_adapter import numeric
    value=numeric(raw)
    if not sem.numeric(value) or value<=0 or value>1e9:
        return minimum(warehouse+'“'+FIELD+'”无效，原值'+repr(raw))
    try:
        aligned=(Decimal(str(value))/Decimal(str(step))).to_integral_value(rounding=ROUND_CEILING)*Decimal(str(step))
        quantity=float(aligned)
        if not sem.numeric(quantity) or quantity>1e9 or Decimal(str(quantity))!=aligned:return minimum('首铺数对齐箱规后超出可维护数量范围')
    except DecimalException:return minimum('首铺数无法安全对齐箱规')
    note=warehouse+'商品资料“'+(info.get('source_field') or FIELD)+'”='+sem.fmt(value,8)+'（库存单位）'
    if quantity!=value:note+='；按最小出库单位'+sem.fmt(step,8)+'向上对齐为'+sem.fmt(quantity,8)
    return quantity,note

def quantity_issue(row,value):
    if not sem.numeric(value) or value<=0:return '首铺数量必须为正数'
    step=row.get('BD')
    if not sem.numeric(step) or step<=0:return '最小出库单位待核'
    try:
        if Decimal(str(value))%Decimal(str(step))!=0:return '首铺数量须为最小出库单位倍数（'+sem.fmt(step,8)+'）'
    except DecimalException:return '首铺数量或箱规超出可校验范围，请核对'
    return ''
