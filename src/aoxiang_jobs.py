"""Host-side durable command intentions, isolated from all replenishment business tables."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
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

    def recent(self, limit=100, *, include_unfinished=False, exclude_analysis=False) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('每次最多读取100个作业')
        with closing(self.connect()) as db:
            condition=" WHERE coalesce(json_extract(context,'$.purpose'),'')!='stockout_analysis'" if exclude_analysis else ''
            ids = [row[0] for row in db.execute('SELECT id FROM ax_jobs'+condition+' ORDER BY rowid DESC LIMIT ?', (limit,))]
        if include_unfinished:
            ids.extend(row['id'] for row in self.unfinished() if row['id'] not in ids)
        values=[self.get(value) for value in ids]
        return [v for v in values if not exclude_analysis or v['context'].get('purpose')!='stockout_analysis']

    def unfinished(self) -> list[dict]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT id,command,state,record,context FROM ax_jobs WHERE state NOT IN ('SUCCEEDED','COMPLETED_WITH_ERRORS','CANCELLED','ENDED') ORDER BY rowid").fetchall()
        result = []
        for row in rows:
            command = json.loads(row[1])
            ended = {i['warehouse_code'] for i in json.loads(row[3] or '{}').get('items', []) if i['state'] == 'ENDED'}
            context=json.loads(row[4]);ended.update(context.get('local_ended_codes',[]))
            command['scope']['warehouse_codes'] = [c for c in command['scope']['warehouse_codes'] if c not in ended]
            if command['scope']['warehouse_codes']:
                result.append({'id': row[0], 'command': command, 'state': row[2], 'purpose':context.get('purpose','replenishment')})
        return result

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
        local=self.get(command_id)
        if local['state']=='PREPARED' and local['context'].get('local_ended_codes'):
            excluded=set(local['context']['local_ended_codes'])
            command=json.loads(encoded(local['command']));command['command_id']='rep-'+uuid.uuid4().hex
            command['scope']['warehouse_codes']=[c for c in command['scope']['warehouse_codes'] if c not in excluded]
            if not command['scope']['warehouse_codes']:raise ValueError('原本地请求已全部结束，请新建采集')
            context={k:v for k,v in local['context'].items() if k!='local_ended_codes'}
            context.update(stores=[s for s in context['stores'] if s['buyerWarehouseCode'] not in excluded],continued_from=command_id)
            successor=self.register(command,context)
            with closing(self.connect()) as db,db:db.execute("UPDATE ax_jobs SET state='CANCELLED',updated=? WHERE id=? AND state='PREPARED'",(now(),command_id))
            return self.dispatch(client,successor['id'])
        """Persisted intent precedes send. Same command replay uses the component's durable ID contract."""
        job = self.get(command_id)
        if job['state'] in {'CANCELLED', 'ENDED'}:
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

    def reexport_preview(self,client,configuration,codes,*,purpose='replenishment',progress=lambda _:None,cancel=lambda:False):
        """Quiesce only overlapping jobs, then preview explicit new exports without browser actions."""
        if not codes or len(set(codes))!=len(codes):raise ValueError('请选择需要重新导出的门店')
        with closing(self.connect()) as db:ids=[r[0] for r in db.execute('SELECT id FROM ax_jobs ORDER BY rowid')]
        self.classify_consumers(client,ids)
        local=[];resume_commands=[]
        for ident in ids:
            if cancel():raise ValueError('已取消重导准备；已暂停任务可继续，未派发新导出')
            job=self.get(ident);command=job['command']
            if (command['account_binding']['merchant_code']!=configuration['account_binding']['merchant_code']
                or job['context'].get('purpose','replenishment')!=purpose or not set(codes).intersection(command['scope']['warehouse_codes'])):continue
            if job['state'] in {'CANCELLED','ENDED','SUCCEEDED'}:continue
            try:job=self.refresh(client,ident)
            except ComponentError as exc:
                if exc.code!='COMMAND_NOT_FOUND' or job['state']!='PREPARED' or job['record'] is not None:raise
                local.append({'id':ident,'fingerprint':job['fingerprint']});continue
            if job['state'] in {'QUEUED','RUNNING','PAUSE_REQUESTED'}:
                if job['state'] in {'QUEUED','RUNNING'}:resume_commands.append(ident)
                progress('正在安全暂停所选门店原采集；保留其他门店，不等待平台生成完成…')
                self.observe(ident,client.request('pause',command_id=ident));deadline=time.monotonic()+90
                while self.get(ident)['state'] in {'QUEUED','RUNNING','PAUSE_REQUESTED'}:
                    if cancel() or time.monotonic()>=deadline:raise ValueError('原页面动作仍在安全收尾，未强杀、未重新导出；请稍后继续')
                    time.sleep(.15);self.refresh(client,ident)
        operation='reexport-'+uuid.uuid4().hex
        plan=client.request('collection.reexport.preview',operation_id=operation,warehouse_codes=list(codes),purpose=purpose)
        plan.update(codes=list(codes),local_requests=local,resume_commands=resume_commands)
        return plan

    def reexport_apply(self,client,plan):
        receipt=client.request('collection.reexport.apply',operation_id=plan['id'],warehouse_codes=plan['codes'],purpose=plan['purpose'],token=plan['token'])
        for record in receipt['records']:
            with closing(self.connect()) as db:known=db.execute('SELECT 1 FROM ax_jobs WHERE id=?',(record['id'],)).fetchone()
            if known:self.observe(record['id'],record)
        for entry in plan.get('local_requests',[]):
            job=self.get(entry['id'])
            if job['fingerprint']!=entry['fingerprint'] or job['record'] is not None:raise ValueError('原本地请求已变化，未派发新导出；请刷新')
            # A trusted COMMAND_NOT_FOUND was observed before this pure-local retirement.
            context=job['context'];ended=set(context.get('local_ended_codes',[]))|set(plan['codes']).intersection(job['command']['scope']['warehouse_codes'])
            context['local_ended_codes']=sorted(ended)
            state='CANCELLED' if ended.issuperset(job['command']['scope']['warehouse_codes']) else 'PREPARED'
            with closing(self.connect()) as db,db:db.execute('UPDATE ax_jobs SET context=?,state=?,updated=? WHERE id=? AND state=\'PREPARED\' AND record IS NULL',(encoded(context),state,now(),job['id']))
        for ident in plan.get('resume_commands',[]):
            job=self.get(ident)
            if job['state'] in {'PAUSED','INTERRUPTED'} and any(i['state'] not in {'ENDED','SUCCEEDED','FAILED','NEEDS_REVIEW'} for i in (job['record'] or {}).get('items',[])):
                self.observe(ident,client.request('resume',command_id=ident))
        return receipt

    def classify_consumers(self,client,ids=None):
        if ids is None:
            with closing(self.connect()) as db:ids=[r[0] for r in db.execute('SELECT id FROM ax_jobs ORDER BY rowid')]
        for ident in ids:
            job=self.get(ident)
            if job['command'].get('consumer') or not job['record']:continue
            context=job['context'];purpose=context.get('purpose','replenishment')
            # Only the host's durable original context classifies old records. Never infer ownership from store/date.
            client.request('collection.consumer',command_id=ident,fingerprint=job['fingerprint'],purpose=purpose,owner=context.get('owner',''))

    def maintenance_preview(self, client, command_id, operation, *, purpose=None, owner=None):
        if operation not in {'end', 'delete'}:
            raise ValueError('未知采集维护动作')
        job = self.get(command_id)
        self.classify_consumers(client)
        if job['context'].get('purpose')=='stockout_analysis' and (purpose!='stockout_analysis' or owner!=job['context'].get('owner')):
            raise ValueError('这是门店缺货率分析的采集任务，请在该板块继续或安全结束，不能从补货页面处理')
        if job['state'] in {'PREPARED', 'CANCELLED'} and job['record'] is None:
            try:
                record = client.request('status', command_id=command_id)
            except ComponentError as exc:
                if exc.code != 'COMMAND_NOT_FOUND':
                    raise
                if operation == 'delete' and job['state'] != 'CANCELLED':
                    raise ValueError('请先结束本次采集，再删除记录')
                return {'id': command_id, 'action': operation, 'local': True,
                        'token': hashlib.sha256(encoded(job).encode()).hexdigest(),
                        'stores': [{'code': s['buyerWarehouseCode'], 'name': s['displayName']} for s in job['context']['stores']],
                        'related': [], 'blocked': []}
            else:
                self.observe(command_id, record)
        return client.request('collection.preview', command_id=command_id, operation=operation)

    def maintenance_apply(self, client, plan):
        command_id, operation = plan['id'], plan['action']
        if plan.get('local'):
            current = self.maintenance_preview(client, command_id, operation)
            if current['token'] != plan['token']:
                raise ValueError('采集记录已变化，请重新预览确认')
            if operation == 'end':
                return self.cancel_unaccepted(client, command_id)
            self._delete_record(command_id, self.get(command_id)['fingerprint'])
            return None
        receipt = client.request('collection.apply', command_id=command_id, operation=operation, token=plan['token'])
        if operation == 'delete':
            if receipt.get('deleted') != command_id:
                raise ValueError('删除回执与原采集不一致')
            self._delete_record(command_id, receipt['fingerprint'])
            return None
        for record in receipt['records']:
            with closing(self.connect()) as db:
                known = db.execute('SELECT 1 FROM ax_jobs WHERE id=?', (record['id'],)).fetchone()
            if known:
                self.observe(record['id'], record)
        return self.get(command_id)

    def _delete_record(self, command_id, digest):
        # Metadata only: never remove command files, adopted manifests, drafts or orders.
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT fingerprint,state FROM ax_jobs WHERE id=?', (command_id,)).fetchone()
            if row is None:
                return
            if row[0] != digest or row[1] not in {'SUCCEEDED', 'ENDED', 'CANCELLED'}:
                raise ValueError('本机记录未结束或身份已变化，未删除')
            db.execute('DELETE FROM ax_jobs WHERE id=?', (command_id,))

    def synchronize(self, client):
        """Recover a lost maintenance response; status queries never dispatch browser actions."""
        for job in self.recent(include_unfinished=True):
            if job['state'] == 'CANCELLED':
                continue
            try:
                self.refresh(client, job['id'])
            except ComponentError as exc:
                if exc.code != 'COMMAND_NOT_FOUND':
                    raise
                if job['state'] == 'PREPARED':
                    continue
                try:
                    plan = client.request('collection.preview', command_id=job['id'], operation='delete')
                except ComponentError as missing:
                    if missing.code == 'COMMAND_NOT_FOUND':
                        continue
                    raise
                if plan.get('already_deleted'):
                    self._delete_record(job['id'], plan['fingerprint'])
