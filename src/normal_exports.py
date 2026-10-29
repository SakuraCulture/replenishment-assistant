"""Merchant template: three visible sheets, twenty visible columns, frozen provenance."""
import os,re,tempfile
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font,Color
from openpyxl.comments import Comment
from openpyxl.packaging.custom import StringProperty
from openpyxl.utils import get_column_letter
import result_semantics as sem
import new_products
import new_product_focus as focus

HEADERS=['仓库/门店名称','仓库/门店编码','供应商编码','商品名称','商品编码','商品条码','基础单位','日销','周销','月销','可用库存','在途库存','在途库存天数','缺货天数（天）','箱规','中心仓价格','补货数量','黑名单状态','所属仓','商品等级']
AUDIT_HEADERS=['补货单号','修订','明细ID','签发建议数','新品识别依据','分组依据','关注清单版本','安排说明']
SHEETS=['往期新品','本月新品','中心仓动销补货清单']
TEMPLATE_SHA256='1cc7a8e9f99fec764d17286c6b0a028a7ea79416add7178a64e91512f311f7c6'


def filename(order):
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',order['payload']['result']['store_name']).rstrip(' .')[:100] or order['store']
    return order['day']+name+'（精简版）('+str(order['revision'])+').xlsx'


def groups(result,profile):
    """New pool has precedence, including anomalous sales rows: no duplicate SKU."""
    focused=focus.members(profile);out={name:[] for name in SHEETS}
    for d in result['rows']:
        if new_products.is_new(d):out['本月新品' if d['E'] in focused else '往期新品'].append(d)
        elif sem.orderable(d):out['中心仓动销补货清单'].append(d)
    return out


def export(order,path,cancel=None,progress=lambda s:None):
    from engine import checkpoint
    path=Path(path)
    if path.exists():raise FileExistsError('文件已存在，请选择新文件名；不会覆盖已发出的单据')
    checkpoint(cancel);result=order['payload']['result'];profile=order['payload'].get('new_focus');focus.require_for(profile,order['day'],[result]);wb=Workbook();wb.remove(wb.active)
    rev=f"V{order['revision']:03d}";historical=order['revision']!=order['current_revision'];font=Font(name='宋体',sz=11,charset=134,scheme='minor',color=Color(theme=1))
    wb.properties.title=order['number']+' '+rev;wb.properties.subject='内部签发建议，非实际采购订单';wb.properties.description='关注清单随修订冻结；Q列可供门店调整，X列保留签发建议；数量为库存单位，价格为基本售价。'
    for key,value in [('补货单号',order['number']),('修订',rev),('模板SHA256',TEMPLATE_SHA256),('版本状态','历史修订' if historical else '当前修订')]:wb.custom_doc_props.append(StringProperty(name=key,value=value))
    for name,rows in groups(result,profile).items():
        ws=wb.create_sheet(name);ws.sheet_format.defaultColWidth=9;ws.sheet_format.defaultRowHeight=13.5;ws.append(HEADERS+AUDIT_HEADERS)
        ws.oddHeader.center.text=order['number']+' '+rev+(' 历史修订' if historical else '')
        ws['A1'].comment=Comment(order['number']+' '+rev+'；业务日期 '+order['day']+'；'+('历史修订' if historical else '当前修订'),'补货助手')
        ws['Q1'].comment=Comment('签发执行数量（库存单位）；未选择或预算未安排的新品为0。门店可修改本列，原签发建议及单号保存在隐藏列。箱规品请填写箱规倍数。','补货助手')
        ws['P1'].comment=Comment('取商品资料基本售价；缺价保持空白或待核，不补造0。','补货助手')
        for rn,d in enumerate(rows,2):
            checkpoint(cancel);qty=sem.order_quantity(d) if sem.orderable(d) else 0
            reason=sem.exclusion_reason(d);status=d.get('AT')
            visible=[d.get(k) for k in 'A B C D E F G H I J K L M N'.split()]+[d.get('BD'),d.get('V'),qty,status,d.get('AU'),d.get('AV')]
            basis=('缺货天数空白按0；原值保留' if d.get('N') in (None,'') else '缺货天数为0') if new_products.is_new(d) else ''
            explanation=sem.explanation(d)
            audit=[order['number'],rev,order['payload']['item_ids'][d['E']],qty,basis,'本店新品且关注' if name=='本月新品' else '本店新品且未关注' if name=='往期新品' else '原模型自动建议并获执行安排',(profile or {}).get('revision'),explanation]
            ws.append(visible+audit)
            if reason or qty==0:ws.cell(rn,17).comment=Comment(explanation,'补货助手')
        for row in ws:
            for cell in row:
                cell.font=font
                if isinstance(cell.value,str):cell.data_type='s'
        # Match the supplied plain template: General format, no filters/freezes,
        # fills/borders or width overrides. Audit columns alone are hidden.
        for i in range(21,29):ws.column_dimensions[get_column_letter(i)].hidden=True
        ws.print_options.horizontalCentered=False
    progress('正在生成模板格式的带号清单…');path.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.normal-',suffix='.xlsx',dir=path.parent);os.close(fd)
    try:
        wb.save(temp);checkpoint(cancel);os.rename(temp,path)
    finally:
        wb.close()
        if os.path.exists(temp):os.unlink(temp)
    return str(path)
