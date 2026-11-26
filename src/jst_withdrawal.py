"""Read-only checks and confirmed release of an unfulfilled JST allocation."""
from contextlib import closing
import re

import jst_auto_order as automatic
import jst_auto_order_store as tasks
import jst_order_store as allocations
import lifecycle_facts
import work_repository as wr
from export_progress import RowProgress
from jst_order_guards import saved_execution_reason
from payment_evidence import same_head
from runtime_support import checkpoint, CommitProgress
from sqlite_write import begin, commit
from warehouse_execution import validate_visibility

UNSENT_STAGES = ('PREPARED', 'PREVIEWED')


def _local(db, head):
    validate_visibility(db, head)
    gid = allocations.active(db, head)
    if not gid:
        raise automatic.Check('当前没有有效生成单，旧单可能已撤销')
    generation, digest = allocations.read_generation(db, gid)
    if not same_head(generation['head'], head):
        raise automatic.Check('所选签发版本与原生成单不同，请刷新')
    saved = tasks.read_warehouse_order(db, head)
    if not saved:
        raise automatic.Check('缺少原导入任务，无法核实线上单号、店铺或发送结果')
    order = tasks.read(db, saved['online'], workbook=True)
    del order['workbook']  # Only keep the verified metadata while querying the platform.
    payload = order['payload']
    if order['gid'] != gid or payload['generation']['digest'] != digest or not same_head(payload['head'], head):
        raise automatic.Check('原导入任务与当前生成单不一致，请核对原分配')
    reserved = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=? ORDER BY sku', (gid,)))
    stamp = {'gid': gid, 'batch_id': generation.get('batch_id'), 'generation_digest': digest,
             'sequence': order['sequence'], 'order_digest': order['digest'],
             'file_sha': order['file_sha'], 'reserved': reserved}
    return order, stamp


def _ending(order):
    """Return the persisted proof that the latest attempt ended without an order."""
    proof = order['proof'] or {}
    receipt = proof.get('import_receipt')
    unsent = proof.get('import_not_sent')
    if tasks.manual_pending(order) or order['stage'] == 'MANUAL_HANDOFF':
        return None, '人工接管结果待核，缺少人工导入已结束且未成单的凭据'
    if order['internal'] or order['platform_status']:
        return None, '已有平台关联，需核对原单并确认已取消'
    if order['stage'] in UNSENT_STAGES and not (receipt or unsent):
        return {'kind': '本地任务尚未发送', 'stage': order['stage'], 'observed_at': order['updated']}, ''
    if order['stage'] in ('IMPORT_UNKNOWN', 'IMPORT_FAILED') and proof.get('file_sha') == order['file_sha']:
        counts = automatic._receipt_counts(receipt)
        if counts == (0, 1) and not unsent and receipt.get('online') == order['online']:
            return {'kind': '平台最终失败回执', 'receipt': receipt}, ''
        if counts == (1, 0) and receipt.get('online') == order['online']:
            return None, '平台回执已受理，需核对原单及内部单号'
        if (not receipt and isinstance(unsent, dict) and unsent.get('online') == order['online']
                and unsent.get('file_sha') == order['file_sha'] and unsent.get('binding') == proof.get('binding')
                and str(unsent.get('reason') or '').strip()
                and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{3})?Z',
                                 str(unsent.get('observed_at') or ''))):
            return {'kind': '最终导入未发送', 'record': unsent}, ''
    return None, '缺少已核实的未发送记录、平台最终失败回执或其他整单终止凭据'


def _release_blocker(db, head):
    reason = saved_execution_reason(db, head, check_automatic=False)
    if reason:
        return reason
    if any(lifecycle_facts.current(db, head, stage) for stage in ('jst_order', 'payment', 'receipt')):
        return '已有原单、付款或收货资料，请先核对对应环节'
    if tasks.exists(db, 'special_packages') and db.execute('SELECT 1 FROM special_packages WHERE oid=? LIMIT 1',
                                                           (head['oid'],)).fetchone():
        return '请到原专项工作台处理该执行轮次'
    return ''


def _inspect(browser, row, order, blocker):
    ending, missing = _ending(order)
    try:
        _, lookup = automatic.lookup_scoped(browser, order, allow_unbound=order['stage'] in UNSENT_STAGES)
    except ValueError as exc:
        return {**row, 'message': str(exc) + ('；' + missing if missing else '')}
    evidence = {'binding': lookup['binding'], 'online': order['online'], 'at': wr.now()}
    if lookup.get('found') is True:
        internal, status = lookup.get('internal'), lookup.get('native_status')
        if not internal or not status:
            return {**row, 'company': lookup['binding']['company'], 'message': '查到订单，内部单号或状态待核'}
        evidence.update(found=True, internal=internal, native_status=status)
        message = f'平台原单 {internal} · {status}。请在自动下单任务使用「核对取消并重做」'
        return {**row, 'company': lookup['binding']['company'], 'result': 'cancelled' if status == '已取消' else 'platform',
                'message': message, 'lookup': evidence}
    row = {**row, 'company': lookup['binding']['company']}
    if lookup.get('found') is not False or lookup.get('exact_absence') is not True:
        return {**row, 'message': '缺少原线上单号的精确零结果' + ('；' + missing if missing else '')}
    evidence.update(found=False, exact_absence=True)
    if not ending or blocker:
        return {**row, 'lookup': evidence, 'message': '精确查询 0 张；' + '；'.join(s for s in (missing, blocker) if s)}
    return {**row, 'result': 'ready', 'ending': ending, 'lookup': evidence,
            'message': ending['kind'] + '；精确查询 0 张'}


def preview(path, heads, owner, progress=lambda _: None, cancel=None):
    checked, pending = [], []
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        for head in {(h['oid'], h['warehouse']): h for h in heads}.values():
            checkpoint(cancel)
            row = {'head': head, 'online': '', 'shop': '', 'company': '', 'result': 'blocked'}
            checked.append(row)
            try:
                order, stamp = _local(db, head)
                binding = (order['proof'] or {}).get('binding') or {}
                row.update(online=order['online'], shop=order['payload']['shop']['name'],
                           company=binding.get('company', ''), stamp=stamp)
                pending.append((row, order, _release_blocker(db, head)))
            except ValueError as exc:
                row['message'] = str(exc)
    if pending:
        try:
            with automatic.browser_session(owner, progress, cancel) as browser:
                for i, (row, order, blocker) in enumerate(pending, 1):
                    checkpoint(cancel)
                    progress(RowProgress('回查原单 ' + automatic.show_name(row['head']), i, len(pending)))
                    row.update(_inspect(browser, row, order, blocker))
        except automatic.Check as exc:
            for row, order, _ in pending:
                if 'message' not in row:
                    _, missing = _ending(order)
                    row['message'] = str(exc) + ('；' + missing if missing else '')
    return checked


def withdraw(path, approved, reason, owner, progress=lambda _: None, cancel=None):
    """Recheck only the approved orders, then archive and release them atomically."""
    from pathlib import Path
    import jst_orders
    import storage_chunks

    reason = reason.strip()
    if not approved or not reason or any(r['result'] != 'ready' for r in approved):
        raise automatic.Check('请先核对可撤销清单并填写撤销原因')
    approvals = {r['online']: r for r in approved}
    observed = preview(path, [r['head'] for r in approved], owner, progress, cancel)
    for row in observed:
        prior = approvals.get(row['online'])
        if (row['result'] != 'ready' or not prior or row['stamp'] != prior['stamp']
                or row['ending'] != prior['ending'] or row['lookup']['binding'] != prior['lookup']['binding']):
            raise wr.Conflict(row['head']['number'] + '：核对结果已变化，本次未撤销。' + row['message'])
    if len(observed) != len(approved):
        raise wr.Conflict('确认清单范围已变化，请重新核对')

    storage_chunks.backup_before_write(Path(path), progress)
    with closing(tasks.connection(path)) as db, db:
        begin(db, cancel)
        for row in observed:
            checkpoint(cancel)
            head = row['head']
            order, stamp = _local(db, head)
            if stamp != row['stamp'] or order['online'] != row['online'] or _release_blocker(db, head):
                raise wr.Conflict(head['number'] + '：原任务、占用或下游已变化，请重新核对')
            evidence = {**stamp, 'reason': reason, 'ending': row['ending'], 'lookup': row['lookup'],
                        'initial_lookup': approvals[row['online']]['lookup']}
            tasks.archive_withdrawn(db, order, {**(order['proof'] or {}), 'withdrawal': evidence})
            jst_orders.release_generation(db, head, stamp['gid'], stamp['batch_id'], reason,
                                          online=order['online'], evidence=evidence)
        progress(CommitProgress(True))
        commit(db, cancel)
    progress(CommitProgress(False))
    return observed


def released_warehouses(db, heads):
    """Identify the selected reallocation warehouses without affecting ordinary new batches."""
    from jst_cancellation import redo_case

    result = set()
    for head in heads:
        if allocations.active(db, head):
            continue
        if redo_case(db, head) or allocations.exists(db) and db.execute(
            """SELECT 1 FROM jst_events WHERE day=? AND oid=? AND warehouse=? AND action='撤销未下单生成单'
               AND json_extract(detail,'$.evidence.lookup.exact_absence')=1 LIMIT 1""",
            (head['day'], head['oid'], head['warehouse'])).fetchone():
            result.add(head['warehouse'])
    return result
