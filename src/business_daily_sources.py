"""Read daily report evidence by source identity, including published analysis revisions."""
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
import hashlib
from pathlib import Path
import tempfile

from openpyxl import load_workbook

from business_daily import PendingInputs, VERSION, WAREHOUSES, decimal
from export_files import publish_new, sha256
from runtime_support import checkpoint
from stockout_common import atomic_json, encoded, now, read_json
from stockout_facts import identifier


SUPPLEMENTS = {
    'stores': '门店清单（含主仓、从仓、标签、运营负责人）',
    'blacklist': '地采黑名单',
    'must_sell': '必卖与季节品经营分析',
    'owners': '采购负责人',
    'weeks': '已发布周值及目标',
}
SALES_COLUMNS = {
    'store': ('仓库/门店编码', '门店编码'), 'sku': ('商品编码',), 'name': ('商品名称', '商品名'),
    'month': ('月销',), 'stock': ('可用库存',), 'inbound': ('在途库存',), 'status': ('补货状态',),
}
CATALOG_COLUMNS = {
    'sku': ('商品编码',), 'status': ('商品状态', '启用'), 'stock': ('可用数',),
    'virtual': ('虚拟分类',), 'category': ('产品分类', '分类'), 'supplier': ('供应商名称', '供应商'),
}


def folder(root):
    return Path(root) / 'analysis' / 'business_daily'


def business_day(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError('业务日应为 YYYY-MM-DD')
    return value


def file_source(root, selected, *, observed=None, sheets=None, account=None, cancel=None):
    """Archive an explicitly adopted file by content. An unchanged file is shared."""
    path = Path(selected).resolve(strict=True)
    suffix = path.suffix.lower()
    if suffix not in ('.xlsx', '.json'):
        raise ValueError('请选择 XLSX 原件或周值 JSON')
    if observed:
        stamp = datetime.fromisoformat(observed)
        if stamp.tzinfo is None:
            raise ValueError('资料观测时点须包含时区')
    destination = folder(root) / 'originals'
    destination.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=destination, suffix='.tmp', delete=False) as fp:
        temp = Path(fp.name)
        try:
            if path.stat().st_size == 0:
                raise ValueError('原件为空：' + path.name)
            with path.open('rb') as source:
                while block := source.read(1024 * 1024):
                    checkpoint(cancel)
                    digest.update(block)
                    fp.write(block)
        except BaseException:
            fp.close()
            temp.unlink(missing_ok=True)
            raise
    target = destination / (digest.hexdigest() + suffix)
    try:
        try:
            publish_new(temp, target)
        except FileExistsError:
            if sha256(target) != digest.hexdigest():
                raise ValueError('留存原件摘要异常：' + path.name)
    finally:
        temp.unlink(missing_ok=True)
    return {'sha256': digest.hexdigest(), 'suffix': suffix, 'name': path.name, 'imported_at': now(),
            'observed': observed, 'sheets': sheets or [], 'account': account}


def profile_path(root, day):
    return folder(root) / 'inputs' / (business_day(day) + '.json')


def profile(root, day):
    path = profile_path(root, day)
    value = read_json(path) if path.is_file() else {'day': day, 'files': {}, 'sales': [], 'catalogs': {}}
    if value.get('day') != day:
        raise ValueError('日报资料业务日与本次请求不一致')
    return value


def save_profile(root, value):
    atomic_json(profile_path(root, value['day']), value)


def import_profile(root, day, manifest, cancel=None):
    """Adopt a local manifest explicitly scoped to the selected business date."""
    path = Path(manifest).resolve(strict=True)
    update = read_json(path)
    if update.get('day') != day:
        raise ValueError('资料清单所列业务日与原运行不同')
    current = profile(root, day)
    def adopt(value, observation_required=False):
        spec = {'path': value} if isinstance(value, str) else dict(value)
        if observation_required and not spec.get('observed'):
            raise ValueError('门店建议及库存原件须注明实际采集或导出时点')
        selected = path.parent / spec.pop('path')
        return file_source(root, selected, cancel=cancel, **spec)
    for key, value in update.get('files', {}).items():
        if key not in SUPPLEMENTS:
            raise ValueError('未知日报资料类型：' + key)
        current['files'][key] = adopt(value)
    if 'sales' in update:
        current['sales'] = [adopt(row, True) for row in update['sales']]
    for wh, value in update.get('catalogs', {}).items():
        if wh not in WAREHOUSES or not value.get('account'):
            raise ValueError('仓库存原件缺少本仓账号标识')
        current['catalogs'][wh] = adopt(value, True)
    if 'order_cutoff' in update:
        cutoff = datetime.fromisoformat(update['order_cutoff'])
        if cutoff.tzinfo is None:
            raise ValueError('订单截至时点须注明时区')
        current['order_cutoff'] = cutoff.isoformat()
    checkpoint(cancel)
    save_profile(root, current)
    return current


def path_for(root, ref):
    checksum = ref['sha256']
    if len(checksum) != 64 or any(c not in '0123456789abcdef' for c in checksum):
        raise ValueError('日报原件摘要无效')
    if ref['suffix'] not in ('.xlsx', '.json'):
        raise ValueError('日报原件类型无效')
    path = folder(root) / 'originals' / (checksum + ref['suffix'])
    if not path.is_file():
        raise PendingInputs(['原件未留存：' + ref['name']])
    if sha256(path) != checksum:
        raise ValueError('日报原件摘要已变化：' + ref['name'])
    return path


def sheet_rows(path, columns, *, sheets=(), multiple=False, optional=None, cancel=None):
    """Project named columns and keep original worksheet name, row and order."""
    with closing(load_workbook(path, read_only=True, data_only=True, keep_links=False)) as book:
        matched = 0
        for name in (sheets or book.sheetnames):
            if name not in book.sheetnames:
                raise ValueError(f'{path.name} 找不到工作表 {name}')
            sheet = book[name]
            sheet.reset_dimensions()
            rows = iter(sheet.values)
            for line, headers in enumerate(rows, 1):
                if line > 40:
                    break
                positions = {key: [i for i, value in enumerate(headers) if value in choices]
                             for key, choices in columns.items()}
                if not all(positions.values()):
                    continue
                positions.update({key: [i for i, value in enumerate(headers) if value in choices]
                                  for key, choices in (optional or {}).items()})
                positions = {key: indexes for key, indexes in positions.items() if indexes}
                if any(len(v) != 1 for v in positions.values()):
                    raise ValueError(f'{name} 第 {line} 行有重复的业务表头')
                matched += 1
                if matched > 1 and not multiple:
                    raise ValueError(f'{path.name} 有多张匹配工作表，请指定来源页')
                for row_id, row in enumerate(rows, line + 1):
                    checkpoint(cancel)
                    if any(v not in (None, '') for v in row):
                        yield {key: row[pos[0]] if pos[0] < len(row) else None
                               for key, pos in positions.items()}, f'{name}!{row_id}'
                break
        if matched == 0 or sheets and matched != len(sheets):
            raise ValueError(f'{path.name} 未找到所需表头：' + '、'.join(names[0] for names in columns.values()))


def text(value):
    return str(value or '').strip()


def warehouse(value):
    name = text(value)
    if name in ('山东', '河北'):
        return name + '仓'
    if name in WAREHOUSES or not name or name in ('无', '未指定'):
        return name if name in WAREHOUSES else None
    raise ValueError('未知仓别：' + name)


def roster(path, ref, cancel=None):
    columns = {k: (v,) for k, v in zip(('code', 'name', 'tag', 'owner', 'primary', 'secondary'),
                                      ('门店编码', '门店名称', '标签', '运营负责人', '主仓', '从仓'))}
    from business_daily import EXCLUDED_TAGS
    result, seen = {}, set()
    for row, line in sheet_rows(path, columns, sheets=ref['sheets'], cancel=cancel):
        code = identifier(row['code'])
        if code in seen:
            raise ValueError(f'{line} 门店清单重复 {code}')
        seen.add(code)
        tag = text(row['tag'])
        if tag in EXCLUDED_TAGS:
            continue
        primary, secondary = warehouse(row['primary']), warehouse(row['secondary'])
        if not text(row['name']) or not primary or primary == secondary:
            raise PendingInputs([f'{line} 门店 {code} 的名称或主从仓待核'])
        result[code] = {'code': code, 'name': text(row['name']), 'tag': tag, 'owner': text(row['owner']),
                        'primary': primary, 'secondary': secondary}
    if not result:
        raise PendingInputs(['门店清单没有可计入门店'])
    return result


def native_stores(root, day, stores):
    from stockout_repository import Repository
    repo = Repository(root)
    if not repo.path.exists():
        return []
    with repo.connect() as db:
        return [dict(r) for r in db.execute('''
            SELECT s.id,s.store,s.name,s.day,s.observed,s.source_sha,s.rows,s.basis,p.id AS publication
            FROM current_days d JOIN publications p ON p.id=d.publication
            JOIN run_stores r ON r.run=p.run JOIN store_snapshots s ON s.id=r.snapshot
            WHERE d.day=? AND s.day=d.day AND s.state='READY' ORDER BY r.rowid
        ''', (day,)) if r['store'] in stores]


def native_catalogs(root, day):
    from warehouse_stockout import Repository
    from stockout_hub_service import Journal
    from work_repository import loads
    repo = Repository(root)
    if not repo.path.exists():
        return {}
    with repo.connect() as db:
        rows = [dict(r) for r in db.execute('''
            SELECT s.*,c.run,r.external,r.source_run FROM current c JOIN snapshots s ON s.id=c.snapshot
            JOIN runs r ON r.id=c.run
            WHERE c.day=? AND s.day=c.day AND s.warehouse=c.warehouse
        ''', (day,))]
    for ref in rows:
        external = loads(ref.pop('external'))
        parent = ref.pop('source_run') or ''
        if parent.startswith('hub:'):
            try:
                external = Journal(root).run(parent[4:])['external']
            except FileNotFoundError:
                pass
        proof = external.get('catalog_sources', {}).get(ref['warehouse'])
        if proof and proof.get('source_sha256') == ref['source_sha']:
            ref.update(account=proof['account'], acquisition_batch=proof['batch'], acquisition_item=proof['item'])
    return {r['warehouse']:r for r in rows}


def freeze(root, day, *, stores=None, partial=False, cancel=None):
    """Freeze source identifiers and order cutoff. Supplemental files are immutable copies."""
    selected = profile(root, day)
    files = selected['files']
    issues = [label for key, label in SUPPLEMENTS.items() if key not in files]
    if issues:
        raise PendingInputs(['缺资料：' + label for label in issues])
    checkpoint(cancel)
    all_stores = roster(path_for(root, files['stores']), files['stores'], cancel)
    if stores and set(stores) - set(all_stores):
        raise PendingInputs(['已选门店在该日清单中缺失：' + '、'.join(sorted(set(stores) - set(all_stores)))])
    scope = {code: row for code, row in all_stores.items() if not stores or code in stores}
    if stores and set(scope) != set(all_stores) and not partial:
        raise ValueError('所选门店是部分范围，请使用参考口径')
    store_snapshots = [] if selected['sales'] else native_stores(root, day, scope)
    warehouses = native_catalogs(root, day)
    catalogs = {name: ({'file': selected['catalogs'][name]} if name in selected['catalogs'] else warehouses.get(name))
                for name in WAREHOUSES}
    issues = [day + ' 缺' + wh + '库存快照' for wh, ref in catalogs.items() if ref is None]
    if not selected['sales']:
        missing = set(scope) - {r['store'] for r in store_snapshots}
        if missing and not partial:
            issues.append(day + ' 缺门店建议：' + '、'.join(sorted(missing)))
        if not store_snapshots:
            issues.append(day + ' 未发布任何所选门店建议')
    if issues:
        raise PendingInputs(issues)
    for wh, ref in catalogs.items():
        source = ref.get('file', ref)
        if not source.get('account') and (not partial or 'file' in ref):
            raise PendingInputs([wh + '原件尚缺核对过的仓账号标识'])
    if partial and not selected['sales']:
        scope = {code: store for code, store in scope.items() if code not in missing}
    return {'template': VERSION, 'day': day, 'frozen_at': now(), 'files': files, 'stores': list(scope.values()),
            'expected_stores': list(all_stores), 'sales': selected['sales'], 'store_snapshots': store_snapshots,
            'catalogs': catalogs, 'partial': partial, 'order_cutoff': selected.get('order_cutoff') or now()}


def published_weeks(path, day):
    if path.suffix.lower() == '.json':
        value = read_json(path)
    else:
        with closing(load_workbook(path, read_only=True, data_only=True, keep_links=False)) as book:
            stamp = str(book['口径说明']['B2'].value)
            published_day = stamp if '-' in stamp else f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}'
            sheet = list(book['汇总'].iter_rows(min_row=1, max_row=16, max_col=5, values_only=True))
        metrics = {}
        for offset in (1, 6, 11):
            metric = sheet[offset][0]
            metrics[metric] = {'previous': sheet[offset+1][2], 'target': sheet[offset+1][3],
                               'weekly_targets': list(sheet[offset+3][1:]), 'weekly_values': list(sheet[offset+4][1:])}
        value = {'month': business_day(published_day)[:7], 'published_day': published_day, 'metrics': metrics}
    if value.get('month') != day[:7] or not isinstance(value.get('metrics'), dict) or value.get('published_day', day) > day:
        raise PendingInputs(['周目标和历史发布值与统计月份或采用日期不一致'])
    return value


def sales_rows(root, basis, cancel):
    if basis['sales']:
        seen = {}
        for ref in basis['sales']:
            blocks = defaultdict(hashlib.sha256)
            products = defaultdict(set)
            for row, line in sheet_rows(path_for(root, ref), SALES_COLUMNS, sheets=ref['sheets'],
                                        multiple=True, optional={'store_name': ('仓库/门店名称', '门店名称')},
                                        cancel=cancel):
                code = row['store'] = identifier(row['store'])
                row['sku'] = identifier(row['sku'])
                if row['sku'] in products[code]:
                    raise ValueError(f'{ref["name"]} / {line} 门店 {code} 商品编码重复：{row["sku"]}')
                products[code].add(row['sku'])
                row['status'] = text(row['status'])
                blocks[code].update(encoded(row).encode('utf-8') + b'\n')
                if code not in seen:
                    yield {**row, 'source': ref['name'] + ' / ' + line}
            for code, state in blocks.items():
                actual = state.hexdigest()
                if code in seen and seen[code] != actual:
                    raise ValueError('同一门店的两份底稿不同：' + code + ' / ' + ref['name'])
                seen[code] = actual
        return
    from stockout_repository import Repository
    with Repository(root).connect() as db:
        db.execute('BEGIN')
        for ref in basis['store_snapshots']:
            count = 0
            for row in db.execute('''
                SELECT sku,name,month_raw AS month,stock_raw AS stock,inbound_raw AS inbound,replen_status AS status,rownum
                FROM fact_records WHERE snapshot=? ORDER BY rownum
            ''', (ref['id'],)):
                checkpoint(cancel)
                count += 1
                yield {**dict(row), 'store': ref['store'], 'store_name': ref['name'],
                       'source': f'{ref["id"]}!{row["rownum"]}'}
            if count != ref['rows']:
                raise PendingInputs(['冻结的门店快照缺失或行数变化：' + ref['store']])


def load_catalog(root, wh, ref, cancel):
    if 'file' in ref:
        src = ref['file']
        rows = sheet_rows(path_for(root, src), CATALOG_COLUMNS, sheets=src['sheets'], cancel=cancel)
    else:
        from warehouse_stockout import Repository
        def snapshots():
            with Repository(root).connect() as db:
                for r in db.execute('SELECT sku,status,stock,virtual_tags AS virtual,category,supplier FROM facts WHERE snapshot=?',
                                    (ref['id'],)):
                    yield dict(r), ref['id']
        rows = snapshots()
    products = {}
    for row, location in rows:
        checkpoint(cancel)
        sku = identifier(row.pop('sku'))
        if sku in products:
            raise ValueError(f'{wh} 商品编码重复：{sku}（{location}）')
        products[sku] = {'status': text(row['status']), 'stock': decimal(row['stock']),
                         'virtual': text(row['virtual']), 'category': text(row['category']), 'supplier': text(row['supplier'])}
    if not products or 'file' not in ref and len(products) != ref['rows']:
        raise PendingInputs([wh + ' 冻结库存快照不可读取'])
    return products


def prepare(root, basis, progress, cancel, *, order_report=None):
    """Build projections from frozen identifiers and pin all warehouse-order revisions."""
    import order_analytics
    from analytics_template_export import _quantity

    if basis['template'] != VERSION:
        raise ValueError('原运行的日报模板版本待核')
    progress('读取两仓库存与经营资料')
    files = {key: path_for(root, ref) for key, ref in basis['files'].items()}
    def rows(key, columns, sheets=None, optional=None):
        return sheet_rows(files[key], columns, sheets=basis['files'][key]['sheets'] if sheets is None else sheets,
                          optional=optional, cancel=cancel)
    stores = {row['code']: row for row in basis['stores']}
    names = {row['name'] for row in stores.values()} | {row['name'] for row in basis['store_snapshots']}
    blacklist, unresolved = set(), []
    for row, location in rows('blacklist', {'name': ('门店名称',), 'sku': ('商品编码', 'SKU编码')}):
        name = row['name']
        try:
            code = identifier(row['sku'])
            if not name:
                raise ValueError('门店名称缺失')
        except ValueError as exc:
            problem = f'{location} · {name or "门店未知"} · {exc}'
            if not name or basis['sales'] or name in names:
                raise PendingInputs(['地采黑名单：' + problem]) from exc
            unresolved.append(problem)
        else:
            blacklist.add((str(name), code))
    owners = {}
    month = str(date.fromisoformat(basis['day']).month) + '月'
    for row, line in rows('owners', {'category': ('分类', '产品分类'), 'owner': (month,)}):
        category, owner = text(row['category']), text(row['owner'])
        if category in owners and owner != owners[category]:
            raise ValueError(f'{line} 类目有多个采购负责人：{category}')
        owners[category] = owner
    required = {'code': ('门店编码',), 'name': ('门店名称',), 'group': ('运营归属',),
                'owner': ('运营负责人',), 'rate': ('必卖清单·剔除大仓缺货率',)}
    must_sell = {}
    for row, line in rows('must_sell', required, basis['files']['must_sell']['sheets'] or ('门店明细',),
                          optional={'tag': ('门店标签', '标签')}):
        code = identifier(row['code'])
        if code in must_sell:
            raise ValueError(f'{line} 必卖外表门店重复：{code}')
        must_sell[code] = {**row, 'tag': text(row.get('tag')), 'rate': decimal(row['rate'])}
    weeks = published_weeks(files['weeks'], basis['day'])
    catalogs = {w: load_catalog(root, w, ref, cancel) for w, ref in basis['catalogs'].items()}
    progress('按冻结截至时间读取仓单原修订')
    if order_report is None:
        order_report = order_analytics.collect(
            Path(root) / 'workbench.sqlite3',
            {'from': basis['day'], 'to': basis['day'], 'as_of': basis['order_cutoff']},
            progress, cancel, _coverage=False, _mode='business_daily')
    if basis.get('order_snapshot') not in (None, order_report['snapshot_id']):
        raise PendingInputs(['原订单修订或履约版本已变化，请核对原依据或另跑'])
    revisions = {h['number']: h.get('child_revision', h['revision']) for h in order_report['heads']}
    def orders():
        for row in order_report['rows']:
            if row['scope'] != '纳入' or basis['partial'] and row['store'] not in stores:
                continue
            demand, ordered = _quantity(row, 'return'), _quantity(row, 'payment')
            yield {**row, 'revision': revisions[row['order']], 'demand': demand, 'ordered': ordered,
                   'demand_value': row.get('order_values', {}).get('return'),
                   'ordered_value': row.get('order_values', {}).get('payment'),
                   'category': row.get('product_category'), 'supplier': row.get('upstream_supplier')}
    frozen = {**basis, 'order_snapshot': order_report['snapshot_id'],
              'order_revisions': [{k: h.get(k) for k in ('number', 'store', 'warehouse', 'revision', 'child_revision',
                                                        'source_parent_revision', 'digest')} for h in order_report['heads']]}
    return frozen, {'stores': stores, 'catalogs': catalogs, 'blacklist': blacklist, 'owners': owners, 'must_sell': must_sell,
                    'weeks': weeks, 'sales': sales_rows(root, frozen, cancel), 'orders': orders(),
                    'blacklist_outside_scope': unresolved}
