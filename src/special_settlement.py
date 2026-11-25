"""Original-order reconciliation, human payment confirmation and one receipt per purchase group."""
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import hashlib
import json

import jst_order_store as stock
import lifecycle_store
import order_lifecycle as lifecycle
import special_plans as sp
import work_repository as wr
from aoxiang_purchase_pricing import priced_row
from special_plans import number, text, required, uid

ZERO=Decimal(0)


def package(db,oid):
    row=db.execute('SELECT plan,run,gid,number,online,scope,body FROM special_packages WHERE oid=?',(oid,)).fetchone()
    if not row:
        raise ValueError('请选择专项执行仓单')
    plan,run,gid,number_local,online,scope,body=row
    data=json.loads(body)
    return dict(plan=plan,run=run,gid=gid,number=number_local,online=online,oid=oid,scope=json.loads(scope),items=data['items'])


def packages(db,plan):
    return [package(db,oid) for oid, in db.execute('SELECT oid FROM special_packages WHERE plan=? ORDER BY rowid',(plan,))]


def head(db,p):
    g,_=stock.read_generation(db,p['gid'])
    return g['head']


def policy_received(db,p):
    if db.execute('SELECT kind FROM special_plans WHERE plan=?',(p['plan'],)).fetchone()!=('policy',):
        return {}
    row=db.execute("SELECT 1 FROM lifecycle_versions WHERE oid=? AND warehouse=? AND stage='receipt' LIMIT 1",
                   (p['oid'],p['scope']['warehouse'])).fetchone() if lifecycle_store.exists(db) else None
    if not row:return {}
    document=lifecycle.current(db,head(db,p),'receipt')
    if not document or document['payload'].get('pending'):return {}
    quantities=defaultdict(Decimal)
    for r in document['payload']['rows']:quantities[r['sku']]+=number(r['base_quantity'],'已归属实收')
    received={}
    for item in p['items']:
        left=quantities[item['sku']]
        for c in item['claims']:
            q=min(left,Decimal(c['quantity']))
            received[c['line']]=q
            left-=q
    return received


def release_unsubmitted(db,oid,actor,reason):
    p=package(db,oid)
    previous=sp.latest(db,oid,'未发送撤销')
    if previous:return previous
    from jst_auto_order_store import exists as has_tasks
    task=db.execute('SELECT stage FROM jst_auto_orders WHERE oid=? AND warehouse=?',(oid,p['scope']['warehouse'])).fetchone() if has_tasks(db) else None
    h=head(db,p)
    if (task and task[0] not in ('PREPARED','PREVIEWED','READY') or stock.locked(db,h) or sp.latest(db,oid,'原单') or
            sp.latest(db,oid,'聚水潭原单回读') or lifecycle.current(db,h,'payment') or lifecycle.current(db,h,'receipt')):
        raise ValueError('无法证实原包未发送，请沿原线上号核对 '+p['online'])
    doc=sp.record(db,oid,'未发送撤销',dict(gid=p['gid'],online=p['online'],reason=required(reason,'撤销依据')),actor)
    for row in p['items']:
        db.execute('INSERT INTO special_releases VALUES(?,?,?,?,?)',(p['gid'],p['scope']['warehouse'],row['sku'],row['quantity'],doc['sha']))
    db.execute('DELETE FROM jst_active WHERE gid=? AND oid=? AND warehouse=?',(p['gid'],oid,p['scope']['warehouse']))
    return doc


def note_file(db,path):
    raw=Path(path).read_bytes()
    sha=hashlib.sha256(raw).hexdigest()
    lifecycle_store.schema(db)
    db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)',(sha,Path(path).name,raw))
    return json.loads(raw.decode('utf-8-sig')),sha


def note_stock_inclusion(path,file,actor):
    import warehouse_catalog
    from catalog_write_lock import acquire
    value=json.loads(Path(file).read_text('utf-8-sig'))
    wh=required(value['warehouse'],'仓')
    with acquire(Path(path).parent,wh),sp.connection(path,True) as db:
        source,sha=note_file(db,file)
        if source!=value:raise wr.Conflict('凭证在读取期间已变化')
        catalog=warehouse_catalog.Repository(Path(path).parent).snapshots((wh,))[0][wh]
        if (value['source_sha'],value['revision'])!=(catalog['source_sha256'],catalog['revision']):
            raise wr.Conflict('凭证与当前仓资料的原件摘要或版本不符')
        required(value.get('reason'),'平台或仓方核对依据')
        seen=set()
        for e in value.get('rows',[]):
            gid,sku=e['gid'],e['sku']
            if (gid,sku) in seen:raise ValueError('同一份凭证重复列出代次和 SKU')
            seen.add((gid,sku))
            row=db.execute("""SELECT n.number,r.quantity FROM jst_active a JOIN normal_orders n ON n.oid=a.oid
                  JOIN jst_reservations r ON r.gid=a.gid WHERE a.gid=? AND a.warehouse=? AND r.sku=?""",(gid,wh,sku)).fetchone()
            if not row or e.get('order_number') not in (row[0],row[0]+('-SD' if wh=='山东仓' else '-HB')):
                raise ValueError('原仓单、仓、SKU 或代次不一致 '+sku)
            qty=number(e['included'],'仓快照已含量')
            if qty>Decimal(row[1]) or not e.get('platform_reference'):raise ValueError('数量超出原认领或缺少仓方／平台原件编号')
            key=(wh,value['source_sha'],gid,sku)
            old=db.execute('SELECT included,sha FROM special_stock_evidence WHERE (warehouse,source_sha,gid,sku)=(?,?,?,?)',key).fetchone()
            if old and (Decimal(old[0])!=qty or old[1]!=sha):raise wr.Conflict('该原件计入口径已登记，冲突请核对新仓资料')
            db.execute('INSERT OR IGNORE INTO special_stock_evidence VALUES(?,?,?,?,?,?,?,?)',(*key,text(qty),sha,required(actor,'经办人'),sp.now()))
        if not seen:raise ValueError('凭证缺少原代次、SKU 及已含量')
    return len(seen)


def original_from_platform(db,p):
    record=sp.latest(db,p['oid'],'聚水潭原单回读')
    if not record:
        raise ValueError('先在聚水潭原任务核对原线上号 '+p['online'])
    v=record['body']
    units={r['sku']:r['unit'] for r in p['items']}
    finals={r['sku']:r['reference'] for r in v['final'] if r.get('reference')}
    cancelled=v['status']=='已取消'
    rows=[dict(sku=r['sku'],unit=units[r['sku']],quantity='0' if cancelled else r['qty'],
               amount='0' if cancelled else r['amount'],final_evidence=record['sha'] if cancelled else finals.get(r['sku']))
          for r in (p['items'] if cancelled else v['rows'])]
    obj={**p['scope'],'gid':p['gid'],'online':p['online'],'internal':v['internal'],'status':v['status'],'task':p['gid'],'rows':rows}
    raw=wr.dumps(obj).encode('utf8')
    sha=hashlib.sha256(raw).hexdigest()
    lifecycle_store.schema(db)
    db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)',(sha,'聚水潭原单_'+v['internal']+'.json',raw))
    return obj,sha


def receive_original(path,oid,file,actor):
    """Operator records a scoped original with its item evidence. Order-count acknowledgements do not qualify."""
    with sp.connection(path,True) as db:
        p=package(db,oid)
        source,sha=note_file(db,file) if file is not None else original_from_platform(db,p)
        for field in ('store','warehouse','account','shop'):
            if source.get(field)!=p['scope'][field]:
                raise ValueError('原回执身份不符 '+field+'，请查 '+p['number'])
        if source.get('online')!=p['online'] or source.get('gid')!=p['gid']:
            raise ValueError('线上单号或库存代次不符，请沿原包核对')
        internal=required(source.get('internal'),'平台内部原号')
        task=required(source.get('task'),'平台原任务号')
        state=source.get('status')
        if state not in ('已付款待审核','已审核','已发货','已完成','已取消'):
            raise ValueError('需要可核实的原单当前状态')
        prev=sp.latest(db,oid,'原单')
        if prev and (prev['body']['internal'],prev['body']['task'])!=(internal,task):
            raise wr.Conflict('已关联的内部单号或任务不一致')
        if prev and prev['body']['status']=='已取消' and state!='已取消':
            raise wr.Conflict('已确认取消的原单不得恢复原承诺')
        native=db.execute('SELECT internal,stage,proof FROM jst_auto_orders WHERE online=?',(p['online'],)).fetchone() if sp.exists(db,'jst_auto_orders') else None
        observed=sp.latest(db,oid,'聚水潭原单回读')
        if not native or not observed or task!=p['gid']:
            raise ValueError('缺少本包原聚水潭任务与逐项回读，请在聚水潭原任务按原线上号查询 '+p['online'])
        binding=json.loads(native[2] or '{}').get('binding') or {}
        obs=observed['body']
        if (native[0]!=internal or str(binding.get('coid'))!=source['account'] or str(binding.get('shop_id'))!=source['shop']
                or (obs['internal'],obs['gid'],obs['status'])!=(internal,task,state)):
            raise ValueError('原件与已登记聚水潭账号、店铺、状态或内部原单不一致')
        final_rows={r['sku']:r for r in obs['final']}
        verified={r['sku']:r for r in obs['rows']}
        from jst_fulfillment_store import current as current_source,source as retained_source
        check=current_source(db,p['online'])
        if check and check.get('digest'):
            saved,_=retained_source(db,p['online'],check['digest'])
            if (saved['internal'],saved['gid'],saved.get('native_status'))==(internal,task,'已发货') and not saved.get('source_kind'):
                for item in saved['rows']:
                    verified[item['sku']]=dict(qty=item['quantity'],amount=item['amount'])
        expected={r['sku']:r for r in p['items']}
        seen=set()
        releases=[]
        for r in source.get('rows',()):
            sku=r['sku']
            if sku not in expected or sku in seen or r.get('unit')!=expected[sku]['unit']:
                raise ValueError('原单商品或单位与执行包不符 '+sku)
            seen.add(sku)
            qty=number(r['quantity'],'实配量')
            amount=number(r['amount'],'原商品额')
            observed_item=verified.get(sku) or {}
            if state!='已取消' and (number(observed_item.get('qty'),'平台原单逐项数量')!=qty or
                    number(observed_item.get('amount'),'平台原单逐项金额')!=amount):
                raise ValueError('逐项数量或金额与已查回原件不符，请先查原单 '+internal+' / '+sku)
            request=Decimal(expected[sku]['quantity'])
            if qty>request:
                raise ValueError('平台实配超出原请求，请先核对 '+sku)
            if state=='已取消' and (qty or not r.get('final_evidence')):
                raise ValueError('取消须逐项有原单明确取消且没有实配的证据')
            delta=request-qty
            if delta and r.get('final_evidence'):
                final=final_rows.get(sku) or {}
                if state!='已取消' and (not final.get('reference') or final.get('unit')!=r['unit'] or
                        number(final.get('quantity'),'原单终结量')!=qty or number(final.get('outstanding'),'原单尚存承诺')!=0):
                    raise ValueError('缺少原任务逐 SKU 不再补配及无剩余承诺的回读依据 '+internal+' / '+sku)
                releases.append((sku,delta))
        if seen!=expected.keys():
            raise ValueError('缺少逐 SKU 原单明细，导入订单数不能证明余量可重下')
        for field in ('freight','adjustment'):
            v=Decimal(str(source.get(field,0)))
            if not v.is_finite() or field=='freight' and v<0:
                raise ValueError('费用无效')
        group=group_id(p)
        if db.execute("SELECT 1 FROM special_effects WHERE object=? AND stage='采购' AND state IN ('已发待核','已关联')",(group,)).fetchone():
            if not prev or prev['body']['file_sha']!=sha:
                raise wr.Conflict('原采购已经发出，请沿原采购处理差异')
        if state=='已取消' and (db.execute('SELECT 1 FROM special_receipts WHERE group_id=?',(group,)).fetchone() or any(policy_received(db,p).values())):
            raise wr.Conflict('已登记正式实收，须先核对实收归属')
        prior_release={s:Decimal(q) for s,q in db.execute('SELECT sku,qty FROM special_releases WHERE gid=?',(p['gid'],))}
        if any(dict(releases).get(s,ZERO)<q for s,q in prior_release.items()):
            raise wr.Conflict('原余量已获准释放，原单新结果与原证据冲突，请人工核查后继供应')
        source={**source,'file_sha':sha}
        result=sp.record(db,oid,'原单',source,actor)
        for sku,qty in releases:
            db.execute('INSERT INTO special_releases VALUES(?,?,?,?,?) ON CONFLICT(gid,sku) DO UPDATE SET qty=excluded.qty,source=excluded.source',
                       (p['gid'],p['scope']['warehouse'],sku,text(qty),sha))
        if state=='已取消':
            db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=? AND gid=?',(oid,p['scope']['warehouse'],p['gid']))
        return result


def payment(path,oid,source_sha,actor,*,amounts=None,reason=None,expected=None):
    with sp.connection(path,True) as db:
        p=package(db,oid)
        observed=sp.latest(db,oid,'原单')
        if not observed or observed['sha']!=source_sha or observed['body']['status']=='已取消':
            raise wr.Conflict('须先核对当前有效原单实配；旧款保留待人工重核')
        source=observed['body']
        rows=[dict(r) for r in source['rows']]
        if amounts is not None:
            required(reason,'改款原因')
            if set(amounts)!={r['sku'] for r in rows}:
                raise ValueError('改款范围必须等于原付款行')
            for r in rows:
                r['amount']=text(number(amounts[r['sku']],'人工核对金额'))
        document=dict(source=source_sha,internal=source['internal'],task=source['task'],rows=rows,
                      freight=str(source.get('freight',0)),adjustment=str(source.get('adjustment',0)),reason=reason)
        result=sp.record(db,oid,'付款',document,actor,expected)
        generation,_=stock.read_generation(db,p['gid'])
        h={**generation['head'],'products':generation['basis']['products']}
        import purchases
        purchases.validate_head(db,h)
        by_sku={i['sku']:i for i in p['items']}
        raw=[dict(sku=r['sku'],name=by_sku[r['sku']]['name'],quantity=r['quantity'],base_quantity=r['quantity'],
                  unit=r['unit'],factor=None,conversion_basis='冻结原行库存单位',price=None,amount=r['amount'],currency='CNY',
                  document_number=source['internal'],document_date=None,note=reason,sources=[{'sha':source['file_sha'],'row':i}],
                  adapter='special_original_payment',raw_headers=[],raw_values=[]) for i,r in enumerate(rows,1)]
        formal=lifecycle.make_plan(db,h,'payment',raw,[])
        if formal['payload']['pending']:
            raise ValueError('当前仓单的上游或库存代次待核')
        if formal['changed']:
            v=(formal['expected'] or 0)+1
            db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                       (*lifecycle_store.key(h),'payment',v,sp.now(),formal['digest'],wr.dumps(formal['payload'])))
            lifecycle_store.insert_event(db,sp.now(),h,'payment',v,'专项人工核款',wr.dumps({'operator':actor,'source':source_sha,'sha':result['sha']}))
        return result


def group_id(p):
    return uid('purchase',p['plan'],sp.procurement_scope(p['scope']))


def group_for_oid(db,oid):
    if not sp.exists(db,'special_packages'):
        return None
    r=db.execute("SELECT 1 FROM special_packages p JOIN special_plans s ON s.plan=p.plan WHERE p.oid=? AND s.kind='opening'",(oid,)).fetchone()
    return group_id(package(db,oid)) if r else None


def native_sources(db,p):
    """Read existing rc67 original binding and the human-approved payment version."""
    if not sp.exists(db,'jst_auto_orders'):
        return None,None
    row=db.execute('SELECT internal,stage,proof FROM jst_auto_orders WHERE online=? AND gid=?',(p['online'],p['gid'])).fetchone()
    if not row or not row[0] or row[1] in ('CANCELLED','IMPORT_UNKNOWN','IMPORT_FAILED','REVIEW'):
        return None,None
    proof=json.loads(row[2] or '{}')
    b=proof.get('binding') or {}
    if str(b.get('coid') or '')!=p['scope']['account'] or str(b.get('shop_id') or '')!=p['scope']['shop']:
        raise wr.Conflict('请核对原聚水潭仓账号和店铺编码 '+p['number'])
    doc=lifecycle.current(db,head(db,p),'payment')
    if not doc or doc['payload'].get('pending'):
        return None,None
    items={i['sku']:i for i in p['items']}
    rows=[]
    for r in doc['payload']['rows']:
        if r['sku'] not in items or r.get('document_number')!=row[0] or r.get('amount') is None:
            raise ValueError('已确认付款与原聚水潭明细不符 '+p['number'])
        rows.append({'sku':r['sku'],'unit':items[r['sku']]['unit'],'quantity':r['base_quantity'],'amount':r['amount']})
    value=dict(internal=row[0],task=p['online'],rows=rows,status='已付款待审核',freight='0',adjustment='0')
    return dict(sha=doc['digest'],body=value),dict(version=doc['version'],sha=doc['digest'],body={**value,'source':doc['digest']})


def sources(db,p):
    actual=sp.latest(db,p['oid'],'原单')
    if not actual:
        return native_sources(db,p)
    pay=sp.latest(db,p['oid'],'付款')
    latest_read=sp.latest(db,p['oid'],'聚水潭原单回读')
    if latest_read and latest_read['body']['status']=='已取消' and actual['body']['status']!='已取消':
        return actual,None
    if pay and pay['body']['source']!=actual['sha']:
        pay=None
    if pay:
        current=lifecycle.current(db,head(db,p),'payment')
        original={(r['sku'],Decimal(r['quantity']),Decimal(r['amount'])) for r in pay['body']['rows']}
        if not current or {(r['sku'],Decimal(r['base_quantity']),Decimal(r['amount'])) for r in current['payload']['rows']}!=original:
            pay=None
    return actual,pay


def assemble(db,plan,scope,*,require_paid=True):
    body=sp.read(db,plan)
    members=[p for p in packages(db,plan) if sp.procurement_scope(p['scope'])==scope]
    supplied=defaultdict(Decimal)
    by_sku={}
    inputs=[]
    freight=adjust=ZERO
    for p in members:
        if sp.latest(db,p['oid'],'未发送撤销'):continue
        actual,paid=sources(db,p)
        if actual and actual['body']['status']=='已取消':
            continue
        if not actual or not paid:
            if require_paid:
                raise ValueError(p['number']+' 尚缺原单实配或当前版本人工付款确认')
            continue
        originals={r['sku']:r for r in actual['body']['rows']}
        entries={r['sku']:r for r in paid['body']['rows']}
        inputs.append(dict(oid=p['oid'],gid=p['gid'],online=p['online'],number=p['number'],internal=actual['body']['internal'],
                           task=actual['body']['task'],payment=paid['version'],sha=paid['sha'],source=actual['sha']))
        for item in p['items']:
            sku=item['sku']
            r=entries[sku]
            qty=number(r['quantity'],'付款数量')
            if qty!=number(originals[sku]['quantity'],'实配') or r['unit']!=item['unit']:
                raise ValueError('付款行与实配或库存单位不符 '+sku)
            if not qty:
                continue
            amount=number(r['amount'],'付款商品额')
            price=priced_row(text(qty),text(amount))
            total=by_sku.setdefault(sku,dict(sku=sku,name=item['name'],unit=item['unit'],quantity=ZERO,payment_amount=ZERO,
                                             unit_price=price['unit_price'],sources=[]))
            if (total['unit'],total['unit_price'])!=(item['unit'],price['unit_price']):
                raise ValueError('同一 SKU 单位或付款单价冲突 '+sku)
            total['quantity']+=qty
            total['payment_amount']+=amount
            left=qty
            for c in item['claims']:
                q=min(left,Decimal(c['quantity']))
                supplied[c['line']]+=q
                if q:
                    total['sources'].append(dict(oid=p['oid'],gid=p['gid'],line=c['line'],source_row=c['source_row'],quantity=text(q),
                                                 online=p['online'],internal=actual['body']['internal'],payment=paid['version'],sku=sku))
                left-=q
        freight+=Decimal(paid['body']['freight'])
        adjust+=Decimal(paid['body']['adjustment'])
    expected=[r for r in body['lines'] if sp.procurement_scope(r)==scope]
    pending=[r['sku'] for r in expected if supplied[r['id']]<Decimal(r['target'])]
    if pending or not inputs:
        raise ValueError('货盘有效实配未齐，尚缺 '+str(len(pending))+' 行 '+', '.join(pending[:10]))
    rows=[]
    for i,r in enumerate(by_sku.values()):
        rows.append({**r,'index':i,'quantity':text(r['quantity']),**priced_row(text(r['quantity']),text(r['payment_amount']))})
    return dict(id=uid('purchase',plan,scope),plan=plan,version=body['version'],scope=scope,inputs=inputs,rows=rows,
                total_quantity=text(sum(Decimal(r['quantity']) for r in rows)),total_amount=text(sum(Decimal(r['amount']) for r in rows)),
                payment_total_amount=text(sum(Decimal(r['payment_amount']) for r in rows)),
                rounding_delta=text(sum(Decimal(r['rounding_delta']) for r in rows)),freight=text(freight),adjustment=text(adjust))


def freeze(path,oid,actor):
    with sp.connection(path,True) as db:
        p=package(db,oid)
        scope=sp.procurement_scope(p['scope'])
        group=group_id(p)
        effect=effect_for(db,group)
        if effect:
            return sp.latest(db,group,'采购预览',effect['body']['version'])
        draft=assemble(db,p['plan'],scope)
        anchor=draft['inputs'][0]['oid']
        db.execute('INSERT INTO special_groups VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET anchor=excluded.anchor',
                   (draft['id'],p['plan'],wr.dumps(scope),anchor))
        return sp.record(db,draft['id'],'采购预览',draft,actor)


def effect_for(db,group):
    row=db.execute("SELECT state,token,sha,original,body FROM special_effects WHERE object=? AND stage='采购'",(group,)).fetchone()
    return dict(state=row[0],token=row[1],sha=row[2],original=row[3],body=json.loads(row[4])) if row else None


def authorize_dispatch(db,group,sha,task):
    previous=effect_for(db,group)
    if previous:
        raise wr.Conflict('已有原采购任务 '+previous['token']+'，请回查原单')
    frozen=sp.latest(db,group,'采购预览')
    if not frozen or frozen['sha']!=sha:
        raise wr.Conflict('采购预览版本已变化')
    body=frozen['body']
    plan=sp.read(db,body['plan'])
    if not sp.enabled(db,'opening') or not sp.in_window(plan,{},sp.instant(sp.now())):
        raise ValueError('专项执行已停用或已过窗，原采购仍可回查')
    if sp.digest(assemble(db,body['plan'],body['scope']))!=sha:
        raise wr.Conflict('来源付款、实配或货盘版本已变化，请重新预览')
    db.execute("INSERT INTO special_effects VALUES(?,'采购','已发待核',?,?,NULL,?)",
               (group,task,sha,wr.dumps({'version':frozen['version'],'at':sp.now()})))


def bind(db,group,task,original):
    effect=effect_for(db,group)
    if not effect or effect['token']!=task or effect['original'] not in (None,original):
        raise ValueError('原采购任务与平台号不符')
    db.execute("UPDATE special_effects SET state='已关联',original=? WHERE object=? AND stage='采购'",(required(original,'翱象原采购号'),group))


def shares(frozen,rows):
    expected={r['sku']:r for r in frozen['rows']}
    if len(rows)!=len(expected) or {r['sku'] for r in rows}!=expected.keys():
        raise ValueError('实收清单缺行、重复或超出原采购，须逐 SKU 核对')
    output=[]
    for row in rows:
        sku=row['sku']
        line=expected[sku]
        qty=number(row['quantity'],'实收')
        amount=number(row['amount'],'实收金额')
        if row['unit']!=line['unit']:
            raise ValueError('实收单位与原采购不一致 '+sku)
        known=[]
        takes=row.get('shares')
        by_line={(r['oid'],r['line']):r for r in line['sources']}
        if takes is not None:
            seen=set()
            for t in takes:
                key=t['oid'],t['line']
                if key not in by_line or key in seen:
                    raise ValueError('实收认领不能跨原采购行或重复')
                seen.add(key)
                q=number(t['quantity'],'认领量')
                if q>Decimal(by_line[key]['quantity']):
                    raise ValueError('归属超出原行份额')
                known.append({**by_line[key],'quantity':text(q)})
        elif qty>=Decimal(line['quantity']):
            known=line['sources']
        elif len(line['sources'])==1:
            known=[{**line['sources'][0],'quantity':text(qty)}]
        pending=qty-sum(Decimal(v['quantity']) for v in known)
        if pending<0:
            raise ValueError('分摊超过真实实收')
        output.append(dict(sku=sku,unit=row['unit'],quantity=text(qty),amount=text(amount),shares=known,pending=text(pending)))
    return output


def post(db,group,task,original,rows,evidence,actor,*,correction=None):
    bind(db,group,task,original)
    effect=effect_for(db,group)
    frozen=sp.latest(db,group,'采购预览',effect['body']['version'])['body']
    scope=frozen['scope']
    existing=db.execute('SELECT version,sha FROM special_receipts WHERE merchant=? AND store=? AND original=? AND group_id=?',
                        (scope['merchant'],scope['store'],original,group)).fetchone()
    if existing and not correction:
        first=sp.latest(db,group,'正式实收',1)
        if first['body']['evidence']!=evidence:
            raise wr.Conflict('同一原采购已正式入账，请在原实收版本更正')
        return first
    if correction and (not existing or correction['version']!=existing[0] or not required(correction.get('reason'),'更正原因')):
        raise wr.Conflict('实收原版本已变化')
    value=dict(task=task,original=original,group=group,evidence=evidence,rows=shares(frozen,rows),
               prior=existing[1] if existing else None,correction=correction)
    result=sp.record(db,group,'正式实收',value,actor)
    db.execute('INSERT INTO special_receipts VALUES(?,?,?,?,?,?) ON CONFLICT(group_id) DO UPDATE SET version=excluded.version,sha=excluded.sha',
               (scope['merchant'],scope['store'],original,group,result['version'],result['sha']))
    return result


def acknowledge(db,group,task,original,evidence):
    first=sp.latest(db,group,'正式实收',1)
    if not first or (first['body']['task'],first['body']['original'],first['body']['evidence'])!=(task,original,evidence):
        raise ValueError('原采购、首次入账与回执摘要不一致')
    return sp.record(db,group,'原平台回执',dict(task=task,original=original,version=1,sha=first['sha'],evidence=evidence),'平台原回执')


def resolve_head(path,h):
    with sp.connection(path) as db:
        group=group_for_oid(db,h['oid'])
        if group is None:
            return h
        row=db.execute('SELECT anchor FROM special_groups WHERE id=?',(group,)).fetchone()
        if not row:
            raise ValueError('该子包属于开业采购组，请到新店开业工作台核对全部子单并冻结合并采购')
        return head(db,package(db,row[0]))
