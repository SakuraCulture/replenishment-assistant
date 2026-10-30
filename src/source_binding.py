"""Explicit bridge provenance with identity verification; never silent offsets."""
from input_adapter import identifier


def bindings(table,records,headers):
    # All business records have store/SKU cells, so bridge projection must retain
    # exactly one row per input. Row labels may be sparse, but identities may not
    # change or move silently.
    rows,_=table
    if len(rows)!=len(records):raise ValueError('来源绑定失败：桥表记录数变化，请重新检查文件')
    result={}
    for raw,record in zip(rows,records):
        key=raw['_row']
        values=record['values']
        identity=tuple(identifier(values.get(k))[0] for k in ('仓库/门店编码','商品编码'))
        actual=tuple(identifier(raw.get(k))[0] for k in ('仓库/门店编码','商品编码'))
        if key in result or actual!=identity:raise ValueError('来源绑定失败：桥表身份或行号不一致')
        result[key]=(identity,record)
    return result


def restore(origins,d,meta):
    entry=origins.get(meta['export_row'])
    if entry is None or entry[0]!=(d['B'],d['E']):raise ValueError('来源绑定失败：计算输入与原商品不一致，已停止以免引用错行')
    return entry[1]

def master_bindings(table,records):
    rows,_=table
    if len(rows)!=len(records):raise ValueError('商品资料来源绑定失败：桥表记录数变化')
    result={}
    for raw,record in zip(rows,records):
        sku=identifier(raw.get('商品编码'))[0]
        if raw['_row'] in result or sku!=identifier(record.get('商品编码'))[0]:raise ValueError('商品资料来源绑定失败：编码或行号不一致')
        result[raw['_row']]=(sku,record['_row'])
    return result

def restore_master(origins,sku,row):
    entry=origins.get(row)
    if entry is None or entry[0]!=sku:raise ValueError('商品资料来源绑定失败：匹配商品与原行不一致')
    return entry[1]
