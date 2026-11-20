"""Frozen local save checkpoints; recovery always rechecks durable business facts.

Native payment recovery may re-read the original order before local commit.
The small index is separate from the compressed preview; no whole DB backup.
"""
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import traceback
from contextlib import closing
from pathlib import Path
import work_repository as wr
from runtime_support import checkpoint


def directory(path):return Path(path).resolve().parent/'lifecycle_recovery'


def _id(value):
    if not isinstance(value,str) or not re.fullmatch('[a-f0-9]{32}',value):raise ValueError('恢复任务编号无效')
    return value


def _read(path,identity):
    value=wr.loads((directory(path)/(_id(identity)+'.json')).read_text('utf-8'))
    if value.get('schema')!=1 or value.get('id')!=identity:raise ValueError('恢复记录不兼容或身份不一致')
    return value


def _atomic(target,value):
    fd,name=tempfile.mkstemp(prefix='.checkpoint-',dir=target.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False,default=wr.encode_default)
            f.flush();os.fsync(f.fileno())
        os.replace(name,target)
    finally:Path(name).unlink(missing_ok=True)


def _alive(db,entries):
    from deletion_barrier import active
    return all(active(db,p['head']['oid'],p['head']['warehouse']) for p in entries)


def _sections(value,depth=0):
    """C-encode large values by order/field instead of one Python step per token."""
    if type(value) is dict and depth<4:
        yield '{'
        for n,(key,item) in enumerate(value.items()):
            yield (', ' if n else '')+wr.dumps(key)+': '
            yield from _sections(item,depth+1)
        yield '}'
    elif type(value) is list and depth<3:
        yield '['
        for n,item in enumerate(value):
            if n:yield ', '
            yield from _sections(item,depth+1)
        yield ']'
    else:yield wr.dumps(value)


def _publish(path,value):
    # Serializes publication with deletion, without holding a business writer
    # while encoding the large preview. Late callbacks cannot recreate it.
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=.15)) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
            if not _alive(db,value['entries']):
                (directory(path)/(value['id']+'.json')).unlink(missing_ok=True)
                (directory(path)/(value['id']+'.json.gz')).unlink(missing_ok=True)
                return False
            _atomic(directory(path)/(value['id']+'.json'),value)
            if value.get('state')=='completed':
                (directory(path)/(value['id']+'.json.gz')).unlink(missing_ok=True)
        finally:
            # Only a deletion/publication fence: no SQL data was written.
            # COMMIT would unnecessarily need an exclusive lock from readers.
            if db.in_transaction:db.rollback()
    return True


def begin(path,plan,cancel=None):
    from order_lifecycle import staged_document
    entries=[]
    for p in plan['plans']:
        checkpoint(cancel)
        target=staged_document(p)
        entries.append({'head':{k:v for k,v in p['head'].items() if k!='products'},'stage':p['stage'],
                        'expected':p['expected'],'version':target['version'],'digest':p['digest'],'changed':p['changed']})
        if p.get('source_evidence'):entries[-1]['source_evidence']=p['source_evidence']
    identity=hashlib.sha256(wr.dumps(entries).encode()).hexdigest()[:32]
    root=directory(path);root.mkdir(exist_ok=True)
    target=root/(identity+'.json.gz')
    # Only persist values needed by the unchanged commit validators. Large
    # display differences, duplicated signed products and process proofs are
    # rebuilt/verified when recovering; they are never treated as authority.
    frozen={k:v for k,v in plan.items() if k!='plans'}
    frozen['plans']=[{k:({a:b for a,b in v.items() if a!='products'} if k=='head' else v)
                      for k,v in p.items() if k not in ('_read_proofs','differences','calibration')}
                     for p in plan['plans']]
    changed=any(p['changed'] or p.get('source_evidence') for p in entries);name=None
    try:
        h=hashlib.sha256()
        if changed:
            fd,name=tempfile.mkstemp(prefix='.preview-',dir=root);os.close(fd)
            with open(name,'wb') as raw:
                with gzip.GzipFile(fileobj=raw,mode='wb',compresslevel=1,mtime=0) as compressed:
                    for fragment in _sections(frozen):
                        checkpoint(cancel);data=fragment.encode('utf-8');h.update(data);compressed.write(data)
                raw.flush();os.fsync(raw.fileno())
        checkpoint(cancel)
        value={'schema':1,'id':identity,'created':wr.now(),'updated':wr.now(),'state':'prepared',
               'entries':entries,'preview_sha256':h.hexdigest() if changed else None,'database':str(Path(path).resolve())}
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=.15)) as db:
            try:
                import order_lifecycle as lc
                lc.begin(db,cancel)
                if not _alive(db,entries):raise wr.Conflict('原仓单已删除，未建立恢复任务；请刷新范围')
                if changed:os.replace(name,target)
                _atomic(root/(identity+'.json'),value)
            finally:
                # Persisted files precede release of the deletion fence. The
                # business save remains a separate, unchanged transaction.
                if db.in_transaction:db.rollback()
        return value
    finally:
        if name:Path(name).unlink(missing_ok=True)


def execute(path,plan,progress,cancel,commit):
    value=begin(path,plan,cancel)
    try:
        result=commit(path,plan,progress,cancel)
    except BaseException as exc:
        value.update(state='interrupted',updated=wr.now(),error={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
        try:_publish(path,value)
        except Exception:logging.exception('保存原始异常的恢复记录未完成：%s',value['id'])
        raise
    value.update(state='completed',updated=wr.now(),receipt=result)
    try:_publish(path,value)
    except Exception:
        # COMMIT already returned: a failed local receipt write is not a failed
        # business save. The prepared checkpoint remains inspectable on reopen.
        logging.exception('单据已保存，恢复回执写入待核：%s',value['id'])
    return result


def recent(path,heads=None,stage=None,limit=100,progress=lambda _:None,cancel=None):
    root=directory(path)
    if not root.exists():return []
    selected={(h['oid'],h['warehouse']) for h in heads} if heads is not None else None
    out=[]
    for file in sorted(root.glob('*.json'),key=lambda p:p.stat().st_mtime_ns,reverse=True):
        checkpoint(cancel)
        if not re.fullmatch('[a-f0-9]{32}',file.stem):continue
        try:value=_read(path,file.stem)
        except FileNotFoundError:continue
        except (ValueError,KeyError) as exc:
            out.append({'schema':1,'id':file.stem,'created':'原记录待核','state':'unreadable','entries':[],
                        'error':{'message':type(exc).__name__+'：'+str(exc)}})
            if len(out)>=limit:break
            continue
        if stage and not any(p['stage']==stage for p in value['entries']):continue
        if selected is not None and not any((p['head']['oid'],p['head']['warehouse']) in selected for p in value['entries']):continue
        out.append(value)
        if len(out)>=limit:break
    return out


def prune_after_delete(path):
    try:prune_deleted(path)
    except Exception:logging.exception('业务删除已提交；恢复资料待清理，可再次执行原件清理')


def inspect(path,identity,progress=lambda _:None,cancel=None):
    import order_lifecycle as lc
    import purchases
    value=_read(path,identity);rows=[];saved=[];untouched=[]
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        for p in value['entries']:
            checkpoint(cancel);h=p['head'];reason='';state='needs_review'
            try:
                purchases.validate_head(db,h)
                doc=lc.current(db,h,p['stage'])
                version=doc['version'] if doc else None
                if doc and (version,doc['digest'])==(p['version'],p['digest']):
                    evidence=p.get('source_evidence')
                    retained=not evidence or bool(db.execute('SELECT 1 FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? AND version=? AND action=? AND detail=?',(*lc.key(h),p['stage'],version,'业务未变化，留存新原件',wr.dumps(evidence))).fetchone())
                    if evidence:retained=retained and all(db.execute('SELECT 1 FROM lifecycle_files WHERE sha=?',(f['sha'],)).fetchone() for f in evidence['files'])
                    if retained:state='saved';saved.append(p)
                    else:state='unchanged';untouched.append(p);reason='业务版本未变化，但本次新原件尚未留存；可继续原保存'
                elif version==p['expected']:
                    state='unchanged';untouched.append(p)
                else:reason='本环节已有其他版本；保留当前事实，请重新核对'
            except (ValueError,KeyError) as exc:reason=str(exc)
            rows.append({'head':h,'stage':p['stage'],'state':state,'reason':reason})
    if value.get('state')=='source_deleted':
        action='review';message='原恢复批次有仓单已删除；未选订单仍保留，请重新核对有效范围，不能回放旧批次'
    elif rows and all(r['state']=='saved' for r in rows):
        action='completed';message='已核实整批保存完成；直接使用原结果，不再保存或新增版本'
    elif rows and all(r['state']=='unchanged' or (r['state']=='saved' and not p['changed']) for r,p in zip(rows,value['entries'])):
        action='retry';message='本批修改尚未提交；可恢复原核对内容，完整复核后原子保存'
    else:
        action='review';message='原单、上游或本环节已有变化；保留现有事实，请重新核对，未自动保存任何部分'
    receipt=[{'number':p['head']['number'],'revision':p['head']['revision'],'stage':p['stage'],
              'version':p['version'],'changed':p['changed']} for p in value['entries']] if action=='completed' else None
    return {'checkpoint':value,'rows':rows,'action':action,'message':message,'receipt':receipt}


def recover(path,identity,progress=lambda _:None,cancel=None,*,order_session=None):
    import order_lifecycle as lc
    status=inspect(path,identity,progress,cancel)
    if status['action']=='completed':return status
    if status['action']!='retry':raise wr.Conflict(status['message'])
    value=status['checkpoint'];file=directory(path)/(_id(identity)+'.json.gz')
    raw=gzip.decompress(file.read_bytes())
    if hashlib.sha256(raw).hexdigest()!=value['preview_sha256']:raise ValueError('原恢复预览摘要不符，未执行保存')
    plan=wr.loads(raw)
    del raw
    if plan.get('jst_fulfillment'):
        from jst_fulfillment import native_guards, verify_order_before_commit
        guards=native_guards(path,plan['jst_fulfillment'])
        if guards:
            if order_session is None:raise wr.Conflict('原订单须在提交前重新回查，请从付款核对或原保存任务继续')
            verify_order_before_commit(path,guards,order_session,progress,cancel)
    for p in plan['plans']:
        checkpoint(cancel);p['head']=lc.full_head(path,p['head'])
    result=lc.commit(path,plan,progress,cancel)
    return {'action':'completed','message':'原核对内容已完整复核并保存；未选单保留','receipt':result,'checkpoint':value}


def prune_deleted(path):
    root=directory(path)
    if not root.exists():return
    import order_lifecycle as lc
    from deletion_barrier import active
    for file in root.glob('*.json'):
        if not re.fullmatch('[a-f0-9]{32}',file.stem):continue
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=.15)) as db,db:
            lc.begin(db,None);value=_read(path,file.stem)
            kept=[p for p in value['entries'] if active(db,p['head']['oid'],p['head']['warehouse'])]
            if len(kept)==len(value['entries']):continue
            (root/(file.stem+'.json.gz')).unlink(missing_ok=True)
            if not kept:file.unlink(missing_ok=True)
            else:
                # A batch save is atomic. Do not replay a silently reduced set.
                value.update(entries=kept,state='source_deleted',receipt=None,error=None,updated=wr.now())
                _atomic(file,value)
