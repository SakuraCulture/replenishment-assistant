"""Native allocation preparation followed by the existing human payment review."""
from decimal import Decimal

from business_ui import run
from lifecycle_ui import Review, commit
from ui_controls import button, label
import jst_fulfillment as service


def make_review(parent, plan, overview):
    dialog = Review(parent, plan, show_source_notices=False)
    dialog.setWindowTitle('实配已回流 · 等待人工确认付款')
    heads = {row['head']['number']: row for row in overview}
    dialog.overview_model.headers += ['原下单数量', '实配数量', '实配商品金额（元）']
    dialog.overview_model.replace([row + [heads[row[0]]['ordered'], heads[row[0]]['quantity'], heads[row[0]]['amount']]
                                  for row in dialog.overview_model.rows])
    dialog.overview.clicked.disconnect(dialog.select_order)
    def select_order(index):
        dialog.scope_tree.setCurrentItem(dialog.scope_tree.topLevelItem(0))
        dialog.search.setText(dialog.overview_model.rows[dialog.overview_model.indices[index.row()]][0])
    dialog.overview.clicked.connect(select_order)
    amount=sum((Decimal(row['amount']) for row in overview),Decimal(0))
    dialog.layout().insertWidget(0, label(f'实配 {len(overview)} 单 · 商品金额 {amount:,.2f} 元；付款须由业务确认。', 'section'))
    for row in dialog.all_rows:
        for index in (4,5,6,8,9):
            if row[index] is not None:row[index]=format(Decimal(str(row[index])),'f')
    dialog.filter_rows()
    dialog.complete.setText('我已对照实配原件核对完整范围、商品数量和金额，且确认已实际付款。')
    dialog.save.setText('人工确认付款并保存')
    def differences():
        from readable_ui import details
        rows = [[entry['head']['number'], r['sku'], r['name'], r['ordered'], r['quantity'],
                 str(Decimal(r['quantity']) - Decimal(r['ordered'])), r['amount']]
                for entry in overview for r in entry['rows']]
        details(dialog, '原下单与实配逐商品对照', ['补货单号', '商品编码', '商品名称', '原下单量', '实配量', '差额', '实配金额'], rows)
    dialog.layout().insertWidget(1, button('查看原下单与实配差异', differences))
    return dialog


def review(parent, host, keys):
    plan, overview = run(parent, service.prepare_review, (host.work_repository.path, keys), '按原单准备实配付款核对')
    dialog = make_review(parent, plan, overview)
    try:
        return commit(parent, host, plan) if dialog.exec() else None
    finally:
        dialog.deleteLater()


def pull_and_review(parent, host, keys):
    # Opening a completed candidate must not create another native export.
    from jst_fulfillment_jobs import waiting_review
    ready = set(waiting_review(host.work_repository.path, include_paid=True))
    missing = [key for key in dict.fromkeys(keys) if key not in ready]
    if not missing:
        return review(parent, host, keys)
    from catalog_session_ui import manager
    result = run(parent, service.run_browser, (host.work_repository.path, missing, manager(host).session), '按原单拉取实配资料')
    blocked = [r for r in result if r['state'] != 'READY']
    if blocked:
        raise ValueError('\n'.join(r['online'] + '：' + r['message'] for r in blocked)
                         + '\n已准备的原件保留。本批未确认付款，可仅勾选准备完成的原单继续核对。')
    return review(parent, host, keys)
