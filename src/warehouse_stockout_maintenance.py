"""Analysis-only deletion, durable original cleanup, and log retirement."""
from pathlib import Path
from stockout_common import digest
from runtime_support import checkpoint

def preview(repo,start,end):
    from warehouse_stockout import validate
    validate({'start':start,'end':end})
    with repo.connect() as db:
        rows=[dict(r) for r in db.execute('SELECT * FROM snapshots WHERE day BETWEEN ? AND ? ORDER BY id',(start,end))]
    return {'start':start,'end':end,'count':len(rows),'rows':sum(r['rows'] for r in rows),'token':digest(rows)}

def remove(repo,plan,cancel=None):
    with repo.connect(True) as db:
        db.execute('BEGIN IMMEDIATE')
        rows=[dict(r) for r in db.execute('SELECT * FROM snapshots WHERE day BETWEEN ? AND ? ORDER BY id',(plan['start'],plan['end']))]
        if digest(rows)!=plan['token']:raise ValueError('分析范围已变化，请重新预览删除')
        for row in rows:
            checkpoint(cancel);sid=row['id']
            import json
            for run in db.execute('SELECT DISTINCT u.id,u.external FROM runs u JOIN run_snapshots r ON r.run=u.id WHERE r.snapshot=?',(sid,)).fetchall():
                ext=json.loads(run['external']);ext.setdefault('deleted_warehouses',[])
                if row['warehouse'] not in ext['deleted_warehouses']:ext['deleted_warehouses'].append(row['warehouse'])
                db.execute('UPDATE runs SET external=? WHERE id=?',(json.dumps(ext,ensure_ascii=False),run['id']))
            db.execute('DELETE FROM current WHERE snapshot=?',(sid,));db.execute('DELETE FROM run_snapshots WHERE snapshot=?',(sid,));db.execute('DELETE FROM snapshots WHERE id=?',(sid,))
        db.execute('INSERT OR IGNORE INTO cleanup SELECT sha,path,\'\' FROM originals WHERE sha NOT IN (SELECT source_sha FROM snapshots)')
        checkpoint(cancel)
    return cleanup(repo,cancel)

def cleanup(repo,cancel=None):
    with repo.connect() as db:rows=db.execute('SELECT sha,path FROM cleanup').fetchall()
    remaining=0
    for sha,path in rows:
        checkpoint(cancel);target=Path(path).resolve();error=''
        try:
            if target.parent!=(repo.folder/'originals').resolve() or target.name!=sha+'.xlsx':raise ValueError('清理路径不属于本分析原件，未删除')
            with repo.connect(True) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM snapshots WHERE source_sha=?',(sha,)).fetchone():
                    db.execute('DELETE FROM cleanup WHERE sha=?',(sha,));continue
                target.unlink(missing_ok=True);db.execute('DELETE FROM originals WHERE sha=?',(sha,));db.execute('DELETE FROM cleanup WHERE sha=?',(sha,))
        except OSError as exc:error=str(exc)
        if error:
            remaining+=1
            with repo.connect(True) as db:db.execute('UPDATE cleanup SET error=? WHERE sha=?',(error,sha))
    return {'deleted':len(rows)-remaining,'pending':remaining}

def delete_log(repo,rid,updated):
    with repo.connect(True) as db:
        db.execute('BEGIN IMMEDIATE');run=db.execute('SELECT * FROM runs WHERE id=?',(rid,)).fetchone()
        if run is None or run['updated']!=updated:raise ValueError('作业已变化，请刷新后重试')
        if run['state'] not in ('COMPLETE','ENDED'):raise ValueError('请先继续或结束原任务；不能删除未决作业日志')
        db.execute('UPDATE runs SET log_deleted=1,message=\'\' WHERE id=?',(rid,))
    return True
