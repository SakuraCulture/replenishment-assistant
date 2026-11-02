"""Read-only log presentation. Original query rows and export contracts stay intact."""
import sqlite3
from contextlib import closing
from pathlib import Path
import storage_chunks
from runtime_support import checkpoint
from business_presenter import flat_rows,names


def attach_names(path,records,cancel=None):
    cache={}
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        for r in records:
            checkpoint(cancel)
            key=(r.get('oid'),r.get('revision'),r.get('day'),r.get('store'))
            if key not in cache:
                if r.get('oid') and r.get('revision'):
                    raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',key[:2]).fetchone()
                else:
                    raw=db.execute('SELECT r.payload FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current WHERE o.day=? AND o.store=?',key[2:]).fetchone()
                cache[key]=storage_chunks.load(raw[0],db,paths=[('result','store_name')]).get(('result','store_name')) if raw else None
            r['display_name']=cache[key] or r.get('store') or '历史名称未留存'
    return records


def events(path,oid,warehouse=None,progress=lambda _:None,cancel=None):
    import history_forms
    result=history_forms.events(path,oid,warehouse,progress,cancel)
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
