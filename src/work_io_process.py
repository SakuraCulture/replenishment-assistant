"""Isolated daily-work I/O. Dual-format storage and fixed-revision projections; no Qt in the worker."""
import hashlib,sqlite3,json,os,tempfile
from pathlib import Path
from contextlib import closing,ExitStack
import draft_spool
import work_repository as wr
from work_transfer import send_tree,receive_tree
from runtime_support import checkpoint
from io_stages import stage
from work_failures import FailureContext,WorkFailure,format_failure


def reader(path):
    return wr.Repository.open_existing(path,read_only=True)


def compact_previous(record):
    p=record['payload']
    return {**{k:v for k,v in record.items() if k!='payload'},'payload':{
        'store_presets':p.get('store_presets',{}),'store_ui':p.get('store_ui',{}),
        'loaded':{'hashes':(p.get('loaded') or {}).get('hashes',{})},
        'catalogs':{w:{'revision':c.get('revision')} for w,c in p.get('catalogs',{}).items()},
        'profile':{'revision':(p.get('profile') or {}).get('revision')}}}


def _staged_arguments(arguments):
    payload,*rest=arguments
    payload['results']=draft_spool.StoreResults(payload['results'])
    if 'new_focus' in payload:payload['new_focus']=draft_spool.native(payload['new_focus'])
    return [payload,*draft_spool.native(rest)]


def serve(connection,path,cancel):
    payload=None;encoded_job=None;save_day=None;old=None;batch=None;review_json=None;review_records=None;review_draft=None;expected_job=None;expected_orders=None;codes=None;ro=reader(path);work=wr.Repository.open_existing(path)
    failure_context=None;drafts=ExitStack()
    def progress(text):
        if failure_context is not None:failure_context.progress(text)
        # RowProgress is a str subclass with a multi-argument constructor; carry
        # its explicit fields instead of relying on pickle's str reconstruction.
        if hasattr(text,'active'):connection.send(('commit',text.active))
        elif hasattr(text,'current') and hasattr(text,'total'):connection.send(('rows',(str(text),text.current,text.total)))
        else:connection.send(('progress',str(text)))
    try:
        while True:
            command=connection.recv()
            if command is None:return
            op,name=command if isinstance(command,tuple) and len(command)==2 else (command,None)
            failure_context=FailureContext(op)
            try:
                if op in ('prepare_save','prepare_normal','delivery_prepare','delivery_probe'):
                    drafts.close();batch=None;payload=None
                    review_json=None;review_records=None;review_draft=None
                if name is None:
                    arguments,_=receive_tree(connection,cancel)
                else:
                    if op not in draft_spool.OPERATIONS:raise ValueError('该作业不能读取临时签发草稿')
                    progress('正在读取签发草稿临时文件…')
                    tree=drafts.enter_context(draft_spool.open_draft(path,name,cancel,
                        lambda n:progress(f'正在读取签发草稿：已核对 {n:,} 个数据块')))
                    arguments=_staged_arguments(tree)
                checkpoint(cancel);writing=op in ('save','confirm_normal','delivery_execute','review_export','storage_copy','delete_orders','delete_job')
                if op=='prepare_save':
                    encoded_job=None;payload=arguments;progress('正在校验完整作业并核对同日记录…')
                    save_day=payload['day'];encoded_job=wr.prepare_job(save_day,payload,progress,known=ro.known_chunks());digest=encoded_job[2]
                    with closing(ro.connect()) as db:row=db.execute('SELECT revision,digest,saved,summary FROM jobs WHERE day=?',(payload['day'],)).fetchone()
                    old={'revision':row[0],'digest':row[1],'saved':row[2],'summary':wr.loads(row[3])} if row else None
                    same=bool(old and old['digest']==digest)
                    # The immutable encoded snapshot is sufficient from here.
                    # Drop this decoded tree before loading a second large job.
                    payload=None;arguments=None
                    if old and not same:
                        with stage('读取原作业以核对覆盖差异',progress):old=ro.review_job(save_day,progress)
                    result={'old':old,'same':same}
                elif op=='save':
                    if encoded_job is None:raise ValueError('缺少已核对的作业快照')
                    # No termination/cancellation once this operation is sent. CAS and
                    # rollback remain inside the existing repository transaction.
                    progress('正在保存完整作业；本次提交结束后才可关闭…')
                    result=work._save_prepared(save_day,encoded_job,old['revision'] if old else None,progress);encoded_job=None
                elif op=='prepare_normal':
                    import normal_orders,normal_delivery,new_product_focus as focus
                    expected_orders=None;payload,codes=arguments[:2]
                    if len(arguments)>2:payload['execution_warehouses']={**payload.get('execution_warehouses',{}),**arguments[2]}
                    repository=normal_orders.Repository.from_existing_work(ro)
                    progress('正在核对正常单与完整差异…')
                    focus.require_for(payload.get('new_focus'),payload['day'],(payload['results'][code] for code in codes))
                    head=normal_delivery.job_head(ro,payload['day']);expected_job=head['revision'] if head else None;expected_orders={};details={};lines=[];review_records=[]
                    for code in codes:
                        progress(f'核对正常单 {codes.index(code)+1}/{len(codes)} · {code}')
                        checkpoint(cancel);r=payload['results'][code];previous_head=repository.head(payload['day'],code)
                        same=bool(previous_head and previous_head['digest']==normal_orders.fingerprint_owned(r,payload.get('new_focus')))
                        from warehouse_orders import summaries
                        available=summaries(r,payload.get('new_focus'))
                        chosen=payload.get('execution_warehouses',{}).get(code,[v['warehouse'] for v in available])
                        if not chosen or len(chosen)!=len(set(chosen)) or any(w not in [v['warehouse'] for v in available] for w in chosen):raise ValueError('请选择有效且不重复的本轮执行仓：'+code)
                        scope_only=False
                        if same:
                            import warehouse_execution
                            with closing(ro.connect()) as db:scope_only=warehouse_execution.needs_publish(db,previous_head['oid'],previous_head['digest'],chosen)
                        previous=repository.get(payload['day'],code,previous_head['revision'],view='review' if same else None,progress=progress) if previous_head else None
                        expected_orders[code]=previous['revision'] if previous else None
                        if not same:
                            import order_review
                            review=order_review.build(previous,r,payload.get('new_focus'));review_records.append(review)
                        action='按原计算方案补签所选仓' if scope_only else '所选仓已签发，无需新修订' if same else f"更新所选仓（原来源 V{previous['revision']:03d}，旧版保留）" if previous else '首次签发所选仓'
                        scope_amount=sum(v['amount'] for v in available if v['warehouse'] in chosen)
                        lines.append(r['store_name']+' ('+code+')：'+action+'；执行仓：'+'、'.join(chosen)+f"；本仓合计¥{scope_amount:,.2f}。未选仓及原双仓计算保持。")
                        details[code]={'关注清单原版本':(previous['payload'].get('new_focus') or {}).get('revision') if previous else None,'关注清单新版本':(payload.get('new_focus') or {}).get('revision'),'new_selection':r.get('new_products',{}),'changes':({'added':[],'removed':[],'changed':[],'old_settings':previous['payload']['result'].get('effective_settings'),'new_settings':r.get('effective_settings'),'old_sources':previous['payload']['result'].get('sources'),'new_sources':r.get('sources')} if same else review['changes']),'before_amount':previous['payload']['result']['summary']['amount'] if previous else None,'after_amount':r['summary']['amount']}
                    if head:details['草稿覆盖']={'原保存时间':head['saved'],'原门店状态':head['summary']['stores'],'新门店状态':wr.summary(payload)['stores']}
                    review_draft=details.get('草稿覆盖',{})
                    from review_json import Prepared
                    review_json=Prepared(details,cancel=cancel)
                    import order_review
                    result={'lines':lines,**order_review.preview(review_records)}
                elif op=='confirm_normal':
                    import normal_orders,normal_delivery
                    if payload is None or expected_orders is None:raise ValueError('缺少已核对的正常单')
                    progress('正在归档正常单与完整草稿；最终提交前可取消…')
                    repository=normal_orders.Repository.from_existing_work(work)
                    def remember(heads):failure_context.heads=[dict(h) for h in heads]
                    issued=repository.confirm(payload,codes,expected_job,expected_orders,progress=progress,headers_only=True,cancel=cancel,prepared_heads=remember,warehouses=payload.get('execution_warehouses'))
                    result={'orders':[{k:v for k,v in r.items() if k!='payload'} for r in issued],'saved':normal_delivery.job_head(work,payload['day'])['saved']}
                elif op=='review_export':
                    if review_json is None and batch is not None:
                        # Confirmation released only private preview objects.
                        # Recreate the same review on demand after a failed run.
                        import order_review,normal_orders
                        from review_json import Prepared,Records
                        repository=normal_orders.Repository.from_existing_work(ro)
                        review_records=Records(path);drafts.callback(review_records.close)
                        for p in batch['plans']:
                            checkpoint(cancel)
                            if not p['same']:
                                review_records.append(order_review.build(repository.get(p['old']['day'],p['store'],p['old']['revision']) if p['old'] else None,
                                    batch['payload']['results'][p['store']],batch['payload'].get('new_focus')))
                        review_json=Prepared(review_records,canonical=True,cancel=cancel,series=True)
                    if review_json is None:raise ValueError('尚未生成完整差异')
                    destination=Path(arguments)
                    if destination.suffix.lower()=='.xlsx':
                        import business_excel,order_review,history_forms
                        if review_records is None:raise ValueError('尚未生成差异表')
                        result=business_excel.write(destination,[('签发差异',order_review.HEADERS,order_review.rows(review_records)),('草稿覆盖',['核对项目','内容','类型'],history_forms.fields(review_draft or {}))],cancel,progress)
                        connection.send(('success',None));send_tree(connection,result,None);continue
                    if destination.suffix.lower()!='.json' or destination.exists():raise ValueError('请使用一个尚不存在的JSON文件名')
                    fd,tmp=tempfile.mkstemp(prefix='.review-',dir=destination.parent)
                    try:
                        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:review_json.write(f)
                        os.rename(tmp,destination)
                    finally:
                        if os.path.exists(tmp):os.unlink(tmp)
                    result=str(destination)
                elif op in ('delivery_prepare','delivery_probe'):
                    import normal_orders,normal_delivery
                    import order_review
                    from review_json import Prepared,Records
                    batch=None;supplied,codes=arguments[:2];warehouses=arguments[2] if len(arguments)>2 else None;repository=normal_orders.Repository.from_existing_work(ro)
                    review_records=Records(path);drafts.callback(review_records.close);shown=[]
                    def reviewed(plan, current):
                        if not plan['same']:
                            review_records.append(order_review.build(plan['old'],current,supplied.get('new_focus')))
                        if plan['old']:
                            plan['old']={k:v for k,v in plan['old'].items() if k!='payload'}
                        shown.append({**plan,'name':current['store_name'],'amount':current['summary']['amount']})
                    with stage('核对导出修订',progress):
                        batch=normal_delivery.prepare_owned_batch(repository,supplied,codes,progress=progress,cancel=cancel,warehouses=warehouses,on_plan=reviewed)
                    batch['complete']=op=='delivery_prepare'
                    review_json=Prepared(review_records,canonical=True,cancel=cancel,series=True)
                    review_draft={'原草稿':batch['head'],'本次草稿':supplied.get('_draft_summary',wr.summary(batch['payload']))}
                    result={'head':batch['head'],'summary':review_draft['本次草稿'],'plans':shown,**order_review.preview(review_records)}
                elif op=='delivery_expand':
                    if batch is None:raise ValueError('缺少已核对的导出批次')
                    tail,order=arguments
                    pending=draft_spool.result_source(tail['results'])
                    checked=draft_spool.result_source(batch['payload']['results'])
                    from scheduled_snapshots import ordered
                    merged=ordered(order,checked,pending)
                    tail['results']=draft_spool.StoreResults(merged) if name is not None else merged
                    del pending,checked,merged
                    tail['execution_warehouses']={**tail.get('execution_warehouses',{}),**batch.get('warehouses',{})}
                    wr.summary(tail);batch['payload']=tail;batch['complete']=True;result=True
                elif op=='delivery_execute':
                    import normal_orders,normal_delivery
                    if batch is None:raise ValueError('缺少已核对的导出批次')
                    if not batch.get('complete') and any(not p['same'] for p in batch['plans']):raise ValueError('完整签发草稿尚未准备，不能归档')
                    review_json=None;review_records=None
                    for plan in batch['plans']:
                        if plan['old']:plan['old']={k:v for k,v in plan['old'].items() if k!='payload'}
                    supplied=None;tail=None;values=None;previous=None;review=None;r=None;order=None
                    repository=normal_orders.Repository.from_existing_work(work)
                    executing=batch
                    try:result=normal_delivery.execute_batch(repository,executing,arguments,progress=progress,cancel=cancel,consume=True,
                                                             on_receipt=lambda task:connection.send(('receipt',task)),
                                                             release_draft=drafts.close)
                    finally:
                        if executing.get('consumed'):
                            batch=None;drafts.close()
                elif op in ('delete_preview','delete_orders','delete_job'):
                    import data_maintenance as maintenance
                    if op=='delete_preview':
                        if isinstance(arguments,dict):
                            result=maintenance.inspect_deletion(path,arguments['heads'],progress,cancel,
                                                                recover_pending=arguments.get('recover_pending',False))
                        else:
                            result=maintenance.inspect_deletion(path,arguments,progress,cancel)
                    elif op=='delete_orders':result=maintenance.execute(path,arguments['plan'],arguments['ack'],progress,cancel)
                    else:result=maintenance.delete_job(path,arguments['day'],arguments['revision'],progress,cancel)
                elif op=='storage_copy':
                    from storage_maintenance import backup
                    result=backup(path,arguments['destination'],progress,cancel)
                elif op in ('load','copy_config'):
                    with stage('读取并校验作业快照',progress):result=ro.get_job(arguments,paths=[('store_presets',),('store_ui',),('active_plan',)] if op=='copy_config' else None,progress=progress)
                    if op=='copy_config' and result:result={k:result['payload'].get((k,)) for k in ('store_presets','store_ui','active_plan')}
                else:raise ValueError('未知作业操作')
                if not writing:checkpoint(cancel)
                connection.send(('success',None));send_tree(connection,result,None if writing else cancel,consume=op in ('load','copy_config'));result=None
            except Exception as exc:
                from runtime_support import Cancelled
                if isinstance(exc,Cancelled):connection.send(('error',('Cancelled',str(exc))))
                else:
                    evidence=failure_context.capture(exc,path)
                    connection.send(('error',{'type':'WorkFailure','message':str(exc) if isinstance(exc,WorkFailure) else format_failure(evidence),'evidence':evidence}))
                if name is not None:
                    drafts.close();batch=None;payload=None;expected_orders=None
                    review_json=None;review_records=None;review_draft=None
    except (EOFError,BrokenPipeError,OSError):pass
    finally:
        try:drafts.close()
        finally:connection.close()
