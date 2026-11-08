"""Background preparation and one-confirmation signed export; no Qt access."""
import copy
from pathlib import Path
from contextlib import closing
import work_repository as wr
import normal_orders,normal_exports,new_product_focus as focus
from runtime_support import checkpoint
from io_stages import stage
import export_receipts as receipts
from export_files import sha256,explain_error


def job_head(work,day):
    with closing(work.connect()) as db:
        row=db.execute('SELECT revision,digest,saved,summary FROM jobs WHERE day=?',(day,)).fetchone()
    return {'revision':row[0],'digest':row[1],'saved':row[2],'summary':wr.loads(row[3])} if row else None


def prepare(repository,payload,store,progress=lambda s:None,cancel=None):
    """Thin compatibility entry for a single store."""
    batch=prepare_batch(repository,payload,[store],progress=progress,cancel=cancel)
    return {**batch['plans'][0],'payload':batch['payload'],'head':batch['head']}


def probe_payload(full,codes):
    """Keep signing blockers when projecting the small export preflight."""
    slim={k:full[k] for k in ('day','input_dirty')}
    slim['new_focus']=full.get('new_focus')
    slim.update(states={s:full['states'][s] for s in codes},results={s:full['results'][s] for s in codes},
                model_version=full['model_version'],_draft_summary=wr.summary(full),
                preissue_drafts={s:v for s,v in full.get('preissue_drafts',{}).items() if s in codes})
    return slim


def prepare_batch(repository,payload,codes,progress=lambda s:None,cancel=None):
    """Freeze the complete draft once, then plan stores in user-visible order."""
    checkpoint(cancel)
    return prepare_owned_batch(repository,copy.deepcopy(payload),codes,progress=progress,cancel=cancel)


def prepare_owned_batch(repository,payload,codes,progress=lambda s:None,cancel=None,*,warehouses=None):
    """Internal process entry: IPC has already transferred an exclusively owned tree."""
    progress('冻结本批次草稿并检查所选门店…');checkpoint(cancel)
    frozen=payload
    if not codes or len(codes)!=len(set(codes)):raise ValueError('请选择互不重复的有效门店')
    from preissue import validate_payload
    validate_payload(frozen,codes,cancel=cancel)
    plans=[];head=job_head(repository.work,frozen['day'])
    for i,store in enumerate(codes,1):
        checkpoint(cancel);progress(f'核对门店 {i}/{len(codes)}：{store}')
        result=frozen['results'][store]
        if frozen.get('input_dirty') or frozen['states'][store]['state']!='已完成':raise ValueError('结果已失效：'+store)
        head_order=repository.head(frozen['day'],store)
        same=bool(head_order and head_order['digest']==normal_orders.fingerprint_owned(result,frozen.get('new_focus')))
        old=repository.get(frozen['day'],store,head_order['revision'],view='export' if same else None,progress=progress) if head_order else None
        if not same:
            if frozen['day']!=wr.today():
                from job_date_messages import export_previous
                raise ValueError(export_previous(frozen['day'],wr.today(),bool(head_order)))
            if frozen.get('admission_required'):
                from admission import verify_result
                with closing(repository.work.connect()) as db:verify_result(db,result,required=True)
            focus.require_for(frozen.get('new_focus'),frozen['day'],[result])
        from warehouse_orders import summaries
        available=summaries(result,frozen.get('new_focus'))
        selected=[v['warehouse'] for v in available] if warehouses is None else warehouses.get(store,[])
        if not selected or len(selected)!=len(set(selected)) or any(w not in [v['warehouse'] for v in available] for w in selected):raise ValueError('请选择有效且不重复的供货仓：'+store)
        descriptor={'store':store,'day':frozen['day'],'payload':{'result':result}}
        files=[{**v,'filename':normal_exports.filename(descriptor,v['warehouse'])} for v in available if v['warehouse'] in selected]
        plans.append({'store':store,'old':old,'same':same,'filename':files[0]['filename'],'files':files})
    if any(not p['same'] for p in plans):wr.summary(frozen)
    return {'payload':frozen,'plans':plans,'head':head}


def execute_batch(repository,batch,destination,progress=lambda s:None,cancel=None):
    """One archive transaction, sequential files, explicit partial-failure receipt."""
    import tempfile,os
    import export_progress as ep
    plans=batch['plans'];payload=batch['payload'];destination=Path(destination)
    files=[(p,f) for p in plans for f in p['files']]
    paths=[destination/f['filename'] for p,f in files] if destination.is_dir() else [destination]
    if len(paths)!=len(files):raise ValueError('多仓或多店导出请选择文件夹')
    if len({os.path.normcase(str(p.resolve())) for p in paths})!=len(paths):raise ValueError('门店文件名重复，请分次导出并指定不同文件名')
    fd,probe=tempfile.mkstemp(prefix='.delivery-check-',dir=paths[0].parent);os.close(fd);os.unlink(probe);checkpoint(cancel)
    signed=any(not p['same'] for p in plans);orders=[]
    receipt=receipts.begin(repository.work.path,'merchant',[
        {'path':str(path.resolve()),'state':'未执行','stage':'准备','head':None,
         'day':payload['day'],'store':p['store'],'warehouse':f['warehouse']}
        for (p,f),path in zip(files,paths)],signature='尚未新增签发' if signed else '归档版本未改变',signed=signed)
    try:
        if signed:
            receipt['state']='正在归档';receipt['signature']='签发结果待核';receipts.update(repository.work.path,receipt)
            progress('后台归档所选门店与完整来源（一次事务）…')
            orders=repository.confirm(payload,[p['store'] for p in plans],batch['head']['revision'] if batch['head'] else None,{p['store']:p['old']['revision'] if p['old'] else None for p in plans},progress=progress,cancel=cancel)
            receipt['signature']='所选正常单已归档，修订保留'
        else:
            heads={h['store']:h for h in repository.headers() if h['day']==payload['day']}
            for p in plans:
                head=heads.get(p['store']);order=p['old']
                if not head or any(head[k]!=order[k] for k in ('oid','revision','digest','saved')):raise wr.Conflict('正常单已更新，请重新核对：'+p['store'])
                orders.append(order)
    except Exception as exc:
        receipt['state']='归档需核对';receipt['warnings'].append(str(exc));receipts.update(repository.work.path,receipt)
        raise RuntimeError(receipts.summary(receipt)) from exc
    by_store={o['store']:o for o in orders}
    records=[]
    for item in receipt['files']:
        order=by_store[item['store']];item['head']=receipts.identity(order,item['warehouse']);records.append((order,item))
    receipt['state']='归档已核实，等待导出';receipts.update(repository.work.path,receipt)
    completed=_write_exports(repository,records,receipt,progress,cancel)
    return {'paths':completed,'signed':signed,'saved':job_head(repository.work,payload['day'])['saved'] if signed else None,
            'export_task':receipt['id'],'warnings':receipt['warnings']}


def _write_exports(repository,records,receipt,progress,cancel):
    import export_progress as ep
    from archive_orders import require_active
    from warehouse_orders import child_number
    done=[];current=None
    try:
        for i,(order,item) in enumerate(records,1):
            current=item;checkpoint(cancel);warehouse=item['warehouse'];path=Path(item['path'])
            with closing(repository.work.connect()) as db:require_active(db,order['oid'],warehouse)
            item.update(stage='写出文件',state='正在写出');receipts.update(repository.work.path,receipt)
            with stage(f'写出确认单 {i}/{len(records)} · {warehouse}',progress):
                normal_exports.export(order,path,cancel=cancel,progress=ep.prefixed(progress,f'文件 {i}/{len(records)} · {order["payload"]["result"]["store_name"]} · {warehouse}'),warehouse=warehouse)
            item.update(state='已生成',stage='核验文件摘要');done.append(str(path));receipts.update(repository.work.path,receipt)
            item['sha256']=sha256(path);item['stage']='记录导出日志';receipts.update(repository.work.path,receipt)
            repository.log(order,'带号导出',{'path':str(path),'sha256':item['sha256'],'warehouse':warehouse,'child_number':child_number(order['number'],warehouse),'export_task':receipt['id']})
            item.update(state='已完成',stage='完成');receipts.update(repository.work.path,receipt)
    except Exception as exc:
        if current is not None:
            current['error']=('文件已生成，日志写入失败：' if current['stage']=='记录导出日志' else '')+explain_error(exc)
            if current['state'] not in ('已生成','已完成'):current['state']='未完成'
        receipt['state']='导出未全部完成';receipts.update(repository.work.path,receipt)
        raise RuntimeError(receipts.summary(receipt)) from exc
    receipt['state']='已完成' if not receipt['warnings'] else '文件已完成，回执需核对';receipts.update(repository.work.path,receipt)
    return done


def execute(repository,plan,path,progress=lambda s:None,cancel=None):
    """Single-store compatibility delegates to the same transaction/export path."""
    batch={'plans':[plan],'payload':plan['payload'],'head':plan['head']}
    receipt=execute_batch(repository,batch,path,progress=progress,cancel=cancel)
    revision=(plan['old']['revision'] if plan['old'] else 0)+(not plan['same'])
    order=repository.get(plan['payload']['day'],plan['store'],revision)
    return {**receipt,'path':receipt['paths'][0],'number':order['number'],'revision':order['revision']}


def export_signed(repository,order,destination,warehouses,progress=lambda s:None,cancel=None):
    """Re-export this immutable revision, including historical revisions; no signing."""
    from warehouse_orders import partitions,child_number
    import export_progress as ep
    available=partitions(order['payload']['result'],order['payload'].get('new_focus'))
    if not warehouses or len(warehouses)!=len(set(warehouses)) or any(w not in available for w in warehouses):raise ValueError('请选择有效仓库')
    selected=[w for w in available if w in warehouses];dest=Path(destination)
    paths=[dest/normal_exports.filename(order,w) for w in selected] if dest.is_dir() else [dest]
    if len(paths)!=len(selected):raise ValueError('多仓导出请选择文件夹')
    checkpoint(cancel)
    receipt=receipts.begin(repository.work.path,'merchant',[
        {'path':str(path.resolve()),'state':'未执行','stage':'准备','head':receipts.identity(order,wh),
         'day':order['day'],'store':order['store'],'warehouse':wh} for wh,path in zip(selected,paths)],
        signature=f'原修订 V{order["revision"]:03d} 保留',signed=False)
    return _write_exports(repository,[(order,item) for item in receipt['files']],receipt,progress,cancel)
