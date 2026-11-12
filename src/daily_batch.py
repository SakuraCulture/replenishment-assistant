"""Daily queue and append-only business journal projections; no model writes."""
import sqlite3
from contextlib import closing
from pathlib import Path
import work_repository as wr
import lifecycle_home
import order_lifecycle as lc
import jst_orders as jst
from runtime_support import checkpoint

STATUSES=('待回传','待运营签发','待付款','待收货','已结束')


def read(path,cache,request,cancel=None,progress=lambda _:None):
    base=lifecycle_home.read(path,cache,{**request,'status':'','all_records':True},cancel,progress)
    records=base['records']
    with closing(sqlite3.connect(path)) as db:
        current={};locks={};tasks={}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():
            for raw,status,message in db.execute('SELECT head,status,message FROM jst_tasks ORDER BY created,rowid'):
                h=wr.loads(raw);tasks[(h['oid'],h['warehouse'])]={'status':status,'message':message}
        if jst.exists(db):
            current={(o,w):(g,wr.loads(p)) for o,w,g,p in db.execute('SELECT a.oid,a.warehouse,a.gid,h.payload FROM jst_active a JOIN jst_headers h ON a.gid=h.gid')}
            locks={(o,w):g for o,w,g in db.execute('SELECT oid,warehouse,gid FROM jst_locks WHERE locked=1')}
        for r in records:
            checkpoint(cancel);key=(r['oid'],r['warehouse']);g=current.get(key)
            r.update(gid=g[0] if g else None,locked=key in locks,generation_summary=g[1]['summary'] if g else None)
            if r['status']=='已收齐':r.update(status='已结束',next=None)
            elif r['next']=='payment':
                r.update(status='待付款',next='payment')
                if g:
                    ret=lc.current(db,r,lc.flow.demand_stage(db,r));token=lc.rules.basis_token(ret)
                    if token!=g[1]['return'] or r['revision']!=g[1]['revision']:
                        r['note']='回传或签发依据已变化，请核对生成单；未自动重分配'
                else:r['note']='待生成聚水潭导入单；也可直接导入实际付款单'
            if key in tasks:r['generation_task']=tasks[key]
    counts={s:sum(r['status']==s for r in records) for s in STATUSES}
    wh=request.get('warehouse');lock=request.get('lock');stage=request.get('status','')
    view=request.get('stage_view');kind=request.get('stage_filter');scope=request.get('scope')
    chosen={tuple(k) for k in scope} if scope is not None else None
    def in_stage(r):
        if chosen is not None and (r['oid'],r['warehouse']) not in chosen:return False
        if not view:return True
        if view=='jst':
            if kind=='待生成':return r['next']=='payment' and not r['gid']
            if kind=='已生成未锁':return bool(r['gid']) and not r['locked']
            if kind=='已锁':return r['locked']
            return bool(r['gid']) or r['next']=='payment'
        pending=r['next'] in ('payment','jst_payment') if view=='payment' else r['next']==view
        return pending if kind=='待处理' else pending or r['present'].get(view,False)
    matched=[r for r in records if in_stage(r) and (not stage or stage==r['status'] or stage=='全部未完成' and r['next']) and (not wh or r['warehouse']==wh) and (not lock or r['locked']==(lock=='已锁'))]
    if request.get('all_matched'):return {'records':matched,'count':len(matched),'counts':counts,'scope':len(records)}
    page=min(max(0,int(request.get('page',0))),max(0,(len(matched)-1)//50))
    visible=matched[page*50:(page+1)*50]
    from aoxiang_receipt_scope import tracking_observations
    observed=tracking_observations(path,visible)
    for h in visible:
        value=observed.get((h['oid'],h['revision'],h['warehouse']))
        if value:h['platform_purchase']=value
    return {'records':visible,'count':len(matched),'page':page,'counts':counts,'scope':len(records)}


def logs(path,start='',end='',search='',progress=lambda _:None,cancel=None,*,page=None):
    rows=[];parts=[]
    with closing(sqlite3.connect(path)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources=[('normal_events','签发'),('lifecycle_events','生命周期'),('jst_events','聚水潭作业')]
        for table,group in sources:
            if table not in tables:continue
            fields={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
            wh='e.warehouse' if 'warehouse' in fields else "''"
            day='e.day' if 'day' in fields else 'NULL'
            parts.append(f"SELECT '{table}:'||e.id AS id,e.at,COALESCE(n.day,{day},substr(e.at,1,10)) AS day,COALESCE(n.store,'') AS store,COALESCE(n.number,'') AS number,{wh} AS warehouse,'{group}' AS kind,e.action,e.detail FROM {table} e LEFT JOIN normal_orders n ON n.oid=e.oid")
        if not parts:return {'rows':[],'count':0,'page':0} if page is not None else []
        conditions=[];args=[]
        if start:conditions.append('day>=?');args.append(start)
        if end:conditions.append('day<=?');args.append(end)
        for term in search.lower().split():
            conditions.append("instr(lower(COALESCE(store,'')||' '||COALESCE(number,'')||' '||COALESCE(warehouse,'')||' '||action||' '||at||' '||detail),?)>0");args.append(term)
        sql='SELECT * FROM ('+' UNION ALL '.join(parts)+')'+(' WHERE '+' AND '.join(conditions) if conditions else '')
        count=db.execute('SELECT count(*) FROM ('+sql+')',args).fetchone()[0] if page is not None else None
        query=sql+' ORDER BY at DESC,id DESC'
        if page is not None:
            page=min(max(0,page),max(0,(count-1)//50));query+=' LIMIT 50 OFFSET ?';args.append(page*50)
        for eid,at,day,store,number,wh,group,action,raw in db.execute(query,args):
            checkpoint(cancel);rows.append({'id':eid,'at':at,'day':day,'store':store,'number':number,'warehouse':wh,'group':group,'action':action,'detail':wr.loads(raw)})
    return {'rows':rows,'count':count,'page':page} if page is not None else rows


def log_page(path,start,end,search,page,progress=lambda _:None,cancel=None):
    return logs(path,start,end,search,progress,cancel,page=page)


def export_logs(path,target,start='',end='',search='',progress=lambda _:None,cancel=None):
    import business_excel
    records=logs(path,start,end,search,progress,cancel)
    rows=[[r[k] for k in ('day','at','store','warehouse','number','group','action')] for r in records]
    return business_excel.write(target,[('批次作业日志',['业务日期','操作时间','门店','仓','补货单号','环节','动作'],rows)],cancel)
