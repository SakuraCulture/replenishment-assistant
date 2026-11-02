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
