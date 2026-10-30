"""Warehouse export identities over immutable store revisions; no model changes."""
from app_defaults import WAREHOUSES

SUFFIXES={'河北仓':'HB','山东仓':'SD'}

def child_number(number,warehouse):
    if warehouse not in SUFFIXES:raise ValueError('无法识别确认单所属仓：'+str(warehouse))
    return number+'-'+SUFFIXES[warehouse]

def resolve(db,number):
    # Exact historical parent numbers remain valid, even if a store code contains HB/SD.
    row=db.execute('SELECT day,store,number FROM normal_orders WHERE number=?',(number,)).fetchone()
    if row:return row,None
    for wh,suffix in SUFFIXES.items():
        if number.endswith('-'+suffix):
            row=db.execute('SELECT day,store,number FROM normal_orders WHERE number=?',(number[:-len(suffix)-1],)).fetchone()
            if row:return row,wh
    raise ValueError('本机未找到补货单号：'+number+'；不得按名称或相近日期猜测')

def partitions(result,profile):
    from normal_exports import groups
    grouped=groups(result,profile);out={}
    for rows in grouped.values():
        for d in rows:
            if d.get('AU') not in WAREHOUSES:
                raise ValueError('无法分仓导出，商品 '+str(d.get('E'))+' 的归属仓缺失或未知：'+str(d.get('AU')))
    # Fixed warehouse order, original sheet and SKU order within each child.
    for wh in WAREHOUSES:
        subset={sheet:[d for d in rows if d['AU']==wh] for sheet,rows in grouped.items()}
        if any(subset.values()):out[wh]=subset
    return out

def summaries(result,profile):
    import result_semantics as sem
    return [{'warehouse':wh,'moving':len(g['中心仓动销补货清单']),
             'new':len(g['本月新品'])+len(g['往期新品']),
             'amount':round(sum(sem.order_quantity(d)*d['V'] for rows in g.values() for d in rows if sem.orderable(d) and sem.numeric(d.get('V'))),2)}
            for wh,g in partitions(result,profile).items()]

def feedback_status(eligible,values):
    import result_semantics as sem
    out={}
    for wh in WAREHOUSES:
        rows=[d for d in eligible.values() if d.get('AU')==wh]
        if not rows:continue
        required=[d['E'] for d in rows if sem.orderable(d)]
        pending=[s for s in required if s not in values]
        received=sum(d['E'] in values for d in rows)
        out[wh]={'pending':pending,'received':received,'status':('尚无回传' if not received else '部分确认' if pending else '已确认')}
    return out
