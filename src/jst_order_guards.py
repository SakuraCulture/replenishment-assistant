"""Version and lock guards shared by allocation and lifecycle saves."""
from jst_order_store import active, has_generation, locked
from lifecycle_facts import current, upstream
from lifecycle_rules import DOCUMENT_SCHEMA, basis_token
from lifecycle_store import decode, exists, latest_version, version_rows
from work_repository import Conflict
import jst_auto_order_store as automatic


def require_generation(db, generation, digest, *, historical=False):
    """The same signed, visible generation for exports and automatic ordering."""
    from jst_batch_store import state as batch_state
    from jst_order_store import state as warehouse_state
    from warehouse_execution import validate_visibility
    h=generation['head'];number=h['number'];gid=generation['gid']
    try:
        validate_visibility(db,h)
    except ValueError as exc:
        raise Conflict(f'{number}：{exc}') from exc
    if db.execute('SELECT digest FROM jst_generations WHERE gid=?',(gid,)).fetchone()!=(digest,):
        raise Conflict('生成记录已变化，请刷新后重试：'+number)
    if historical:
        return
    wh=h['warehouse']
    stock=batch_state(db,generation['batch_id'],wh) if generation.get('batch_id') else warehouse_state(db,wh)
    if (active(db,h)!=gid or not stock or (stock[0]!=generation['bid'] and locked(db,h)!=gid)):
        raise Conflict('生成记录或仓资料已更新，请刷新后重试：'+number)


def require_generation_basis(db, generation):
    h=generation['head']
    products=generation.get('signed_products',generation['basis']['products'])
    if upstream(db,{**h,'products':products},'payment')!=generation['basis']:
        raise Conflict('门店回传或运营签发依据已更新，请重新分配后重试：'+h['number'])


def upstream_stamp(db, head, products):
    """Compact commit-time check for a fully verified allocation basis."""
    from lifecycle_facts import legacy_return
    from lifecycle_routes import stages
    route=stages(db,head)
    active=route[:route.index('payment')]
    has_docs=exists(db)
    versions=[latest_version(db,head,stage) if has_docs else None for stage in active]
    legacy=legacy_return(db,head,products) if versions[0] is None else None
    return [list(active),[list(v) if v else None for v in versions],
            [legacy['version'],legacy['digest']] if legacy and legacy['complete'] else None]


def guard_resign(db, oid):
    if has_generation(db, oid):
        raise Conflict('本单已有聚水潭生成记录；请先在订单跟踪核对下游，不能覆盖签发绕过库存占用或锁单')


def guard_return(db, head):
    pending=automatic.state(db,head)
    if pending and pending[0] in automatic.RELEASED:
        raise Conflict('该仓单已有聚水潭下单结果或提交待核，请先核对原自动下单任务')
    if locked(db, head):
        raise Conflict('该订单由旧版下单标记锁定。请在“聚水潭导入单”勾选本单，从“其他订单操作”核对原单后再更正回传。')


def guard_payment(db, p):
    gid=p['payload'].get('jst_generation')
    if gid and active(db,p['head'])!=gid:raise Conflict('付款草稿对应生成单已变化，请重新核对')


def saved_execution_reason(db, head):
    """A saved fact survives later changes to the upstream comparison basis."""
    order=automatic.state(db,head)
    if order and order[0] in automatic.RELEASED:
        return '聚水潭自动下单已关联' if order[1] else '聚水潭自动下单提交待核'
    if locked(db, head):
        return '已有历史下单标记'
    if not exists(db):
        return None
    from analytics_identifiers import registered
    try:
        if registered(db, head):
            return '已有聚水潭内部订单号'
    except (ValueError, KeyError, TypeError):
        return '聚水潭内部订单号待核'
    for stage, title in (('receipt', '实收记录'), ('payment', '已确认付款')):
        for row in version_rows(db, head, stage):
            try:
                if not decode(row)['payload'].get('pending'):
                    return '已有'+title
            except (ValueError, KeyError, TypeError):
                # Maintenance can remove a damaged record after explicit review.
                # Its unknown execution state still consumes the original batch.
                return title+'资料待核'
    return None


def previous_payment_for_return(db, head, payment):
    """Only a confirmed payment invalidated by a newer return can be corrected.

    Keep the payment fact; its exact token is frozen and checked again at commit.
    Pending/legacy documents and receipt facts do not acquire this permission.
    """
    if not payment:return None
    p=payment['payload'];basis=p.get('basis',{});dependencies=basis.get('dependencies',{})
    if (p.get('schema')!=DOCUMENT_SCHEMA or p.get('pending') or not basis.get('ready')
            or basis.get('signed_digest')!=head['digest']
            or not dependencies.get('return')):return None
    expected=upstream(db,{**head,'products':{}},'payment')
    if dependencies==expected['dependencies']:return None
    if not upstream(db,{**head,'products':{}},'payment')['ready']:return None
    return basis_token(payment)


def allocation_gate(db, head, old, correction=False):
    """Return a blocker and, for a return correction, the old payment token."""
    order=automatic.state(db,head)
    if order and order[0] in automatic.RELEASED:
        return '已有聚水潭下单结果或提交待核，请核对原任务',None
    if locked(db,head):return '已锁单',None
    if current(db,head,'receipt'):return '已有收货记录，请先核对收货事实，不能直接重新分配',None
    payment=current(db,head,'payment')
    previous=previous_payment_for_return(db,head,payment) if correction else None
    if payment and not previous:return '已有付款记录，当前回传未形成可更正的旧付款依据，请先核对付款',None
    if old and not correction:return '已生成，原样导出或选择重新分配',None
    return None,previous
