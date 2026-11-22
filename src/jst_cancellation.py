"""Verified cancellations, warehouse execution succession and payment review."""
import hashlib
from contextlib import closing

import jst_auto_order_store as orders
import jst_order_store as allocations
import lifecycle_store
from order_keys import warehouse_revision
from sqlite_write import begin, commit
import work_repository as wr


CANCELLED = '已取消'
PAYMENT_REVIEW = '原单已取消，付款待重新核对'
_FIELDS = ('online', 'oid', 'revision', 'warehouse', 'internal', 'gid',
           'observed', 'payment_version', 'payment_digest', 'successor', 'release')


def schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS jst_cancellations(
        online TEXT PRIMARY KEY,oid TEXT NOT NULL,revision INTEGER NOT NULL,warehouse TEXT NOT NULL,
        internal TEXT NOT NULL,gid TEXT NOT NULL,observed TEXT NOT NULL,
        payment_version INTEGER,payment_digest TEXT,successor TEXT UNIQUE,release TEXT)''')
    db.execute('CREATE INDEX IF NOT EXISTS jst_cancellation_scope ON jst_cancellations(oid,warehouse)')
    db.execute('''CREATE TABLE IF NOT EXISTS jst_cancellation_reviews(
        id INTEGER PRIMARY KEY,oid TEXT NOT NULL,warehouse TEXT NOT NULL,revision INTEGER NOT NULL,
        cancelled_online TEXT NOT NULL,execution_online TEXT NOT NULL,decision TEXT NOT NULL,
        payment_version INTEGER,payment_digest TEXT,reviewer TEXT NOT NULL,basis TEXT NOT NULL,checked_at TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS jst_cancellation_latest_review ON jst_cancellation_reviews(cancelled_online,id)')


def read(db, head):
    if orders.exists(db, 'jst_cancellations'):
        row = db.execute('SELECT '+','.join(_FIELDS)+' FROM jst_cancellations '
                         'WHERE oid=? AND warehouse=? ORDER BY observed DESC,rowid DESC LIMIT 1',
                         (head['oid'], head['warehouse'])).fetchone()
        if row:
            return dict(zip(_FIELDS, row))
    # A pre-upgrade cancellation signal can hold payment even after the latest
    # lookup note changes. Only record() can establish a verified cancellation.
    scope = head['oid'], head['warehouse']
    row = None
    if orders.exists(db, orders.CANCELLATION_HOLDS):
        row = db.execute('SELECT online,internal,gid FROM jst_cancellation_holds WHERE oid=? AND warehouse=?',
                         scope).fetchone()
    if not row and orders.exists(db):
        row = db.execute('SELECT online,internal,gid FROM jst_auto_orders WHERE oid=? AND warehouse=? AND '
                         +orders.LEGACY_CANCELLATION, scope).fetchone()
    if not row:
        row = orders.legacy_cancellation_history(db, head)
    if row:
        return {'online':row[0], 'internal':row[1], 'gid':row[2], 'successor':None, 'legacy':True}
    return None


def affected_scopes(db, batches):
    scopes = set()
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for marks, oids in batches:
        for table in tables & {'jst_cancellations', orders.CANCELLATION_HOLDS}:
            scopes.update(db.execute('SELECT DISTINCT oid,warehouse FROM '+table+' WHERE oid IN ('+marks+')',oids))
        if 'jst_auto_orders' in tables:
            scopes.update(db.execute('SELECT oid,warehouse FROM jst_auto_orders WHERE oid IN ('+marks+') AND '
                                     +orders.LEGACY_CANCELLATION,oids))
        if orders.LEGACY_HISTORY_TABLES <= tables:
            scopes.update(db.execute('SELECT a.oid,a.warehouse' + orders.LEGACY_HISTORY.format(
                scope='a.oid IN ('+marks+')'), oids))
    return scopes


def record(db, order):
    """Called after exact original order identity and cancellation are checked."""
    if order.get('historical') or order['stage'] != 'CANCELLED' or not order['internal']:
        raise wr.Conflict('原单取消凭据不完整，请重新核对')
    head = order['payload']['head']
    row = lifecycle_store.version_row(db, head, 'payment') if lifecycle_store.exists(db) else None
    paid = lifecycle_store.decode(row)
    token = (paid['version'], paid['digest']) if paid and not paid['payload'].get('pending') else (None, None)
    schema(db)
    db.execute('INSERT OR IGNORE INTO jst_cancellations VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)',
               (order['online'], *warehouse_revision(head), order['internal'], order['gid'], wr.now(), *token))


def latest_review(db, online):
    if not orders.exists(db, 'jst_cancellation_reviews'):
        return None
    row = db.execute('''SELECT id,revision,execution_online,decision,payment_version,payment_digest,
        reviewer,basis,checked_at FROM jst_cancellation_reviews
        WHERE cancelled_online=? ORDER BY id DESC LIMIT 1''', (online,)).fetchone()
    fields = ('id','revision','execution_online','decision','payment_version','payment_digest',
              'reviewer','basis','checked_at')
    return dict(zip(fields, row)) if row else None


def redo_case(db, head):
    """A registered successor is scoped to the original warehouse signing."""
    case = read(db, head)
    if case and case['successor'] and case['revision'] == head['revision']:
        original = wr.loads(case['release'])['head']
        if original['digest'] == head['digest']:
            return case
    return None


def payment_state(db, head, payment):
    """Return the cancellation-specific hold, and exact evidence for a new preview."""
    case = read(db, head)
    if not case:
        return '', None
    online = case['online']
    if case.get('legacy'):
        return PAYMENT_REVIEW+'。请在聚水潭自动下单任务重新核对原单。', None
    execution = case['successor'] or online
    review = latest_review(db, online)
    if review and review['revision'] == head['revision'] and review['execution_online'] == execution:
        if review['decision'] == 'stop':
            return '原单已取消，已终止本轮采购。请在付款核对查看原记录。', None
        if payment and (review['payment_version'], review['payment_digest']) == (payment['version'], payment['digest']):
            return '', {'cancelled':online, 'execution':execution, 'review':review['id']}
    # When nothing had been paid, manual confirmation of genuine successor
    # details is already the first payment check for the new execution.
    if not review and case['successor'] and case['payment_version'] is None and payment:
        rows = payment['payload'].get('raw_rows') or ()
        if rows and all(r.get('adapter') == 'jst_allocated' and
                        (r.get('source_order') or {}).get('线上订单号') == execution for r in rows):
            return '', {'cancelled':online, 'execution':execution, 'review':None}
    return PAYMENT_REVIEW+'。请在付款核对选择「原单取消付款重核」。', None


def review_context(db, head):
    import lifecycle_facts
    case = read(db, head)
    if not case or case.get('legacy'):
        raise ValueError('请先在聚水潭自动下单任务核实原单已取消')
    payment = lifecycle_facts.current(db, head, 'payment')
    review = latest_review(db, case['online'])
    return {'case':case, 'payment':payment, 'review':review,
            'stamp':[case['online'],case['successor'],head['revision'],
                     payment['version'] if payment else None,payment['digest'] if payment else None,
                     review['id'] if review else None]}


def save_review(path, head, expected, decision, reviewer, basis, progress=lambda _:None, cancel=None):
    import purchases
    from runtime_support import CommitProgress
    reviewer, basis = reviewer.strip(), basis.strip()
    if decision not in ('reuse','correct','stop') or not reviewer or not basis:
        raise ValueError('请选择付款处理方式，填写核对人及财务凭据或处理依据')
    with closing(orders.connection(path)) as db, db:
        begin(db, cancel)
        purchases.validate_head(db, head)
        context = review_context(db, head)
        if context['stamp'] != expected:
            raise wr.Conflict('原单、执行轮次或付款已变化，请重新打开核对')
        payment, case = context['payment'], context['case']
        version, digest = (payment['version'], payment['digest']) if payment else (None,None)
        if decision != 'stop':
            if not payment or payment['payload'].get('pending'):
                raise ValueError('请先在付款核对确认有效的正式付款')
            unchanged = (version,digest) == (case['payment_version'],case['payment_digest'])
            if unchanged != (decision == 'reuse'):
                raise ValueError('沿用原款须选择原付款版本，更正须先在付款核对保存更正版本')
            if case['successor']:
                successor = orders.read_warehouse_order(db,head)
                if not successor or successor['online'] != case['successor'] or successor['stage'] not in ('IMPORTED','AUDITED'):
                    raise ValueError('请先回查并核实后继聚水潭下单结果，再核对本轮付款')
        previous = context['review'] or {}
        execution = case['successor'] or case['online']
        values = (decision,version,digest,reviewer,basis)
        if (previous.get('execution_online') == execution and previous.get('revision') == head['revision']
                and values == tuple(previous[k] for k in ('decision','payment_version','payment_digest','reviewer','basis'))):
            return previous
        db.execute('''INSERT INTO jst_cancellation_reviews
            (oid,warehouse,revision,cancelled_online,execution_online,decision,payment_version,
             payment_digest,reviewer,basis,checked_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                   (head['oid'],head['warehouse'],head['revision'],case['online'],execution,*values,wr.now()))
        result = latest_review(db,case['online'])
        progress(CommitProgress(True))
        commit(db,cancel)
    progress(CommitProgress(False))
    return result


def begin_redo(path, online, progress=lambda _:None, cancel=None):
    """Release the original active reservation and reserve one successor identity."""
    import jst_batches
    import purchases
    from payment_references import reference_key
    from runtime_support import CommitProgress
    with closing(orders.connection(path)) as db, db:
        begin(db, cancel)
        order = orders.read(db,online)
        if not order or order['stage'] != 'CANCELLED':
            raise ValueError('请先核对原单，确认聚水潭明确返回「已取消」')
        head = order['payload']['head']
        case = db.execute('SELECT internal,gid,successor,release FROM jst_cancellations WHERE online=?',
                          (online,)).fetchone() if orders.exists(db,'jst_cancellations') else None
        if not case or case[:2] != (order['internal'],order['gid']):
            raise wr.Conflict('缺少可核实的原单取消凭据，请继续核对原任务')
        if case[2]:
            return {'online':case[2], **wr.loads(case[3])}
        purchases.validate_head(db,head)
        if lifecycle_store.has_formal_document(db,head['oid'],head['warehouse'],'receipt'):
            raise ValueError('本仓单已有正式实收，请进入原收货核对处理后续事实')
        gid, wh, oid = order['gid'], head['warehouse'], head['oid']
        active = allocations.active(db,head)
        if active not in (None,gid) or allocations.locked(db,head) not in (None,gid):
            raise wr.Conflict('当前生成或下单关联已变化，请核对原分配')
        generation,digest = allocations.read_generation(db,gid)
        if digest != order['payload']['generation']['digest'] or generation['head']['warehouse'] != wh:
            raise wr.Conflict('原分配依据摘要或仓不符，不能释放占用')
        reference = reference_key(head)
        row = db.execute("SELECT payload FROM lifecycle_mappings WHERE stage='payment' AND name=?",
                         (reference,)).fetchone() if lifecycle_store.exists(db) else None
        original = wr.loads(row[0]) if row else None
        if original and original.get('number') != order['internal']:
            raise wr.Conflict('本机内部订单号关联与原单不同，请先核对订单关联')
        released = dict(db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=? ORDER BY rowid',
                                   (gid,))) if active else {}
        if active:
            db.execute('DELETE FROM jst_active WHERE oid=? AND warehouse=? AND gid=?',(oid,wh,gid))
            jst_batches.cancel_member(db,head,gid)
            if not generation.get('batch_id'):
                db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(wh,))
        db.execute('UPDATE jst_locks SET locked=0,at=?,reason=? WHERE oid=? AND warehouse=? AND gid=?',
                   (wr.now(),'聚水潭原单明确取消，业务确认沿原仓单重做',oid,wh,gid))
        if original:
            db.execute("DELETE FROM lifecycle_mappings WHERE stage='payment' AND name=?",(reference,))
        successor = 'BH'+hashlib.sha256(('replenishment-jst-redo:'+online).encode()).hexdigest()[:24].upper()
        if orders.read(db,successor):
            raise wr.Conflict('后继线上单号已关联记录，请核对原任务')
        release = {'head':head,'gid':gid,'batch_id':generation.get('batch_id'),'bid':generation['bid'],
                   'released':released,'internal':order['internal'],'reference':original,'at':wr.now()}
        orders.archive_cancelled(db,order)
        db.execute('UPDATE jst_cancellations SET successor=?,release=? WHERE online=?',
                   (successor,wr.dumps(release),online))
        allocations.event(db,head,'原取消单释放占用并登记后继',
                          {'original':online,'successor':successor,'gid':gid,'batch_id':release['batch_id'],
                           'bid':release['bid'],'released':released})
        progress(CommitProgress(True))
        commit(db,cancel)
    progress(CommitProgress(False))
    return {'online':successor, **release}
