"""Independent analysis storage. Construction does no I/O; callers initialize explicitly."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import uuid
from stockout_common import encoded, digest, now, today, file_hash, atomic_json, read_json, WAREHOUSES

SCHEMA = '''
CREATE TABLE meta(version INTEGER NOT NULL);
INSERT INTO meta VALUES(1);
CREATE TABLE settings_versions(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE settings_current(id INTEGER PRIMARY KEY CHECK(id=1), version TEXT NOT NULL REFERENCES settings_versions(id));
CREATE TABLE originals(sha TEXT PRIMARY KEY, path TEXT NOT NULL, bytes INTEGER NOT NULL, created TEXT NOT NULL);
CREATE TABLE catalogs(sha TEXT NOT NULL, warehouse TEXT NOT NULL, rows INTEGER NOT NULL, observed TEXT NOT NULL,
  PRIMARY KEY(sha,warehouse));
CREATE TABLE catalog_items(sha TEXT NOT NULL, sku TEXT NOT NULL, name TEXT, status TEXT, stock REAL,
  category TEXT, supplier TEXT, tags TEXT, rownum INTEGER NOT NULL, PRIMARY KEY(sha,sku));
CREATE TABLE runs(id TEXT PRIMARY KEY, day TEXT NOT NULL, basis TEXT NOT NULL REFERENCES settings_versions(id),
  trigger TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
  external TEXT NOT NULL DEFAULT '{}', catalogs TEXT NOT NULL DEFAULT '{}', source_run TEXT);
CREATE TABLE store_snapshots(id TEXT PRIMARY KEY, store TEXT NOT NULL, name TEXT NOT NULL, day TEXT NOT NULL,
  observed TEXT NOT NULL, source_sha TEXT NOT NULL REFERENCES originals(sha), basis TEXT NOT NULL,
  catalog_set TEXT NOT NULL, rows INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL);
CREATE TABLE run_stores(run TEXT NOT NULL REFERENCES runs(id), store TEXT NOT NULL, name TEXT NOT NULL,
  snapshot TEXT REFERENCES store_snapshots(id), state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', PRIMARY KEY(run,store));
CREATE TABLE fact_dimensions(id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL, warehouse TEXT NOT NULL, mandatory INTEGER, category TEXT NOT NULL, supplier TEXT NOT NULL,
  retired INTEGER, assessment INTEGER, tags TEXT, error TEXT NOT NULL, product_status TEXT NOT NULL);
CREATE TABLE fact_records(snapshot TEXT NOT NULL REFERENCES store_snapshots(id), sku TEXT NOT NULL, name TEXT,
  month_raw TEXT, stock_raw TEXT, inbound_raw TEXT, month REAL, stock REAL, inbound REAL,
  eligible INTEGER, bad INTEGER NOT NULL, zero INTEGER NOT NULL,
  dimension INTEGER NOT NULL REFERENCES fact_dimensions(id),rownum INTEGER NOT NULL,replen_status TEXT NOT NULL,
  PRIMARY KEY(snapshot,sku));
CREATE INDEX fact_cuts ON fact_records(snapshot,dimension);
CREATE TABLE dimension_summaries(snapshot TEXT NOT NULL REFERENCES store_snapshots(id),dimension INTEGER NOT NULL REFERENCES fact_dimensions(id),
  replen_status TEXT NOT NULL,inbound_status TEXT NOT NULL,rows INTEGER NOT NULL,eligible INTEGER NOT NULL,zero INTEGER NOT NULL,bad INTEGER NOT NULL,
  PRIMARY KEY(snapshot,dimension,replen_status,inbound_status));
CREATE VIEW facts AS SELECT f.snapshot,f.sku,f.name,f.month_raw,f.stock_raw,f.inbound_raw,f.month,f.stock,f.inbound,
  f.eligible,f.bad,f.zero,d.source,d.warehouse,d.mandatory,d.category,d.supplier,d.retired,d.assessment,
  d.tags,d.error,f.rownum,d.product_status,f.replen_status,
  CASE WHEN f.inbound IS NULL OR f.inbound<0 THEN '在途待核' WHEN f.inbound>0 THEN '有在途' ELSE '无在途' END AS inbound_status
  FROM fact_records f JOIN fact_dimensions d ON d.id=f.dimension;
CREATE VIEW aggregate_facts AS SELECT a.snapshot,a.replen_status,a.inbound_status,a.rows,a.eligible,a.zero,a.bad,
  d.source,d.warehouse,d.mandatory,d.category,d.supplier,d.retired,d.assessment,d.tags,d.error,d.product_status
  FROM dimension_summaries a JOIN fact_dimensions d ON d.id=a.dimension;
CREATE TABLE summaries(snapshot TEXT NOT NULL REFERENCES store_snapshots(id), view TEXT NOT NULL,
  base INTEGER NOT NULL, zero INTEGER NOT NULL, bad INTEGER NOT NULL, unknown_tags INTEGER NOT NULL,
  unknown_zero INTEGER NOT NULL, removed_base INTEGER NOT NULL, removed_zero INTEGER NOT NULL,
  PRIMARY KEY(snapshot,view));
CREATE TABLE publications(id INTEGER PRIMARY KEY, day TEXT NOT NULL, run TEXT NOT NULL REFERENCES runs(id),
  created TEXT NOT NULL, UNIQUE(day,run));
CREATE TABLE current_days(day TEXT PRIMARY KEY, publication INTEGER NOT NULL REFERENCES publications(id));
CREATE TABLE cleanups(id TEXT PRIMARY KEY, plan TEXT NOT NULL, created TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE cleanup_files(cleanup TEXT NOT NULL REFERENCES cleanups(id), sha TEXT NOT NULL, path TEXT NOT NULL,
  state TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', PRIMARY KEY(cleanup,sha));
'''


class Repository:
    def __init__(self, folder):
        self.root = Path(folder).resolve()
        self.folder = self.root / 'analysis' / 'stockout'
        self.path = self.folder / 'stockout.sqlite3'

    @contextmanager
    def connect(self, *, write=False):
        db = sqlite3.connect(self.path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA cache_size=-8192')
        db.execute('PRAGMA temp_store=FILE')
        if not write:
            db.execute('PRAGMA query_only=ON')
        try:
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        if self.path.exists():
            try:
                with self.connect() as db:
                    if [tuple(r) for r in db.execute('SELECT version FROM meta')] != [(1,)]:
                        raise ValueError('分析库版本不兼容，原补货不受影响；请保留资料后检查版本')
            except sqlite3.DatabaseError as exc:
                raise ValueError('分析历史库无法读取。请保留该文件并恢复本板块的独立备份；原补货可以继续使用，详情见应用日志') from exc
            return
        guard = self.folder / 'database.guard'
        if guard.exists():
            raise ValueError('分析库已丢失，请恢复分析备份；不会重建空库覆盖历史')
        self.folder.mkdir(parents=True, exist_ok=True)
        # Initialize at a unique temporary path, publish only the complete schema.
        temp = self.folder / (uuid.uuid4().hex + '.sqlite3')
        db = sqlite3.connect(temp)
        try:
            db.executescript(SCHEMA)
            db.commit()
        finally:
            db.close()
        try:
            import os
            os.link(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)
        guard.write_text('stockout-schema-1\n', encoding='utf-8')

    def settings(self, version=None):
        with self.connect() as db:
            row = db.execute('SELECT payload,id FROM settings_versions WHERE id=?', (version,)).fetchone() if version else db.execute(
                'SELECT v.payload,v.id FROM settings_versions v JOIN settings_current c ON c.version=v.id WHERE c.id=1').fetchone()
        if not row:
            return None
        value = json.loads(row['payload'])
        if digest(value) != row['id']:
            raise ValueError('分析设置内容与版本不一致')
        return dict(value, version=row['id'])

    def save_settings(self, value):
        value = dict(value)
        value.pop('version', None)
        stores = value.get('stores') or []
        if not stores or len(stores) > 256 or len({s['code'] for s in stores}) != len(stores):
            raise ValueError('请选择1至256家不同门店')
        for store in stores:
            if not isinstance(store.get('code'), str) or not store['code'].strip() or not store.get('name'):
                raise ValueError('门店编码或名称无效')
        for route in value.get('routes', {}).values():
            if route.get('primary') not in WAREHOUSES or route.get('secondary') not in (None, *WAREHOUSES) or route.get('primary') == route.get('secondary'):
                raise ValueError('分析供货设置无效')
        version = digest(value)
        with self.connect(write=True) as db:
            db.execute('INSERT OR IGNORE INTO settings_versions VALUES(?,?,?)', (version, encoded(value), now()))
            db.execute('INSERT INTO settings_current VALUES(1,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version', (version,))
        return self.settings(version)

    def new_run(self, *, trigger='manual', day=None, source_run=None):
        basis = self.settings()
        old = self.run(source_run) if source_run else None
        if old:
            basis = self.settings(old['basis'])
        if not basis:
            raise ValueError('请先选择门店并采用分析供货设置及必卖清单')
        rid = uuid.uuid4().hex
        from version_info import INFO
        external=dict(old['external'] if old else {})
        external.setdefault('versions',{k:INFO[k] for k in ('version','model_version','routing_version')})
        with self.connect(write=True) as db:
            # Never create a second local intention over an unfinished analysis run.
            active = db.execute("SELECT id FROM runs WHERE state NOT IN ('COMPLETE','PARTIAL','ENDED') LIMIT 1").fetchone()
            if active:
                raise ValueError('请先继续或结束原分析任务：' + active[0])
            latest=db.execute('SELECT external FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
            if not source_run and latest and json.loads(latest[0]).get('catalogs_pending'):
                raise ValueError('原分析的仓资料采集尚未核对，请先继续原任务，不重复提交新仓导出')
            db.execute('INSERT INTO runs(id,day,basis,trigger,state,message,created,updated,source_run,external,catalogs) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (rid, day or today(), basis['version'], trigger, 'PREPARED', '已保存采集意图', now(), now(), source_run,
                        encoded(external), encoded(old['catalogs'] if old else {})))
            for store in basis['stores']:
                db.execute('INSERT INTO run_stores(run,store,name,state) VALUES(?,?,?,?)', (rid, store['code'], store['name'], 'PENDING'))
        return self.run(rid)

    def run(self, rid):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone()
            if not row:
                raise ValueError('未找到分析任务')
            stores = [dict(r) for r in db.execute('SELECT * FROM run_stores WHERE run=? ORDER BY rowid', (rid,))]
        value = dict(row)
        for key in ('external', 'catalogs'):
            value[key] = json.loads(value[key])
        return dict(value, stores=stores)

    def recent(self, limit=30):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,day,state,message,created,trigger FROM runs ORDER BY rowid DESC LIMIT ?', (min(100, max(1, limit)),))]

    def change(self, rid, **values):
        if set(values) - {'state','message','external','catalogs'}:
            raise ValueError('不能修改分析任务身份')
        with self.connect(write=True) as db:
            row = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
            if not row or row[0] in {'COMPLETE','PARTIAL','ENDED'}:
                raise ValueError('已发布或已结束任务不可修改，请创建新修订')
            values['updated'] = now()
            db.execute('UPDATE runs SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?',
                       tuple(encoded(v) if k in {'external','catalogs'} else v for k,v in values.items()) + (rid,))

    def store_result(self, rid, store, snapshot=None, error=''):
        with self.connect(write=True) as db:
            if db.execute('SELECT state FROM runs WHERE id=?',(rid,)).fetchone()[0] in {'COMPLETE','PARTIAL','ENDED'}:
                raise ValueError('已发布任务不可修改')
            db.execute('UPDATE run_stores SET snapshot=?,state=?,error=? WHERE run=? AND store=?',
                       (snapshot, 'READY' if snapshot else 'FAILED', error, rid, store))

    def publish(self, rid):
        with self.connect(write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute('SELECT state FROM runs WHERE id=?', (rid,)).fetchone()
            if not run or run[0] in {'COMPLETE','PARTIAL','ENDED'}:
                raise ValueError('该任务已发布或已结束')
            days = [r[0] for r in db.execute("SELECT DISTINCT s.day FROM run_stores r JOIN store_snapshots s ON s.id=r.snapshot WHERE r.run=? AND s.state='READY'", (rid,))]
            if not days:
                raise ValueError('没有完整验收的门店快照，未发布日报')
            for day in days:
                cursor = db.execute('INSERT INTO publications(day,run,created) VALUES(?,?,?)', (day,rid,now()))
                db.execute('INSERT INTO current_days VALUES(?,?) ON CONFLICT(day) DO UPDATE SET publication=excluded.publication', (day,cursor.lastrowid))
            missing = db.execute("SELECT count(*) FROM run_stores WHERE run=? AND state!='READY'", (rid,)).fetchone()[0]
            external=json.loads(db.execute('SELECT external FROM runs WHERE id=?',(rid,)).fetchone()[0])
            state = 'PARTIAL' if missing or len(days)>1 or external.get('catalogs_pending') else 'COMPLETE'
            db.execute('UPDATE runs SET state=?,message=?,updated=? WHERE id=?', (state, '日报已发布；按实际观测日期及已完成门店展示',now(),rid))
        return self.run(rid)

    def adopt_business(self, stores, *, use_builtin=False):
        """Read saved files directly: no repository defaults, migrations or writeback."""
        paths = [self.root/'store_supply.json', self.root/'must_sell.json']
        before = {str(p):file_hash(p) if p.exists() else None for p in paths}
        routes = read_json(paths[0]) if paths[0].exists() else {}
        profile = read_json(paths[1]) if paths[1].exists() else None
        if profile is None and use_builtin:
            profile = read_json(Path(__file__).parent/'default_must_sell.json')
        if profile is not None:
            from priority_list import validate_profile
            validate_profile(profile)
        if before != {str(p):file_hash(p) if p.exists() else None for p in paths}:
            raise ValueError('采用期间原业务设置变化，请重新核对采用')
        return self.save_settings({'stores':stores, 'routes':{s['code']:routes[s['code']] for s in stores if s['code'] in routes},
                                   'profile':profile, 'adopted_at':now(), 'source_hashes':before})

    def retain(self, source, expected, cancel=None):
        from runtime_support import checkpoint
        from stockout_common import resource_check
        source = Path(source).resolve()
        checkpoint(cancel)
        resource_check(self.folder,source.stat().st_size*2)
        if file_hash(source) != expected:
            raise ValueError('原件校验值不符，未采用')
        target = self.folder/'originals'/(expected+source.suffix.lower())
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            temp = target.with_name('.'+uuid.uuid4().hex+'.tmp')
            try:
                with source.open('rb') as inp, temp.open('xb') as out:
                    for block in iter(lambda:inp.read(1024*1024), b''):
                        checkpoint(cancel)
                        out.write(block)
                    out.flush()
                    import os
                    os.fsync(out.fileno())
                if file_hash(temp)!=expected or file_hash(source)!=expected:
                    raise ValueError('原件复制期间变化，未采用')
                os.replace(temp,target)
            finally:
                temp.unlink(missing_ok=True)
        if file_hash(target)!=expected:
            raise ValueError('分析已留存原件被改动，请恢复原件')
        with self.connect(write=True) as db:
            db.execute('INSERT OR IGNORE INTO originals VALUES(?,?,?,?)',(expected,str(target),target.stat().st_size,now()))
        return target
