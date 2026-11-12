"""Direct generation-to-payment entry; only the existing review can confirm facts."""
from decimal import Decimal
from business_ui import run
from lifecycle_ui import Review,commit
from ui_controls import label
import jst_payment


def review(parent,host,entries):
    plan=run(parent,jst_payment.prepare,(host.work_repository.path,entries),'读取已保存生成单，准备付款核对')
    d=Review(parent,plan,show_source_notices=False);d.setWindowTitle(f'付款核对 · 使用聚水潭生成单（{len(plan["plans"])}单）')
    amounts={p['head']['number']:sum((Decimal(str(r['amount'])) for r in p['payload']['rows'] if Decimal(str(r['base_quantity']))>0),Decimal(0)) for p in plan['plans']}
    heads={p['head']['number']:p['head'] for p in plan['plans']}
    d.overview_model.headers=[*d.overview_model.headers,'门店','供货仓','付款金额（元）']
    d.overview_model.replace([r+[heads[r[0]]['name'],heads[r[0]]['warehouse'],f'{amounts[r[0]]:,.2f}'] for r in d.overview_model.rows])
    d.overview.clicked.disconnect(d.select_order)
    def select_order(i):
        d.scope_tree.setCurrentItem(d.scope_tree.topLevelItem(0));d.search.setText(d.overview_model.rows[d.overview_model.indices[i.row()]][0])
    d.overview.clicked.connect(select_order)
    for i,width in enumerate((240,140,140,140,140,230,110,160)):d.overview.setColumnWidth(i,width)
    d.layout().insertWidget(0,label(f'核对 {len(plan["plans"])} 单 · 付款金额合计 {sum(amounts.values()):,.2f} 元。采用已保存生成单，无需导出再导入；仅当实际已付款且数量、金额一致时确认。','section'))
    d.layout().insertWidget(1,label('存在差异请取消并导入真实付款单。内部号可在“订单详情 → 维护聚水潭内部订单号”补填。','muted'))
    try:
        if d.exec():return commit(parent,host,plan)
        return None
    finally:d.deleteLater()
