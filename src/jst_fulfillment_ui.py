"""Native allocation preparation followed by the existing human payment review."""
from decimal import Decimal

from business_ui import run
from lifecycle_ui import Review, commit
from ui_controls import button, label
import jst_fulfillment as service


def make_review(parent, plan, overview):
    dialog = Review(parent, plan, show_source_notices=False)
    audited = any(row.get('audited') for row in overview)
    dialog.setWindowTitle('等待人工确认付款')
    heads = {row['head']['number']: row for row in overview}
    if audited:
        dialog.overview_model.headers += ['付款来源','本地分配量','平台数量','本地分配金额','商品金额','商品金额差','运费','平台应付']
        values = []
        for row in dialog.overview_model.rows:
            entry = heads[row[0]]
            totals = entry.get('totals') or {}
            difference = Decimal(entry['amount']) - Decimal(entry['allocated_amount'])
            values.append(row + [entry['source'],entry['ordered'],entry['quantity'],entry['allocated_amount'],
                                 entry['amount'],f'{difference:+.2f}',totals.get('freight'),totals.get('payable')])
        dialog.overview_model.replace(values)
    else:
        dialog.overview_model.headers += ['原下单数量', '实配数量', '实配商品金额（元）']
        dialog.overview_model.replace([row + [heads[row[0]]['ordered'], heads[row[0]]['quantity'], heads[row[0]]['amount']]
                                      for row in dialog.overview_model.rows])
    dialog.overview.clicked.disconnect(dialog.select_order)
    def select_order(index):
        dialog.scope_tree.setCurrentItem(dialog.scope_tree.topLevelItem(0))
        dialog.search.setText(dialog.overview_model.rows[dialog.overview_model.indices[index.row()]][0])
    dialog.overview.clicked.connect(select_order)
    amount=sum((Decimal(row['amount']) for row in overview),Decimal(0))
    dialog.layout().insertWidget(0, label(f'{len(overview)} 单待核对 · 商品金额 {amount:,.2f} 元。付款须由业务确认。', 'section'))
    for row in dialog.all_rows:
        for index in (4,5,6,8,9):
            if row[index] is not None:row[index]=format(Decimal(str(row[index])),'f')
    dialog.filter_rows()
    dialog.complete.setText('我已核对原单、商品数量、金额及运费，并确认已实际付款。')
    dialog.save.setText('人工确认付款并保存')
    def differences():
        from readable_ui import details
        rows = []
        for entry in overview:
            for row in entry['rows']:
                allocated = row.get('allocated_amount')
                rows.append([entry['head']['number'], row['sku'], row['name'], row['ordered'], row['quantity'],
                             str(Decimal(row['quantity']) - Decimal(row['ordered'])), allocated, row['price'], row['amount'],
                             f"{Decimal(row['amount'])-Decimal(allocated):+.2f}" if allocated is not None else None])
        details(dialog, '本地分配与付款资料的逐商品对照',
                ['补货单号','商品编码','商品名称','本地分配量','平台数量','数量差',
                 '本地分配金额','成交单价','商品金额','金额差'], rows)
    dialog.layout().insertWidget(1, button('查看逐商品数量和金额差异', differences))
    return dialog


def review(parent, host, keys):
    path = host.work_repository.path
    plan, overview = run(parent, service.prepare_review, (path, keys), '按原单准备付款核对')
    dialog = make_review(parent, plan, overview)
    try:
        if not dialog.exec():
            return None
        guards = [guard for guard,row in zip(plan['jst_fulfillment'],overview) if row.get('audited')]
        if guards:
            from catalog_session_ui import manager
            run(parent, service.verify_audited_before_commit, (path, guards, manager(host).session), '保存前核对聚水潭原单')
        return commit(parent, host, plan)
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
    result = run(parent, service.run_audited_browser, (host.work_repository.path, missing, manager(host).session), '按原单补取付款资料')
    blocked = [r for r in result if r['state'] != 'READY']
    if blocked:
        raise ValueError('\n'.join(r['online'] + '：' + r['message'] for r in blocked)
                         + '\n已准备的原件保留。本批未确认付款，可仅勾选准备完成的原单继续核对。')
    return review(parent, host, keys)
