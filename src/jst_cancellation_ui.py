"""Business confirmation of a saved payment after the original JST order was cancelled."""
from contextlib import closing
from decimal import Decimal

from PySide6.QtWidgets import QComboBox, QDialog, QFormLayout, QHBoxLayout, QLineEdit, QVBoxLayout
from business_ui import run
import jst_auto_order_store as orders
from jst_auto_order_ui import notify
import jst_cancellation as cancellation
import lifecycle_store
from ui_controls import button, label
import work_repository as wr


def _context(path, head, progress=lambda _:None, cancel=None):
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        state = cancellation.review_context(db,head)
        case = state['case']
        original = lifecycle_store.decode(lifecycle_store.version_row(
            db,{**head,'revision':case['revision']},'payment',case['payment_version'])) if case['payment_version'] else None
        state['original'] = original
        state['execution'] = orders.read(db,case['successor'] or case['online'])
        return state


def _payment_label(document):
    if not document:
        return '未保存'
    rows = document['payload']['rows']
    amounts = {}
    for row in rows:
        if row.get('amount') is not None:
            currency = row.get('currency') or '未标币种'
            amounts[currency] = amounts.get(currency,Decimal(0)) + Decimal(row['amount'])
    total = '、'.join(f'{amount} {currency}' for currency,amount in amounts.items()) or '金额未留存'
    return f"V{document['version']:03d} · {len(rows)} 款 · {total}"


def review(parent, host, head):
    path = host.work_repository.path
    dialog = QDialog(parent)
    dialog.setWindowTitle('原单取消付款重核')
    dialog.resize(680,340)
    layout = QVBoxLayout(dialog)
    note = label('')
    note.setWordWrap(True)
    layout.addWidget(note)
    form = QFormLayout()
    choice = QComboBox()
    for title, key in (('请选择…',None),('确认沿用原款','reuse'),('确认已保存的更正付款','correct'),('终止本轮采购','stop')):
        choice.addItem(title,key)
    reviewer, basis = QLineEdit(), QLineEdit()
    form.addRow('核对结果',choice)
    form.addRow('核对人',reviewer)
    form.addRow('财务凭据、现行订单或处理依据',basis)
    layout.addLayout(form)
    state = None
    result = None

    def reload():
        nonlocal state
        state = notify(dialog,lambda:run(dialog,_context,(path,head),'读取原款与当前版本'))
        if not state:
            return False
        case, execution = state['case'], state['execution']
        current = (execution or {}).get('internal') or case['successor'] or '沿原取消轮次处理'
        text = (f"{head['number']} · {head['warehouse']}\n"
                f"已取消聚水潭内部单号 {case['internal']} · {case['online']}\n"
                f"当前执行 {current}\n原正式付款 {_payment_label(state['original'])}\n"
                f"当前付款 {_payment_label(state['payment'])}")
        if state['review']:
            last=state['review']
            text+=f"\n最近核对 {last['reviewer']} · {last['checked_at']} · {last['basis']}"
        note.setText(text)
        return True

    def show_details():
        from readable_ui import details
        rows = []
        for title, document in (('原正式付款',state['original']),('当前付款',state['payment'])):
            if document:
                rows.extend([title,r['sku'],r.get('name'),r['base_quantity'],r.get('amount'),
                             r.get('document_number')] for r in document['payload']['rows'])
        details(dialog,'付款商品对照',['版本','SKU','商品','数量','实付金额','付款单号'],rows)

    def correct():
        from lifecycle_batch_ui import import_batch
        saved = import_batch(dialog,host,'payment',scope=[head])
        if saved and reload():
            choice.setCurrentIndex(choice.findData('correct'))

    def save():
        nonlocal result
        if not state:
            return
        result = notify(dialog,lambda:run(dialog,cancellation.save_review,
            (path,head,state['stamp'],choice.currentData(),reviewer.text(),basis.text()),'保存付款人工重核'))
        if result:
            dialog.accept()

    row = QHBoxLayout()
    for title, action in (('查看付款商品对照',show_details),('导入付款更正',correct),
                          ('保存本轮人工核对',save),('关闭',dialog.reject)):
        row.addWidget(button(title,action,title=='保存本轮人工核对'))
    layout.addLayout(row)
    try:
        if reload():
            dialog.exec()
        return result
    finally:
        dialog.deleteLater()
