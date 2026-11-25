"""Confirmed material batches and their projections into the existing order flow."""
import hashlib
import io
import uuid
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import openpyxl

import jst_allocation as allocation
import jst_batches
import jst_orders
import lifecycle_store
import material_requests as requests
import material_stock
import normal_orders
import order_lifecycle as lifecycle
import source_archive
import warehouse_catalog
import warehouse_execution
import work_repository as wr
from runtime_support import checkpoint


def schema(db):
    requests.schema(db)
    db.execute('''CREATE TABLE IF NOT EXISTS material_batches(
        id TEXT PRIMARY KEY, created TEXT NOT NULL, state TEXT NOT NULL, digest TEXT NOT NULL,
        preview TEXT, result TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS material_claims(
        oid TEXT NOT NULL, warehouse TEXT NOT NULL, request TEXT NOT NULL, version INTEGER NOT NULL,
        sku TEXT NOT NULL, quantity TEXT NOT NULL, position INTEGER NOT NULL, retired INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(oid,warehouse,request,sku))''')
    db.execute('CREATE INDEX IF NOT EXISTS material_claim_request ON material_claims(request,sku)')


def _claimed(db):
    if not material_stock.has_material(db) or not jst_orders.exists(db):
        return {}
    cancellations = db.execute("SELECT 1 FROM sqlite_master WHERE name='jst_cancellations'").fetchone()
    redo = ("EXISTS(SELECT 1 FROM jst_cancellations x WHERE x.oid=c.oid AND x.warehouse=c.warehouse "
            "AND x.successor IS NOT NULL)" if cancellations else '0')
    sql = f'''SELECT c.oid,c.warehouse,c.request,c.sku,c.quantity,c.retired,a.gid,r.quantity,{redo}
              FROM material_claims c LEFT JOIN jst_active a ON a.oid=c.oid AND a.warehouse=c.warehouse
              LEFT JOIN jst_reservations r ON r.gid=a.gid AND r.sku=c.sku
              ORDER BY c.oid,c.warehouse,c.position'''
    available, claimed = {}, {}
    for oid, wh, request, sku, qty, retired, gid, active_qty, has_redo in db.execute(sql):
        qty = Decimal(qty)
        if gid:
            key = oid, wh, sku
            balance = available.setdefault(key, Decimal(active_qty or '0'))
            qty = min(balance, qty)
            available[key] = balance-qty
        elif not (retired or has_redo):
            continue
        key = request, sku
        claimed[key] = claimed.get(key, Decimal(0)) + qty
    return claimed


def available_requests(path):
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        claimed = _claimed(db)
        result = requests.read(db)
        for request in result:
            for line in request['lines']:
                used = claimed.get((request['id'], line['sku']), Decimal(0))
                line['claimed'] = allocation.text(used)
                line['remaining'] = allocation.text(max(Decimal(line['quantity'])-used, Decimal(0)))
        return result


def _units(db, catalog, wanted, cancel):
    sha = catalog.get('source_sha256')
    retained = source_archive.read(db, sha) if sha else None
    source = Path(catalog.get('source_path') or '.')
    raw = retained[1] if retained else source.read_bytes() if sha and source.is_file() else None
    if raw is None or hashlib.sha256(raw).hexdigest() != sha:
        return {}
    book = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    try:
        sheet = catalog.get('sheet')
        if sheet not in book:
            return {}
        cells = book[sheet].values
        titles = list(next(cells, ()))
        if titles.count('商品编码') != 1 or titles.count('商品单位') != 1:
            return {}
        sku_column, unit_column = titles.index('商品编码'), titles.index('商品单位')
        found = {}
        for i, values in enumerate(cells, 2):
            checkpoint(cancel)
            sku, issue = requests.input_adapter.identifier(values[sku_column])
            unit = values[unit_column]
            if not issue and sku in wanted and catalog['items'][sku].get('_row') == i and isinstance(unit, str) and unit.strip():
                found[sku] = unit.strip()
        return found
    finally:
        book.close()


def _selection(db, selections):
    by_id = {r['id']: r for r in requests.read(db)}
    claimed = _claimed(db)
    used = set()
    stores = {}
    for choice in selections:
        key = choice['request'], choice['sku']
        if key in used:
            raise ValueError('同一物料申请行只能选择一次')
        used.add(key)
        request = by_id.get(key[0])
        if not request or request['version'] != choice['version']:
            raise wr.Conflict('原物料申请已更正，请核对新版本')
        line = next((r for r in request['lines'] if r['sku'] == key[1]), None)
        if not line:
            raise wr.Conflict('原申请已无商品 '+key[1])
        remaining = max(Decimal(line['quantity'])-claimed.get(key, Decimal(0)), Decimal(0))
        demand = remaining if choice.get('quantity') is None else allocation.number(choice['quantity'], '核对量')
        if demand > remaining:
            raise wr.Conflict(f'{request["store"]} {key[1]} 核对量超过当前申请未分配量 {allocation.text(remaining)}')
        source = {**request['source'], 'row':line['row']} if request['source'] else {}
        row = {**line, 'request': request['id'], 'version': request['version'], 'request_digest':request['digest'],
               'store':request['store'], 'warehouse':request['warehouse'], 'source':source,
               'remaining':allocation.text(remaining), 'demand':allocation.text(demand)}
        stores.setdefault(request['store'], []).append(row)
    return [row for rows in stores.values() for row in rows]


def _calculate(db, selections, stocks, units, cancel):
    selected = _selection(db, selections)
    balances, conflicts = {}, {}
    for wh, baseline in stocks.items():
        catalog = baseline['catalog']
        wanted = {row['sku'] for row in selected if row['warehouse'] == wh and Decimal(row['demand'])}
        cross = material_stock.cross_reservations(db, wh, baseline, wanted, material=True)
        conflicts[wh] = cross['uncertain']
        balances[wh] = {}
        for sku in wanted:
            item = catalog['items'].get(sku, {})
            try:
                stock = allocation.number(item.get('可用数'), '可用库存')
            except ValueError:
                continue
            balances[wh][sku] = stock - Decimal(cross['reserved'].get(sku, '0'))
    result = []
    for line in selected:
        checkpoint(cancel)
        sku, wh, demand = line['sku'], line['warehouse'], line['demand']
        catalog = stocks.get(wh, {}).get('catalog', {})
        items = catalog.get('items', {})
        item = items.get(sku, {})
        unknown = conflicts.get(wh, {}).get(sku, [])
        issue = ('尚未采用本仓商品资料' if not catalog else
                 '旧执行是否已反映在本版可用数中待核 '+', '.join(unknown) if unknown else '')
        inputs = dict(sku=sku, base_quantity=demand, name=item.get('商品名称'),
                      sources=[line['source']] if line['source'] else [], _admission_error=issue)
        row = allocation.allocate([inputs], items, balances.get(wh, {}), material=True)
        allocation_row = row[0] if row else dict(quantity='0', missing='0', issue=False, reason='本次核对量为 0',
                                                 stock=None, before=None, after=None, amount=None, price=None, step=None)
        if allocation_row['stock'] is None:
            try:
                allocation_row['stock'] = allocation.text(allocation.number(item.get('可用数'), '可用库存'))
            except ValueError:
                pass
        unit = units.get(wh, {}).get(sku)
        result.append({**line, **allocation_row, 'requested':line['quantity'], 'name':item.get('商品名称') or '',
                       'status':item.get('商品状态'), 'unit':unit or '库存单位',
                       'unit_basis':'仓原件商品单位' if unit else '仓可用数基础单位', 'catalog_revision':catalog.get('revision')})
    return result


def preview(path, selections, *, previous=None, cancel=None):
    if not selections:
        raise ValueError('请勾选要分配的物料需求行')
    if previous and previous['selections'] != selections:
        raise wr.Conflict('继续原批须沿用原需求范围和店序')
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        rows = _selection(db, selections)
        if previous:
            value = dict(previous)
        else:
            catalogs, versions = warehouse_catalog.Repository(Path(path).parent).snapshots()
            batch_id, created = uuid.uuid4().hex, wr.now()
            stocks, units = {}, {}
            for wh in dict.fromkeys(row['warehouse'] for row in rows):
                catalog = catalogs.get(wh)
                if catalog:
                    base = dict(bid=uuid.uuid4().hex, warehouse=wh, catalog=catalog, created=created,
                                adoption=versions.get(wh), batch_id=batch_id, included=[])
                    base['included'] = sorted(material_stock.included(db, wh, base))
                    stocks[wh] = base
                    units[wh] = _units(db, catalog, {r['sku'] for r in rows if r['warehouse']==wh and r['sku'] in catalog['items']}, cancel)
            value = dict(id=batch_id, created=created, day=created[:10], selections=selections, stocks=stocks, units=units)
        value['rows'] = _calculate(db, selections, value['stocks'], value['units'], cancel)
    return {'payload':value, 'digest':jst_orders.digest(value)}


def pending(path):
    with closing(wr.read_connection(path)) as db:
        if not material_stock.has_material(db):
            return []
        return [jst_orders.decode(row) for row in db.execute("SELECT preview,digest FROM material_batches WHERE state='pending' ORDER BY rowid")]


def register(path, plan, *, accept_issues=False, cancel=None):
    value = plan['payload']
    if jst_orders.digest(value) != plan['digest']:
        raise ValueError('物料分配预览内容已变化')
    if not accept_issues and any(row['issue'] for row in value['rows']):
        raise ValueError('请核对待核行后确认仅安排可分配行')
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        lifecycle.begin(db, cancel)
        schema(db)
        old = db.execute('SELECT state,digest,preview FROM material_batches WHERE id=?', (value['id'],)).fetchone()
        if old and old[:2] == ('done', plan['digest']):
            return value['id']
        if old:
            original = jst_orders.decode((old[2],old[1])) if old[0]=='pending' else None
            if not original or any(original[k] != value[k] for k in ('stocks', 'selections', 'units', 'day', 'created')):
                raise wr.Conflict('原物料分配批次已处理或依据已变化')
        now = _calculate(db, value['selections'], value['stocks'], value['units'], cancel)
        if now != value['rows']:
            raise wr.Conflict('申请余额或共享仓库存已变化，请重新预览')
        db.execute('''INSERT INTO material_batches VALUES(?,?,'pending',?,?,NULL)
                   ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,preview=excluded.preview''',
                   (value['id'], value['created'], plan['digest'], wr.dumps(value)))
        checkpoint(cancel)
    return value['id']


def end_pending(path, batch_id):
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("UPDATE material_batches SET state='ended' WHERE id=? AND state='pending'", (batch_id,)).rowcount != 1:
            raise wr.Conflict('此批已处理，请刷新')


def _number(value):
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def _orders(rows):
    groups = {}
    for row in rows:
        if not Decimal(row['quantity']):
            continue
        items = groups.setdefault((row['store'], row['warehouse']), {})
        sku = row['sku']
        if sku not in items:
            items[sku] = dict(row, claims=[])
        else:
            item = items[sku]
            for name in ('quantity', 'demand', 'missing', 'amount'):
                item[name] = allocation.text(Decimal(item[name])+Decimal(row[name]))
            item['after'] = row['after']
            item['return_sources'] = item['return_sources'] + row['return_sources']
        items[sku]['claims'].append({k: row[k] for k in ('request','version','request_digest','sku','requested','quantity','demand','note','source')})
    return {key:list(skus.values()) for key,skus in groups.items()}


def _save_parent(db, batch, store, name, scopes, saved, cancel):
    batch_id = batch['id']
    oid = uuid.uuid5(uuid.UUID(batch_id), store).hex
    number = f'WL-{batch["day"].replace("-", "")}-{store}-{batch_id[:12].upper()}'
    rows = []
    for wh, items in scopes:
        for item in items:
            rows.append(dict(E=item['sku'], D=item['name'], AU=wh, G=item['unit'], V=_number(item['price']),
                             _source='资料匹配', _meta={'blocking':[]}, _execution_qty=_number(item['quantity']),
                             _execution_amount=_number(item['amount']), BV=_number(item['amount']),
                             S=item['status'], _material={**item, 'batch_id':batch_id}))
    result = dict(order_kind=requests.KIND, store=store, store_name=name, rows=rows,
                  summary={'rows':len(rows)}, material_batch=batch_id)
    payload = {'result':result, 'work_revision':'material-'+batch_id}
    digest = normal_orders.fingerprint_owned(result)
    db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,1)', (batch['day'], store, requests.KIND, oid, number))
    db.execute('INSERT INTO normal_revisions VALUES(?,1,?,?,?)', (oid, digest, saved, normal_orders.encode(payload,db)))
    warehouse_execution.publish(db, oid, 1, digest, saved, payload, [wh for wh,_ in scopes], cancel=cancel)
    return oid, number, digest


def _save_generation(db, batch, head, items, position, saved, *, kind='material'):
    from lifecycle_rules import DOCUMENT_SCHEMA, RULE, reconcile

    rows = [dict(sku=r['sku'], name=r['name'], quantity=r['quantity'], base_quantity=r['quantity'], unit=r['unit'],
                 conversion_basis=r['unit_basis'], factor=None, amount=None, price=None, currency='',
                 document_number=head['number'], document_date=head['day'], note=None, sources=r['return_sources'],
                 adapter='material_request' if kind=='material' else kind, raw_headers=[], raw_values=[]) for r in items]
    products = {r['sku']:dict(sku=r['sku'], name=r['name'], quantity=r['quantity'], price=r['price'], unit=r['unit']) for r in items}
    full = dict(head, products=products)
    basis = lifecycle.upstream(db, full, 'return')
    # Reuse the common demand document slot and its dependency token. The payload
    # records actual confirmed material requests, including immutable claim references.
    payload = dict(schema=DOCUMENT_SCHEMA, rule=RULE, stage='return', head=head, basis=basis, pending=False,
                   raw_rows=rows, rows=reconcile(rows, products), files=[], missing=[], extra=[], explicit_zero=False,
                   **{'material_requests' if kind=='material' else 'special_targets':[claim for item in items for claim in item['claims']]})
    digest = lifecycle_store.fingerprint(payload)
    key = head['oid'], head['revision'], head['warehouse']
    db.execute("INSERT INTO lifecycle_versions VALUES(?,?,?,'return',1,?,?,?)", (*key, saved, digest, wr.dumps(payload)))
    db.execute("INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) "
               "VALUES(?,?,?,?,'return',1,?,?)", (saved,*key,'确认物料申请与分配' if kind=='material' else '确认专项原行与分配',wr.dumps({'batch_id':batch['id'],'digest':digest})))
    wh = head['warehouse']
    stock = batch['stocks'][wh]
    gid = uuid.uuid5(uuid.UUID(batch['id']), head['number']).hex
    generation = dict(gid=gid, operation=f'{batch["id"]}:{position}', rule=allocation.MATERIAL_RULE if kind=='material' else 'special-stock-v1', head=head,
                      basis=lifecycle.upstream(db, full, 'payment'), bid=stock['bid'], old=None, rows=items,
                      summary=allocation.totals(items), saved=saved, position=position, catalog=warehouse_catalog.metadata(stock['catalog']),
                      draft_status='待核对付款单', batch_id=batch['id'], batch_epoch=0)
    db.execute('INSERT INTO jst_batch_members VALUES(?,?,?,?,?,?,?)',
               (batch['id'],position,head['oid'],wh,wr.dumps(head),'pending','等待本批分配'))
    jst_orders.put_generation(db, generation)
    jst_batches.record(db,generation)
    db.execute('UPDATE jst_batch_stock SET serial=serial+1 WHERE batch_id=? AND warehouse=?', (batch['id'],wh))
    return {'head':head, 'gid':gid}


def commit(path, batch_id, cancel=None):
    """One write transaction for all confirmed warehouse orders and their stock claims."""
    with closing(wr.Repository.open_existing(path).connect()) as db, db:
        lifecycle.begin(db,cancel)
        schema(db)
        row = db.execute('SELECT state,digest,preview,result FROM material_batches WHERE id=?',(batch_id,)).fetchone()
        if not row or row[0]=='ended':
            raise wr.Conflict('未找到有效物料分配批次')
        if row[0]=='done':
            return wr.loads(row[3])
        batch = jst_orders.decode((row[2],row[1]))
        if _calculate(db,batch['selections'],batch['stocks'],batch['units'],cancel) != batch['rows']:
            raise wr.Conflict('仓库存或申请余额已变化。请从物料作业恢复本批后重新核对预览')
        jst_batches.schema(db)
        lifecycle_store.schema(db)
        effective = _orders(batch['rows'])
        saved = wr.now()
        fixed = dict(batch_id=batch_id, created=batch['created'], label='物料单 '+batch['created'], preview_digest=row[1])
        if effective:
            metadata = {wh:warehouse_catalog.metadata(stock['catalog']) for wh,stock in batch['stocks'].items()}
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',
                       ('material-'+batch_id,batch['day'],normal_orders.encode(dict(catalogs=metadata),db)))
            db.execute('INSERT INTO jst_batches VALUES(?,?,?,?,?,?,?)',
                       (batch_id,saved,fixed['label'],'complete',0,wr.dumps(fixed),jst_orders.digest(fixed)))
            for wh in dict.fromkeys(wh for _,wh in effective):
                base = batch['stocks'][wh]
                db.execute('INSERT INTO jst_baselines VALUES(?,?,?,?,?)',(base['bid'],wh,base['created'],wr.dumps(base),jst_orders.digest(base)))
                db.execute('INSERT INTO jst_batch_stock VALUES(?,?,?,0)',(batch_id,wh,base['bid']))
        parent_scopes = {}
        for (store,wh),items in effective.items():
            parent_scopes.setdefault(store,[]).append((wh,items))
        parents = {}
        for store,scopes in parent_scopes.items():
            checkpoint(cancel)
            raw = db.execute('SELECT data FROM stores WHERE code=?',(store,)).fetchone()
            if not raw:
                raise wr.Conflict('门店资料已删除 '+store)
            name = wr.loads(raw[0]).get('name') or store
            parents[store] = (*_save_parent(db,batch,store,name,scopes,saved,cancel),name)
        result = dict(batch=batch_id, orders=[], rows=batch['rows'])
        for position, ((store,wh), items) in enumerate(effective.items(),1):
            checkpoint(cancel)
            oid,parent,digest,name = parents[store]
            head = warehouse_execution._head(dict(oid=oid,day=batch['day'],store=store,name=name,
                                                  number=parent,kind=requests.KIND), wh, 1, digest, saved)
            head['visibility_token'] = 0
            result['orders'].append(_save_generation(db, batch, head, items, position, saved))
            for seq, claim in enumerate((c for item in items for c in item['claims']),1):
                db.execute('INSERT INTO material_claims VALUES(?,?,?,?,?,?,?,0)',
                           (oid,wh,claim['request'],claim['version'],claim['sku'],claim['quantity'],seq))
        db.execute("UPDATE material_batches SET state='done',preview=NULL,result=? WHERE id=?",
                   (wr.dumps(result),batch_id))
        checkpoint(cancel)
        return result
