"""Application-owned export plans, occurrences and durable publication intents."""
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import perf_counter
import uuid

from business_daily import PendingInputs
from export_files import explain_error, publish_new, sha256
from runtime_support import Cancelled, checkpoint
from scheduled_replenishment import Repository as ScheduleRepository, TZ, next_at, stamp
from stockout_common import encoded
from work_repository import Conflict, loads


KINDS = {'business_daily': '门店缺货率日报（经营版）', 'stockout': '缺货率统一导出',
         'sku_completion': 'SKU订单完成率汇总'}
DATABASE = 'timed_reports.sqlite3'
STATES = {'QUEUED': '已排队', 'WAITING': '等待资料或资源', 'RUNNING': '执行中', 'PAUSED': '已暂停',
          'REVIEW': '需处理', 'MISSED': '已错过', 'COMPLETE': '已导出', 'PARTIAL': '部分范围已导出',
          'EMPTY': '无匹配记录'}
SCHEMA = (
    'CREATE TABLE IF NOT EXISTS timed_plans(id TEXT PRIMARY KEY,next_at TEXT,payload TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS timed_plan_due ON timed_plans(next_at) WHERE next_at IS NOT NULL',
    'CREATE TABLE IF NOT EXISTS timed_runs(id TEXT PRIMARY KEY, occurrence TEXT NOT NULL UNIQUE, day TEXT NOT NULL, '
    'state TEXT NOT NULL, retry_at TEXT, payload TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS timed_run_queue ON timed_runs(state,retry_at)',
    'CREATE INDEX IF NOT EXISTS timed_run_day ON timed_runs(day,state)',
    'CREATE TABLE IF NOT EXISTS timed_artifacts(run TEXT PRIMARY KEY, payload TEXT NOT NULL)',
    'CREATE TABLE IF NOT EXISTS timed_events(id INTEGER PRIMARY KEY,run TEXT NOT NULL,at TEXT NOT NULL,message TEXT NOT NULL)',
    'CREATE INDEX IF NOT EXISTS timed_event_run ON timed_events(run,id)',
)


def dates(plan, at):
    day = at.astimezone(TZ).date()
    rule = plan['date_rule']
    if rule == 'fixed':
        return tuple(plan['dates'])
    end = (day - timedelta(days=rule != 'today')).isoformat()
    return ((day - timedelta(days=7)).isoformat(), end) if rule == 'last7' else (end, end)


def filename(run):
    start, end = run['dates']
    span = start.replace('-', '') if start == end else start.replace('-', '') + '_' + end.replace('-', '')
    prefix = '部分_' if run['plan'].get('partial') else ''
    return f'{prefix}{KINDS[run["plan"]["type"]]}_{span}_{run["id"][:10]}.xlsx'


def plan_value(value):
    p = dict(value)
    p['name'] = str(p.get('name') or '').strip()
    if p.get('type') not in KINDS or not p['name'] or len(p['name']) > 80:
        raise ValueError('请选择报表类型并填写安排名称')
    if p.get('date_rule') not in ('today', 'yesterday', 'last7', 'fixed'):
        raise ValueError('业务日期规则无效')
    if p['date_rule'] == 'fixed':
        start, end = p['dates']
        if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end or start > end:
            raise ValueError('业务日期区间无效')
    if p['type'] == 'business_daily' and (p['date_rule'] == 'last7' or
                                          p['date_rule'] == 'fixed' and p['dates'][0] != p['dates'][1]):
        raise ValueError('经营日报每次使用一个业务日')
    parsed = time.fromisoformat(p['time'])
    if parsed.tzinfo or len(p['time']) != 5 or p.get('kind') not in ('once', 'weekly'):
        raise ValueError('请选择单次或每周及北京时间 HH:mm')
    if p['kind'] == 'once':
        date.fromisoformat(p['date'])
    elif not p.get('weekdays') or any(type(n) is not int or n not in range(7) for n in p['weekdays']):
        raise ValueError('请选择执行星期')
    if type(p.get('wait_minutes')) is not int or not 1 <= p['wait_minutes'] <= 1440:
        raise ValueError('最长等候须为 1 至 1440 分钟')
    if not p.get('directory') or not Path(p['directory']).is_absolute():
        raise ValueError('请选择实际输出目录')
    p['timezone'] = 'Asia/Shanghai'
    return p


def preview(plan, after=None):
    p = plan_value(plan)
    after = after or stamp()
    rows = []
    for _ in range(3):
        value = next_at(p, after)
        if value is None:
            break
        after = datetime.fromisoformat(value)
        rows.append({'time': value, 'dates': dates(p, after),
                     'name': filename({'plan': p, 'dates': dates(p, after), 'id': '运行号'})})
    return rows


class Repository(ScheduleRepository):
    def exists(self):
        if not self.path.exists():
            return False
        with self.db() as db:
            return db.execute("SELECT 1 FROM sqlite_master WHERE name='timed_plans'").fetchone() is not None

    def ensure(self):
        if not self.exists():
            with self.db(True) as db:
                for statement in SCHEMA:
                    db.execute(statement)

    def specs(self):
        if not self.exists():
            return []
        with self.db() as db:
            return [loads(row[0]) for row in db.execute('SELECT payload FROM timed_plans ORDER BY rowid')]

    def save_plan(self, plan, expected=None):
        self.ensure()
        p = plan_value(plan)
        p['id'] = expected['id'] if expected else uuid.uuid4().hex
        p['revision'] = expected['revision'] + 1 if expected else 1
        p['next_at'] = next_at(p, self.clock()) if p.get('enabled') else None
        if p.get('enabled') and not p['next_at']:
            raise ValueError('所选时间已过，请选择下一次执行时间')
        with self.db(True) as db:
            old = db.execute('SELECT payload FROM timed_plans WHERE id=?', (p['id'],)).fetchone()
            if expected and (old is None or loads(old[0]) != expected):
                raise Conflict('执行计划已变化，请刷新')
            db.execute('INSERT INTO timed_plans VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET '
                       'next_at=excluded.next_at,payload=excluded.payload', (p['id'], p['next_at'], encoded(p)))
        return p

    def manual(self, plan, request_id, *, repeated_from=None):
        self.ensure()
        with self.db(True) as db:
            return self._register(db, plan_value(plan), self.clock(), 'manual:' + request_id, repeated_from=repeated_from)

    def _register(self, db, plan, when, key, *, missed=False, repeated_from=None):
        old = db.execute('SELECT payload FROM timed_runs WHERE occurrence=?', (key,)).fetchone()
        if old:
            return loads(old[0])
        run = {'id': uuid.uuid4().hex, 'plan': plan, 'dates': list(dates(plan, when)), 'scheduled_at': when.isoformat(),
               'deadline': (when + timedelta(minutes=plan['wait_minutes'])).isoformat(), 'epoch': 0,
               'requested_by': 'manual' if key.startswith('manual:') else 'timer',
               'state': 'MISSED' if missed else 'QUEUED', 'frozen': None, 'repeated_from': repeated_from,
               'message': '超过等候期限，已错过' if missed else '等待执行', 'updated': self.clock().isoformat()}
        db.execute('INSERT INTO timed_runs VALUES(?,?,?,?,?,?)',
                   (run['id'], key, run['dates'][1], run['state'], None, encoded(run)))
        self._event(db, run)
        return run

    def due(self):
        if not self.exists():
            return []
        now = self.clock()
        iso = now.isoformat()
        with self.db() as db:
            due = db.execute('SELECT 1 FROM timed_plans WHERE next_at<=? LIMIT 1', (iso,)).fetchone()
        if not due:
            return []
        result = []
        with self.db(True) as db:
            for row in db.execute('SELECT payload FROM timed_plans WHERE next_at<=? ORDER BY next_at', (iso,)).fetchall():
                p = loads(row[0])
                when = datetime.fromisoformat(p['next_at'])
                result.append(self._register(db, p, when, p['id'] + ':' + when.isoformat(),
                                             missed=now > when + timedelta(minutes=p['wait_minutes'])))
                p['next_at'] = next_at(p, now)
                p['enabled'] = bool(p['next_at'])
                db.execute('UPDATE timed_plans SET next_at=?,payload=? WHERE id=?', (p['next_at'], encoded(p), p['id']))
        return result

    def _read(self, db, rid):
        row = db.execute('SELECT payload FROM timed_runs WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise ValueError('原运行已不存在')
        return loads(row[0])

    def _event(self, db, run):
        db.execute('INSERT INTO timed_events(run,at,message) VALUES(?,?,?)',
                   (run['id'], run['updated'], run['message']))

    def _change(self, db, run, **values):
        events = 'state' in values or values.get('message', run['message']) != run['message']
        run.update(values, updated=self.clock().isoformat())
        db.execute('UPDATE timed_runs SET state=?,retry_at=?,payload=? WHERE id=?',
                   (run['state'], run.get('retry_at'), encoded(run), run['id']))
        if events:
            self._event(db, run)
        return run

    def change(self, rid, epoch, **values):
        with self.db(True) as db:
            run = self._read(db, rid)
            if run['epoch'] != epoch or run['state'] != 'RUNNING':
                raise Conflict('原执行轮次已结束')
            return self._change(db, run, **values)

    def next(self):
        if not self.exists():
            return None
        with self.db() as db:
            row = db.execute("SELECT id FROM timed_runs WHERE state='QUEUED' OR (state='WAITING' AND retry_at<=?) "
                             "ORDER BY CASE state WHEN 'QUEUED' THEN 0 ELSE 1 END, retry_at,rowid LIMIT 1",
                             (self.clock().isoformat(),)).fetchone()
        return row[0] if row else None

    def claim(self, rid):
        with self.db(True) as db:
            run = self._read(db, rid)
            if run['state'] not in ('QUEUED', 'WAITING'):
                raise Conflict('该运行已由其他操作处理')
            return self._change(db, run, state='RUNNING', epoch=run['epoch'] + 1, message='核对本次所需资料')

    def recover(self):
        if not self.exists():
            return
        with self.db(True) as db:
            for row in db.execute("SELECT payload FROM timed_runs WHERE state='RUNNING'").fetchall():
                run = loads(row[0])
                self._change(db, run, state='QUEUED', epoch=run['epoch'] + 1, message='重开后核对原发布记录')

    def request(self, rid, pause=False):
        with self.db(True) as db:
            run = self._read(db, rid)
            allow = ('QUEUED', 'WAITING') if pause else ('WAITING', 'REVIEW', 'PAUSED')
            if run['state'] not in allow:
                raise ValueError('当前状态不能执行该操作')
            return self._change(db, run, state='PAUSED' if pause else 'QUEUED',
                                message='已暂停本次' if pause else '继续原运行')

    def runs(self, *, offset=0, limit=50):
        if not self.exists():
            return []
        with self.db() as db:
            return [loads(row[0]) for row in db.execute('SELECT payload FROM timed_runs ORDER BY rowid DESC LIMIT ? OFFSET ?',
                                                       (limit, offset))]

    def inspect(self, rid):
        with self.db() as db:
            return self._read(db, rid), self._artifact(db, rid)

    def _artifact(self, db, rid):
        row = db.execute('SELECT payload FROM timed_artifacts WHERE run=?', (rid,)).fetchone()
        return loads(row[0]) if row else None

    def intent(self, rid, epoch, artifact):
        with self.db(True) as db:
            self._require_running(db, rid, epoch)
            db.execute('INSERT INTO timed_artifacts VALUES(?,?) ON CONFLICT(run) DO UPDATE SET payload=excluded.payload',
                       (rid, encoded(artifact)))

    def _require_running(self, db, rid, epoch):
        run = self._read(db, rid)
        if run['state'] != 'RUNNING' or run['epoch'] != epoch:
            raise Conflict('发布前执行权已变化')
        return run

    def publish(self, rid, epoch, artifact, temporary):
        with self.db(True) as db:
            self._require_running(db, rid, epoch)
            if self._artifact(db, rid) != artifact:
                raise Conflict('发布意图已变化')
            publish_new(temporary, artifact['path'])

    def confirmed(self, rid, epoch, artifact, *, publication_seconds=None, repaired=False):
        with self.db(True) as db:
            run = self._require_running(db, rid, epoch)
            if self._artifact(db, rid) != artifact:
                raise Conflict('文件回执的原发布意图已变化')
            artifact = {**artifact, 'published_at': self.clock().isoformat(), 'receipt_repaired': repaired}
            if publication_seconds is not None:
                artifact['result'] = {**artifact['result'], 'publication_seconds': publication_seconds}
            db.execute('UPDATE timed_artifacts SET payload=? WHERE run=?', (encoded(artifact), rid))
            self._change(db, run, state='PARTIAL' if run['plan'].get('partial') else 'COMPLETE',
                         message='文件已核验并登记回执', output=artifact['path'])


class NoRecords(ValueError):
    pass


class Runner:
    def __init__(self, repo, backend, cancel):
        self.repo, self.backend, self.cancel = repo, backend, cancel

    def execute(self, rid):
        run = self.repo.claim(rid)
        epoch = run['epoch']
        def progress(message):
            message = str(message)
            if message != run['message']:
                run.update(self.repo.change(rid, epoch, message=message))
        try:
            directory = Path(run['plan']['directory'])
            directory.mkdir(parents=True, exist_ok=True)
            temporary = directory / ('.timed-' + rid + '.xlsx')
            _, artifact = self.repo.inspect(rid)
            if artifact is not None:
                target = Path(artifact['path'])
                if target.exists():
                    if sha256(target) != artifact['sha256']:
                        raise ValueError('已发布路径的文件内容变化，请核对现有文件：' + str(target))
                    self.repo.confirmed(rid, epoch, artifact, repaired=True)
                    temporary.unlink(missing_ok=True)
                    return
                if temporary.exists() and sha256(temporary) == artifact['sha256']:
                    checkpoint(self.cancel)
                    self.repo.publish(rid, epoch, artifact, temporary)
                    self.repo.confirmed(rid, epoch, artifact)
                    return
            checkpoint(self.cancel)
            temporary.unlink(missing_ok=True)
            try:
                with self.backend.lock(run):
                    basis, projection = self.backend.prepare(run, progress, self.cancel)
                    if basis != run['frozen']:
                        run.update(self.repo.change(rid, epoch, frozen=basis))
                    measurements = self.backend.render(run, projection, temporary, progress, self.cancel)
                checkpoint(self.cancel)
                publication_started = perf_counter()
                progress('登记发布路径及校验值')
                target = directory / filename(run)
                if target.exists():
                    target = target.with_name(target.stem + '_' + uuid.uuid4().hex[:8] + '.xlsx')
                artifact = {'path': str(target), 'sha256': sha256(temporary),
                            'bytes': temporary.stat().st_size, 'result': measurements}
                self.repo.intent(rid, epoch, artifact)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            checkpoint(self.cancel)
            self.repo.publish(rid, epoch, artifact, temporary)
            self.repo.confirmed(rid, epoch, artifact, publication_seconds=perf_counter() - publication_started)
            temporary.unlink(missing_ok=True)
        except Cancelled:
            self.repo.change(rid, epoch, state='PAUSED', message='已保存原运行与可用成品')
        except PendingInputs as exc:
            now = self.repo.clock()
            waiting = now < datetime.fromisoformat(run['deadline'])
            self.repo.change(rid, epoch, state='WAITING' if waiting else 'REVIEW', message=str(exc),
                             retry_at=(now + timedelta(seconds=15)).isoformat())
        except NoRecords as exc:
            self.repo.change(rid, epoch, state='EMPTY', message=str(exc))
        except Exception as exc:
            self.repo.change(rid, epoch, state='REVIEW', message=explain_error(exc))
