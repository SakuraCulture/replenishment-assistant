"""Host-side durable command intentions, isolated from all replenishment business tables."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from aoxiang_client import ComponentError


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True, allow_nan=False)


def fingerprint(command: dict) -> str:
    return hashlib.sha256(encoded({k: v for k, v in command.items() if k != 'command_id'}).encode('utf-8')).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Jobs:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        present = self.path.exists()
        guard = self.path.with_suffix(self.path.suffix + '.guard')
        if not present and guard.exists():
            raise ValueError('翱象作业库已丢失，不能作为全新作业库重建，请恢复备份')
        with closing(self.connect()) as db, db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if present:
                if not {'ax_meta', 'ax_jobs'} <= tables or db.execute('SELECT version FROM ax_meta').fetchall() != [(1,)]:
                    raise ValueError('翱象作业库结构不完整或版本不兼容，不能重建空表')
            else:
                db.executescript('''BEGIN IMMEDIATE;
                    CREATE TABLE ax_meta(version INTEGER NOT NULL);
                    INSERT INTO ax_meta VALUES(1);
                    CREATE TABLE ax_jobs(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                      command TEXT NOT NULL, context TEXT NOT NULL, state TEXT NOT NULL,
                      sequence INTEGER NOT NULL, record TEXT, last_error TEXT, updated TEXT NOT NULL);
                    COMMIT;''')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('翱象作业库校验失败，请恢复备份')
        try:
            with guard.open('x', encoding='utf-8') as stream:
                stream.write('aoxiang-host-jobs-v1\n')
                stream.flush()
                import os
                os.fsync(stream.fileno())
        except FileExistsError:
            pass

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        return db

    def register(self, command: dict, context: dict) -> dict:
        command_id = command.get('command_id')
        if not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', command_id):
            raise ValueError('命令编号无效')
        if type(command.get('protocol_version')) is not int or command['protocol_version'] != 1:
            raise ValueError('命令协议版本无效')
        raw, basis, digest = encoded(command), encoded(context), fingerprint(command)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT command,context FROM ax_jobs WHERE id=?', (command_id,)).fetchone()
            if previous:
                if previous != (raw, basis):
                    raise ValueError('原命令或业务来源不能更换，请保留原作业关联')
            else:
                db.execute('INSERT INTO ax_jobs VALUES(?,?,?,?,?,?,?,?,?)',
                           (command_id, digest, raw, basis, 'PREPARED', 0, None, None, now()))
        return self.get(command_id)

    def get(self, command_id: str) -> dict:
        with closing(self.connect()) as db:
            db.row_factory = sqlite3.Row
            row = db.execute('SELECT * FROM ax_jobs WHERE id=?', (command_id,)).fetchone()
        if not row:
            raise ValueError('未找到本地自动化作业')
        result = dict(row)
        for key in ['command', 'context', 'record', 'last_error']:
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def recent(self, limit=100, *, include_unfinished=False) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('每次最多读取100个作业')
        with closing(self.connect()) as db:
            ids = [row[0] for row in db.execute('SELECT id FROM ax_jobs ORDER BY rowid DESC LIMIT ?', (limit,))]
        if include_unfinished:
            ids.extend(row['id'] for row in self.unfinished() if row['id'] not in ids)
        return [self.get(value) for value in ids]

    def unfinished(self) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,command,state FROM ax_jobs WHERE state NOT IN ('SUCCEEDED','COMPLETED_WITH_ERRORS','CANCELLED') ORDER BY rowid").fetchall()
        return [{'id': row[0], 'command': json.loads(row[1]), 'state': row[2]} for row in rows]

    def observe(self, command_id: str, record: dict) -> bool:
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT fingerprint,command,sequence,record FROM ax_jobs WHERE id=?', (command_id,)).fetchone()
            if not current or record.get('id') != command_id or record.get('fingerprint') != current[0] or encoded(record.get('request')) != current[1]:
                raise ValueError('组件回执与本地原命令不一致，未采用')
            sequence = record.get('sequence')
            if type(sequence) is not int or sequence < 1:
                raise ValueError('组件回执序号无效')
            if sequence < current[2]:
                return False
            raw = encoded(record)
            if sequence == current[2] and current[3] != raw:
                raise ValueError('相同回执序号返回不同内容，需核对')
            db.execute('UPDATE ax_jobs SET state=?,sequence=?,record=?,last_error=NULL,updated=? WHERE id=?',
                       (record['state'], sequence, raw, now(), command_id))
        return True

    def dispatch(self, client, command_id: str) -> dict:
        """Persisted intent precedes send. Same command replay uses the component's durable ID contract."""
        job = self.get(command_id)
        if job['state'] == 'CANCELLED':
            raise ValueError('未受理请求已取消，不能再次派发原请求')
        try:
            receipt = client.request('reserve', command=job['command'])
            self.observe(command_id, receipt['record'])
        except ComponentError as error:
            with closing(self.connect()) as db, db:
                db.execute('UPDATE ax_jobs SET last_error=?,updated=? WHERE id=?',
                           (encoded({'code': error.code, 'message': str(error), 'requires_query': True}), now(), command_id))
            raise
        return self.get(command_id)

    def cancel_unaccepted(self, client, command_id: str) -> dict:
        job = self.get(command_id)
        if job['state'] != 'PREPARED' or job['record'] is not None:
            raise ValueError('已受理任务不能取消本地关联，请保留原任务')
        try:
            record = client.request('status', command_id=command_id)
        except ComponentError as exc:
            if exc.code != 'COMMAND_NOT_FOUND':
                raise
        else:
            self.observe(command_id, record)
            raise ValueError('原任务已受理，已更新状态；不能作为未受理请求取消')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT state,record FROM ax_jobs WHERE id=?', (command_id,)).fetchone()
            if row != ('PREPARED', None):
                raise ValueError('原任务状态已改变，请刷新')
            db.execute("UPDATE ax_jobs SET state='CANCELLED',last_error=NULL,updated=? WHERE id=?", (now(), command_id))
        return self.get(command_id)

    def refresh(self, client, command_id: str) -> dict:
        self.observe(command_id, client.request('status', command_id=command_id))
        return self.get(command_id)
