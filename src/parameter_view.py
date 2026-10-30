"""Readable effective-value comparison, without inventing persisted provenance."""
def flatten(value,prefix=''):
    if isinstance(value,dict):
        for k,v in value.items():yield from flatten(v,prefix+' / '+str(k) if prefix else str(k))
    else:yield prefix,value

def comparison(standard,preset,current,edited):
    maps=[dict(flatten(c)) for c in (standard,preset,current,edited)];rows=[]
    for key in dict.fromkeys(k for m in maps for k in m):
        values=[m.get(key,'未设置') for m in maps]
        origin='本次编辑' if values[3]!=values[2] else '门店当前配置与方案不同' if values[2]!=values[1] else '方案值'
        effect='通用默认；实际按各仓周期' if key in ('parameters / depth','parameters / lead') else '路由后按商品分类/资料分类覆盖重新绑定' if key.startswith(('weight_profiles /','category_profiles /')) else '应用后生效'
        rows.append([key,*values,origin,effect])
    return rows
