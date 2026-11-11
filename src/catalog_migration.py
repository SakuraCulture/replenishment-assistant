"""Portable catalog task facts; browser bindings always belong to the target PC."""
import copy,json,sqlite3,uuid
from contextlib import closing
from pathlib import Path
from stockout_common import atomic_json,now

ROOT='jst_catalog_sync'
ACCOUNT_FIELDS=('id','warehouse','expectedCompany','expectedCoid','expectedUid','mappingVerified','bindingVersion')


def validate(data,guard):
    if (data.get('schemaVersion')!=1 or not data.get('storeId') or data.get('storeId')!=guard.get('storeId')
            or not isinstance(data.get('batches'),dict) or not isinstance(data.get('assets'),dict)):
        raise ValueError('聚水潭任务状态或身份标记不完整，未迁移')


def freeze(root,stack,snapshot=None,*,folder_name=ROOT):
    folder=Path(root)/folder_name;file=folder/'jobs.json';guard=folder/'store.guard.json'
    if not file.exists() and not guard.exists():
        if snapshot is not None:raise ValueError('聚水潭原任务目录已变化，未迁移')
        return None
    if not file.exists() or not guard.exists():raise ValueError('聚水潭任务状态或身份标记缺失，未迁移')
    if snapshot is None:
        lock=folder/'writer.lock.sqlite'
        if lock.exists():
            db=stack.enter_context(closing(sqlite3.connect(lock.resolve().as_uri()+'?mode=rw',uri=True,timeout=.3,isolation_level=None)))
            try:db.execute('BEGIN EXCLUSIVE')
            except sqlite3.OperationalError as exc:
                raise ValueError('聚水潭原采集服务仍占用任务锁；请从应用迁移入口导出，或等原服务安全退出') from exc
        snapshot=json.loads(file.read_text('utf-8-sig'))
    validate(snapshot,json.loads(guard.read_text('utf-8-sig')))
    return portable(snapshot)


def portable(data):
    """No account/probe result may carry a source machine browser binding."""
    out={k:copy.deepcopy(data[k]) for k in ('schemaVersion','storeId','batches','assets')}
    out.update(accounts={},operations={},bindingEpochs=copy.deepcopy(data.get('bindingEpochs',{})),rebindRequired={})
    warehouses=set(data.get('accounts',{}))|set(data.get('rebindRequired',{}))
    for batch in out['batches'].values():
        previous=batch.get('migration',{}).get('previous_state',batch.get('state'))
        batch['migration']={'previous_state':previous,'paused_at':now()}
        if batch.get('state') in ('QUEUED','RUNNING') or any(not i.get('adoption') and not i.get('retirement') for i in batch['items']):batch['state']='PAUSED'
        for item in batch['items']:
            account=item['account'];wh=account['warehouse'];warehouses.add(wh)
            out['bindingEpochs'][wh]=max(out['bindingEpochs'].get(wh,0),account.get('bindingVersion',0))
            item['account']={k:copy.deepcopy(account[k]) for k in ACCOUNT_FIELDS if k in account}
            item['connection_state']='DISCONNECTED'
            item['migration']={**item.get('migration',{}),'needs_binding':not item.get('adoption') and not item.get('retirement')}
    out['rebindRequired']={w:True for w in warehouses}
    return out


def local_connections(root):
    folder=Path(root)/ROOT;file=folder/'jobs.json';guard=folder/'store.guard.json'
    if not file.exists() and not guard.exists():return {}
    if not file.exists() or not guard.exists():raise ValueError('目标聚水潭状态缺失，原资料未替换')
    # The app has closed its retained component. Do not replace another live
    # component's records, even if the main order database itself is unlocked.
    lock=folder/'writer.lock.sqlite'
    if lock.exists():
        with closing(sqlite3.connect(lock.resolve().as_uri()+'?mode=rw',uri=True,timeout=.3,isolation_level=None)) as db:
            try:db.execute('BEGIN EXCLUSIVE')
            except sqlite3.OperationalError as exc:raise ValueError('目标聚水潭采集服务仍占用资料，原资料未替换') from exc
    data=json.loads(file.read_text('utf-8-sig'));validate(data,json.loads(guard.read_text('utf-8-sig')))
    return {'accounts':data.get('accounts',{}),'bindingEpochs':data.get('bindingEpochs',{})}


def restore(new,root,manifest,local):
    folder=Path(new)/ROOT;file=folder/'jobs.json';guard=folder/'store.guard.json'
    if file.exists():
        data=json.loads(file.read_text('utf-8'));validate(data,json.loads(guard.read_text('utf-8')));data=portable(data)
    else:
        # Older packages omitted this module. Target tasks cannot be mixed into
        # an imported order database; they remain in the complete target backup.
        data={'schemaVersion':1,'storeId':str(uuid.uuid4()),'batches':{},'assets':{},'accounts':{},'operations':{},'bindingEpochs':{},'rebindRequired':{}}
    def relocate(value):
        if isinstance(value,dict):return {k:relocate(v) for k,v in value.items()}
        if isinstance(value,list):return [relocate(v) for v in value]
        if isinstance(value,str) and value in manifest.get('external_map',{}):return str(Path(root)/manifest['external_map'][value])
        return value
    data=relocate(data)
    for wh,account in local.get('accounts',{}).items():
        epoch=max(data['bindingEpochs'].get(wh,0),local.get('bindingEpochs',{}).get(wh,0),account.get('bindingVersion',0))+1
        data['accounts'][wh]={**account,'enabled':False,'bindingVersion':epoch}
        data['bindingEpochs'][wh]=epoch;data['rebindRequired'][wh]=True
    for wh in data['rebindRequired']:
        data['bindingEpochs'][wh]=data['bindingEpochs'].get(wh,0)+1
        if wh in data['accounts']:data['accounts'][wh]['bindingVersion']=data['bindingEpochs'][wh]
    atomic_json(file,data);atomic_json(guard,{'schemaVersion':1,'storeId':data['storeId']})
    atomic_json(folder/'迁入核对说明.json',{'at':now(),'batches':len(data['batches']),
        'notice':'迁入采集及自动同步已暂停；本机浏览器配置保留。先核验相同仓账号并保存，再逐项继续原任务；已受理任务不重发。',
        'source_tasks_present':any(e['path'].startswith(ROOT+'/') for e in manifest['entries'])})
