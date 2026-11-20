"""Pull allocation originals into a pending review; payment remains explicit.

Native order quantity is not assumed to be allocation quantity. This adapter
requires an explicit allocation/shipped quantity column, complete SKU coverage,
and the frozen company/shop/order/generation identity.
"""
from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import re
from uuid import uuid4

import openpyxl
import jst_audited_payment as audited
import jst_auto_order as automatic
import jst_auto_order_store as orders
import jst_fulfillment_store as sources
import jst_order_store as generations
import order_lifecycle as lifecycle
import purchase_io
import purchases
import work_repository as wr
from payment_evidence import HEAD_KEYS, same_head
from runtime_support import checkpoint, Cancelled
from sqlite_write import begin
import jst_customer_audit as customer_audit


QTY = ('实配数量', '实际配货数量', '实发数量', '实际发货数量', '出库数量')
AMOUNT = ('实配金额', '实际配货金额', '实发金额', '实际发货金额', '出库金额', '商品金额')
FINAL = frozenset(('已发货',))


class Browser(automatic.Browser):
    def __init__(self, lease, retained_only=False):
        super().__init__(lease)
        self.retained_only = retained_only

    def request(self, action, **values):
        from catalog_sync_client import ComponentError
        try:
            return self.lease.request('order.' + action, timeout=180, **values)
        except ComponentError as exc:
            if self.retained_only and exc.code in ('SESSION_NOT_RETAINED', 'LOGIN_REQUIRED'):
                spec = values['spec']
                return {'binding': spec['binding'], 'online': spec['online'], 'internal': spec['internal'],
                        'state': 'WAIT_CONNECTION', 'message': '等待该仓的登录连接空闲并通过核验'}
            if exc.code.startswith('ALLOCATION_'):
                raise automatic.PlatformCheck(str(exc)) from None
            raise automatic._client_error(exc) from None

    def action(self, action, order, **extra):
        if action not in ('allocation', 'payment_detail'):
            raise ValueError('付款资料仅允许查回原单')
        payload = order['payload']
        return self.request(action, spec={
            'warehouse': payload['head']['warehouse'], 'shop': payload['shop']['name'],
            'online': order['online'], 'internal': order['internal'], 'phase': order['stage'],
            'binding': (order['proof'] or {}).get('binding'),
            'contact': {'buyer': payload['profile']['values']['buyer']},
        }, retained_only=self.retained_only, **extra)


def _order(db, online, expected=None):
    order = orders.read(db, online)
    if not order or not re.fullmatch(r'[1-9]\d*', order['internal'] or ''):
        raise ValueError('原任务尚未确认聚水潭内部订单号，请先核对原单')
    payload = order['payload']
    binding = (order['proof'] or {}).get('binding') or {}
    if (binding.get('warehouse') != payload['head']['warehouse']
            or binding.get('shop') != payload['shop']['name']
            or not all(binding.get(k) for k in ('company', 'coid', 'uid', 'shop_id', 'version'))):
        raise ValueError('原任务缺少完整企业、仓和店铺身份，请核对原关联')
    purchases.validate_head(db, payload['head'])
    if generations.active(db, payload['head']) != order['gid']:
        raise wr.Conflict('原分仓生成版本已变化，不能回流到另一版本')
    _, digest = generations.read_generation(db, order['gid'])
    if digest != payload['generation']['digest']:
        raise wr.Conflict('原生成单摘要不一致，停止回流')
    if expected and (order['digest'] != expected['order_digest'] or order['internal'] != expected['internal']
                     or binding != expected['binding'] or not same_head(payload['head'], expected['head'])):
        raise wr.Conflict('回流期间原任务、店仓身份或生成版本已变化')
    if expected and expected.get('source_kind') == audited.KIND and order['stage'] != 'AUDITED':
        raise wr.Conflict('普通审核状态已变化，请重新核对原单')
    return order


def _sheets(raw):
    if raw.startswith(b'PK'):
        book = openpyxl.load_workbook(BytesIO(raw), read_only=True, data_only=False, keep_links=False)
        try:
            for sheet in book.worksheets:
                sheet.reset_dimensions()
                yield sheet.title, sheet.values
        finally:
            book.close()
    elif raw.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        import xlrd
        book = xlrd.open_workbook(file_contents=raw)
        try:
            for sheet in book.sheets():
                yield sheet.name, (sheet.row_values(i) for i in range(sheet.nrows))
        finally:
            book.release_resources()
    else:
        raise ValueError('实配原件不是可核验的 Excel，不能把网页或错误响应当成导出')


def parse(raw, order, receipt, cancel=None):
    if receipt.get('source_kind')=='jst_saleout_detail_v1':
        from jst_saleout import parse as parse_saleout
        return parse_saleout(raw,order,receipt,cancel)
    """Strict, whole-document adapter. Neither missing SKUs nor blanks become 0."""
    payload = order['payload']
    binding = (order['proof'] or {}).get('binding')
    if receipt.get('binding') != binding or receipt.get('online') != order['online'] or receipt.get('internal') != order['internal']:
        raise ValueError('实配来源的企业、店铺、线上或内部单号与原任务不同')
    if receipt.get('native_status') not in FINAL or receipt.get('complete') is not True:
        raise ValueError('尚未取得完整实配明细，原下单数量不能替代实配数量')
    if not raw or len(raw) > 32 * 1024 * 1024 or sha256(raw).hexdigest() != receipt.get('sha'):
        raise ValueError('实配原件大小或摘要不一致')
    expected = {str(sku): Decimal(str(qty)) for sku, qty, _ in payload['lines']}
    seen = set()
    rows = []
    quantity_headers = []
    for title, values in _sheets(raw):
        checkpoint(cancel)
        iterator = iter(values)
        headers = [str(value or '').strip() for value in next(iterator, ())]
        if not headers or not any(headers):
            raise ValueError('实配工作表为空，无法核验完整导出范围')
        qty = [h for h in QTY if h in headers]
        amount = [h for h in AMOUNT if h in headers]
        if len(qty) != 1 or not amount:
            raise ValueError('导出未明确提供实配/实发数量；普通“数量”列不能自动作为实配依据')
        monetary = amount[0]
        required = ('内部订单号', '线上订单号', '店铺名称', '商品编码', qty[0], monetary)
        if any(headers.count(h) != 1 for h in required):
            raise ValueError('实配原件缺少唯一身份或数量金额列')
        if any(headers.count(h) > 1 for h in ('商品名称', '数量单位', '币种', '商品单价')):
            raise ValueError('实配原件存在重复字段，请核对导出列')
        quantity_headers.append([title, qty[0], monetary])
        for line, values in enumerate(iterator, 2):
            checkpoint(cancel)
            if not any(v not in (None, '') for v in values):
                continue
            item = {name: values[i] if i < len(values) else None for i, name in enumerate(headers)}
            if any(isinstance(v, str) and v.startswith('=') for v in values):
                raise ValueError('实配原件含公式，不能在未核对结果时自动采用')
            if (purchase_io.code(item['内部订单号']) != order['internal']
                    or purchase_io.code(item['线上订单号']) != order['online']
                    or item['店铺名称'] != binding['shop']):
                raise ValueError('实配原件混入其他订单或店铺，停止整单回流')
            sku = purchase_io.code(item['商品编码'])
            if sku in seen or sku not in expected:
                raise ValueError('实配商品重复或不属于原生成单：' + sku)
            seen.add(sku)
            unit = str(item.get('数量单位') or '库存单位').strip()
            if unit != '库存单位':
                raise ValueError('实配数量单位未核清，不能按箱规猜测换算：' + sku)
            qty_value = purchase_io.number(item[qty[0]])
            money = purchase_io.number(item[monetary])
            currency = str(item.get('币种') or 'CNY').strip()
            if currency != 'CNY':
                raise ValueError('实配币种与当前人民币订单不一致')
            price = purchase_io.number(item.get('商品单价'), optional=True)
            if Decimal(qty_value) == 0 and Decimal(money) != 0:
                raise ValueError('实配为0但商品金额不为0：' + sku)
            if price is not None and (Decimal(price) * Decimal(qty_value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP) != Decimal(money).quantize(Decimal('.01'), rounding=ROUND_HALF_UP):
                raise ValueError('实配数量、商品单价和金额不一致：' + sku)
            rows.append({'sku': sku, 'name': str(item.get('商品名称') or ''), 'quantity': qty_value,
                         'amount': money, 'currency': currency, 'price': price, 'sheet': title, 'row': line,
                         'headers': headers, 'values': list(values), 'ordered': str(expected[sku])})
    if seen != set(expected):
        raise ValueError('实配原件缺少原单商品；取消明细或少配须提供明确0数量，不能把缺行当0')
    if not rows:
        raise ValueError('实配原件没有明细，不能视为整单0配货')
    return {'head': {k: payload['head'][k] for k in HEAD_KEYS}, 'order_digest': order['digest'],
            'internal': order['internal'], 'online': order['online'], 'gid': order['gid'], 'binding': binding,
            'native_status': receipt['native_status'], 'fields': quantity_headers,
            'file': {'sha': receipt['sha'], 'name': '聚水潭实配_' + order['internal'] + ('.xlsx' if raw.startswith(b'PK') else '.xls')},
            'rows': rows, 'quantity': str(sum((Decimal(r['quantity']) for r in rows), Decimal(0))),
            'amount': str(sum((Decimal(r['amount']) for r in rows), Decimal(0)))}


def _original(path, online, payload, raw):
    folder = Path(path).parent / 'jst_fulfillments' / online
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / (payload['file']['sha'] + Path(payload['file']['name']).suffix)
    if dest.exists():
        if sha256(dest.read_bytes()).hexdigest() != payload['file']['sha']:
            raise ValueError('本机实配原件已变化，原件保留，请核对归档')
    else:
        with dest.open('xb') as stream:
            stream.write(raw)
    return {'path': str(dest), **payload['file']}


def prepare_review(path, keys, progress=lambda _: None, cancel=None):
    plans, files, guards, overview = [], [], [], []
    for online in dict.fromkeys(keys):
        checkpoint(cancel)
        with closing(wr.read_connection(path)) as db:
            db.execute('BEGIN')
            state = sources.current(db, online)
            if not state or state['state'] != 'READY':
                raise ValueError((state or {}).get('message') or '尚无已核验实配资料，请先拉取原单')
            payload, raw = sources.source(db, online, state['digest'])
            order = _order(db, online, payload)
        file = _original(path, online, payload, raw)
        head = order['payload']['head']
        is_audited = payload.get('source_kind') == audited.KIND
        title = audited.LABEL if is_audited else '聚水潭销售出库明细'
        rows = []
        for r in payload['rows']:
            rows.append({'sku': r['sku'], 'name': r['name'], 'signed_number': head['number'],
                         'revision': head['revision'], 'store': head['store'], 'warehouse': head['warehouse'],
                         'document_number': order['internal'], 'document_date': None, 'quantity': r['quantity'],
                         'unit': '库存单位', 'factor': None, 'conversion_basis': None, 'base_quantity': r['quantity'],
                         'amount': r['amount'], 'currency': r['currency'], 'price': r['price'],
                         'note': (title if is_audited else '聚水潭实配回流') + '，付款须人工确认',
                         'source': {'sha': file['sha'], 'file': file['name'], 'sheet': r['sheet'], 'row': r['row']},
                         'raw_headers': r['headers'], 'raw_values': r['values'], 'adapter': 'jst_allocated',
                         'source_order': {'内部订单号': order['internal'], '线上订单号': online, '店铺名称': payload['binding']['shop'],
                                          **({'付款来源': title} if is_audited else {})}})
        result = lifecycle.prepare_numbered(path, {'stage': 'payment', 'rows': rows, 'files': [file]}, head, progress, cancel)
        plans.extend(result['plans'])
        files.extend(result['files'])
        guards.append({'online': online, 'digest': state['digest']})
        overview.append({'head': head, 'internal': order['internal'], 'ordered': order['payload']['stats']['quantity'],
                         'allocated_amount': order['payload']['stats']['amount'],
                         'quantity': payload['quantity'], 'amount': payload['amount'], 'rows': payload['rows'],
                         'source': title, 'audited': is_audited, 'totals': payload.get('totals')})
    if not plans:
        raise ValueError('请选择需要核对实配的原仓单')
    return {'plans': plans, 'files': files, 'jst_fulfillment': guards}, overview


def validate_guard(db, plan):
    """Called under the lifecycle write lock, before any payment version is saved."""
    allocated = [p for p in plan['plans'] if any(r.get('adapter') == 'jst_allocated' for r in p['payload']['raw_rows'])]
    guards = plan.get('jst_fulfillment', [])
    if not allocated and not guards:
        return
    if len(guards) != len(allocated) or len({g['online'] for g in guards}) != len(guards):
        raise ValueError('实配核对缺少唯一原单证据，未保存付款')
    for guard in guards:
        current = sources.current(db, guard['online'])
        if not current or current['state'] != 'READY' or current['digest'] != guard['digest']:
            raise wr.Conflict('实配资料已更新或原单状态变化，请重新核对；未保存付款')
        payload, _ = sources.source(db, guard['online'], guard['digest'])
        _order(db, guard['online'], payload)
        matches = [p for p in allocated if same_head(p['head'], payload['head'])]
        if len(matches) != 1:
            raise ValueError('实配证据与付款目标不一致')
        if payload['file']['sha'] not in {file['sha'] for file in plan['files']}:
            raise ValueError('实配付款核对缺少本次原件，未保存付款')
        raw = matches[0]['payload']['raw_rows']
        expected = [(r['sku'], r['quantity'], r['amount'], r['currency']) for r in payload['rows']]
        if [(r['sku'], r['base_quantity'], r['amount'], r['currency']) for r in raw] != expected:
            raise ValueError('实配核对数量或金额已变化，未保存付款')
        if any(row.get('document_number') != payload['internal']
               or (row.get('source_order') or {}).get('线上订单号') != payload['online']
               or (row.get('source_order') or {}).get('店铺名称') != payload['binding']['shop'] for row in raw):
            raise ValueError('实配付款核对的原单身份已变化，未保存付款')


def pull(path, keys, browser, progress=lambda _: None, cancel=None):
    """One selection, serial native reads, durable receipts, no payment commits."""
    result = []
    for online in dict.fromkeys(keys):
        checkpoint(cancel)
        order = None
        try:
            with closing(wr.read_connection(path)) as db:
                db.execute('BEGIN')
                order = _order(db, online)
            progress('拉取实配 ' + automatic.show_name(order['payload']['head']))
            receipt = browser.action('allocation', order, operation_id=uuid4().hex)
            checkpoint(cancel)
            if receipt.get('binding') != (order['proof'] or {}).get('binding') or receipt.get('online') != online or receipt.get('internal') != order['internal']:
                raise ValueError('原单查询身份与已保存关联不同，停止回流')
            state = receipt.get('state')
            if state != 'FILE_READY':
                if state not in ('CANCELLED', 'WAIT_ALLOCATION', 'WAIT_CONNECTION', 'SOURCE_UNVERIFIED'):
                    raise ValueError('实配导出结果待核，未生成付款资料，请继续原任务')
                message = receipt.get('message') or '原单尚未提供已核验实配资料'
                digest = None
            else:
                file = Path(receipt['path'])
                raw = file.read_bytes()
                payload = parse(raw, order, receipt, cancel)
                with closing(orders.connection(path)) as db, db:
                    begin(db, cancel)
                    _order(db, online, payload)
                    digest = sources.save(db, online, payload, raw)
                state, message = 'READY', '实配资料已准备，等待人工核对付款'
            if state != 'READY':
                with closing(orders.connection(path)) as db, db:
                    begin(db, cancel)
                    _order(db, online)
                    sources.observe(db, online, state, message)
            result.append({'online': online, 'state': state, 'digest': digest, 'message': message})
        except Cancelled:
            raise
        except ValueError as exc:
            if order is not None:
                with closing(orders.connection(path)) as db, db:
                    begin(db, cancel)
                    identity = db.execute('SELECT digest,internal,gid FROM jst_auto_orders WHERE online=?', (online,)).fetchone()
                    existing = sources.current(db, online)
                    if (identity == (order['digest'], order['internal'], order['gid'])
                            and (existing or {}).get('state') != 'READY'):
                        sources.observe(db, online, 'NEEDS_REVIEW', str(exc))
            result.append({'online': online, 'state': 'NEEDS_REVIEW', 'message': str(exc)})
    return result


def _observe_audited(path, order, state, message, native_status, candidate, cancel):
    online = order['online']
    with closing(orders.connection(path)) as db, db:
        begin(db, cancel)
        current = _order(db, online)
        if (current['digest'], current['sequence'], current['internal']) != (order['digest'], order['sequence'], order['internal']):
            raise wr.Conflict('回查期间原单已变化，请重新读取付款资料')
        previous = sources.current(db, online)
        digest = (previous or {}).get('digest')
        if native_status == audited.AUDITED and current['stage'] != 'AUDITED':
            current = orders.update(db, current, 'AUDITED', status='audited', reason='已按原内部订单号核实普通审核通过')
        elif native_status in ('已取消', '已付款待审核', '待付款') and current['stage'] == 'AUDITED':
            current = orders.update(db, current, 'REVIEW', status='review', reason='平台当前为' + native_status + '，原付款资料待核')
        customer_audit.record(db, current, native_status)
        if state == 'READY':
            payload, raw = candidate
            _order(db, online, payload)
            digest = sources.save(db, online, payload, raw, message)
        elif native_status == '已发货' and digest and previous['state'] == 'READY':
            old, _ = sources.source(db, online, digest)
            if old.get('source_kind') != audited.KIND:
                return {**previous, 'online': online, 'audit_stage': current['stage']}
            sources.observe(db, online, state, message, digest)
        else:
            sources.observe(db, online, state, message, digest)
        return {'online': online, 'state': state, 'digest': digest, 'message': message, 'audit_stage': current['stage']}


def pull_audited(path, keys, browser, progress=lambda _: None, cancel=None):
    """Read the original order only, keeping audit outcome apart from candidate readiness."""
    results = []
    for online in dict.fromkeys(keys):
        checkpoint(cancel)
        result = None
        try:
            with closing(wr.read_connection(path)) as db:
                db.execute('BEGIN')
                order = _order(db, online)
            progress('读取审核后订单明细 ' + automatic.show_name(order['payload']['head']))
            status, candidate = '', None
            try:
                receipt = browser.action('payment_detail', order)
            except automatic.PlatformCheck as exc:
                state, message = 'RETRY_PAYMENT_READ', str(exc)
            else:
                checkpoint(cancel)
                binding = (order['proof'] or {}).get('binding')
                if any(receipt.get(k) != v for k, v in {'binding': binding, 'online': online, 'internal': order['internal']}.items()):
                    state, message = 'NEEDS_REVIEW', '付款资料的原单身份不同，请核对原关联'
                elif receipt.get('state') == 'WAIT_CONNECTION' or receipt.get('found') is not True:
                    state, message = 'RETRY_PAYMENT_READ', receipt.get('message') or '尚未读到唯一原单，保留原内部订单号继续核对'
                else:
                    status = receipt.get('native_status') or '未知状态'
                    if status != audited.AUDITED:
                        state, message = 'PAYMENT_SOURCE_PENDING', f'聚水潭当前为{status}，请核对普通审核及原单'
                    elif receipt.get('detail_error'):
                        state, message = 'RETRY_PAYMENT_READ', receipt['detail_error']
                    else:
                        try:
                            candidate = audited.prepare(order, receipt, cancel)
                        except (ValueError, TypeError, ArithmeticError) as exc:
                            state, message = 'NEEDS_REVIEW', str(exc)
                        else:
                            state, message = 'READY', audited.LABEL + '已核验，等待业务确认实际付款'
            result = _observe_audited(path, order, state, message, status, candidate, cancel)
        except ValueError as exc:
            result = {'online': online, 'state': 'NEEDS_REVIEW', 'message': str(exc)}
        results.append(result)
    return results


def run_order_actions(path, keys, owner, action, approved=None, progress=lambda _: None, cancel=None):
    return automatic.run_browser(path, keys, owner, action, approved, progress, cancel, prepare_payment=pull_audited)


def run_browser(path, keys, owner, progress=lambda _: None, cancel=None, *, retained_only=False, audit_details=False):
    from catalog_sync_client import ComponentError
    checkpoint(cancel)
    try:
        lease = owner.acquire()
    except ComponentError as exc:
        raise automatic._client_error(exc) from None
    try:
        try:
            lease.start()
        except ComponentError as exc:
            raise automatic._client_error(exc) from None
        reader = pull_audited if audit_details else pull
        return reader(path, keys, Browser(lease, retained_only), progress, cancel)
    finally:
        import time
        while not lease.close(timeout=.5):
            progress('等待实配原单查询安全结束')
            time.sleep(.2)


def run_background(path, keys, owner, progress=lambda _: None, cancel=None):
    return run_browser(path, keys, owner, progress, cancel, retained_only=True)


def run_audited_browser(path, keys, owner, progress=lambda _: None, cancel=None, *, retained_only=False):
    return run_browser(path, keys, owner, progress, cancel, retained_only=retained_only, audit_details=True)


def retry_audited(path, keys, owner, progress=lambda _: None, cancel=None):
    return run_audited_browser(path, keys, owner, progress, cancel, retained_only=True)


def verify_audited_before_commit(path, guards, owner, progress=lambda _: None, cancel=None):
    expected = {guard['online']: guard['digest'] for guard in guards}
    for row in run_audited_browser(path, list(expected), owner, progress, cancel):
        if row['state'] != 'READY' or row['digest'] != expected[row['online']]:
            raise wr.Conflict(row.get('message', '') + '。审核后付款资料已变化或无法再次核实，请重新打开核对。')
