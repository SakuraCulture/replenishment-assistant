"""Run one explicitly approved scope through stock refresh, allocation and original-order readback."""
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import time

import catalog_sync
import jst_auto_order as automatic
import jst_auto_order_store as tasks
import jst_auto_order_template as template
import jst_batches as batches
import jst_cancellation as cancellation
import jst_fulfillment
import jst_order_store as generations
import jst_orders
import lifecycle_store
import order_lifecycle as lifecycle
import purchases
import work_repository as wr
from catalog_sync_client import ComponentError
from export_progress import RowProgress
from jst_order_guards import saved_execution_reason
from payment_evidence import same_head
from runtime_support import checkpoint


def _key(head):
    return head['oid'], head['warehouse']


def _result(head, status, message, **details):
    return {'head': head, 'status': status, 'message': message, **details}


def _verified_binding(db, head, shop, profile, cancel):
    """A prior successful native preview is evidence for the same saved inputs."""
    if tasks.exists(db):
        sources = 'SELECT online,oid,warehouse,proof,updated,rowid FROM jst_auto_orders'
        if tasks.exists(db,tasks.HISTORY):
            sources += ' UNION ALL SELECT online,oid,warehouse,proof,updated,rowid FROM '+tasks.HISTORY
        rows = db.execute(f"""SELECT a.online FROM ({sources}) a JOIN normal_orders o ON o.oid=a.oid
            WHERE o.store=? AND a.warehouse=? AND a.proof IS NOT NULL ORDER BY a.updated DESC,a.rowid DESC""",
                          (head['store'],head['warehouse']))
        for online, in rows:
            checkpoint(cancel)
            saved = tasks.read(db, online)
            payload, proof = saved['payload'], saved['proof'] or {}
            binding = proof.get('binding') or {}
            if ('order_phone' not in payload and payload['head']['store'] == head['store']
                    and payload['head']['warehouse'] == head['warehouse']
                    and payload['shop']['name'] == shop['name']
                    and payload['profile']['values'] == profile['values']
                    and proof.get('file_sha') == saved['file_sha']
                    and binding.get('warehouse') == head['warehouse'] and binding.get('shop') == shop['name']
                    and all(binding.get(k) for k in ('company', 'coid', 'uid', 'shop_id'))
                    and binding.get('version') is not None):
                return binding
    raise automatic.Check('当前配送资料和店铺映射尚未通过平台预览，请先用原入口核验一次')


def _configuration(db, head, cancel):
    shop = automatic._shop(db, head)
    profile = tasks.profile(db, head['store'], head['warehouse'])
    values = (profile or {}).get('values', {})
    missing = [label for field, (_, label) in template.CONTACT.items() if not values.get(field)]
    if missing:
        raise automatic.Check('配送资料缺少' + '、'.join(missing))
    template.validate_phone(values)
    binding = _verified_binding(db, head, shop, profile, cancel)
    return {'shop': shop, 'profile_version': profile['version'], 'binding': binding}


def _prepare_entry(path, db, head, pending, cancel):
    purchases.validate_head(db, head)
    original = tasks.read_warehouse_order(db, head)
    if original and original['stage'] == 'CANCELLED':
        raise automatic.Check('原单已取消，请先使用「核对取消并重做」')
    if original and original['stage'] in tasks.RELEASED:
        if not same_head(original['payload']['head'], head):
            raise automatic.Check('已有其他签发版本的下单记录，请核对原任务')
        paid = lifecycle.current(db, head, 'payment')
        redo = cancellation.redo_case(db, head)
        if paid and not paid['payload'].get('pending') and not (redo and redo['successor']==original['online']):
            raise automatic.Check('该单已保存正式付款，请进入后续环节')
        return {'head': head, 'mode': 'recover', 'online': original['online'], 'sequence': original['sequence'],
                'shop': original['payload']['shop']}
    blocker = saved_execution_reason(db, head, allow_redo=True)
    if blocker:
        raise automatic.Check(blocker + '，请核对原订单')
    if _key(head) in pending:
        raise automatic.Check('已有未完成分配批次 '+pending[_key(head)][:8]+'，请先继续原批')
    gid = generations.active(db, head)
    if gid:
        generation, digest = generations.read_generation(db, gid)
        if not same_head(generation['head'], head):
            raise automatic.Check('原生成单的签发修订已变化，请按原批核对')
        automatic._generation_proof(db, generation, digest)
        basis = generation['basis']
    else:
        full = {**head, 'products': {}}
        if not (lifecycle_store.exists(db) and lifecycle_store.latest_version(db, head, 'return')):
            full = lifecycle.full_head(path, head)
        basis, demand, _, _ = jst_orders.return_basis(db, full)
        if not any(Decimal(row['base_quantity']) > 0 for row in demand):
            raise automatic.Check('确认无要货，无需分配和下单')
        if batches.exists(db) and db.execute(
                """SELECT 1 FROM jst_batch_members
                   WHERE oid=? AND warehouse=? AND status='done' AND message=?
                     AND json_extract(head,'$.revision')=? LIMIT 1""",
                (*_key(head), batches.ZERO_OUTCOME, head['revision'])).fetchone():
            raise automatic.Check('原批已保存零分配；需要再次分配时请先核对原批')
    config = _configuration(db, head, cancel)
    return {'head': head, 'mode': 'order' if gid else 'allocate', 'gid': gid, 'basis': generations.digest(basis),
            'online': (original or {}).get('online'), 'sequence': (original or {}).get('sequence'), **config}


def preflight(path, heads, progress=lambda _: None, cancel=None):
    """Freeze the selected signed order, upstream facts and exact setup before confirmation."""
    from platform_order_scope import require_single
    require_single(path,heads)
    if not heads or len({_key(h) for h in heads}) != len(heads):
        raise automatic.Check('请明确勾选需要作业的仓单，每张仓单只选一次')
    ready, skipped = [], []
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        pending = {_key(m['head']): group['batch_id'] for group in batches._unfinished(db, heads, cancel)
                   for m in group['selected']}
        for i, head in enumerate(heads, 1):
            checkpoint(cancel)
            progress(RowProgress('读取已验证配送与店铺关系', i, len(heads)))
            try:
                ready.append(_prepare_entry(path, db, head, pending, cancel))
            except ValueError as exc:
                skipped.append(_result(head, '待核', str(exc)))
    return {'ready': ready, 'skipped': skipped}


def _unchanged(path, selected, results, progress, cancel):
    checked = preflight(path, [r['head'] for r in selected], progress, cancel)
    current = {_key(r['head']): r for r in checked['ready']}
    results.update((_key(r['head']), r) for r in checked['skipped'])
    unchanged = []
    for entry in selected:
        head = entry['head']
        key = _key(head)
        if current.get(key) == entry:
            unchanged.append(entry)
        elif key not in results:
            results[key] = _result(head, '待核', '确认后仓单、回传、原任务或配送与店铺资料已变化，请重新检查')
    return unchanged


def _same_account(account, binding):
    return account.get('mappingVerified') is True and all(
        account.get(k) == binding[v] for k, v in (
            ('warehouse', 'warehouse'), ('expectedCompany', 'company'),
            ('expectedCoid', 'coid'), ('expectedUid', 'uid'), ('bindingVersion', 'version')))


def _refresh(path, lease, warehouse, binding, progress, cancel):
    progress('获取'+warehouse+'最新库存')
    value = catalog_sync.run_sync(lease, Path(path).parent, path, [warehouse], 'allocation_baseline',
                                  progress=progress, cancel=cancel)
    checkpoint(cancel)
    batch = value['batch']
    if batch['purpose'] != 'allocation_baseline' or len(batch['items']) != 1:
        raise automatic.Check('库存刷新回执的仓或用途不符')
    item, = batch['items']
    receipt = item.get('adoption') or {}
    if (not value['stopped'] and not item.get('retirement') and _same_account(item['account'], binding)
            and receipt.get('warehouse') == warehouse and receipt.get('purpose') == 'allocation_baseline'
            and all(receipt.get(k) for k in ('apply_id', 'revision', 'source_sha256'))):
        return receipt
    reason = item.get('adoption_error') or (item.get('error') or {}).get('message')
    raise automatic.Check(reason or '未取得本次库存的有效采用回执，请在仓资料采集记录核对原任务')


def _allocate(path, selected, receipts, results, progress, cancel):
    progress('按本批刷新库存核对分配')
    try:
        plan = batches.preview_new(path, [r['head'] for r in selected], progress=progress, cancel=cancel,
                                   stock_receipts=receipts)
        entries = {_key(r['head']): r for r in selected}
        planned = {_key(p['head']): p for p in plan['payload']['plans']}
        if planned.keys() != entries.keys() or any(
                generations.digest(p['basis']) != entries[k]['basis'] for k, p in planned.items()):
            raise automatic.Check('确认范围或回传依据已变化，请重新核对本批')
        issues = [p['head']['number'] for p in planned.values() if p['summary']['issues']]
        if issues:
            raise automatic.Check('本批有商品资料待核，请走原分配预览处理：'+'、'.join(issues))
        # Register creates the active batch. Skip reactivation so another batch
        # cannot be silently paused between registration and commit.
        saved = batches.execute(path, plan, progress=progress, cancel=cancel, background=True)
    except ValueError as exc:
        for entry in selected:
            results[_key(entry['head'])] = _result(entry['head'], '分配待核', str(exc))
        return {}
    checkpoint(cancel)
    by_key = {}
    for key, p in planned.items():
        head = p['head']
        if p['gid'] in saved['generated'] and not saved.get('finalization_pending'):
            by_key[key] = p['gid']
        elif head['number'] in saved['zero']:
            results[key] = _result(head, '分配为0', '本批分配已保存，无可下单商品', batch_id=saved['batch_id'])
        else:
            message = saved.get('recovery_warning') or '本批分配未完成，请继续原分配批次'
            results[key] = _result(head, '分配待核', message, batch_id=saved['batch_id'])
    return by_key


def _order(path, entry, gid, browser, progress, cancel):
    head = entry['head']
    if entry['mode'] == 'recover':
        online = entry['online']
        status, = automatic.process(path, [online], browser, 'recover', progress, cancel,
                                    approved={online: entry['sequence']}, prepare_payment=jst_fulfillment.pull_order_payment)
    else:
        prepared = automatic.prepare(path, [{'head': head, 'gid': gid}], progress, cancel,
                                     expected_setup=entry)
        if not prepared['ready']:
            raise automatic.Check(prepared['failed'][0]['reason'])
        online, = prepared['ready']
        with closing(wr.read_connection(path)) as db:
            order = tasks.read(db, online)
        if (not order or order['stage'] in tasks.RELEASED or order['gid'] != gid
                or 'order_phone' in order['payload']
                or order['payload']['shop'] != entry['shop']
                or order['payload']['profile']['version'] != entry['profile_version']):
            raise automatic.Check('原任务或配送与店铺资料已变化，请核对原任务')
        preview, = automatic.process(path, [online], browser, 'preview', progress, cancel,
                                     approved={online: order['sequence']}, expected_binding=entry['binding'])
        if not preview['ok'] or preview['stage'] != 'PREVIEWED':
            return _result(head, '预览待核', preview['reason'], online=online)
        checkpoint(cancel)
        status, = automatic.process(path, [online], browser, 'submit', progress, cancel,
                                    approved={online: preview['sequence']}, prepare_payment=jst_fulfillment.pull_order_payment)
    payment = status.get('payment') or {}
    if status.get('internal') and status['ok'] and status['stage'] in ('IMPORTED','AUDITED'):
        with closing(wr.read_connection(path)) as db:
            db.execute('BEGIN')
            redo = cancellation.redo_case(db,head)
            paid = lifecycle.current(db,head,'payment') if redo else None
            if redo and redo['successor'] == online and paid and not paid['payload'].get('pending'):
                reason, _ = cancellation.payment_state(db, head, paid)
                if reason:
                    return _result(head, '付款待重新核对', reason, online=online, internal=status['internal'])
    label = ('待人工确认付款' if payment.get('state') == 'READY' else
             '已查回原单，付款资料待核' if status.get('internal') and status['ok'] else
             '导入失败' if status['stage']=='IMPORT_FAILED' else
             '导入结果待核' if status['stage'] in tasks.RELEASED else '下单待核')
    return _result(head, label, payment.get('message') or status['reason'],
                   online=online, internal=status.get('internal'))


def execute(path, approved, owner, progress=lambda _: None, cancel=None):
    """Use existing durable receipts. A released task always follows the original order."""
    results = {}
    selected = _unchanged(path, approved, results, progress, cancel)
    if not selected:
        return [results[_key(r['head'])] for r in approved]
    checkpoint(cancel)
    try:
        lease = owner.acquire()
    except ComponentError as exc:
        raise automatic._client_error(exc) from None
    try:
        lease.start()
        browser = automatic.Browser(lease)
        accounts = {a['warehouse']: a for a in lease.request('sync.status')['accounts']}
        new_by_warehouse = {}
        for entry in selected:
            head = entry['head']
            wh = head['warehouse']
            if entry['mode'] == 'recover':
                continue
            account = accounts.get(wh, {})
            if not _same_account(account, entry['binding']):
                results[_key(head)] = _result(head, '待核', '仓账号或绑定版本已变化，请核验本仓及店铺')
            elif entry['mode'] == 'allocate':
                new_by_warehouse.setdefault(wh, []).append(entry)
        receipts = {}
        for wh, entries in new_by_warehouse.items():
            try:
                checkpoint(cancel)
                if not accounts[wh].get('enabled'):
                    raise automatic.Check('该仓自动同步尚未启用')
                receipts[wh] = _refresh(path, lease, wh, entries[0]['binding'], progress, cancel)
            except (ValueError, ComponentError) as exc:
                for entry in entries:
                    head = entry['head']
                    results[_key(head)] = _result(head, '库存刷新待核', str(exc))
        pending = [r for r in selected if _key(r['head']) not in results]
        if receipts:
            pending = _unchanged(path, pending, results, progress, cancel)
        new = [r for r in pending if r['mode'] == 'allocate']
        needed = {r['head']['warehouse'] for r in new}
        gids = _allocate(path, new, {w: receipts[w] for w in needed}, results, progress, cancel) if new else {}
        for entry in pending:
            checkpoint(cancel)
            head = entry['head']
            key = _key(head)
            if key in results:
                continue
            try:
                results[key] = _order(path, entry, gids.get(key, entry.get('gid')), browser, progress, cancel)
            except ValueError as exc:
                results[key] = _result(head, '原任务待核', str(exc))
    except ComponentError as exc:
        raise automatic._client_error(exc) from None
    finally:
        while not lease.close(timeout=.5):
            progress('等待聚水潭原动作安全结束')
            time.sleep(.2)
    return [results[_key(r['head'])] for r in approved]
