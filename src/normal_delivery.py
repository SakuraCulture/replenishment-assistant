"""Background preparation and one-confirmation signed export; no Qt access."""
import copy,hashlib
from pathlib import Path
from contextlib import closing
import work_repository as wr
import normal_orders,normal_exports,new_product_focus as focus
from runtime_support import checkpoint


def job_head(work,day):
    with closing(work.connect()) as db:
        row=db.execute('SELECT revision,digest,saved,summary FROM jobs WHERE day=?',(day,)).fetchone()
    return {'revision':row[0],'digest':row[1],'saved':row[2],'summary':wr.loads(row[3])} if row else None


def prepare(repository,payload,store,progress=lambda s:None,cancel=None):
    """Thin compatibility entry for a single store."""
    batch=prepare_batch(repository,payload,[store],progress=progress,cancel=cancel)
    return {**batch['plans'][0],'payload':batch['payload'],'head':batch['head']}


def prepare_batch(repository,payload,codes,progress=lambda s:None,cancel=None):
    """Freeze the complete draft once, then plan stores in user-visible order."""
    checkpoint(cancel)
    return prepare_owned_batch(repository,copy.deepcopy(payload),codes,progress=progress,cancel=cancel)


def prepare_owned_batch(repository,payload,codes,progress=lambda s:None,cancel=None):
    """Internal process entry: IPC has already transferred an exclusively owned tree."""
    progress('冻结本批次草稿并检查所选门店…');checkpoint(cancel)
    frozen=payload
    if not codes or len(codes)!=len(set(codes)):raise ValueError('请选择互不重复的有效门店')
    plans=[];head=job_head(repository.work,frozen['day'])
    for i,store in enumerate(codes,1):
        checkpoint(cancel);progress(f'核对门店 {i}/{len(codes)}：{store}')
        result=frozen['results'][store]
        if frozen.get('input_dirty') or frozen['states'][store]['state']!='已完成':raise ValueError('结果已失效：'+store)
        old=repository.get(frozen['day'],store);same=bool(old and old['digest']==normal_orders.fingerprint_owned(result,frozen.get('new_focus')))
        if not same:
            if frozen['day']!=wr.today():raise ValueError('历史只读，请从历史页导出已签发修订')
            focus.require_for(frozen.get('new_focus'),frozen['day'],[result])
        plans.append({'store':store,'old':old,'same':same,'filename':normal_exports.filename({'store':store,'day':frozen['day'],'revision':old['revision']+(not same) if old else 1,'payload':{'result':result}})})
    if any(not p['same'] for p in plans):wr.summary(frozen)
    return {'payload':frozen,'plans':plans,'head':head}


def execute_batch(repository,batch,destination,progress=lambda s:None,cancel=None):
    """One archive transaction, sequential files, explicit partial-failure receipt."""
    import tempfile,os
    import export_progress as ep
    plans=batch['plans'];payload=batch['payload'];destination=Path(destination)
    paths=[destination/p['filename'] for p in plans] if destination.is_dir() else [destination]
    if len(paths)!=len(plans):raise ValueError('多店导出请选择文件夹')
    if len({os.path.normcase(str(p.resolve())) for p in paths})!=len(paths):raise ValueError('门店文件名重复，请分次导出并指定不同文件名')
    for path in paths:
        if path.exists():raise FileExistsError('未归档、未覆盖；已有文件：'+str(path))
    fd,probe=tempfile.mkstemp(prefix='.delivery-check-',dir=paths[0].parent);os.close(fd);os.unlink(probe);checkpoint(cancel)
    signed=any(not p['same'] for p in plans);completed=[];orders=[]
    if signed:
        progress('后台归档所选门店与完整来源（一次事务）…')
        orders=repository.confirm(payload,[p['store'] for p in plans],batch['head']['revision'] if batch['head'] else None,{p['store']:p['old']['revision'] if p['old'] else None for p in plans})
    else:
        for p in plans:
            order=repository.get(payload['day'],p['store'])
            if not order or order['revision']!=p['old']['revision'] or order['digest']!=p['old']['digest']:raise wr.Conflict('正常单已更新，请重新核对：'+p['store'])
            orders.append(order)
    try:
        for i,(order,path) in enumerate(zip(orders,paths),1):
            checkpoint(cancel);normal_exports.export(order,path,cancel=cancel,progress=ep.prefixed(progress,f'门店 {i}/{len(orders)} · {order["payload"]["result"]["store_name"]}'))
            completed.append(str(path))
            repository.log(order,'带号导出',{'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    except Exception as exc:
        remaining=[str(p) for p in paths if str(p) not in completed]
        raise RuntimeError(('所选正常单已归档，修订保留。' if signed else '归档版本未改变。')+f'已生成 {len(completed)}/{len(paths)} 份：'+('、'.join(completed) or '无')+'；未生成：'+('、'.join(remaining) or '无（日志写入失败）')+'。'+str(exc)+'；可从历史页重导未完成门店。') from exc
    return {'paths':completed,'signed':signed,'saved':job_head(repository.work,payload['day'])['saved'] if signed else None}


def execute(repository,plan,path,progress=lambda s:None,cancel=None):
    """Single-store compatibility delegates to the same transaction/export path."""
    batch={'plans':[plan],'payload':plan['payload'],'head':plan['head']}
    receipt=execute_batch(repository,batch,path,progress=progress,cancel=cancel)
    order=repository.get(plan['payload']['day'],plan['store'])
    return {**receipt,'path':receipt['paths'][0],'number':order['number'],'revision':order['revision']}
