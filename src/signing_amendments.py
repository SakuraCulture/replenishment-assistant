"""Issue a corrected revision of one unexecuted warehouse signing."""
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import archive_orders
import jst_order_store
import lifecycle_routes
import lifecycle_store
import normal_orders
import order_amendments
import preissue
import purchases
import result_semantics as sem
import storage_chunks as chunks
import warehouse_catalog
import warehouse_execution as execution
import work_repository as wr
from admission import execution_evidence, snapshot
from jst_allocation import number, status_issue
from jst_order_guards import saved_execution_reason
from platform_import_files import author
from runtime_support import CommitProgress, checkpoint
from sqlite_write import begin


def _scope(db, head):
    if head['revision']!=head['current_revision'] or head.get('kind')=='material':
        raise wr.Conflict('请选择有效的补货仓单；物料需求在物料单作业中更正')
    if head['warehouse'] not in execution.WAREHOUSES:
        raise ValueError('原单供货仓待核')
    purchases.validate_head(db, head)
    blocked=saved_execution_reason(db,head)
    if blocked or jst_order_store.active(db,head):
        raise wr.Conflict((blocked or '本仓单已分配')+'，请在对应环节更正')
    if lifecycle_store.exists(db) and any(lifecycle_store.latest_version(db,head,stage)
                                         for stage in ('return','operations')):
        raise wr.Conflict('本仓单已有回传或运营签发，请在该环节更正')
    parent=db.execute("SELECT current FROM normal_orders WHERE oid=? AND kind='normal_model'",(head['oid'],)).fetchone()
    if not parent:
        raise ValueError('本地签发记录不存在')
    return parent[0]


def _source(db, repo, head):
    return execution.project(execution.get_order(repo,head,db=db)['payload'],head['warehouse'])


def _line(row):
    if row is None:return None
    quantity, amount=sem.order_quantity(row),sem.order_amount(row)
    return {'sku':row['E'],'name':row.get('D'),'base_quantity':quantity if sem.numeric(quantity) else None,
            'amount':amount if sem.numeric(amount) else None,'unit':row.get('G')}


def read(path,head,stage,progress=lambda _:None,cancel=None):
    checkpoint(cancel);repo=normal_orders.read_repository(path)
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        parent=_scope(db,head)
        payload=_source(db,repo,head)
        return {'token':parent,'version':f"V{head['revision']:03d}",'saved':head['saved'],
                'rows':[_line(row) for row in payload['result']['rows']]}


def _warehouse_rows(previous,warehouse,rows):
    selected=iter(rows)
    result=[]
    for row in previous:
        if archive_orders.warehouse(row)==warehouse:
            replacement=next(selected,None)
            if replacement is not None:result.append(replacement)
        else:
            result.append(row)
    result.extend(selected)
    if len({r['E'] for r in result})!=len(result):
        raise ValueError('更正商品已归属另一仓，请在原仓单处理，避免跨仓重复签发')
    return result


def _apply(rows, changes, head, items, audit):
    current={row['E']:row for row in rows};differences=[];manual={};placeholders=[]
    for index,(sku,edit) in enumerate(changes.items()):
        before=current.get(sku)
        previous=_line(before)
        if edit['action']=='delete':
            if before is None:raise ValueError('原签发没有该商品：'+sku)
            del current[sku]
            differences.append({'sku':sku,'action':'delete','before':previous,'after':None})
            continue
        requested=Decimal(edit['quantity'])
        if before is None:
            item=items.get(sku)
            if not item:raise ValueError(sku+' 在本仓商品资料中不存在')
            if issue:=status_issue(item):raise ValueError(sku+' '+issue)
            if not requested:raise ValueError('新增签发商品须有正数数量：'+sku)
            unit=str(item.get('商品单位') or '库存单位')
            price=number(item.get('基本售价'),'基本售价')
            step=number(item.get('出库箱规') or 1,'箱规')
            if not price or step<1 or step!=step.to_integral_value():
                raise ValueError(sku+' 的基本售价或出库箱规待核')
            before={'B':head['store'],'C':item.get('分类'),'D':item.get('商品名称') or edit.get('name') or sku,
                    'E':sku,'G':unit,'S':item['商品状态'],'V':preissue.money_value(price),
                    'BD':int(step),'AU':head['warehouse'],
                    'AS':None,'BV':0,'_execution_qty':0,'_execution_amount':0,'_source':'资料匹配',
                    '_meta':{'blocking':[]}}
            placeholders.append(before)
        elif requested > Decimal(str(previous['base_quantity'] or 0)):
            if issue:=preissue.blocked(before):raise ValueError(sku+' '+issue)
        qty=preissue.quantity(requested,before.get('BD',1))
        if previous and previous['base_quantity']==qty:continue
        amount=Decimal(str(qty))*number(before.get('V'),'基本售价') if qty else Decimal(0)
        values={'_execution_qty':qty,'_execution_amount':preissue.money_value(amount)}
        if before.get('_budget_active'):
            values.update(_allocated_qty=qty,_allocated_amount=preissue.money_value(amount),_allocated_amount_exact=str(amount))
        after={**before,**values,'_signed_correction':{**audit,'before':previous,'quantity':qty,'amount_exact':str(amount)}}
        current[sku]=after
        manual[sku]={'sequence':index}
        differences.append({'sku':sku,'action':'set','before':previous,'after':_line(after)})
    return list(current.values()),differences,manual,placeholders


def prepare(path,head,stage,expected,changes,operator,reason,evidence=None,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    audit={**author(operator,reason),'id':uuid.uuid4().hex,'at':wr.now()}
    changes=order_amendments.edits(changes,allow_blank=False)
    warehouse=head['warehouse']
    repo=normal_orders.read_repository(path)
    catalogs=warehouse_catalog.Repository(Path(path).parent).catalogs()
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        parent=_scope(db,head)
        if parent!=expected:raise wr.Conflict('原签发版本已变化，请重新读取')
        child=_source(db,repo,head)
        record=repo.get(head['day'],head['store'],parent)['payload'];result=record['result']
        fixed,diff,edits,placeholders=_apply(child['result']['rows'],changes,head,catalogs.get(warehouse,{}).get('items',{}),audit)
        if not diff:raise ValueError('本次没有签发数量变化')
        if warehouse not in execution.warehouses({'result':{'rows':fixed},'new_focus':record.get('new_focus')}):
            raise ValueError('整仓签发无可执行商品，请从订单维护结束该仓单')
        increasing=[r['sku'] for r in diff if r['after'] and (r['after']['base_quantity'] or 0) > ((r['before'] or {}).get('base_quantity') or 0)]
        proof=execution_evidence(path,head['store'],warehouse,increasing,catalogs=catalogs,db=db) if increasing else None
        if proof:
            for sku, facts in proof['items'].items():
                if facts['reasons']:raise ValueError(sku+' '+ '、'.join(facts['reasons']))
        original=_warehouse_rows(result['rows'],warehouse,child['result']['rows'])
        final=_warehouse_rows(result['rows'],warehouse,fixed)
        summary=sem.summarize(final)
        budget_base={**result,'rows':original+placeholders}
        budget_rows=[]
        for row in final:
            correction=row.get('_signed_correction')
            if row['E'] in edits and correction:
                row={**row,'_preissue':{'delta':1,'amount_exact':correction['amount_exact']}}
            budget_rows.append(row)
        removed={row['sku'] for row in diff if row['action']=='delete'}
        budget_rows.extend({**row,'_execution_qty':0,'_execution_amount':0,'_budget_active':False,
                            '_preissue':{'delta':1,'amount_exact':'0'}} for row in original if row['E'] in removed)
        budget,errors,risk=preissue._budget(budget_base,{**result,'rows':budget_rows,'summary':summary},edits,head['digest'])
        if errors:raise ValueError('、'.join(errors))
        updated={**result,'rows':final,'summary':summary,'budget':budget,
                 'signed_correction':{**audit,'prior':head['revision'],'warehouse':warehouse,'changes':diff}}
        record={**record,'result':updated,'item_ids':{r['E']:normal_orders.item_id(head['oid'],r['E']) for r in final}}
        source_digest=normal_orders.fingerprint_owned(updated,record.get('new_focus'))
        value={'head':head,'parent':parent,'record':record,'digest':source_digest,'id':audit['id'],
               'at':audit['at'],'admission':proof,'risk':risk}
    return {'changes':diff,'note':risk['text'] if risk else '', 'write':value}


def commit(path,plan,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    head=plan['head'];record=plan['record'];digest=plan['digest'];revision=plan['parent']+1
    warehouse=head['warehouse'];oid=head['oid'];approved=plan['admission']
    if normal_orders.fingerprint_owned(record['result'],record.get('new_focus'))!=digest:
        raise ValueError('更正后的签发预览已变化')
    encoded=chunks.prepare(record,binding=digest)
    chunks.backup_before_write(Path(path),progress)
    check=warehouse_catalog.CatalogRevisionCheck(Path(path).parent)
    with check.guard(bool(approved),cancel) as catalog_versions,closing(wr.Repository.open_existing(path).connect()) as db,db:
        begin(db,cancel)
        archive_orders.require_active(db,oid,warehouse)
        saved=db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(oid,revision)).fetchone()
        if saved==(digest,):return {'revision':revision,'signed':True}
        if _scope(db,head)!=plan['parent']:raise wr.Conflict('原签发版本已变化，请重新核对')
        if approved and (snapshot(path,head['store'],db=db)!=approved['context'] or
                         approved['catalog_revisions']!=catalog_versions):
            raise wr.Conflict('商品准入或仓资料已更新，请重新核对')
        execution.ensure_parent(db,oid,progress,cancel)
        db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,revision,digest,plan['at'],chunks.put(db,encoded,cancel=cancel)))
        execution.publish(db,oid,revision,digest,plan['at'],record,[warehouse],progress,cancel)
        if lifecycle_routes.required(db,head):
            lifecycle_routes.schema(db)
            db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',(oid,revision))
        db.execute('UPDATE normal_orders SET current=? WHERE oid=?',(revision,oid))
        db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',
                   (plan['at'],oid,revision,'手动更正签发',wr.dumps(record['result']['signed_correction'])))
        if approved:check.verify()
        checkpoint(cancel);progress(CommitProgress(True));db.commit()
    progress(CommitProgress(False))
    return {'revision':revision,'signed':True}
