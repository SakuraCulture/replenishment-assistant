"""Isolated daily-work I/O. Dual-format storage and fixed-revision projections; no Qt in the worker."""
import hashlib,sqlite3,json,os,tempfile
from pathlib import Path
from contextlib import closing
import work_repository as wr
from work_transfer import send_tree,receive_tree
from runtime_support import checkpoint
from io_stages import stage


def reader(path):
    class ReadOnly(wr.Repository):
        def connect(self):return sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True)
    value=ReadOnly.__new__(ReadOnly);value.path=Path(path).resolve();return value


def compact_previous(record):
    p=record['payload']
    return {**{k:v for k,v in record.items() if k!='payload'},'payload':{
        'store_presets':p.get('store_presets',{}),'store_ui':p.get('store_ui',{}),
        'loaded':{'hashes':(p.get('loaded') or {}).get('hashes',{})},
        'catalogs':{w:{'revision':c.get('revision')} for w,c in p.get('catalogs',{}).items()},
        'profile':{'revision':(p.get('profile') or {}).get('revision')}}}


def serve(connection,path,cancel):
    payload=None;encoded_job=None;save_day=None;old=None;batch=None;review_text=None;review_records=None;review_draft=None;expected_job=None;expected_orders=None;codes=None;ro=reader(path);work=wr.Repository.__new__(wr.Repository);work.path=Path(path)
    def progress(text):
        # RowProgress is a str subclass with a multi-argument constructor; carry
        # its explicit fields instead of relying on pickle's str reconstruction.
        if hasattr(text,'active'):connection.send(('commit',text.active))
        elif hasattr(text,'current') and hasattr(text,'total'):connection.send(('rows',(str(text),text.current,text.total)))
        else:connection.send(('progress',str(text)))
    try:
        while True:
            command=connection.recv()
            if command is None:return
            try:
                op=command;arguments,_=receive_tree(connection,cancel)
                checkpoint(cancel);writing=op in ('save','confirm_normal','delivery_execute','review_export','storage_copy')
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
                    expected_orders=None;payload,codes=arguments
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=ro
                    progress('正在核对正常单与完整差异…')
                    focus.require_for(payload.get('new_focus'),payload['day'],[payload['results'][code] for code in codes])
                    head=normal_delivery.job_head(ro,payload['day']);expected_job=head['revision'] if head else None;expected_orders={};details={};lines=[];review_records=[]
                    for code in codes:
                        progress(f'核对正常单 {codes.index(code)+1}/{len(codes)} · {code}')
                        checkpoint(cancel);r=payload['results'][code];previous_head=repository.head(payload['day'],code)
                        same=bool(previous_head and previous_head['digest']==normal_orders.fingerprint_owned(r,payload.get('new_focus')))
                        previous=repository.get(payload['day'],code,previous_head['revision'],view='review' if same else None,progress=progress) if previous_head else None
                        expected_orders[code]=previous['revision'] if previous else None
                        if not same:
                            import order_review
                            review_records.append(order_review.build(previous,r,payload.get('new_focus')))
                        action='已归档，无需新修订' if same else f"同号 V{previous['revision']:03d} → V{previous['revision']+1:03d}" if previous else '首次 V001'
                        lines.append(r['store_name']+' ('+code+')：'+action+f"；¥{r['summary']['amount']:,.2f}，{r['summary']['positive']}款，待核{r['summary']['issues']}，预算缺口{r['summary']['budget_deferred']}")
                        details[code]={'关注清单原版本':(previous['payload'].get('new_focus') or {}).get('revision') if previous else None,'关注清单新版本':(payload.get('new_focus') or {}).get('revision'),'new_selection':r.get('new_products',{}),'changes':({'added':[],'removed':[],'changed':[],'old_settings':previous['payload']['result'].get('effective_settings'),'new_settings':r.get('effective_settings'),'old_sources':previous['payload']['result'].get('sources'),'new_sources':r.get('sources')} if same else normal_orders.revision_diff(previous['payload']['result'] if previous else None,r)),'before_amount':previous['payload']['result']['summary']['amount'] if previous else None,'after_amount':r['summary']['amount']}
                    if head:details['草稿覆盖']={'原保存时间':head['saved'],'原门店状态':head['summary']['stores'],'新门店状态':wr.summary(payload)['stores']}
                    review_draft=details.get('草稿覆盖',{})
                    review_text=json.dumps(details,ensure_ascii=False,indent=2)
                    import order_review
                    result={'lines':lines,**order_review.preview(review_records)}
                elif op=='confirm_normal':
                    import normal_orders,normal_delivery
                    if payload is None or expected_orders is None:raise ValueError('缺少已核对的正常单')
                    progress('正在归档正常单与完整草稿；最终提交前可取消…')
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=work
                    issued=repository.confirm(payload,codes,expected_job,expected_orders,progress=progress,headers_only=True,cancel=cancel)
                    result={'orders':[{k:v for k,v in r.items() if k!='payload'} for r in issued],'saved':normal_delivery.job_head(work,payload['day'])['saved']}
                elif op=='review_export':
                    if review_text is None:raise ValueError('尚未生成完整差异')
                    destination=Path(arguments)
                    if destination.suffix.lower()=='.xlsx':
                        import business_excel,order_review,history_forms
                        if review_records is None:raise ValueError('尚未生成差异表')
                        result=business_excel.write(destination,[('签发差异',order_review.HEADERS,order_review.rows(review_records)),('草稿覆盖',['核对项目','内容','类型'],history_forms.fields(review_draft or {}))],cancel,progress)
                        connection.send(('success',None));send_tree(connection,result,None);continue
                    if destination.suffix.lower()!='.json' or destination.exists():raise ValueError('请使用一个尚不存在的JSON文件名')
                    fd,tmp=tempfile.mkstemp(prefix='.review-',dir=destination.parent)
                    try:
                        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:f.write(review_text)
                        os.rename(tmp,destination)
                    finally:
                        if os.path.exists(tmp):os.unlink(tmp)
                    result=str(destination)
                elif op in ('delivery_prepare','delivery_probe'):
                    import normal_orders,normal_delivery
                    batch=None;supplied,codes=arguments;repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=ro
                    with stage('核对导出修订',progress):batch=normal_delivery.prepare_owned_batch(repository,supplied,codes,progress=progress,cancel=cancel)
                    batch['complete']=op=='delivery_prepare'
                    import order_review
                    review_records=[order_review.build(p['old'],batch['payload']['results'][p['store']],supplied.get('new_focus')) for p in batch['plans'] if not p['same']]
                    review_text=wr.dumps(review_records)
                    review_draft={'原草稿':batch['head'],'本次草稿':supplied.get('_draft_summary',wr.summary(batch['payload']))}
                    result={'head':batch['head'],'summary':supplied.get('_draft_summary',wr.summary(batch['payload'])), 'plans':[],**order_review.preview(review_records)}
                    for plan in batch['plans']:
                        r=batch['payload']['results'][plan['store']]
                        result['plans'].append({**{k:v for k,v in plan.items() if k!='old'},'old':{k:v for k,v in plan['old'].items() if k!='payload'} if plan['old'] else None,'name':r['store_name'],'amount':r['summary']['amount']})
                elif op=='delivery_expand':
                    if batch is None:raise ValueError('缺少已核对的导出批次')
                    tail,order=arguments
                    values={**tail['results'],**batch['payload']['results']}
                    tail['results']={s:values[s] for s in order}
                    wr.summary(tail);batch['payload']=tail;batch['complete']=True;result=True
                elif op=='delivery_execute':
                    import normal_orders,normal_delivery
                    if batch is None:raise ValueError('缺少已核对的导出批次')
                    if not batch.get('complete') and any(not p['same'] for p in batch['plans']):raise ValueError('完整签发草稿尚未准备，不能归档')
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=work
                    result=normal_delivery.execute_batch(repository,batch,arguments,progress=progress,cancel=cancel)
                elif op=='storage_copy':
                    from storage_maintenance import convert
                    result=convert(path,arguments['destination'],arguments['mode'],progress,cancel)
                elif op in ('load','copy_config'):
                    with stage('读取并校验作业快照',progress):result=ro.get_job(arguments,paths=[('store_presets',),('store_ui',)] if op=='copy_config' else None,progress=progress)
                    if op=='copy_config' and result:result={k:result['payload'][(k,)] for k in ('store_presets','store_ui')}
                else:raise ValueError('未知作业操作')
                if not writing:checkpoint(cancel)
                connection.send(('success',None));send_tree(connection,result,None if writing else cancel,consume=op in ('load','copy_config'));result=None
            except Exception as exc:
                connection.send(('error',(type(exc).__name__,str(exc))))
    except (EOFError,BrokenPipeError,OSError):pass
    finally:connection.close()
