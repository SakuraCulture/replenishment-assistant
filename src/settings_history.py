"""Atomic defaults history. Whole-store compare-and-swap protects later edits."""
import copy
import uuid
from contextlib import closing
import work_repository as wr

DDL = 'CREATE TABLE IF NOT EXISTS settings_history(id TEXT PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL,before_data TEXT NOT NULL,after_data TEXT NOT NULL)'

def record(db, before, after, action):
    changed = [s for s in after if before.get(s) != after[s]]
    if not changed:return None
    id = uuid.uuid4().hex
    db.execute(DDL)
    db.execute('INSERT INTO settings_history VALUES(?,?,?,?,?)', (id, wr.now(), action,
        wr.dumps({s:before.get(s) for s in changed}), wr.dumps({s:after[s] for s in changed})))
    return id

def history(repo):
    with closing(repo.connect()) as db:
        db.execute(DDL)
        return [dict(id=i, at=t, action=a, before=wr.loads(b), after=wr.loads(c))
                for i,t,a,b,c in db.execute('SELECT * FROM settings_history ORDER BY rowid DESC')]

def page(repo,before=None,limit=50,progress=lambda _:None,cancel=None):
    """Bounded list metadata. Full immutable before/after values load on selection."""
    if type(limit) is not int or not 1<=limit<=100:raise ValueError('历史分页范围无效')
    if before is not None and (type(before) is not int or before<=0):raise ValueError('历史分页游标无效')
    from runtime_support import checkpoint
    checkpoint(cancel)
    with closing(repo.connect()) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='settings_history'").fetchone():return {'items':[],'next':None}
        sql='SELECT rowid,id,at,action,(SELECT count(*) FROM json_each(after_data)) FROM settings_history'
        rows=list(db.execute(sql+(' WHERE rowid<?' if before is not None else '')+' ORDER BY rowid DESC LIMIT ?',((*([before] if before is not None else []),limit+1))))
    checkpoint(cancel)
    return {'items':[dict(cursor=n,id=i,at=t,action=a,stores=s) for n,i,t,a,s in rows[:limit]],'next':rows[limit-1][0] if len(rows)>limit else None}

def entry(repo,identity,progress=lambda _:None,cancel=None):
    from runtime_support import checkpoint
    checkpoint(cancel)
    with closing(repo.connect()) as db:
        row=db.execute('SELECT id,at,action,before_data,after_data FROM settings_history WHERE id=?',(identity,)).fetchone()
    if row is None:raise wr.Conflict('所选设置变更记录已不存在，请刷新')
    checkpoint(cancel)
    i,t,a,b,c=row
    return dict(id=i,at=t,action=a,before=wr.loads(b),after=wr.loads(c))

def plan(repo, entry, *, restore=False):
    """Undo ignores stores subsequently changed; explicit restore previews current values."""
    current = repo.stores(); target = entry['after'] if restore else entry['before']
    values = {}; conflicts = []
    for code, value in target.items():
        if not restore and current.get(code) != entry['after'].get(code):
            conflicts.append(code)
        elif current.get(code) != value:
            values[code] = copy.deepcopy(value)
    return {'values':values, 'expected':{s:current.get(s) for s in values},
            'conflicts':conflicts, 'action':('恢复配置 ' if restore else '撤销变更 ')+entry['id']}

def apply(repo, preview):
    # Deletion here only removes the selected future default, never a business order.
    return repo.save_stores(preview['values'], expected=preview['expected'], action=preview['action'])
