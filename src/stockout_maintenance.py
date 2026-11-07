"""Explicit analysis-only cleanup. Preview/CAS, durable file cleanup, no business table access."""
from datetime import date
from pathlib import Path
import hashlib
import uuid
from stockout_common import encoded,now,file_hash
from runtime_support import checkpoint


def preview(repo,start,end):
    span=(date.fromisoformat(end)-date.fromisoformat(start)).days
    if span<0 or span>365:raise ValueError('每次清理请选择1至366天的明确范围')
    with repo.connect() as db:return _preview(db,start,end)


def _preview(db,start,end):
    active=db.execute("SELECT id FROM runs WHERE state NOT IN ('COMPLETE','PARTIAL','ENDED') LIMIT 1").fetchone()
    if active:raise ValueError('还有未完成的分析任务，请先继续或结束原任务后再清理分析历史')
    h=hashlib.sha256();count=rows=0
    for row in db.execute('SELECT id,source_sha,rows,state FROM store_snapshots WHERE day BETWEEN ? AND ? ORDER BY id',(start,end)):
        h.update(encoded(tuple(row)).encode());count+=1;rows+=row['rows']
    publications=0
    for row in db.execute('SELECT id,day,run FROM publications WHERE day BETWEEN ? AND ? ORDER BY id',(start,end)):
        h.update(encoded(tuple(row)).encode());publications+=1
    for row in db.execute('SELECT id,state,updated FROM runs ORDER BY id'):h.update(encoded(tuple(row)).encode())
    return {'start':start,'end':end,'snapshots':count,'rows':rows,'revisions':publications,'token':h.hexdigest(),
            'scope':'所选日期内全部门店、全部剔除视角的分析快照及不再引用的分析原件；原补货和平台任务不删除'}


def execute(repo,plan,cancel=None,progress=lambda _:None):
    start,end=plan['start'],plan['end'];checkpoint(cancel);cid=uuid.uuid4().hex
    with repo.connect(write=True) as db:
        db.execute('BEGIN IMMEDIATE')
        current=_preview(db,start,end)
        if current!=plan:raise ValueError('分析记录已变化，请重新预览清理范围')
        db.execute('INSERT INTO cleanups VALUES(?,?,?,?)',(cid,encoded(plan),now(),'COMMITTING'))
        db.execute('''INSERT INTO cleanup_files(cleanup,sha,path,state)
          SELECT ?,o.sha,o.path,'PENDING' FROM originals o WHERE
          EXISTS(SELECT 1 FROM store_snapshots s WHERE s.source_sha=o.sha AND s.day BETWEEN ? AND ?)
          AND NOT EXISTS(SELECT 1 FROM store_snapshots s WHERE s.source_sha=o.sha AND NOT(s.day BETWEEN ? AND ?))
          AND NOT EXISTS(SELECT 1 FROM catalogs c WHERE c.sha=o.sha)''',(cid,start,end,start,end))
        checkpoint(cancel)
        progress('正在提交所选分析历史清理；提交结束后才可退出')
        db.execute('DELETE FROM current_days WHERE day BETWEEN ? AND ?',(start,end))
        db.execute('DELETE FROM publications WHERE day BETWEEN ? AND ?',(start,end))
        selected='SELECT id FROM store_snapshots WHERE day BETWEEN ? AND ?'
        db.execute("UPDATE run_stores SET snapshot=NULL,state='CLEANED',error='该日期分析明细已按确认范围清理' WHERE snapshot IN ("+selected+')',(start,end))
        db.execute('DELETE FROM summaries WHERE snapshot IN ('+selected+')',(start,end))
        db.execute('DELETE FROM dimension_summaries WHERE snapshot IN ('+selected+')',(start,end))
        db.execute('DELETE FROM fact_records WHERE snapshot IN ('+selected+')',(start,end))
        db.execute('DELETE FROM store_snapshots WHERE day BETWEEN ? AND ?',(start,end))
        db.execute("UPDATE cleanups SET state='FILES_PENDING' WHERE id=?",(cid,))
    # The outbox survives crashes and cannot point outside analysis-owned originals.
    return finish_files(repo,cid,progress)


def finish_files(repo,cid,progress=lambda _:None):
    failed=[];deleted=0
    with repo.connect() as db:
        files=[dict(r) for r in db.execute("SELECT * FROM cleanup_files WHERE cleanup=? AND state!='DONE'",(cid,))]
    for item in files:
        target=Path(item['path']).resolve();error=''
        try:
            if not target.is_relative_to((repo.folder/'originals').resolve()) or target.stem!=item['sha']:
                raise ValueError('清理原件不在分析拥有目录，未删除')
            with repo.connect() as db:
                used=db.execute('SELECT 1 FROM store_snapshots WHERE source_sha=? UNION ALL SELECT 1 FROM catalogs WHERE sha=? LIMIT 1',(item['sha'],item['sha'])).fetchone()
            if used:raise ValueError('原件已被新快照引用，未删除')
            if target.exists():
                if file_hash(target)!=item['sha']:raise ValueError('原件已变化，保留待核')
                target.unlink()
            with repo.connect(write=True) as db:
                db.execute('DELETE FROM originals WHERE sha=?',(item['sha'],))
                db.execute("UPDATE cleanup_files SET state='DONE',error='' WHERE cleanup=? AND sha=?",(cid,item['sha']))
            deleted+=1
        except Exception as exc:
            error=str(exc);failed.append(error)
            with repo.connect(write=True) as db:db.execute("UPDATE cleanup_files SET state='PENDING',error=? WHERE cleanup=? AND sha=?",(error,cid,item['sha']))
    with repo.connect(write=True) as db:db.execute('UPDATE cleanups SET state=? WHERE id=?',('FILES_PENDING' if failed else 'COMPLETE',cid))
    return {'id':cid,'deleted_originals':deleted,'pending':len(failed),'message':
            '分析记录清理已提交；'+('部分原件待重试清理：'+'；'.join(failed[:3]) if failed else '仅分析自有原件已按范围清理；数据库空闲页留待后续复用，未执行全库压缩')}
