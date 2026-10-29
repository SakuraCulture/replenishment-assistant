"""Isolated daily-work I/O. Existing Repository code and on-disk schema are unchanged."""
import hashlib,sqlite3,json,os,tempfile
from pathlib import Path
from contextlib import closing
import work_repository as wr
from work_transfer import send_tree,receive_tree
from runtime_support import checkpoint


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
    payload=None;old=None;batch=None;review_text=None;expected_job=None;expected_orders=None;codes=None;ro=reader(path);work=wr.Repository.__new__(wr.Repository);work.path=Path(path)
    def progress(text):
        # RowProgress is a str subclass with a multi-argument constructor; carry
        # its explicit fields instead of relying on pickle's str reconstruction.
        if hasattr(text,'current') and hasattr(text,'total'):connection.send(('rows',(str(text),text.current,text.total)))
        else:connection.send(('progress',str(text)))
    try:
        while True:
            command=connection.recv()
            if command is None:return
            try:
                op=command;arguments,_=receive_tree(connection,cancel)
                checkpoint(cancel);writing=op in ('save','confirm_normal','delivery_execute','review_export')
                if op=='prepare_save':
                    payload=arguments;progress('正在校验完整作业并核对同日记录…')
                    wr.summary(payload);digest=hashlib.sha256(wr.dumps(payload).encode()).hexdigest()
                    with closing(ro.connect()) as db:row=db.execute('SELECT revision,digest,saved,summary FROM jobs WHERE day=?',(payload['day'],)).fetchone()
                    old={'revision':row[0],'digest':row[1],'saved':row[2],'summary':wr.loads(row[3])} if row else None
                    same=bool(old and old['digest']==digest)
                    if old and not same:old=compact_previous(ro.get_job(payload['day']))
                    result={'old':old,'same':same}
                elif op=='save':
                    if payload is None:raise ValueError('缺少已核对的作业快照')
                    # No termination/cancellation once this operation is sent. CAS and
                    # rollback remain inside the existing repository transaction.
                    progress('正在保存完整作业；本次提交结束后才可关闭…')
                    result=work.save_job(payload['day'],payload,old['revision'] if old else None)
                elif op=='prepare_normal':
                    import normal_orders,normal_delivery,new_product_focus as focus
                    payload,codes=arguments
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=ro
                    progress('正在核对正常单与完整差异…')
                    focus.require_for(payload.get('new_focus'),payload['day'],[payload['results'][code] for code in codes])
                    head=normal_delivery.job_head(ro,payload['day']);expected_job=head['revision'] if head else None;expected_orders={};details={};lines=[]
                    for code in codes:
                        checkpoint(cancel);r=payload['results'][code];previous=repository.get(payload['day'],code);expected_orders[code]=previous['revision'] if previous else None
                        same=previous and previous['digest']==normal_orders.fingerprint_owned(r,payload.get('new_focus'))
                        action='已归档，无需新修订' if same else f"同号 V{previous['revision']:03d} → V{previous['revision']+1:03d}" if previous else '首次 V001'
                        lines.append(r['store_name']+' ('+code+')：'+action+f"；¥{r['summary']['amount']:,.2f}，{r['summary']['positive']}款，待核{r['summary']['issues']}，预算缺口{r['summary']['budget_deferred']}")
                        details[code]={'关注清单原版本':(previous['payload'].get('new_focus') or {}).get('revision') if previous else None,'关注清单新版本':(payload.get('new_focus') or {}).get('revision'),'new_selection':r.get('new_products',{}),'changes':normal_orders.revision_diff(previous['payload']['result'] if previous else None,r),'before_amount':previous['payload']['result']['summary']['amount'] if previous else None,'after_amount':r['summary']['amount']}
                    if head:details['草稿覆盖']={'原保存时间':head['saved'],'原门店状态':head['summary']['stores'],'新门店状态':wr.summary(payload)['stores']}
                    review_text=json.dumps(details,ensure_ascii=False,indent=2)
                    result={'lines':lines,'detail_text':review_text[:120000],'truncated':len(review_text)>120000}
                elif op=='confirm_normal':
                    import normal_orders,normal_delivery
                    if payload is None or expected_orders is None:raise ValueError('缺少已核对的正常单')
                    progress('正在归档正常单与完整草稿；提交结束后才可关闭…')
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=work
                    issued=repository.confirm(payload,codes,expected_job,expected_orders)
                    result={'orders':[{k:v for k,v in r.items() if k!='payload'} for r in issued],'saved':normal_delivery.job_head(work,payload['day'])['saved']}
                elif op=='review_export':
                    if review_text is None:raise ValueError('尚未生成完整差异')
                    destination=Path(arguments)
                    if destination.suffix.lower()!='.json' or destination.exists():raise ValueError('请使用一个尚不存在的JSON文件名')
                    fd,tmp=tempfile.mkstemp(prefix='.review-',dir=destination.parent)
                    try:
                        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:f.write(review_text)
                        os.rename(tmp,destination)
                    finally:
                        if os.path.exists(tmp):os.unlink(tmp)
                    result=str(destination)
                elif op=='delivery_prepare':
                    import normal_orders,normal_delivery
                    supplied,codes=arguments;repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=ro
                    batch=normal_delivery.prepare_owned_batch(repository,supplied,codes,progress=progress,cancel=cancel)
                    result={'head':batch['head'],'summary':wr.summary(batch['payload']), 'plans':[]}
                    for plan in batch['plans']:
                        r=batch['payload']['results'][plan['store']]
                        result['plans'].append({**{k:v for k,v in plan.items() if k!='old'},'old':{k:v for k,v in plan['old'].items() if k!='payload'} if plan['old'] else None,'name':r['store_name'],'amount':r['summary']['amount']})
                elif op=='delivery_execute':
                    import normal_orders,normal_delivery
                    if batch is None:raise ValueError('缺少已核对的导出批次')
                    repository=normal_orders.Repository.__new__(normal_orders.Repository);repository.work=work
                    result=normal_delivery.execute_batch(repository,batch,arguments,progress=progress,cancel=cancel)
                elif op in ('load','copy_config'):
                    progress('正在读取并校验作业快照…');result=ro.get_job(arguments)
                    if op=='copy_config' and result:result={k:result['payload'][k] for k in ('store_presets','store_ui')}
                else:raise ValueError('未知作业操作')
                if not writing:checkpoint(cancel)
                connection.send(('success',None));send_tree(connection,result,None if writing else cancel,consume=op in ('load','copy_config'));result=None
            except Exception as exc:
                connection.send(('error',(type(exc).__name__,str(exc))))
    except (EOFError,BrokenPipeError,OSError):pass
    finally:connection.close()
