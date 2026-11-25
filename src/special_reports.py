"""Reconstruct source-row progress from immutable plans, package members and group receipts."""
from collections import defaultdict
from decimal import Decimal
import csv
import json

import special_plans as sp
import special_settlement as settlement


def detail(db,plan):
    original=sp.read(db,plan,1)
    current=sp.read(db,plan)
    original_rows={r['id']:r for r in original['lines']}
    traced=defaultdict(list)
    received=defaultdict(Decimal)
    settled={}
    pending=set()
    for gid, in db.execute('SELECT id FROM special_groups WHERE plan=?',(plan,)):
        doc=sp.latest(db,gid,'正式实收')
        if not doc:
            continue
        for r in doc['body']['rows']:
            if Decimal(r['pending']):
                pending.add((gid,r['sku']))
            for share in r['shares']:
                received[share['line']]+=Decimal(share['quantity'])
                if not Decimal(r['pending']):
                    settled[share['oid'],share['line']]=Decimal(share['quantity'])
    for p in settlement.packages(db,plan):
        actual,paid=settlement.sources(db,p)
        actuals={r['sku']:r for r in actual['body']['rows']} if actual else {}
        payments={r['sku']:r for r in paid['body']['rows']} if paid else {}
        group=settlement.group_id(p)
        effect=settlement.effect_for(db,group)
        formal=sp.latest(db,group,'正式实收')
        for i in p['items']:
            sku=i['sku']
            actual_qty=Decimal(actuals.get(sku,{}).get('quantity','0'))
            source_qty=actual_qty
            payment_amount=Decimal(payments.get(sku,{}).get('amount','0'))
            released=db.execute('SELECT qty FROM special_releases WHERE gid=? AND sku=?',(p['gid'],sku)).fetchone()
            valid=Decimal(i['quantity'])-(Decimal(released[0]) if released else 0)
            for c in i['claims']:
                q=Decimal(c['quantity'])
                allotted=min(actual_qty,q)
                held=min(valid,q)
                actual_qty-=allotted
                valid-=held
                held=settled.get((p['oid'],c['line']),held)
                traced[c['line']].append(dict(仓单=p['number'],线上号=p['online'],内部号=actual['body']['internal'] if actual else '',
                    代次=p['gid'],执行轮次=p['run'],来源份额=sp.text(q),有效份额=sp.text(held),平台实配=sp.text(allotted),
                    付款版本=paid['version'] if paid else '',付款金额=sp.text(payment_amount*allotted/source_qty) if paid and source_qty else '',
                    采购组=group,翱象原号=(effect or {}).get('original') or '',实收版本=formal['version'] if formal else '',
                    待认领=(group,sku) in pending))
    rows=[]
    for r in current['lines']:
        key=r['id']
        old=original_rows.get(key,{})
        entries=traced[key]
        target=Decimal(r['target'])
        orig=Decimal(old.get('target',0))
        got=received[key]
        held=sum((Decimal(i['有效份额']) for i in entries),Decimal(0))
        rows.append(dict(主单=plan,修订=current['version'],原行=r['source_row'],门店=r['store'],仓=r['warehouse'],SKU=r['sku'],
                         单位=r['unit'],原目标=sp.text(orig),当前目标=sp.text(target),已归属实收=sp.text(got),
                         平台实配=sp.text(sum((Decimal(i['平台实配']) for i in entries),Decimal(0))),
                         有效未结=sp.text(max(Decimal(0),held-got)),尚可补=sp.text(max(Decimal(0),target-held)),
                         原货盘缺口=sp.text(max(Decimal(0),orig-got)),实收待认领=any(i['待认领'] for i in entries),
                         经批准免供=sp.text(max(Decimal(0),orig-target)),原单来源=json.dumps(entries,ensure_ascii=False)))
    return rows


def export(path,plan,destination):
    with sp.connection(path) as db:
        rows=detail(db,plan)
    with open(destination,'x',encoding='utf-8-sig',newline='') as output:
        writer=csv.DictWriter(output,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return rows


def operating(db):
    import lifecycle_store
    categories=('普通','物料','政策','开业')
    result={kind:dict(付款件数=Decimal(0),商品付款额=Decimal(0),实收件数=Decimal(0),实收金额=Decimal(0)) for kind in categories}
    kinds={'material':'物料','policy':'政策','opening':'开业'}
    if lifecycle_store.exists(db):
        sql="""SELECT n.kind,v.oid,v.stage,v.version,v.saved,v.digest,v.payload FROM lifecycle_versions v
          JOIN normal_orders n ON n.oid=v.oid AND n.current=v.revision
          WHERE v.stage IN ('payment','receipt') AND v.version=(SELECT max(x.version) FROM lifecycle_versions x
            WHERE (x.oid,x.revision,x.warehouse,x.stage)=(v.oid,v.revision,v.warehouse,v.stage))"""
        for kind,oid,stage,version,saved,digest,raw in db.execute(sql):
            if kind=='opening' and stage=='receipt':continue
            doc=lifecycle_store.decode((version,saved,digest,raw))['payload']
            if doc.get('pending'):continue
            if kind in ('policy','opening'):
                original=sp.latest(db,oid,'原单')
                if original and original['body']['status']=='已取消':continue
            row=result[kinds.get(kind,'普通')]
            for item in doc['rows']:
                row['付款件数' if stage=='payment' else '实收件数']+=Decimal(str(item.get('base_quantity') or '0'))
                row['商品付款额' if stage=='payment' else '实收金额']+=Decimal(str(item.get('amount') or '0'))
    if sp.exists(db,'special_receipts'):
        for group, in db.execute('SELECT group_id FROM special_receipts'):
            for r in sp.latest(db,group,'正式实收')['body']['rows']:
                result['开业']['实收件数']+=Decimal(r['quantity'])
                result['开业']['实收金额']+=Decimal(r['amount'])
    result['全部']={key:sum(v[key] for v in result.values()) for key in result['普通']}
    return {k:{label:sp.text(value) for label,value in row.items()} for k,row in result.items()}
