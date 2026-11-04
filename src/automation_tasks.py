"""Small task metadata journal in the existing application data environment."""
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from automation_protocol import CommandError, digest


def stamp(): return datetime.now(timezone.utc).isoformat()


class Journal:
    def __init__(self, path, session):
        self.path, self.session = path, session
        from storage_chunks import backup_before_write
        with closing(self.connect()) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_tasks'").fetchone()
        if not exists: backup_before_write(path, lambda _: None)
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS automation_tasks (id TEXT PRIMARY KEY, operation_key TEXT UNIQUE NOT NULL, signature TEXT NOT NULL, session TEXT NOT NULL, created TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS automation_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, at TEXT NOT NULL, payload TEXT NOT NULL)')
            # Caller owns the workspace automation lock before opening this journal.
            for task_id, raw in db.execute('SELECT id,payload FROM automation_tasks WHERE session<>?', (session,)).fetchall():
                p = json.loads(raw)
                if p['state'] in ('preparing', 'prepared', 'running', 'stopping'):
                    p.update(state='interrupted', message='原工作台已退出；请重新检查，未保存结果不可恢复')
                    db.execute('UPDATE automation_tasks SET payload=? WHERE id=?', (self.dumps(p), task_id))

    def connect(self): return sqlite3.connect(self.path, timeout=.5)
    @staticmethod
    def dumps(value): return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def create(self, operation_key, arguments, kind, **values):
        signature = digest(arguments)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT id,signature,payload FROM automation_tasks WHERE operation_key=?', (operation_key,)).fetchone()
            if row:
                if row[1] != signature: raise CommandError('OPERATION_CONFLICT', '同一操作键的内容已改变，请先查询原任务')
                return json.loads(row[2]), False
            task_id = uuid.uuid4().hex
            p = {'task_id': task_id, 'kind': kind, 'state': 'preparing' if kind == 'prepare' else 'running',
                 'created': stamp(), 'message': '已受理', 'scope': '未保存结果仅在当前工作台会话有效', **values}
            db.execute('INSERT INTO automation_tasks VALUES(?,?,?,?,?,?)',
                       (task_id, operation_key, signature, self.session, p['created'], self.dumps(p)))
        return p, True

    def get(self, task_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT payload FROM automation_tasks WHERE id=?', (task_id,)).fetchone()
        if not row: raise CommandError('TASK_NOT_FOUND', '未找到本机任务，请核对任务编号和资料环境')
        return json.loads(row[0])

    def lookup(self, operation_key, arguments):
        with closing(self.connect()) as db:
            row = db.execute('SELECT signature,payload FROM automation_tasks WHERE operation_key=?', (operation_key,)).fetchone()
        if not row: return None
        if row[0] != digest(arguments): raise CommandError('OPERATION_CONFLICT', '同一操作键的内容已改变，请先查询原任务')
        return json.loads(row[1])

    def update(self, task_id, **values):
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM automation_tasks WHERE id=?', (task_id,)).fetchone()
            if not row: raise CommandError('TASK_NOT_FOUND', '任务不存在')
            p = json.loads(row[0]); p.update(values); p['updated'] = stamp()
            db.execute('UPDATE automation_tasks SET payload=? WHERE id=?', (self.dumps(p), task_id))
            db.execute('INSERT INTO automation_events(task,at,payload) VALUES(?,?,?)', (task_id, p['updated'], self.dumps(values)))
        return p

    def events(self, task_id, after=0):
        with closing(self.connect()) as db:
            rows = db.execute('SELECT id,at,payload FROM automation_events WHERE task=? AND id>? ORDER BY id LIMIT 100', (task_id, after)).fetchall()
        return [{'cursor': row[0], 'at': row[1], **json.loads(row[2])} for row in rows]

    def list(self, offset=0, limit=100):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM automation_tasks ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', (limit, offset))]

    def page(self, offset, limit, search=''):
        # Search literal text, not user-provided SQL LIKE patterns.
        with closing(self.connect()) as db:
            total = db.execute('SELECT count(*) FROM automation_tasks WHERE instr(lower(payload),?)>0', (search.lower(),)).fetchone()[0]
            rows = db.execute('SELECT payload FROM automation_tasks WHERE instr(lower(payload),?)>0 ORDER BY created DESC,id DESC LIMIT ? OFFSET ?', (search.lower(), limit, offset))
            return {'total': total, 'offset': offset, 'items': [json.loads(r[0]) for r in rows],
                    'next_offset': offset + limit if offset + limit < total else None}
