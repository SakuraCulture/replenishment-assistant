"""Durable return-triggered generation. Payment facts never reserve stock.

Prepared operations are persisted before execution and reused after interruption.
SQLite CAS plus the generation service protect competing consumers.
"""
import sqlite3
from contextlib import closing
import order_lifecycle as lc
import jst_orders as j
import work_repository as wr
from runtime_support import checkpoint, Cancelled
from export_progress import RowProgress


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS jst_tasks(task TEXT PRIMARY KEY,created TEXT,head TEXT,token TEXT,status TEXT,plan TEXT,message TEXT)')


def enqueue(db, head, token):
    schema(db)
    h={k:v for k,v in head.items() if k!='products'}
    identity=j.digest([lc.key(h),token])
    row=db.execute("SELECT saved FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='return' AND version=?",(*lc.key(h),token['version'])).fetchone()
    db.execute('INSERT OR IGNORE INTO jst_tasks VALUES(?,?,?,?,?,?,?)',
               (identity,row[0] if row else wr.now(),wr.dumps(h),wr.dumps(token),'pending',None,'等待生成'))


def pending(path):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_tasks'").fetchone():return []
        return db.execute("SELECT task,head,token,plan FROM jst_tasks WHERE status IN ('pending','prepared') ORDER BY created,json_extract(head,'$.day'),json_extract(head,'$.number'),rowid LIMIT 100").fetchall()


def mark(path,task,status,message):
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,None)
        db.execute('UPDATE jst_tasks SET status=?,message=? WHERE task=?',(status,message,task))


def retry(path, heads, progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    import storage_chunks
    from pathlib import Path
    storage_chunks.backup_before_write(Path(path),progress)
    with closing(sqlite3.connect(path,timeout=.1)) as db,db:
        lc.begin(db,cancel);schema(db)
        for head in heads:
            h=lc.full_head(path,head);doc=lc.current(db,h,'return')
            if not doc:continue
            token=lc.rules.basis_token(doc);enqueue(db,h,token)
            db.execute("UPDATE jst_tasks SET status='pending',plan=NULL,message='等待重试' WHERE task=? AND status='waiting'",(j.digest([lc.key(h),token]),))
        checkpoint(cancel)


def drain(path, folder, progress=lambda _:None,cancel=None):
    tasks=pending(path);result=[];catalogs=None
    for i,(task,raw,raw_token,stored) in enumerate(tasks):
        checkpoint(cancel);h=wr.loads(raw)
        progress(RowProgress('回传已保存；生成 '+h['number'],i,len(tasks)))
        try:
            with closing(sqlite3.connect(path)) as db:
                doc=lc.current(db,h,'return')
                if lc.rules.basis_token(doc)!=wr.loads(raw_token):
                    mark(path,task,'done','回传已更新，此任务不再执行');continue
                if stored is None and (j.active(db,h) or j.locked(db,h) or lc.current(db,h,'payment') or lc.current(db,h,'receipt')):
                    mark(path,task,'done','已有生成／付款／收货，未自动重新分配');continue
                ready=j.state(db,h['warehouse']) is not None
            if stored is None:
                if not ready:
                    if catalogs is None:catalogs=j.catalogs(path,folder,cancel=cancel)
                    c=catalogs.get(h['warehouse'])
                    if not c:raise ValueError(h['warehouse']+'缺少商品资料，请在聚水潭导入单面板更新后重试')
                    j.commit_refresh(path,j.prepare_refresh(path,h['warehouse'],catalog=c['catalog'],cancel=cancel),progress,cancel)
                plan=j.prepare_checked(path,[h],progress=progress,cancel=cancel)
                if any(p['summary']['issues'] for p in plan['payload']['plans']):
                    reasons=list(dict.fromkeys(r['reason'] for p in plan['payload']['plans'] for r in p['rows'] if r['issue']))
                    raise ValueError('待核商品：'+'；'.join(reasons)+'；请在生成面板核对')
                with closing(sqlite3.connect(path,timeout=.1)) as db,db:
                    lc.begin(db,cancel)
                    changed=db.execute("UPDATE jst_tasks SET status='prepared',plan=? WHERE task=? AND status='pending'",(wr.dumps(plan),task)).rowcount
                    if not changed:continue
            else:plan=wr.loads(stored)
            receipt=j.commit(path,plan,progress=progress,cancel=cancel)
            if receipt['cancelled']:raise Cancelled('生成停止；已保存结果保留')
            if receipt['errors']:raise ValueError('；'.join(e['error'] for e in receipt['errors']))
            message='已生成，可导出' if receipt['generated'] else '无可分配库存' if receipt['zero'] else '无需生成'
            mark(path,task,'done',message);result.append({'number':h['number'],**receipt})
        except Cancelled:raise
        except Exception as exc:
            mark(path,task,'waiting',str(exc));result.append({'number':h['number'],'error':str(exc)})
        progress(RowProgress('回传后生成已处理 '+h['number'],i+1,len(tasks)))
    return result
