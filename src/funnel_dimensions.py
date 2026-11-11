"""Read-only historical supplier/category properties; never change admission."""

def text(value):return None if value is None or not str(value).strip() else str(value).strip()

def attributes(row,catalog,sku,source):
    frozen=catalog.get('items',{}).get(sku) or {};original=source['items'].get(sku)
    notes=[];provenance=[]
    if catalog.get('revision'):provenance.append('冻结仓资料 '+str(catalog['revision']))
    if catalog.get('source_sha256'):
        provenance.append('原件 '+str(catalog['source_sha256'])+' / '+str(catalog.get('sheet') or '工作表未留存')+
                          (' / 第'+str(original['source_row'])+'行' if original and original.get('source_row') else ''))
    def resolve(field,extra=()):
        reasons=[];values=list(extra);heads=source['headers']
        if heads.count(field)>1:reasons.append('原件“'+field+'”表头重复')
        if sku in source['items'] and original is None:reasons.append('原件商品编码重复')
        if text(frozen.get(field)) is not None:values.append(('冻结仓资料',text(frozen[field])))
        if original and text(original.get(field)) is not None:values.append(('原件',text(original[field])))
        unique=list(dict.fromkeys(v for _,v in values if v is not None))
        if len(unique)>1:reasons.append(field+'来源冲突：'+'；'.join(k+'='+v for k,v in values))
        if reasons:return None,reasons
        if unique:return unique[0],[]
        if not source['present']:reasons.append('历史原件未留存且冻结资料缺少“'+field+'”')
        elif heads.count(field)==0:reasons.append('历史原件缺少“'+field+'”列')
        elif original is None:reasons.append('原件未唯一匹配商品编码')
        else:reasons.append('历史原件“'+field+'”为空')
        return None,reasons
    supplier,supplier_reasons=resolve('供应商名称')
    extra=[]
    if row:
        for key,value in [('签发商品类目',row.get('_category')),('签发来源类目',row.get('_meta',{}).get('category'))]:
            if text(value) is not None:extra.append((key,text(value)))
    category,category_reasons=resolve('分类',extra)
    notes.extend(supplier_reasons+category_reasons)
    return {'upstream_supplier':supplier,'product_category':category,
            'supplier_state':'已留存' if supplier is not None else '供应商待核',
            'category_state':'已留存' if category is not None else '类目待核',
            'dimension_notes':list(dict.fromkeys(notes)),'dimension_sources':'；'.join(provenance) or '维度依据未留存'}
