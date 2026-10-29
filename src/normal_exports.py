"""Merchant-facing, per-store frozen issue export. No automatic external transmission."""
import os,tempfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill,Alignment
from openpyxl.worksheet.datavalidation import DataValidation
import result_semantics as sem
import new_products


def table(book,title,headers,rows):
    ws=book.create_sheet(title);ws.append(headers)
    for row in rows:
        ws.append(row)
        for cell in ws[ws.max_row]:
            if isinstance(cell.value,str):cell.data_type='s'
    for c in ws[1]:c.font=Font(color='FFFFFF',bold=True);c.fill=PatternFill('solid',fgColor='176B60')
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width=min(42,max(14,max(len(str(c.value or '')) for c in col)+2))
    return ws


def export(order,path,cancel=None,progress=lambda s:None):
    from engine import checkpoint
    path=Path(path)
    if path.exists():raise FileExistsError('文件已存在，请选择新文件名；不会覆盖已发出的单据')
    checkpoint(cancel);result=order['payload']['result'];wb=Workbook();wb.remove(wb.active)
    rev=f"V{order['revision']:03d}";historical=order['revision']!=order['current_revision']
    table(wb,'概览',['项目','内容'],[
        ['补货单号',order['number']],['修订',rev],['业务日期',order['day']],['门店编码',order['store']],['门店名称',result['store_name']],
        ['版本状态','历史修订，非当前正常单' if historical else '当前签发修订'],['内部确认时间',order['saved']],
        ['签发建议款数',result['summary']['positive']],['签发建议金额（基本售价估算）',result['summary']['amount']],
        ['预算',result.get('budget',{}).get('limit','不限预算')],['待核行数',result['summary']['issues']],['预算暂缓/未补足',result['summary']['budget_deferred']],
        ['说明','此单为内部签发建议，尚非加盟商确认数或实际采购订单。只包含本店签发数量大于0的商品。']])
    headers=['补货单号','修订','明细ID','门店编码','商品编码','商品名称','推荐类型','必卖','归属仓','库存单位','最小出库单位','基本售价','签发建议数','估算金额','月销','可用库存','在途库存','原缺货天数','加盟商确认数','确认说明']
    groups={'动销补货':[],'新品推荐':[],'缺货恢复':[]}
    for d in result['rows']:
        checkpoint(cancel)
        if not sem.orderable(d):continue
        kind=new_products.category(d);sheet=kind if kind in groups else '缺货恢复'
        groups[sheet].append([order['number'],rev,order['payload']['item_ids'][d['E']],order['store'],d['E'],d.get('D',''),kind,'是' if d.get('_must_sell') else '否',d.get('AU'),d.get('G'),d.get('BD'),d.get('V'),sem.order_quantity(d),sem.order_amount(d),d.get('J'),d.get('K'),d.get('L'),d.get('N'),None,None])
    for name,rows in groups.items():
        ws=table(wb,name,headers,rows)
        for row in ws.iter_rows(min_row=2):
            for c in row[18:]:c.fill=PatternFill('solid',fgColor='FFF2CC')
            for c in [row[11],row[13]]:c.number_format='0.00'
        if rows:
            rule=DataValidation(type='decimal',operator='greaterThanOrEqual',formula1=0,allow_blank=True);rule.errorTitle='确认数量无效';rule.error='请填写非负数量；取消请填0，保持请填写签发建议数。';rule.showErrorMessage=True;ws.add_data_validation(rule);rule.add(f'S2:S{len(rows)+1}')
    table(wb,'填写说明',['事项','说明'],[
        ['识别','请保留单号、修订、明细ID和商品编码；一店一单，三个商品页互不重复。'],
        ['动销补货','有日/周/月销量证据的经营补货。新品推荐由运营主动选择；缺货恢复单独查看。'],
        ['确认数','黄色列填写最终确认数量：保持也填写建议数，取消填写0。空白代表尚未确认，不能当0。'],
        ['数量单位','全部为库存单位。箱规品数量须满足最小出库单位倍数；Excel提示不代替后续核验。'],
        ['金额','按基本售价估算，不等同采购结算金额；本单没有自动下单功能。'],
        ['回传','请回传原工作簿；历史确认、实际下单和收货资料的导入关联为后续独立功能。'],
        ['旧修订','如收到同号新修订，核对修订号后处理；旧文件仍对应原修订，不自动合并。']])
    progress('正在生成带号确认单…');path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.normal-',suffix='.xlsx',dir=path.parent);os.close(fd)
    try:
        wb.save(temp);checkpoint(cancel)
        # Windows rename is no-clobber; unlike replace it refuses an existing target.
        os.rename(temp,path)
    finally:
        wb.close()
        if os.path.exists(temp):os.unlink(temp)
    return str(path)
