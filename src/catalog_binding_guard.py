"""Cross-process gate: binding retirement cannot race a local catalog commit."""
import sqlite3,time
from pathlib import Path
from contextlib import contextmanager
from runtime_support import checkpoint

@contextmanager
def adoption_guard(state_root,batch,item,cancel=None,*,state_reader=None):
    root=Path(state_root);root.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(root/'binding-change.lock.sqlite',timeout=0,isolation_level=None)
    try:
        deadline=time.monotonic()+5
        while True:
            checkpoint(cancel)
            try:db.execute('BEGIN IMMEDIATE');break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() or time.monotonic()>=deadline:raise ValueError('仓绑定正在纠正，请稍后继续原任务') from exc
                time.sleep(.05)
        if item.get('retirement'):raise ValueError('原采集已在本地结束，旧资料不会采用')
        if state_reader:
            # Ask the original service while holding the retirement gate.
            # Reading its atomically replaced jobs.json from Python races
            # Windows rename; a fresh RPC snapshot also avoids stale caches.
            data=state_reader(batch['id'])
            current_batch=next((b for b in data['batches'] if b['id']==batch['id']),{})
            current=next((i for i in current_batch.get('items',[]) if i['id']==item['id']),None)
            if not current or current.get('retirement'):raise ValueError('原采集已在本地结束或不存在，旧资料不会采用')
            account=next((a for a in data['accounts'] if a['warehouse']==item['account']['warehouse']),None)
            keys=('id','warehouse','expectedCompany','expectedCoid','expectedUid','mappingVerified','bindingVersion','binding')
            if not account or any(account.get(k)!=item['account'].get(k) for k in keys):raise ValueError('原采集账号绑定已变化，旧资料不会采用')
        elif (root/'store.guard.json').exists() or (root/'jobs.json').exists():raise ValueError('原采集服务状态缺失，未采用旧资料')
        yield
    finally:
        try:db.execute('ROLLBACK')
        except sqlite3.Error:pass
        db.close()
