"""Read-only store/SKU projections of frozen execution rows. No rule writes."""
from decimal import Decimal, InvalidOperation


def number(value):
    if value is None or value=='':return None
    try:
        n=Decimal(str(value))
        return n if n.is_finite() and n>=0 else None
    except (InvalidOperation,ValueError):return None


def metrics(entries):
    demand=Decimal(0);allocated=Decimal(0);reduced=Decimal(0);skus={};unknown=set();priced=0
    for head,row in entries:
        sku=(head['store'],row['sku']);q=number(row.get('quantity'));want=number(row.get('demand'));price=number(row.get('price'));amount=number(row.get('amount'))
        counts=skus.setdefault(sku,{'arranged':False,'reduced':False})
        counts['arranged'] |= q is not None and q>0
        counts['reduced'] |= want is not None and q is not None and q<want
        if want is None or q is None or price is None or amount is None:
            unknown.add(sku);continue
        priced+=1;demand+=want*price;allocated+=amount;reduced+=want*price-amount
    return {'demand':demand,'allocated':allocated,'reduced':reduced,'rate':allocated/demand if demand else None,
            'sku':len(skus),'arranged':sum(r['arranged'] for r in skus.values()),'reduced_sku':sum(r['reduced'] for r in skus.values()),
            'unallocated':sum(not r['arranged'] for r in skus.values()),'unknown':len(unknown),'priced_rows':priced}


def stores(plans):
    grouped={}
    for p in plans:
        h=p['head'];g=grouped.setdefault(h['store'],{'store':h['store'],'name':h.get('name') or h['store'],'plans':[],'entries':[]})
        g['plans'].append(p);g['entries'].extend((h,r) for r in p['rows'])
    for g in grouped.values():g['metrics']=metrics(g['entries'])
    return list(grouped.values())


def money(value):return f'{value:,.2f}'


def amount_text(m,key):
    return '待核' if m['sku'] and not m['priced_rows'] else money(m[key])+('（部分）' if m['unknown'] else '')


def caption(m):
    rate=f"{m['rate']:.1%}" if m['rate'] is not None else '—'
    return (f"回传需求 {amount_text(m,'demand')} 元  →  已分配 {amount_text(m,'allocated')} 元  ·  减少 {amount_text(m,'reduced')} 元  ·  金额满足率 {rate}\n"
            f"涉及 {m['sku']} 款次；已安排 {m['arranged']}，有减量 {m['reduced_sku']}，完全未安排 {m['unallocated']}；金额待核 {m['unknown']}。")
