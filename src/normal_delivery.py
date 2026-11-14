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
    if 'execution_warehouses' in full:slim['execution_warehouses']=copy.deepcopy(full['execution_warehouses'])
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
    plans=[];head=job_head(repository.work,frozen['day']);execution={}
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
        chosen=warehouses if warehouses is not None else frozen.get('execution_warehouses')
        selected=[v['warehouse'] for v in available] if chosen is None or store not in chosen else chosen.get(store,[])
        if not selected or len(selected)!=len(set(selected)) or any(w not in [v['warehouse'] for v in available] for w in selected):raise ValueError('请选择有效且不重复的供货仓：'+store)
        execution[store]=list(selected);parent_same=same
        if head_order:
            import warehouse_execution
            with closing(repository.work.connect()) as db:
                if any((warehouse_execution.state(db,head_order['oid'],w) or {}).get('state')=='deleted' for w in selected):raise wr.Conflict('所选仓单已永久删除，请新建本仓任务；不能由旧草稿或默认勾选恢复')
        if same:
            import warehouse_execution
            with closing(repository.work.connect()) as db:same=not warehouse_execution.needs_publish(db,head_order['oid'],head_order['digest'],selected)
        descriptor={'store':store,'day':frozen['day'],'payload':{'result':result}}
        files=[{**v,'filename':normal_exports.filename(descriptor,v['warehouse'])} for v in available if v['warehouse'] in selected]
        plans.append({'store':store,'old':old,'same':same,'parent_same':parent_same,'filename':files[0]['filename'],'files':files})
    frozen['execution_warehouses']={**frozen.get('execution_warehouses',{}),**execution}
    if any(not p['same'] for p in plans):wr.summary(frozen)
    return {'payload':frozen,'plans':plans,'head':head,'warehouses':execution}


def warehouse_identity(order,warehouse):
    """Receipts keep the exact warehouse source, never a store-wide replacement."""
    from warehouse_orders import child_number
    normalized={**order,'number':order.get('parent_number') or order['number']}
    result=receipts.identity(normalized,warehouse)
    result['number']=child_number(normalized['number'],warehouse)
    for key in ('child_revision','source_parent_revision','snapshot_digest','execution_epoch','visibility_token'):
        if key in order:result[key]=order[key]
    return result


def selected_heads(repository,orders,warehouses):
    """Index exact child references even when two warehouses use different sources."""
    result={}
    for parent in orders:
        for head in parent.get('warehouse_heads',[]):result[(head['store'],head['warehouse'])]=head
        if parent.get('warehouse'):result[(parent['store'],parent['warehouse'])]=parent
    missing={(store,warehouse) for store,values in warehouses.items() for warehouse in values}-result.keys()
    if missing:
        import warehouse_execution
        with closing(repository.work.connect()) as db:
            for head in warehouse_execution.heads(db,parent_headers=orders):
                key=(head['store'],head['warehouse'])
                if key in missing:result[key]=head
    if any((store,warehouse) not in result for store,values in warehouses.items() for warehouse in values):
        raise wr.Conflict('所选仓单尚未签发或已删除，请重新核对本轮执行范围')
    return result


def execute_batch(repository,batch,destination,progress=lambda s:None,cancel=None):
    """One archive transaction, sequential files, explicit partial-failure receipt."""
    import tempfile,os
    import export_progress as ep
    from work_failures import FailureContext,WorkFailure
    failure_context=FailureContext('delivery_execute');notify=progress
    def progress(value):
        failure_context.progress(value);notify(value)
    plans=batch['plans'];payload=batch['payload'];destination=Path(destination)
    files=[(p,f) for p in plans for f in p['files']]
    paths=[destination/f['filename'] for p,f in files] if destination.is_dir() else [destination]
    if len(paths)!=len(files):raise ValueError('多仓或多店导出请选择文件夹')
    if len({os.path.normcase(str(p.resolve())) for p in paths})!=len(paths):raise ValueError('门店文件名重复，请分次导出并指定不同文件名')
    fd,probe=tempfile.mkstemp(prefix='.delivery-check-',dir=paths[0].parent);os.close(fd);os.unlink(probe);checkpoint(cancel)
    signed=any(not p['same'] for p in plans);orders=[]
    warehouses=batch.get('warehouses') or {p['store']:[f['warehouse'] for f in p['files']] for p in plans}
    receipt=receipts.begin(repository.work.path,'merchant',[
        {'path':str(path.resolve()),'state':'未执行','stage':'准备','head':None,
         'day':payload['day'],'store':p['store'],'warehouse':f['warehouse']}
        for (p,f),path in zip(files,paths)],signature='尚未新增签发' if signed else '归档版本未改变',signed=signed)
    try:
        if signed:
            receipt['state']='正在归档';receipt['signature']='签发结果待核';receipts.update(repository.work.path,receipt)
            progress('后台归档所选门店与完整来源（一次事务）…')
            def remember(heads):
                failure_context.heads=[dict(h) for h in heads]
                by_child=selected_heads(repository,heads,warehouses)
                for item in receipt['files']:item['head']=warehouse_identity(by_child[item['store'],item['warehouse']],item['warehouse'])
                receipt['state']='修订身份已准备，等待提交'
                if not receipts.update(repository.work.path,receipt):
                    raise OSError('提交前未能保存确切修订身份，未开始签发；'+receipt['warnings'][-1])
            orders=repository.confirm(payload,[p['store'] for p in plans],batch['head']['revision'] if batch['head'] else None,{p['store']:p['old']['revision'] if p['old'] else None for p in plans},progress=progress,cancel=cancel,headers_only=True,prepared_heads=remember,warehouses=warehouses)
            receipt['signature']='所选正常单已归档，修订保留'
        else:
            heads={h['store']:h for h in repository.headers() if h['day']==payload['day']}
            for p in plans:
                head=heads.get(p['store']);order=p['old']
                if not head or any(head[k]!=order[k] for k in ('oid','revision','digest','saved')):raise wr.Conflict('正常单已更新，请重新核对：'+p['store'])
                orders.append(order)
    except Exception as exc:
        evidence=failure_context.capture(exc,repository.work.path)
        receipt['state']='归档需核对';receipt['failure']=evidence;receipt['warnings'].append(evidence['reason'])
        if evidence['commit_state']=='committed':
            receipt['signature']='所选正常单已提交，后续校验未完成；原修订保留'
        elif evidence['commit_state']=='not_started':
            receipt['signature']='本次签发未进入最终提交'
        receipts.update(repository.work.path,receipt)
        raise WorkFailure(evidence,receipts.summary(receipt)) from exc
    by_child=selected_heads(repository,orders,warehouses)
    records=[]
    for item in receipt['files']:
        order=by_child[item['store'],item['warehouse']];item['head']=warehouse_identity(order,item['warehouse']);records.append((order,item))
    receipt['state']='归档已核实，等待导出';receipts.update(repository.work.path,receipt)
    completed=_write_exports(repository,records,receipt,progress,cancel)
    return {'paths':completed,'signed':signed,'saved':job_head(repository.work,payload['day'])['saved'] if signed else None,
            'orders':[{**{k:v for k,v in o.items() if k!='payload'},
                       'warehouse_heads':[by_child[o['store'],w] for w in warehouses[o['store']]]} for o in orders],
            'export_task':receipt['id'],'warnings':receipt['warnings']}


def _write_exports(repository,records,receipt,progress,cancel):
    import export_progress as ep
    from work_failures import FailureContext,WorkFailure
    failure_context=FailureContext('export_signed');failure_context.commit_state='committed';notify=progress
    def progress(value):
        failure_context.progress(value);notify(value)
    from archive_orders import require_active
    from warehouse_orders import child_number
    done=[];current=None;loaded=None
    try:
        for i,(descriptor,item) in enumerate(records,1):
            current=item;checkpoint(cancel);warehouse=item['warehouse'];path=Path(item['path'])
            if 'payload' in descriptor:order=descriptor
            else:
                if loaded is None or any(loaded.get(k)!=descriptor.get(k) for k in ('oid','revision','digest','warehouse')):
                    # Release the preceding decoded store before reading another.
                    loaded=None;order=None
                    progress('读取并校验原签发修订：'+descriptor['store'])
                    import warehouse_execution
                    loaded=warehouse_execution.get_order(repository,descriptor,progress=progress)
                    if not loaded or any(loaded[k]!=descriptor[k] for k in ('oid','revision','digest')):
                        raise ValueError('原签发修订身份变化，未导出替代版本：'+descriptor['store'])
                order=loaded
            with closing(repository.work.connect()) as db:require_active(db,order['oid'],warehouse)
            item.update(stage='写出文件',state='正在写出');receipts.update(repository.work.path,receipt)
            with stage(f'写出确认单 {i}/{len(records)} · {warehouse}',progress):
                normal_exports.export(order,path,cancel=cancel,progress=ep.prefixed(progress,f'文件 {i}/{len(records)} · {order["payload"]["result"]["store_name"]} · {warehouse}'),warehouse=warehouse)
            item.update(state='已生成',stage='核验文件摘要');done.append(str(path));receipts.update(repository.work.path,receipt)
            item['sha256']=sha256(path);item['stage']='记录导出日志';receipts.update(repository.work.path,receipt)
            repository.log(order,'带号导出',{'path':str(path),'sha256':item['sha256'],'warehouse':warehouse,'child_number':child_number(order['number'],warehouse),'export_task':receipt['id']})
            item.update(state='已完成',stage='完成');receipts.update(repository.work.path,receipt)
    except Exception as exc:
        evidence=failure_context.capture(exc,repository.work.path);receipt['failure']=evidence
        if current is not None:
            current['error']=('文件已生成，日志写入失败：' if current['stage']=='记录导出日志' else '')+explain_error(exc)
            if current['state'] not in ('已生成','已完成'):current['state']='未完成'
        receipt['state']='导出未全部完成';receipts.update(repository.work.path,receipt)
        raise WorkFailure(evidence,receipts.summary(receipt)) from exc
    receipt['state']='已完成' if not receipt['warnings'] else '文件已完成，回执需核对';receipts.update(repository.work.path,receipt)
    return done


def execute(repository,plan,path,progress=lambda s:None,cancel=None):
    """Single-store compatibility delegates to the same transaction/export path."""
    batch={'plans':[plan],'payload':plan['payload'],'head':plan['head']}
    receipt=execute_batch(repository,batch,path,progress=progress,cancel=cancel)
    order=receipt['orders'][0]
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
    records=[]
    import warehouse_execution
    with closing(repository.work.connect()) as db:
        children=warehouse_execution.heads(db,parent_headers=[order],history=True)
        managed=warehouse_execution.has_parent(db,order['oid'])
    for warehouse in selected:
        matches=[h for h in children if h['warehouse']==warehouse and h['revision']==order['revision'] and h['digest']==order['digest']]
        if managed and len(matches)!=1:raise wr.Conflict('该仓未签发此来源版本或已删除，请选择实际仓单原版本导出：'+warehouse)
        records.append(matches[0] if managed else order)
    receipt=receipts.begin(repository.work.path,'merchant',[
        {'path':str(path.resolve()),'state':'未执行','stage':'准备','head':warehouse_identity(descriptor,wh),
         'day':order['day'],'store':order['store'],'warehouse':wh} for descriptor,wh,path in zip(records,selected,paths)],
        signature=f'原修订 V{order["revision"]:03d} 保留',signed=False)
    return _write_exports(repository,list(zip(records,receipt['files'])),receipt,progress,cancel)
