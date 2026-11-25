"""Daily queue and append-only business journal projections; no model writes."""
from contextlib import closing
from pathlib import Path
import work_repository as wr
import lifecycle_home
from jst_order_store import list_generation_state
from jst_task_store import latest_statuses
from lifecycle_rules import JST_ORDER
from lifecycle_stages import WAITING_SHIPMENT
from runtime_support import checkpoint
from query_scope import oid_batches

STATUSES=('待回传','待运营签发','待付款',WAITING_SHIPMENT,'待收货','已结束')
GENERATION_FILTERS={'generated':'已生成导入单','missing':'未生成导入单'}
PURCHASE_FILTER = '可生成采购预览'


def read(path,cache,request,cancel=None,progress=lambda _:None):
    """Project warehouse-order progress, current generation and display filters.

    Business connections are read-only. Only the disposable projection cache
    may be updated. The returned scope and counts precede list pagination.
    """
    purchase_filter = request.get('stage_view') == 'payment' and request.get('stage_filter') == PURCHASE_FILTER
    base=lifecycle_home.read(path,cache,{**request,'status':'','all_records':True,'_with_tokens':True,
                                       'platform_tracking':purchase_filter or request.get('platform_tracking')},cancel,progress)
    records=base['records']
    batches=list(oid_batches(r['oid'] for r in records))
    with closing(wr.read_connection(path)) as db:
        from jst_cancellation import affected_scopes, payment_state
        from lifecycle_facts import current as document
        tasks = latest_statuses(db, batches, cancel)
        current, locks = list_generation_state(db, batches, cancel)
        affected = affected_scopes(db,batches)
        for r in records:
            checkpoint(cancel);key=(r['oid'],r['warehouse']);g=current.get(key)
            r.update(gid=g[0] if g else None,locked=key in locks,generation_summary=g[1]['summary'] if g else None)
            if r['status']=='已收齐':r.update(status='已结束',next=None)
            elif r['next']=='payment':
                r.update(status='待付款',next='payment')
                if g:
                    stage='operations' if r['flow_version']==2 else 'return'
                    token=base['_basis_tokens'][r['oid'],r['warehouse']][stage]
                    if token!=g[1]['return'] or r['revision']!=g[1]['revision']:
                        r['note']='回传或签发依据已变化，请核对生成单；未自动重分配'
                else:r['note']='待生成聚水潭导入单；也可直接导入实际付款单'
            if key in tasks:r['generation_task']=tasks[key]
            if key in affected:
                hold, authorization = payment_state(db,r,document(db,r,'payment'))
                if hold:
                    r['cancelled_payment_hold'] = hold
                    r['note'] = hold
                if authorization:
                    r['cancellation_review'] = authorization
    counts={s:sum(r['status']==s for r in records) for s in STATUSES}
    wh=request.get('warehouse');lock=request.get('lock');stage=request.get('status','')
    view=request.get('stage_view');kind=request.get('stage_filter');scope=request.get('scope')
    generation=request.get('jst_generation') if view=='payment' else None
    chosen={tuple(k) for k in scope} if scope is not None else None
    def in_scope(r):return chosen is None or (r['oid'],r['warehouse']) in chosen
    def in_stage(r):
        if not view:return True
        if view=='jst':
            if kind=='待生成':return r['next']=='payment' and not r['gid']
            if kind=='已生成':return bool(r['gid'])
            if kind=='已生成未锁':return bool(r['gid']) and not r['locked']
            if kind=='已锁':return r['locked']
            return bool(r['gid']) or r['next']=='payment'
        pending=(r['next'] in ('payment','jst_payment') or bool(r.get('cancelled_payment_hold'))) if view=='payment' else r['next']==view
        return pending if kind=='待处理' else pending or r['present'].get(view,False) or (
            view=='payment' and r['health'].get(JST_ORDER,False))
    def in_status(r):return not stage or stage==r['status'] or stage=='全部未完成' and r['next']
    def in_generation(r):return generation not in GENERATION_FILTERS or bool(r['gid'])==(generation=='generated')
    matched=[r for r in records if in_scope(r) and in_stage(r) and in_status(r) and in_generation(r) and (not wh or r['warehouse']==wh) and (not lock or r['locked']==(lock=='已锁'))]
    purchase_excluded = {}
    if purchase_filter:
        from aoxiang_preview_scope import existing_tasks
        prior = existing_tasks(path, Path(path).parent/'aoxiang', matched, cancel=cancel)
        eligible = []
        for r in matched:
            checkpoint(cancel)
            key = (r['oid'], r['warehouse'])
            task = prior.get(key)
            tracking = r['platform_tracking']
            version, digest = tracking['payment_basis']
            if task:
                purchase_excluded[key] = task['message']
            elif r.get('cancelled_payment_hold'):
                purchase_excluded[key] = r['cancelled_payment_hold']
            elif r['warehouse'] not in ('山东仓', '河北仓') or r['revision'] != r['current_revision']:
                purchase_excluded[key] = '仓单身份或签发修订待核'
            elif not r['health'].get('payment') or version is None or not digest:
                purchase_excluded[key] = '尚无有效正式付款'
            elif not tracking['payment_positive']:
                purchase_excluded[key] = '正式付款数量为零'
            else:
                r['preview_payment'] = {'version': version, 'digest': digest}
                if r.get('cancellation_review'):
                    r['preview_payment']['cancellation_review'] = r['cancellation_review']
                eligible.append(r)
        matched = eligible
    explanation={}
    if request.get('explain_filters'):
        # Reuse the same active-head projection. No source details, extra SQL,
        # recycle-bin records or cache-only identities are loaded for this view.
        titles={'return':'回传处理','operations':'运营签发','payment':'付款核对','receipt':'收货核对','jst':'聚水潭导入单'}
        excluded=[]
        for r in records:
            checkpoint(cancel);reasons=[];codes=[]
            def add(code,text):codes.append(code);reasons.append(text)
            if not in_scope(r):add('scope','限定范围：未在所选订单中')
            if not in_stage(r):
                text='环节筛选：不符合“'+titles.get(view,view)+' / '+str(kind or '本环节全部')+'”'
                if view=='operations' and kind!='待处理' and r.get('flow_version',1)==1:
                    text+='；旧流程未留存运营签发单据'
                add('stage',text)
            if wh and r['warehouse']!=wh:add('warehouse','仓筛选：所选'+wh+'，该单为'+r['warehouse'])
            if lock and r['locked']!=(lock=='已锁'):add('lock','锁状态筛选：所选'+lock+'，该单'+('已锁' if r['locked'] else '未锁'))
            if not in_status(r):add('status','进度筛选：所选“'+stage+'”，该单“'+r['status']+'”')
            if not in_generation(r):
                actual=GENERATION_FILTERS['generated' if r['gid'] else 'missing']
                add('jst_generation',f'聚水潭生成状态筛选：所选“{GENERATION_FILTERS[generation]}”，该单“{actual}”')
            if (r['oid'], r['warehouse']) in purchase_excluded:
                add('purchase_preview', purchase_excluded[r['oid'], r['warehouse']])
            if reasons:excluded.append({**{k:r[k] for k in ('oid','number','store','name','warehouse','status')},'reason_codes':codes,'reasons':reasons})
        explanation={'filter_explanation':{'excluded_count':len(excluded),'excluded':excluded}}
    if request.get('all_matched'):return {'records':matched,'count':len(matched),'counts':counts,'scope':len(records),**explanation}
    page=min(max(0,int(request.get('page',0))),max(0,(len(matched)-1)//50))
    visible=matched[page*50:(page+1)*50]
    if not request.get('platform_tracking') or request.get('stage_view'):
        from aoxiang_receipt_scope import tracking_observations
        observed=tracking_observations(path,visible)
        for h in visible:
            value=observed.get((h['oid'],h['revision'],h['warehouse']))
            if value:h['platform_purchase']=value
    if purchase_filter:
        from aoxiang_preview_scope import MAX_ORDERS
        # Retain exact all-page selections only up to the existing batch limit.
        explanation['purchase_scope'] = matched if len(matched) <= MAX_ORDERS else None
    return {'records':visible,'count':len(matched),'page':page,'counts':counts,'scope':len(records),**explanation}


def logs(path,start='',end='',search='',progress=lambda _:None,cancel=None,*,page=None):
    rows=[];parts=[]
    with closing(wr.read_connection(path)) as db:
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        sources=[('normal_events','签发'),('lifecycle_events','生命周期'),('jst_events','聚水潭作业')]
        for table,group in sources:
            if table not in tables:continue
            fields={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
            wh='e.warehouse' if 'warehouse' in fields else "''"
            day='e.day' if 'day' in fields else 'NULL'
            revision='e.revision' if 'revision' in fields else 'NULL'
            active=''
            if 'warehouse_execution_heads' in tables:
                # Filter before counting/paging, not after LIMIT. Shared parent
                # journal entries remain common; warehouse entries require a
                # published, surviving child at their own frozen source revision.
                active=" WHERE (NOT EXISTS(SELECT 1 FROM warehouse_execution_heads x WHERE x.oid=e.oid) OR EXISTS(SELECT 1 FROM warehouse_execution_heads x WHERE x.oid=e.oid AND x.state='active' AND (COALESCE("+wh+",'')='' OR x.warehouse="+wh+" AND EXISTS(SELECT 1 FROM warehouse_execution_revisions v WHERE v.oid=x.oid AND v.warehouse=x.warehouse AND v.source_revision="+revision+"))))"
            parts.append(f"SELECT '{table}:'||e.id AS id,e.at,COALESCE(n.day,{day},substr(e.at,1,10)) AS day,COALESCE(n.store,'') AS store,COALESCE(n.number,'') AS number,{wh} AS warehouse,'{group}' AS kind,e.action,e.detail,e.oid AS oid,{revision} AS revision FROM {table} e LEFT JOIN normal_orders n ON n.oid=e.oid"+active)
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
        for eid,at,day,store,number,wh,group,action,raw,oid,revision in db.execute(query,args):
            checkpoint(cancel);rows.append({'id':eid,'at':at,'day':day,'store':store,'number':number,'warehouse':wh,'group':group,'action':action,'detail':wr.loads(raw),'oid':oid,'revision':revision})
    return {'rows':rows,'count':count,'page':page} if page is not None else rows


def log_page(path,start,end,search,page,progress=lambda _:None,cancel=None):
    return logs(path,start,end,search,progress,cancel,page=page)


def export_logs(path,target,start='',end='',search='',progress=lambda _:None,cancel=None):
    import business_excel
    records=logs(path,start,end,search,progress,cancel)
    rows=[[r[k] for k in ('day','at','store','warehouse','number','group','action')] for r in records]
    return business_excel.write(target,[('批次作业日志',['业务日期','操作时间','门店','仓','补货单号','环节','动作'],rows)],cancel)
