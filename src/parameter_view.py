"""Readable effective-value comparison, without inventing persisted provenance."""
def flatten(value,prefix=''):
    if isinstance(value,dict):
        for k,v in value.items():yield from flatten(v,prefix+' / '+str(k) if prefix else str(k))
    else:yield prefix,value

def display_path(path):
    import parameter_schema as schema
    labels={'parameters':'模型超参','weight_profiles':'分类权重','profile_parameters':'分类超参','category_profiles':'分类映射','warehouses':'各仓周期','exception':'启用一箱例外','alpha':'趋势敏感度','w1':'最近7天权重','w2':'此前7天权重'}
    labels.update({k:v[0] for k,v in schema.HELP.items()})
    from restock_policy import LABELS,KEY
    labels.update(LABELS);labels[KEY]='补货策略'
    return ' / '.join(labels.get(k,k) for k in path.split(' / '))

def comparison(standard,preset,current,edited):
    import parameter_schema as schema
    from restock_policy import KEY,values
    maps=[dict(flatten({**c,KEY:values(c)})) for c in (standard,preset,current,edited)];rows=[]
    for key in dict.fromkeys(k for m in maps for k in m):
        values=[m.get(key,'未设置') for m in maps]
        origin='本次编辑' if values[3]!=values[2] else '门店当前配置与方案不同' if values[2]!=values[1] else '方案值'
        effect='通用默认；实际按各仓周期' if key in ('parameters / depth','parameters / lead') else '分类值优先于门店通用值；无分类跟随快销；两仓共用' if key.startswith('profile_parameters /') else '路由后按商品分类/资料分类覆盖重新绑定' if key.startswith(('weight_profiles /','category_profiles /')) else '应用后生效'
        if key in {'parameters / '+k for k in schema.PROFILE_KEYS}:effect='门店通用值；已启用分类值时按分类覆盖'
        rows.append([key,*values,origin,effect])
    return rows
