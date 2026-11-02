"""Small persistent local workflow queue. No browser or original business-table writes."""
import copy
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from aoxiang_jobs import encoded

HEAD_FIELDS = ('oid', 'day', 'store', 'name', 'warehouse', 'number', 'revision', 'current_revision', 'digest', 'visibility_token')
ACTIVE = ('QUEUED', 'RUNNING', 'YIELDED', 'PAUSED')
DONE_ITEM = ('PREPARED', 'BOUND', 'OBSERVED', 'SYNCED', 'REVIEW', 'FAILED', 'SKIPPED')


class Workflows:
    def __init__(self, folder, *, clock=time.time):
        self.folder = Path(folder)
        self.path = self.folder / 'workflows.sqlite3'
        self.clock = clock

    @contextmanager
    def db(self, write=False, *, timeout=10):
        self.folder.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:
                db.execute('BEGIN IMMEDIATE')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {'ax_workflows', 'ax_workflow_events', 'ax_schedules', 'ax_workflow_meta'}
            if not tables:
                if not write:
                    db.execute('BEGIN IMMEDIATE')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    db.execute('CREATE TABLE ax_workflows(id TEXT PRIMARY KEY,state TEXT NOT NULL,payload TEXT NOT NULL)')
                    db.execute('CREATE TABLE ax_workflow_events(id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,sequence))')
                    db.execute('CREATE TABLE ax_schedules(id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
                    db.execute('CREATE TABLE ax_workflow_meta(version INTEGER NOT NULL)')
                    db.execute('INSERT INTO ax_workflow_meta VALUES(1)')
                    db.execute('CREATE INDEX ax_workflow_state ON ax_workflows(state)')
                elif tables != required:
                    raise ValueError('翱象批次队列初始化冲突，请核对原记录')
                if not write:
                    db.commit()
            elif tables != required or db.execute('SELECT version FROM ax_workflow_meta').fetchone()[0] != 1:
                raise ValueError('翱象批次队列结构缺失或不兼容，不能重建后重派任务')
            yield db
            if write:
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, id):
        row = db.execute('SELECT payload FROM ax_workflows WHERE id=?', (id,)).fetchone()
        if not row:
            raise ValueError('未找到原翱象批次作业')
        return json.loads(row[0])

    def get(self, id):
        with self.db() as db:
            return self._get(db, id)

    def list(self, limit=100):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM ax_workflows WHERE rowid IN (SELECT rowid FROM ax_workflows ORDER BY rowid DESC LIMIT ?) OR state IN (?,?,?,?) ORDER BY rowid', (limit, *ACTIVE))]

    def next_queued(self, *, timeout=10):
        with self.db(timeout=timeout) as db:
            row=db.execute("SELECT payload FROM ax_workflows WHERE state IN ('QUEUED','YIELDED') ORDER BY rowid LIMIT 1").fetchone()
            return json.loads(row[0]) if row else None

    def _save(self, db, job, event):
        job = {**job, 'sequence': job.get('sequence', 0) + 1, 'updated_at': self.clock()}
        db.execute('INSERT INTO ax_workflows VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,payload=excluded.payload', (job['id'], job['state'], encoded(job)))
        db.execute('INSERT INTO ax_workflow_events VALUES(?,?,?)', (job['id'], job['sequence'], encoded({'at': job['updated_at'], 'event': event, 'state': job['state'], 'message': job.get('message')})))
        return job

    def _new(self, db, kind, items, **kw):
        if not items or len(items) > 256:
            raise ValueError('每批请选择1至256张明确仓单')
        keys = [i['key'] for i in items]
        if len(set(keys)) != len(keys):
            raise ValueError('同批次订单重复')
        for row in db.execute('SELECT payload FROM ax_workflows WHERE state IN (?,?,?,?)', ACTIVE):
            old = json.loads(row[0])
            if old['kind'] == kind and set(keys) & {i['key'] for i in old['items'] if i['state'] not in DONE_ITEM}:
                raise ValueError('所选订单已有未完成同类作业，请继续原批次：' + old['id'])
        job = {'id': 'axbatch-' + uuid.uuid4().hex, 'kind': kind, 'created_at': self.clock(), 'state': 'QUEUED',
               'sequence': 0, 'items': [{**copy.deepcopy(i), 'position': n, 'state': 'PENDING', 'error': None} for n, i in enumerate(items)], **kw}
        return self._save(db, job, 'QUEUED')

    def prepare(self, heads):
        items = []
        for h in heads:
            head = {k: copy.deepcopy(h[k]) for k in HEAD_FIELDS}
            if h['warehouse'] not in ('山东仓', '河北仓') or h.get('deleted'):
                raise ValueError('只能准备有效且明确供货仓的订单')
            items.append({'key': h['number'], 'head': head})
        with self.db(True) as db:
            return self._new(db, 'prepare', items)

    def upload(self, prepared_id, positions, expected_sequence):
        with self.db(True) as db:
            original = self._get(db, prepared_id)
            if original['kind'] != 'prepare' or original['sequence'] != expected_sequence or original['state'] in ACTIVE:
                raise ValueError('采购预览已变化或尚未停止，请重新核对')
            if not positions or positions != sorted(set(positions)) or any(n < 0 or n >= len(original['items']) for n in positions):
                raise ValueError('待上传范围或顺序无效')
            items = [copy.deepcopy(original['items'][n]) for n in positions]
            if any(i['state'] != 'PREPARED' or not i.get('package') for i in items):
                raise ValueError('仅可确认已成功生成预览的仓单')
            for row in db.execute('SELECT payload FROM ax_workflows ORDER BY rowid'):
                old = json.loads(row[0])
                if old.get('prepared_id') == prepared_id:
                    previous = [i['key'] for i in old['items']]
                    current = [i['key'] for i in items]
                    if previous == current:
                        return old
                    if set(previous) & set(current):
                        raise ValueError('部分预览已确认上传，请继续原上传批次；剩余单据须单独选择')
            return self._new(db, 'upload', items, prepared_id=prepared_id, confirmed_sequence=expected_sequence)

    def receipt(self, records):
        items = [{'key': r['id'], 'purchase_id': r['id'], 'basis': copy.deepcopy(r['basis']), 'display_name': r.get('target', {}).get('displayName'), 'order_id': r['binding']['orderId']} for r in records]
        with self.db(True) as db:
            return self._new(db, 'receipt', items)

    def update(self, id, *, state=None, message=None, position=None, changes=None, **job_changes):
        with self.db(True) as db:
            job = self._get(db, id)
            if state:
                job['state'] = state
            if message is not None:
                job['message'] = message
            if position is not None:
                job['items'][position].update(copy.deepcopy(changes or {}))
            job.update(copy.deepcopy(job_changes))
            return self._save(db, job, 'UPDATED')

    def resume(self, id):
        with self.db(True) as db:
            job = self._get(db, id)
            if job['state'] not in ('PAUSED', 'YIELDED', 'FAILED'):
                raise ValueError('此批次不需要继续')
            job['state'] = 'QUEUED'
            return self._save(db, job, 'RESUMED_ORIGINAL')

    def cancel_queued(self, id):
        with self.db(True) as db:
            job = self._get(db, id)
            if job['state'] not in ('QUEUED', 'PAUSED', 'YIELDED', 'FAILED'):
                raise ValueError('先停止正在执行的批次，再取消剩余任务')
            job['state'] = 'CANCELLED'
            return self._save(db, job, 'CANCELLED_REMAINDER_ONLY')

    def recover(self, *, timeout=10):
        with self.db(True,timeout=timeout) as db:
            for row in db.execute("SELECT payload FROM ax_workflows WHERE state='RUNNING'").fetchall():
                job = json.loads(row[0])
                job.update(state='YIELDED' if job.get('automatic') else 'PAUSED', message='上次中断；保留确切采购及下载任务，继续前读取原状态')
                self._save(db, job, 'RECOVERED_WITHOUT_REPLAY')

    def retry_items(self, id, positions, expected_sequence):
        """Re-arm original items; keep package IDs and successful siblings intact."""
        with self.db(True) as db:
            job=self._get(db,id)
            if job['sequence']!=expected_sequence:raise ValueError('批次状态已变化，请刷新后重试')
            if not positions or len(set(positions))!=len(positions):raise ValueError('请选择失败或待核任务')
            if job['state']=='CANCELLED':raise ValueError('本批已结束，请使用后续新批次')
            for n in positions:
                if n<0 or n>=len(job['items']) or job['items'][n]['state'] not in ('FAILED','REVIEW'):raise ValueError('仅可重跑失败或核对待核项')
            for n in positions:
                item=job['items'][n];item['attempts']=item.get('attempts',0)+1
                item['previous_error']=item.get('error');item['state']='PENDING';item['error']=None
            if job['state']!='RUNNING':job['state']='QUEUED'
            job['message']='原任务已排入重跑；成功项保持，采购仍核对原任务号'
            return self._save(db,job,'RETRY_ORIGINAL_ITEMS')

    def finish_and_new(self,id,expected_sequence):
        """New execution batch, same immutable external task identities."""
        with self.db(True) as db:
            job=self._get(db,id)
            if job['sequence']!=expected_sequence:raise ValueError('批次已变化，请重新核对')
            if job['state']=='RUNNING':raise ValueError('请先停止，等待当前安全阶段退出')
            if job.get('successor'):return self._get(db,job['successor'])
            items=[i for i in job['items'] if i['state'] in ('PENDING','FAILED')]
            if any(i['state'] in ('RUNNING','REVIEW') for i in job['items']):raise ValueError('仍有正在执行或结果待核项，请先核对原任务再新建')
            if not items:raise ValueError('没有未开始或失败项需要转入新批次')
            job['state']='CANCELLED';self._save(db,job,'ENDED_KEEP_SUCCESS')
            new=self._new(db,job['kind'],items,predecessor=id,**{k:job[k] for k in ('prepared_id','confirmed_sequence','template_receipt') if k in job})
            job=self._get(db,id);job['successor']=new['id'];self._save(db,job,'LINKED_SUCCESSOR')
            return new

    def schedules(self):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM ax_schedules ORDER BY rowid')]

    def schedule(self, purchase_id):
        with self.db() as db:
            row = db.execute('SELECT payload FROM ax_schedules WHERE id=?', (purchase_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def configure_schedules(self, records, minutes, enabled):
        if type(minutes) is not int or not 5 <= minutes <= 1440:
            raise ValueError('同步周期须为5至1440分钟；未启用前不会自动查询')
        with self.db(True) as db:
            for r in records:
                old = db.execute('SELECT payload FROM ax_schedules WHERE id=?', (r['id'],)).fetchone()
                s = json.loads(old[0]) if old else {'id': r['id'], 'basis': r['basis'], 'order_id': r['binding']['orderId'], 'terminal': False, 'final_pending': False, 'failures': 0, 'active_job': None, 'created_at': self.clock()}
                if enabled and (s['terminal'] or r.get('receipt_terminal')):
                    raise ValueError('已完成订单不能重新启用常规监控；最终同步待办仍保留')
                s.update(enabled=bool(enabled), automation_paused=not enabled, display_name=r.get('target', {}).get('displayName'), interval_seconds=minutes * 60, next_at=self.clock(), updated_at=self.clock())
                db.execute('INSERT INTO ax_schedules VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (r['id'], encoded(s)))

    def _due(self, db):
        due=[]
        for row in db.execute("SELECT payload FROM ax_schedules WHERE json_extract(payload,'$.enabled')=1 OR json_extract(payload,'$.final_pending')=1 ORDER BY rowid").fetchall():
            s=json.loads(row[0])
            if s.get('active_job'):
                job=self._get(db,s['active_job'])
                if job['state'] in ('PAUSED','FAILED') or job['state'] in ACTIVE:continue
                s['active_job']=None
            if not s.get('automation_paused') and (s.get('enabled') and not s['terminal'] or s.get('final_pending')) and s.get('next_at',0)<=self.clock() and not s.get('needs_review'):due.append(s)
        return due

    def enqueue_due(self, *, timeout=10):
        # Idle polling takes no write lock. Recheck everything inside the write
        # transaction, so a concurrent pause/change never uses a stale read plan.
        with self.db(timeout=timeout) as db:
            if not self._due(db):return None
        with self.db(True,timeout=timeout) as db:
            due=self._due(db)
            if not due:
                return None
            # Existing manual batches take precedence; do not duplicate their selected orders.
            busy = set()
            for row in db.execute('SELECT payload FROM ax_workflows WHERE state IN (?,?,?,?)', ACTIVE):
                job = json.loads(row[0])
                if job['kind'] == 'receipt': busy.update(i['key'] for i in job['items'])
            items = [{'key': s['id'], 'purchase_id': s['id'], 'basis': s['basis'], 'display_name': s.get('display_name'), 'order_id': s['order_id']} for s in due if s['id'] not in busy][:256]
            if not items:
                return None
            job = self._new(db, 'receipt', items, automatic=True)
            for s in due:
                if s['id'] in {i['key'] for i in items}:
                    s['active_job'] = job['id']
                    db.execute('UPDATE ax_schedules SET payload=? WHERE id=?', (encoded(s), s['id']))
            return job

    def observe_schedule(self, purchase_id, record, *, error=None, review=False):
        with self.db(True) as db:
            row = db.execute('SELECT payload FROM ax_schedules WHERE id=?', (purchase_id,)).fetchone()
            if not row:
                return
            s = json.loads(row[0])
            terminal = s['terminal'] or bool((record or {}).get('terminal'))
            if record:
                s['last_state'] = record['state']
                s['summary'] = record.get('summary')
            s.update(terminal=terminal, enabled=s['enabled'] and not terminal, needs_review=bool(review), last_error=error,
                     last_observed_at=self.clock(), failures=s['failures'] + 1 if error else 0)
            s['final_pending'] = terminal and s.get('last_state') != 'SYNCED'
            s['next_at'] = self.clock() + min(86400, s['interval_seconds'] * (2 ** min(s['failures'], 6)))
            db.execute('UPDATE ax_schedules SET payload=? WHERE id=?', (encoded(s), purchase_id))
