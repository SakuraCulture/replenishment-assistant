"""Frozen inputs and the existing report writers behind the timed workbench."""
from contextlib import ExitStack, contextmanager
import copy
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter

from business_daily import PendingInputs, WAREHOUSES
from stockout_common import now, task_lock
from timed_exports import NoRecords


def _days(start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if not 0 <= (last - first).days <= 366:
        raise ValueError('缺货率导出日期范围最多 367 天')
    return [(first + timedelta(days=n)).isoformat() for n in range((last - first).days + 1)]


def _section_refs(folder, args, partial):
    from stockout_repository import Repository
    from warehouse_stockout import Repository as Warehouses

    mode = args['mode']
    issues, result = [], {'store_days': [], 'warehouse_days': []}
    if mode in ('both', 'store'):
        options = args['store_query']
        repo = Repository(folder)
        day_map = {}
        if repo.path.exists():
            with repo.connect() as db:
                day_map = {r['day']: dict(r) for r in db.execute('''
                    SELECT d.day,d.publication,p.run,r.basis FROM current_days d
                    JOIN publications p ON p.id=d.publication JOIN runs r ON r.id=p.run
                    WHERE d.day BETWEEN ? AND ?
                ''', (options['start'], options['end']))}
                for info in day_map.values():
                    settings = repo.settings(info['basis'])
                    explicit = options.get('filters', {}).get('store') or []
                    wanted = set(explicit) if explicit else {item['code'] for item in settings['stores']}
                    available = {r['store']: r['snapshot'] for r in db.execute('''
                        SELECT r.store,r.snapshot FROM run_stores r JOIN store_snapshots s ON s.id=r.snapshot
                        WHERE r.run=? AND s.state='READY' AND s.day=?
                    ''', (info['run'], info['day'])) if r['store'] in wanted}
                    if not wanted:
                        issues.append(info['day'] + ' 应采门店范围待核')
                    if wanted - set(available):
                        issues.append(info['day'] + ' 缺门店：' + '、'.join(sorted(wanted - set(available))))
                    info.update(expected=sorted(wanted), snapshots=available)
        issues.extend(day + ' 缺已发布门店建议' for day in _days(options['start'], options['end']) if day not in day_map)
        result['store_days'] = list(day_map.values())
    if mode in ('both', 'warehouse'):
        options = args['warehouse_query']
        repo = Warehouses(folder)
        warehouses = options.get('filters', {}).get('warehouse') or list(WAREHOUSES)
        expected = {(day, wh) for day in _days(options['start'], options['end']) for wh in warehouses}
        if repo.path.exists():
            with repo.connect() as db:
                result['warehouse_days'] = [dict(r) for r in db.execute('''
                    SELECT c.day,c.warehouse,c.snapshot,c.run,s.observed,s.source_sha,s.rows
                    FROM current c JOIN snapshots s ON s.id=c.snapshot AND s.warehouse=c.warehouse AND s.day=c.day
                    WHERE c.day BETWEEN ? AND ? ORDER BY c.day,c.warehouse
                ''', (options['start'], options['end'])) if (r['day'], r['warehouse']) in expected]
        missing = expected - {(r['day'], r['warehouse']) for r in result['warehouse_days']}
        issues.extend(day + ' 缺' + wh + '已发布快照' for day, wh in sorted(missing))
    if issues and (not partial or not any(result.values())):
        raise PendingInputs(issues)
    return {**result, 'missing': issues}


class FrozenPublications:
    """Use temporary views over retained immutable snapshots on read-only connections."""
    def __init__(self, repo, mode, refs):
        self.repo, self.mode, self.refs = repo, mode, refs
        self.path = repo.path

    def __getattr__(self, name):
        return getattr(self.repo, name)

    @contextmanager
    def connect(self, **options):
        if options.get('write'):
            raise ValueError('报表快照只读')
        with self.repo.connect() as db:
            db.execute('PRAGMA query_only=OFF')
            if self.mode == 'store':
                db.execute('CREATE TEMP TABLE current_days(day TEXT PRIMARY KEY, publication INTEGER)')
                db.execute('''CREATE TEMP TABLE run_stores(
                    run TEXT,store TEXT,snapshot TEXT,state TEXT,PRIMARY KEY(run,store))''')
                db.executemany('INSERT INTO temp.current_days VALUES(?,?)',
                               ((r['day'], r['publication']) for r in self.refs))
                for ref in self.refs:
                    actual = {r[0]: r[1] for r in db.execute('''
                        SELECT r.store,r.snapshot FROM main.publications p JOIN main.run_stores r ON r.run=p.run
                        JOIN main.store_snapshots s ON s.id=r.snapshot
                        WHERE p.id=? AND p.run=? AND p.day=? AND s.state='READY' AND s.day=p.day
                    ''', (ref['publication'], ref['run'], ref['day']))}
                    if any(actual.get(code) != sid for code, sid in ref['snapshots'].items()):
                        raise PendingInputs(['冻结门店发布版本无法复原：' + ref['day']])
                    db.executemany('INSERT OR IGNORE INTO temp.run_stores VALUES(?,?,?,?)',
                                   ((ref['run'], code, ref['snapshots'].get(code),
                                     'READY' if code in ref['snapshots'] else 'PENDING')
                                    for code in ref['expected']))
            else:
                db.execute('CREATE TEMP TABLE current(day TEXT,warehouse TEXT,snapshot TEXT,run TEXT,PRIMARY KEY(day,warehouse))')
                for ref in self.refs:
                    actual = db.execute('SELECT source_sha,rows FROM main.snapshots WHERE id=? AND day=? AND warehouse=?',
                                        (ref['snapshot'], ref['day'], ref['warehouse'])).fetchone()
                    if not actual or tuple(actual) != (ref['source_sha'], ref['rows']):
                        raise PendingInputs([ref['day'] + ref['warehouse'] + '冻结快照无法复原'])
                    db.execute('INSERT INTO temp.current VALUES(?,?,?,?)',
                               tuple(ref[k] for k in ('day', 'warehouse', 'snapshot', 'run')))
            db.execute('PRAGMA query_only=ON')
            yield db


class Reports:
    def __init__(self, root):
        self.root = Path(root)
        self.path = self.root / 'workbench.sqlite3'

    @contextmanager
    def lock(self, run):
        with ExitStack() as locks:
            if run['plan']['type'] in ('business_daily', 'stockout'):
                try:
                    for name in ('stockout', 'warehouse_stockout'):
                        locks.enter_context(task_lock(self.root / 'analysis' / name))
                except ValueError as exc:
                    raise PendingInputs(['分析资料正在采集或导出，原运行等候资源']) from exc
            yield

    def prepare(self, run, progress, cancel):
        plan = run['plan']
        spec = copy.deepcopy(plan.get('scope') or {})
        start, end = run['dates']
        saved = run.get('frozen')
        began = perf_counter()
        if plan['type'] == 'business_daily':
            import business_daily_sources as sources
            basis = saved or sources.freeze(self.root, end, stores=spec.get('stores'),
                                            partial=bool(plan.get('partial')), cancel=cancel)
            basis, data = sources.prepare(self.root, basis, progress, cancel)
            return basis, (basis, data, perf_counter() - began)
        if plan['type'] == 'stockout':
            for key in ('store_query', 'warehouse_query'):
                spec[key] = {**spec.get(key, {}), 'start': start, 'end': end}
            basis = saved or {'version': 'stockout-unified-1', **_section_refs(self.root, spec, plan.get('partial'))}
            return basis, (basis, spec, perf_counter() - began)
        from order_analytics import collect_overview
        query = {**spec.get('query', {}), 'from': start, 'to': end}
        if saved:
            query = saved['query']
        elif not query.get('as_of'):
            query['as_of'] = now()
        wanted = spec.get('orders')
        if wanted is not None:
            if not wanted:
                raise NoRecords('当前计划没有指定仓单')
            query['orders'] = wanted
        report = collect_overview(self.path, query, progress, cancel)
        if not report['heads']:
            raise NoRecords('当前范围没有符合条件的签发仓单')
        if saved and report['snapshot_id'] != saved['snapshot_id']:
            raise PendingInputs(['冻结的订单、修订或生命周期版本无法复原'])
        identity = lambda h: {k: h.get(k) for k in ('number', 'store', 'warehouse', 'revision', 'child_revision', 'digest')}
        heads = [identity(h) for h in report['heads']]
        if spec.get('heads') and {h['number']: h for h in spec['heads']} != {h['number']: h for h in heads}:
            raise PendingInputs(['指定原仓单及修订已变化，请核对该计划的原选单'])
        basis = {'version': 'sku-completion-1', 'query': report['query'], 'snapshot_id': report['snapshot_id'],
                 'heads': heads}
        return basis, (basis, report, perf_counter() - began)

    def render(self, run, projection, target, progress, cancel):
        began = perf_counter()
        basis, data, reading = projection
        kind = run['plan']['type']
        if kind == 'business_daily':
            from business_daily_workbook import write
            info = write(target, basis, data, progress, cancel)
        elif kind == 'stockout':
            from stockout_repository import Repository
            from warehouse_stockout import Repository as Warehouses
            from stockout_hub_exports import export
            store = FrozenPublications(Repository(self.root), 'store', basis['store_days'])
            warehouse = FrozenPublications(Warehouses(self.root), 'warehouse', basis['warehouse_days'])
            export(self.root, {**data, 'destination': str(target)}, cancel, progress,
                   stores=store, warehouses=warehouse)
            info = {'missing': basis['missing'], 'calculate_and_write_seconds': perf_counter() - began}
        else:
            from order_analytics_exports import export
            info = export(self.path, data, target, 'sku_completion', None, progress, cancel)
            info.pop('path', None)
        return {'read_seconds': reading, **info}
