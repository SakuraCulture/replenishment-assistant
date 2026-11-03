"""Persistent generation results and frozen export scopes. No allocation writes.

The SQLite projection cache is disposable. Business snapshots and export code
remain the existing sources of truth; exporting only appends export events.
"""
import copy
import hashlib
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
import work_repository as wr
import lifecycle_home
import order_lifecycle as lc
import purchases
import jst_orders as orders
import jst_exports
import jst_batches
import jst_review as review
from history_read import identity
from runtime_support import checkpoint
from export_progress import RowProgress


def key(h):return (h['oid'],h['warehouse'])
def connect(path):return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)


def project(p):
    m=review.metrics([(p['head'],r) for r in p['rows']]);flags={}
    for r in p['rows']:
        want,q,price,amount=[review.number(r.get(k)) for k in ('demand','quantity','price','amount')]
        bits=flags.setdefault(r['sku'],[False,False,False])
        bits[0] |= q is not None and q>0;bits[1] |= want is not None and q is not None and q<want
        bits[2] |= any(n is None for n in (want,q,price,amount))
    return {'amounts':{k:str(m[k]) for k in ('demand','allocated','reduced')},'priced_rows':m['priced_rows'],'flags':flags,
            'position':p['position'],'saved':p['saved'],'bid':p['bid'],'head':p['head'],
            'return':p['basis']['dependencies'].get('return'),'signed_products':p.get('signed_products'),
            'batch_id':p.get('batch_id')}


def aggregate(records):
    amounts={k:Decimal(0) for k in ('demand','allocated','reduced')};flags={};priced=0
    for r in records:
        p=r.get('projection')
        if not p:continue
        for k in amounts:amounts[k]+=Decimal(p['amounts'][k])
        priced+=p['priced_rows']
        for sku,bits in p['flags'].items():
            current=flags.setdefault((r['head']['store'],sku),[False,False,False])
            for i,b in enumerate(bits):current[i] |= b
    return {**amounts,'rate':amounts['allocated']/amounts['demand'] if amounts['demand'] else None,'priced_rows':priced,
            'sku':len(flags),'arranged':sum(b[0] for b in flags.values()),'reduced_sku':sum(b[1] for b in flags.values()),
            'unallocated':sum(not b[0] for b in flags.values()),'unknown':sum(b[2] for b in flags.values())}


def cached_projection(idx,source_id,gid,digest,loader):
    k=wr.dumps([2,source_id,gid,digest]);hit=idx.execute('SELECT payload,sha FROM jst_result_projection WHERE k=?',(k,)).fetchone()
    if hit and hashlib.sha256(hit[0].encode()).hexdigest()==hit[1]:return wr.loads(hit[0])
    p=loader();value=project(p);raw=wr.dumps(value)
    idx.execute('INSERT OR REPLACE INTO jst_result_projection VALUES(?,?,?)',(k,raw,hashlib.sha256(raw.encode()).hexdigest()));idx.commit()
    return value


def return_token(db,h,p=None):
    if lc.exists(db):
        row=db.execute("SELECT version,digest FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='return' ORDER BY version DESC LIMIT 1",lc.key(h)).fetchone()
        if row:return {'version':row[0],'digest':row[1]}
    if p and p.get('signed_products'):
        legacy=lc.legacy_return(db,h,p['signed_products'])
        if legacy and legacy['complete']:return lc.rules.basis_token(legacy)
    return None


def read(path,cache,day,scope=None,progress=lambda _:None,cancel=None,batch_id=None,date_end=None):
    date.fromisoformat(day);checkpoint(cancel)
    cache=Path(cache);cache.parent.mkdir(parents=True,exist_ok=True)
    def home_progress(p):progress(RowProgress('读取本批订单',p['done'],p['total']))
    batch_members=None
    if batch_id:
        with closing(connect(path)) as db:batch_context=jst_batches.context(db,batch_id,load_stock=False)
        batch_members={key(m['head']) for m in batch_context['members']}
        days=[m['head']['day'] for m in batch_context['members']];day=min(days);date_end=max(days)
    date_end=date_end or day;date.fromisoformat(date_end)
    base=lifecycle_home.read(path,cache,{'from':day,'to':date_end,'all_records':True},cancel,home_progress)
    selected={tuple(k) for k in scope} if scope is not None else None
    heads=[h for h in base['records'] if (selected is None or key(h) in selected) and (batch_members is None or key(h) in batch_members)]
    source_id=identity(path);records=[]
    with closing(connect(path)) as db,closing(sqlite3.connect(cache)) as idx:
        db.execute('BEGIN')
        idx.execute('CREATE TABLE IF NOT EXISTS jst_result_projection(k TEXT PRIMARY KEY,payload TEXT,sha TEXT)')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tasks={};events={};last_export={};active={}
        if 'jst_tasks' in tables:
            for raw,token,status,message in db.execute('SELECT head,token,status,message FROM jst_tasks ORDER BY created,rowid'):
                h=wr.loads(raw)
                if day<=h['day']<=date_end:tasks[key(h)]={'token':wr.loads(token),'status':status,'message':message}
        if orders.exists(db):
            for oid,wh,gid,digest,seq in db.execute('SELECT a.oid,a.warehouse,a.gid,g.digest,g.rowid FROM jst_active a JOIN jst_generations g ON a.gid=g.gid WHERE g.day BETWEEN ? AND ? ORDER BY g.rowid',(day,date_end)):
                if batch_id and jst_batches.generation_batch(db,gid)!=batch_id:continue
                active[(oid,wh)]=(gid,digest,seq)
            for eid,at,oid,wh,action,raw in db.execute('SELECT id,at,oid,warehouse,action,detail FROM jst_events WHERE day BETWEEN ? AND ? ORDER BY id',(day,date_end)):
                if action=='导出聚水潭订单':
                    detail=wr.loads(raw);last_export[detail['gid']]=at
                elif action in ('生成聚水潭订单','库存不足未生成','撤销未下单生成单'):
                    events[(oid,wh)]=(eid,action,raw)
        for n,h in enumerate(heads):
            checkpoint(cancel);purchases.validate_head(db,h)
            item={'head':h,'gid':None,'projection':None,'state':'pending','reason':'等待生成','locked':False,'last_export':'—','sequence':None}
            token=return_token(db,h);task=tasks.get(key(h));hit=active.get(key(h))
            if not h['present']['return']:
                item.update(state='waiting',reason='尚未确认回传')
            elif not h['health'].get('return'):
                item.update(state='stale',reason='回传依据已变化，请重新核对')
            elif task and task['token']==token:
                item['reason']=task['message']
                if task['status'] in ('waiting','cancelled'):item['state']='waiting'
            if hit:
                gid,digest,seq=hit
                def loader(g=gid):
                    return orders.decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(g,)).fetchone())
                p=cached_projection(idx,source_id,gid,digest,loader);lock=orders.locked(db,h)==gid
                current=jst_batches.state(db,p['batch_id'],h['warehouse']) if p.get('batch_id') else orders.state(db,h['warehouse'])
                token=return_token(db,h,p)
                valid=h['revision']==p['head']['revision'] and token==p['return'] and h['health'].get('return')
                stock_same=current and current[0]==p['bid']
                item.update(gid=gid,generation_digest=digest,projection=p,locked=lock,sequence=seq,
                            state='ready' if valid and (stock_same or lock) else 'stale',
                            reason='可导出' if valid and (stock_same or lock) else '仓资料已更新，请核对当时分配' if valid else '回传或签发依据已变化，请重新核对',
                            last_export=last_export.get(gid,'未导出'),return_token=token,current_bid=current[0] if current else None,batch_id=p.get('batch_id'))
            elif key(h) in events and events[key(h)][1]=='库存不足未生成':
                eid,_,raw=events[key(h)];event=wr.loads(raw)
                run=orders.decode(db.execute('SELECT payload,digest FROM jst_runs WHERE runid=?',(event['runid'],)).fetchone())
                p=next((p for p in run['plans'] if key(p['head'])==key(h)),None) if run else None
                if p and (not batch_id or p.get('batch_id')==batch_id) and h['health'].get('return') and p['head']['revision']==h['revision'] and p['basis']['dependencies'].get('return')==return_token(db,h,p):
                    pr=cached_projection(idx,source_id,'zero:'+str(eid),orders.digest(p),lambda:p)
                    current=jst_batches.state(db,p['batch_id'],h['warehouse']) if p.get('batch_id') else orders.state(db,h['warehouse']);valid=current and current[0]==p['bid']
                    item.update(state='zero' if valid else 'stale',reason='分配为0，无下单商品' if valid else '上次分配为0，仓资料已更新，请重新核对',projection=pr,batch_id=p.get('batch_id'))
            if not hit and h['present']['return'] and h['next'] is None and not h['present']['payment']:
                item.update(state='zero',reason='回传无要货，无需生成')
            records.append(item);progress(RowProgress('汇总门店结果',n+1,len(heads)))
    # Actual generation insertion order preserves run/position order across grouped stores.
    if batch_id:
        positions={key(m['head']):m['position'] for m in batch_context['members']}
        records.sort(key=lambda r:positions[key(r['head'])])
    else:records.sort(key=lambda r:(r['sequence'] is None,r['sequence'] or r['head']['list_position']))
    grouped={}
    for r in records:
        h=r['head'];g=grouped.setdefault(h['store'],{'store':h['store'],'name':h['name'],'records':[]});g['records'].append(r)
    for g in grouped.values():g['metrics']=aggregate([r for r in g['records'] if r['state'] in ('ready','zero')])
    return {'day':day,'date_end':date_end,'batch_id':batch_id,'batch_label':batch_context['label'] if batch_id else None,
            'batch_pending':sum(m['status'] in ('pending','failed') for m in batch_context['members']) if batch_id else 0,
            'scope':scope,'records':records,'groups':list(grouped.values()),
            'metrics':aggregate([r for r in records if r['state'] in ('ready','zero')]),
            'counts':{s:sum(r['state']==s for r in records) for s in ('ready','zero','pending','waiting','stale')}}


def export_plan(data,chosen=None,allow_old=False):
    wanted={tuple(k) for k in chosen} if chosen is not None else None
    rows=[r for r in data['records'] if r['gid'] and (r['state']=='ready' or allow_old and r['state']=='stale') and (wanted is None or key(r['head']) in wanted)]
    return {'day':data['day'],'entries':copy.deepcopy(rows),'allow_old':allow_old}


def validate_export(db,plan,cancel=None):
    if not plan['entries']:raise ValueError('没有可导出的已保存结果')
    for r in plan['entries']:
        checkpoint(cancel);h=r['head'];purchases.validate_head(db,h)
        current=jst_batches.state(db,r['batch_id'],h['warehouse']) if r.get('batch_id') else orders.state(db,h['warehouse'])
        digest=db.execute('SELECT digest FROM jst_generations WHERE gid=?',(r['gid'],)).fetchone()
        if (orders.active(db,h)!=r['gid'] or digest!=(r['generation_digest'],)
                or return_token(db,h,r['projection'])!=r['return_token']
                or (orders.locked(db,h)==r['gid'])!=r['locked']
                or (current[0] if current else None)!=r['current_bid']):
            raise wr.Conflict('结果或依据已更新，请刷新后再导出：'+h['number'])
        if r['state']!='ready' and not plan['allow_old']:raise ValueError('需核对的结果不能混入普通导出')


def prepare_export_ids(path,gids,progress=lambda _:None,cancel=None):
    """Generation-panel export uses the same captured validity contract as results."""
    entries=[]
    with closing(connect(path)) as db:
        db.execute('BEGIN')
        for gid in gids:
            checkpoint(cancel)
            raw=db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(gid,)).fetchone()
            p=orders.decode(raw)
            if p is None or p['gid']!=gid:raise ValueError('生成单不存在或标识不一致')
            h=p['head'];purchases.validate_head(db,h)
            current=jst_batches.state(db,p['batch_id'],h['warehouse']) if p.get('batch_id') else orders.state(db,h['warehouse']);lock=orders.locked(db,h)==gid
            basis=lc.upstream(db,{**h,'products':p.get('signed_products',p['basis']['products'])},'payment')
            if orders.active(db,h)!=gid or basis!=p['basis'] or not current or (current[0]!=p['bid'] and not lock):
                raise wr.Conflict('生成结果或仓资料已更新，请到分配结果页核对；需要当时文件时明确选择“导出旧结果”：'+h['number'])
            projection={'signed_products':p.get('signed_products')}
            entries.append({'gid':gid,'head':h,'generation_digest':raw[1],'projection':projection,'state':'ready',
                            'return_token':return_token(db,h,projection),'locked':lock,'current_bid':current[0],'batch_id':p.get('batch_id')})
    return {'entries':entries,'allow_old':False}


def export(path,plan,folder,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    with closing(connect(path)) as db:
        db.execute('BEGIN');validate_export(db,plan,cancel)
    # From this boundary onward only these immutable gids are used, even if a
    # different window later changes the active results. No source recalculation.
    gids=[r['gid'] for r in plan['entries']]
    receipt=jst_exports.export(path,gids,folder,progress,cancel,historical=plan['allow_old'])
    done={f['gid'] for f in receipt['files']};receipt['remaining']=[g for g in gids if g not in done]
    receipt['total']=len(gids);return receipt


def detail(path,gids,progress=lambda _:None,cancel=None):
    out=[]
    for i,g in enumerate(gids):
        checkpoint(cancel);out.append(orders.get(path,g,cancel=cancel));progress(RowProgress('读取本店商品',i+1,len(gids)))
    return out
