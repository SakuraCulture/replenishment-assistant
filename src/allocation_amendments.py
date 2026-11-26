"""Apply explicit quantity changes to one unsubmitted warehouse allocation."""
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import jst_batches as batches
import jst_orders as orders
import material_stock
import order_amendments
import order_lifecycle as lifecycle
import purchases
import warehouse_catalog
import work_repository as wr
from admission import execution_evidence
from jst_allocation import allocate, number, text, totals
from jst_order_store import read_generation
from platform_import_files import author
from runtime_support import checkpoint


def _scope(db, head):
    if head['revision'] != head['current_revision']:
        raise wr.Conflict('请打开本仓单的有效修订')
    purchases.validate_head(db, head)
    gid=orders.active(db,head)
    if not gid:
        raise ValueError('本仓单尚无已保存的分配')
    blocked, payment=orders.allocation_gate(db,head,gid,True)
    if blocked:
        raise wr.Conflict(blocked)
    old, digest=read_generation(db,gid)
    warehouse=head['warehouse']; batch_id=old.get('batch_id')
    batch=batches.context(db,batch_id) if batch_id else None
    if batch:
        state, stock=batch['states'][warehouse],batch['stocks'][warehouse]
    else:
        state, stock=orders.state(db,warehouse),orders.baseline(db,warehouse)
    if not state or not stock:
        raise ValueError('分配采用的仓资料未留存')
    if gid in stock['included']:
        raise wr.Conflict('原占用已计入仓快照，请从原平台单核对，不重复扣减或释放')
    basis,_,_,_=orders.return_basis(db,head)
    token=orders.digest([gid,digest,state,batch['epoch'] if batch else None,payment,basis])
    return old, state, stock, batch, payment, basis, token


def read(path, head, stage, progress=lambda _:None, cancel=None):
    checkpoint(cancel); head=lifecycle.full_head(path,head)
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        old,*_,token=_scope(db,head)
        return {'token':token,'version':old['gid'],'saved':old['saved'],
                'rows':[{**r,'base_quantity':r['quantity']} for r in old['rows']]}


def _entry(row):
    return {'base_quantity':row['quantity'],'amount':row['amount']} if row else None


def validate_material_claims(db, head, gid, requested):
    from material_orders import _claimed
    from material_requests import read as read_requests
    claimed=_claimed(db);extras={}
    for sku,quantity in requested.items():
        row=db.execute('SELECT quantity FROM jst_reservations WHERE gid=? AND sku=?',(gid,sku)).fetchone()
        before=Decimal(row[0]) if row else Decimal(0)
        after=Decimal(quantity)
        if after<=before:continue
        for request,limit in db.execute('SELECT request,quantity FROM material_claims WHERE oid=? AND warehouse=? AND sku=? AND retired=0 ORDER BY position',
                                       (head['oid'],head['warehouse'],sku)):
            limit=Decimal(limit)
            previous,new=min(before,limit),min(after,limit)
            before-=previous;after-=new
            if new>previous:
                key=(request,sku)
                extras[key]=extras.get(key,Decimal(0))+new-previous
        if after:
            raise wr.Conflict(sku+' 超出原仓单已核对的物料申请范围，请从物料需求登记增量')
    for (request,sku),extra in extras.items():
        docs=read_requests(db,request)
        line=next((row for doc in docs for row in doc['lines'] if row['sku']==sku),None)
        if not line or claimed.get((request,sku),Decimal(0))+extra>Decimal(line['quantity']):
            raise wr.Conflict(sku+' 的物料需求已变化或余量已分配，请核对原申请')


def prepare(path, head, stage, expected, changes, operator, reason, evidence=None,
            progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    audit=author(operator,reason)
    changes=order_amendments.edits(changes,allow_blank=False)
    head=lifecycle.full_head(path,head); warehouse=head['warehouse']
    catalogs, versions=warehouse_catalog.Repository(Path(path).parent).snapshots()
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        old,state,stock,batch,payment,basis,token=_scope(db,head)
        if token!=expected:
            raise wr.Conflict('原分配或仓库存已变化，请重新打开更正')
        gid, batch_id=old['gid'],old.get('batch_id')
        material=material_stock.has_material(db) and material_stock.is_material(db,head['oid'])
        rows={r['sku']:dict(r) for r in old['rows']}
        difference=[]; increase={}
        for sku, edit in changes.items():
            before=rows.get(sku)
            if edit['action']=='delete':
                if before is None:raise ValueError('原分配没有可删除的商品：'+sku)
                del rows[sku]
            else:
                qty=number(edit['quantity'],'更正数量'); old_qty=number(before['quantity'],'原分配数量') if before else Decimal(0)
                if qty > old_qty:increase[sku]=edit['quantity']
                if not before and qty==0:
                    raise ValueError('新增分配的商品须有正数数量：'+sku)
                rows[sku]=before or {'sku':sku,'name':edit.get('name')}
        if material and increase:validate_material_claims(db,head,gid,increase)
        b=stock['catalog'];exclude=(gid,)
        cross=material_stock.cross_reservations(db,warehouse,stock,set(rows),material=material,batch_id=batch_id,exclude=exclude)
        held=batches.reservations(db,batch_id,warehouse,exclude) if batch_id else ({} if material else orders.reservations(db,warehouse,set(stock['included']),exclude))
        for sku, qty in cross['reserved'].items():
            held[sku]=held.get(sku,Decimal(0))+Decimal(qty)
        admission=None
        if increase:
            effective=dict(catalogs)
            if warehouse not in effective or str(versions.get(warehouse)).startswith('legacy:'):
                effective[warehouse]=b
            admission=execution_evidence(path,head['store'],warehouse,increase,catalogs=effective,db=db)
            admission['default_catalog_revisions']={w:c['revision'] for w,c in catalogs.items()}
        old_rows={r['sku']:r for r in old['rows']}
        for sku, edit in changes.items():
            checkpoint(cancel)
            before=old_rows.get(sku)
            if edit['action']=='set':
                qty=number(edit['quantity'],'更正数量')
                if sku in increase:
                    if cross['uncertain'].get(sku):
                        raise wr.Conflict(sku+' 原执行是否已计入仓库存待核')
                    reasons=admission['items'][sku]['reasons']
                    if reasons:raise ValueError(sku+' '+ '、'.join(reasons))
                    item=b['items'].get(sku)
                    if not item:raise ValueError(sku+' 原批仓资料中没有该商品')
                    balance=number(item.get('可用数'),'可用库存')-held.get(sku,Decimal(0))
                    admitted=allocate([{'sku':sku,'name':edit.get('name') or rows[sku].get('name'),'base_quantity':text(qty)}],
                                      b['items'],{sku:balance},material=material,exact=True)[0]
                    if admitted['issue'] or Decimal(admitted['quantity'])!=qty:
                        raise ValueError(f"{sku} {admitted['reason']}，可分配 {admitted['quantity']}，拟更正为 {text(qty)}")
                    if before:
                        admitted['demand']=before['demand']
                        admitted['missing']=text(max(Decimal(before['demand'])-qty,Decimal(0)))
                    rows[sku]={**(before or {}),**admitted}
                elif Decimal(before['quantity'])!=qty:
                    row=rows[sku]
                    amount=text(qty*number(row['price'],'原分配单价')) if qty else '0'
                    item=b['items'].get(sku,{}); available=item.get('可用数')
                    base=number(available,'可用库存') if available not in (None,'') else None
                    balance=base-held.get(sku,Decimal(0)) if base is not None else None
                    row.update(quantity=text(qty),amount=amount,stock=text(base) if base is not None else None,
                               before=text(balance) if balance is not None else None,
                               after=text(balance-qty) if balance is not None else None,
                               missing=text(max(Decimal(row['demand'])-qty,Decimal(0))),reason='手动更正分配数量')
            after=rows.get(sku)
            if _entry(before)!=_entry(after):
                difference.append({'sku':sku,'action':edit['action'],'before':_entry(before),'after':_entry(after)})
        if not difference:
            raise ValueError('本次没有分配数量变化')
        if evidence:
            raise ValueError('分配更正使用本批仓资料和分配记录作为凭据，请在原因中登记补充凭据编号')
        runid=uuid.uuid4().hex
        detail={**audit,'id':runid,'at':wr.now(),'prior':gid,'basis_stock':stock['bid'],'changes':difference}
        if material and increase:detail['material_increase']=increase
        goods=list(rows.values()); compact={k:v for k,v in head.items() if k!='products'}
        item={**old,'head':compact,'gid':uuid.uuid4().hex,'operation':runid+':0','old':gid,
              'bid':stock['bid'],'catalog':warehouse_catalog.metadata(b),'basis':basis,
              'signed_products':head['products'],'rows':goods,'summary':totals(goods),'saved':wr.now(),
              'manual_correction':detail,'material_cross':cross,'material_cross_exclude':[gid]}
        if batch:item['batch_epoch']=batch['epoch']
        item.pop('admission',None);item.pop('previous_payment',None)
        if admission:item['admission']=admission
        if payment:item['previous_payment']=payment
        result={'runid':runid,'states':{warehouse:state},'plans':[item],'skipped':[],'correction':True}
        if batch:result.update(batch_id=batch_id,batch_epoch=batch['epoch'])
    return {'changes':difference,'write':{'payload':result,'digest':orders.digest(result)}}


def commit(path, plan, progress=lambda _:None, cancel=None):
    result=orders.commit(path,plan,True,progress,cancel)
    if result['errors']:
        raise wr.Conflict(result['errors'][0]['error'])
    if not result['generated'] and not result['zero']:
        raise wr.Conflict('更正未保存，请打开原分配记录核对')
    return result
