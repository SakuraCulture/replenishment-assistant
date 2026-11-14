"""Read-only log presentation. Original query rows and export contracts stay intact."""
import sqlite3
from contextlib import closing
from pathlib import Path
import storage_chunks
import warehouse_execution as execution
from runtime_support import checkpoint
from business_presenter import flat_rows,names


def attach_names(path,records,cancel=None):
    cache={}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        for r in records:
            checkpoint(cancel)
            key=(r.get('oid'),r.get('revision'),r.get('day'),r.get('store'),r.get('warehouse'))
            if key not in cache:
                oid=r.get('oid')
                if not oid:
                    parent=db.execute('SELECT oid,current FROM normal_orders WHERE day=? AND store=?',key[2:4]).fetchone()
                    oid=parent[0] if parent else None
                if oid and r.get('warehouse') and execution.has_parent(db,oid):
                    revision=r.get('revision') or (execution.state(db,oid,r['warehouse']) or {}).get('source_revision')
                    raw=db.execute('SELECT payload,snapshot_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(oid,r['warehouse'],revision)).fetchone()
                    cache[key]=storage_chunks.load(raw[0],db,paths=[('result','store_name')],binding=raw[1]).get(('result','store_name')) if raw else None
                    r['display_name']=cache[key] or r.get('store') or '历史名称未留存'
                    continue
                elif r.get('oid') and r.get('revision'):
                    raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',key[:2]).fetchone()
                else:
                    raw=db.execute('SELECT r.payload FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current WHERE o.day=? AND o.store=?',key[2:4]).fetchone()
                cache[key]=storage_chunks.load(raw[0],db,paths=[('result','store_name')]).get(('result','store_name')) if raw else None
            r['display_name']=cache[key] or r.get('store') or '历史名称未留存'
    return records


def events(path,oid,warehouse=None,progress=lambda _:None,cancel=None):
    import history_forms
    from history_read import repository
    from archive_orders import scopes,number
    repo=repository(path);heads={h['oid']:h for h in repo.headers()}
    with closing(repo.work.connect()) as db:
        managed={key for key in heads if (not oid or key==oid) and execution.has_parent(db,key)}
        published=execution.heads(db,[h for key,h in heads.items() if key in managed],history=True) if managed else []
    if not managed:result=history_forms.events(path,oid,warehouse,progress,cancel)
    else:
        progress('读取日志并关联补货单号…');expanded=[];scope_cache={}
        by_revision={}
        for child in published:by_revision.setdefault((child['oid'],child['revision']),[]).append(child['warehouse'])
        for record in repo.events(oid):
            checkpoint(cancel);h=heads.get(record['oid']);detail=record['detail']
            if not h:continue
            key=(record['oid'],record['revision'])
            whs=[detail['warehouse']] if detail.get('warehouse') else list(detail.get('warehouses',{}))
            if record['oid'] in managed:
                valid=by_revision.get(key,[])
                whs=[w for w in whs if w in valid] if whs else valid
            elif not whs:
                if key not in scope_cache:
                    order=repo.get(h['day'],h['store'],record['revision'],view='export');scope_cache[key]=scopes(order['payload']['result']['rows'])
                whs=scope_cache[key]
            for wh in whs:
                if warehouse and wh!=warehouse:continue
                expanded.append({**record,'warehouse':wh,'child_number':number(h['number'],wh)})
        result={'records':expanded,'rows':history_forms.event_rows(expanded,heads)}
    attach_names(path,result['records'],cancel)
    return result


def log_page(path,start,end,search,page,progress=lambda _:None,cancel=None):
    import daily_batch
    result=daily_batch.log_page(path,start,end,search,page,progress,cancel)
    attach_names(path,result['rows'],cancel)
    return result


TECHNICAL={'gid','bid','oid','sha256','source_sha256','digest','path','file','source_path','revision','old_revision','new_revision','previous_revision','old','source','token','generation','files','paths','sources','hashes'}


def detail_rows(detail,technical=False):
    """Unknown internal fields stay accessible, but do not crowd the business form."""
    if technical:return flat_rows(detail,technical=True)
    def select(value):
        if isinstance(value,dict):return {k:select(v) for k,v in value.items() if k not in TECHNICAL and k in names()}
        if isinstance(value,(list,tuple)):return [select(v) for v in value]
        return value
    selected=select(detail)
    return flat_rows(selected) if selected else [['操作记录','完整过程请展开技术详情','','该记录没有独立业务数量字段']]


def summary(detail):
    rows=detail_rows(detail)
    return '；'.join(str(r[0])+'：'+str(r[1]) for r in rows[:2])[:110]+('…' if len(rows)>2 else '')
