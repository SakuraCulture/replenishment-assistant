"""Read-only product search projection and append-only list construction.

Selection identifies products, never supply warehouses, prices or final quantities.
The existing list preview remains authoritative for each store's business gates.
"""
import copy,uuid
import input_adapter as adapter
import stocking_lists as sl
from runtime_support import checkpoint
from export_progress import RowProgress


def products(catalogs,records,progress=lambda _:None,cancel=None):
    items={};stores={}
    def get(sku,name):
        if sku not in items:items[sku]={'sku':sku,'name':str(name or ''),'names':[],
            'variants':[],'selection_sources':[],'stores':[]}
        d=items[sku]
        if name and str(name) not in d['names']:d['names'].append(str(name))
        if not d['name'] and name:d['name']=str(name)
        return d
    total=sum(len(c.get('items',{})) for c in catalogs.values())+len(records);done=0
    for wh,catalog in catalogs.items():
        for sku,row in catalog['items'].items():
            checkpoint(cancel);done+=1
            if done%300==0:progress(RowProgress('整理可查询商品资料',done,total))
            code,error=adapter.identifier(sku)
            if error or code!=sku:raise ValueError('商品资料编码无效，请重新导入：'+str(sku))
            d=get(sku,row.get('商品名称'))
            d['variants'].append({'warehouse':wh,'category':str(row.get('分类') or '无分类'),'status':str(row.get('商品状态') or '状态未提供')})
            d['selection_sources'].append({'仓库':wh,'资料版本':catalog.get('revision'),
                '原文件':catalog.get('source_path'),'文件校验':catalog.get('source_sha256'),
                '工作表':catalog.get('sheet'),'原行号':row.get('_row')})
    catalog_skus=set(items)
    for row in records:
        checkpoint(cancel);done+=1
        if done%300==0:progress(RowProgress('核对已载入门店商品范围',done,total))
        sku,error=adapter.identifier(row.get('E'))
        if error:continue  # Invalid store identifiers cannot create selectable SKUs.
        s=row.get('B');stores.setdefault(sku,[])
        if s and s not in stores[sku]:stores[sku].append(s)
        # Legacy mixed-master users can select their matched records too. Local
        # or pending rows never manufacture a catalog product.
        if sku not in catalog_skus and row.get('_source')=='资料匹配':
            d=get(sku,row.get('D'));m=row.get('_meta',{})
            variant={'warehouse':str(row.get('AU') or '归属待核'),'category':str(row.get('U') or '无分类'),'status':str(row.get('S') or '状态未提供')}
            source={'仓库':row.get('AU'),'资料版本':m.get('product_revision'),
                '原文件':m.get('product_file'),'工作表':m.get('product_sheet'),'原行号':m.get('product_row')}
            if variant not in d['variants']:d['variants'].append(variant)
            if source not in d['selection_sources']:d['selection_sources'].append(source)
        elif sku in items:get(sku,row.get('D'))
    for sku,d in items.items():d['stores']=stores.get(sku,[])
    checkpoint(cancel);progress(RowProgress('可查询商品已就绪',total,total))
    return list(items.values())


def matches(item,needle='',warehouse='',category=''):
    tokens=needle.casefold().split();text=' '.join([item['sku'],item['name'],*item['names']]).casefold()
    return all(t in text for t in tokens) and any((not warehouse or v['warehouse']==warehouse) and (not category or v['category']==category) for v in item['variants'])


def append_products(value,selected):
    """Keep existing items and order exactly; append unique explicit choices."""
    d=copy.deepcopy(value);seen={i['sku'] for i in d['items']}
    for product in selected:
        sku,error=adapter.identifier(product.get('sku'))
        if error or sku!=product['sku']:raise ValueError('选品编码不可靠')
        if sku in seen:continue
        if not product.get('selection_sources'):raise ValueError('选品缺少商品资料依据')
        seen.add(sku);d['items'].append({'sku':sku,'name':product['name'],'target':None,
            'note':'搜索选商品','focused':False,'source_rows':[],
            'selection_sources':copy.deepcopy(product['selection_sources'])})
    d['revision']=sl.revision(d)
    return d  # An unfinished target strategy stays editable until existing validation.


def empty(name,kind):
    return {'schema':1,'id':uuid.uuid4().hex,'name':name,'kind':kind,'items':[],
        'sources':[],'defaults':{'strategy':'minimum','target':None,'scopes':['zero']},'valid_from':'','valid_to':''}
