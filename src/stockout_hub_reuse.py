"""Explicit offline derivation of warehouse results using a hub's frozen originals."""
import uuid
from pathlib import Path
from stockout_common import file_hash
from runtime_support import checkpoint


def reuse(journal,rid,cancel,progress):
    from stockout_repository import Repository as Store
    from warehouse_stockout import Repository,load_master
    row=journal.run(rid)
    if row['state'] not in ('COMPLETE','PARTIAL','ENDED'):raise ValueError('请先暂停/完成原采集，再按其留存原件补算')
    if not row['catalogs'] or not row['store_run']:raise ValueError('该记录没有可复用的门店仓原件')
    store=Store(journal.root);repo=Repository(journal.root);repo.initialize()
    run=row.get('reuse_run')
    if not run:run=uuid.uuid4().hex;journal.change(rid,reuse_run=run)
    repo.new_run(sorted(row['catalogs']),source='hub-reuse:'+rid,day=row['day'],run_id=run)
    for wh,sha in row['catalogs'].items():
        checkpoint(cancel)
        if wh in repo.run(run)['external'].get('deleted_warehouses',[]):raise ValueError('补算结果已删除，旧操作不会恢复它')
        with store.connect() as db:original=db.execute('SELECT c.*,o.path FROM catalogs c JOIN originals o ON o.sha=c.sha WHERE c.sha=? AND c.warehouse=?',(sha,wh)).fetchone()
        if not original:raise ValueError('原仓资料已删除，不能恢复或借用其他日期资料')
        target=Path(original['path']).resolve()
        if not target.is_relative_to((store.folder/'originals').resolve()) or file_hash(target)!=sha:raise ValueError('原件位置或完整性核验失败')
        with repo.connect() as db:existing=db.execute('SELECT snapshot FROM run_snapshots WHERE run=? AND warehouse=?',(run,wh)).fetchone()
        if not existing:
            if repo.run(run)['state']=='COMPLETE':raise ValueError('已补算结果被删除，旧操作不会恢复它')
            load_master(repo,target,wh,run=run,expected=sha,expected_rows=original['rows'],observed=original['observed'],cancel=cancel,progress=progress)
        repo.publish(run,wh,once=True)
    repo.change(run,state='COMPLETE',message='由原仓资料离线补算；保持原观测日期')
    return {'run':run,'state':'COMPLETE'}
