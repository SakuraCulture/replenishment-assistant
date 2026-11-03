"""Minimum accepted headers; examples never mix with imported business records."""
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font,PatternFill
import os,tempfile

TEMPLATES={
 'warehouse_cycles':('分仓周期','分仓周期',['门店编码','属仓'],['门店名称','备货天数','到货天数']),
 'classified_parameters':('分类维护完整参数','分仓周期',['门店编码','属仓'],[]),
 'store_policy':('补货策略','补货策略',['门店编码'],[]),
 'store_routes':('供货关系','供货关系',['门店编码'],[]),
 'store_budget':('门店预算','门店预算',['门店编码'],[]),
 'store_qualification':('门店二类医疗资质','门店资质',['门店编码','资质状态'],['门店名称','核验人','核验日期','到期日','备注']),
 'store_refusals':('门店拒收清单','门店拒收清单',['门店编码','商品编码','拒收原因'],['门店名称','商品名称','到期日']),
 'store_parameters':('门店参数','门店参数',['门店编码'],['门店名称','主仓','从仓','预算方式','预算金额']),
 'aoxiang_stores':('翱象门店目录','门店目录',['门店编码','门店名称'],[]),
 'stocking':('铺货清单','铺货清单',['商品编码'],['商品名称','目标持有量','备注','关注']),
 'sales':('补货建议','商品补货建议',['仓库/门店编码','商品编码','日销','周销','月销','可用库存','在途库存','缺货天数（天）'],['仓库/门店名称','商品名称','单位类型','补货状态']),
 'catalog':('分仓商品资料','Sheet1',['商品编码','分类','基本售价','出库箱规','商品状态','可用数','商品标签'],['商品名称','商品等级','补货模型分类','实际库存数','采购在途数','订单占有数']),
 'legacy':('旧版混合商品资料','Sheet1',['商品编码','分类','基本售价','出库箱规','商品状态'],['商品名称','仓归属']),
 'priority':('必卖清单','必卖清单',['商品编码'],['商品名称','分类']),
 'focus':('新品关注清单','新品明细',['商品编码','关注'],['商品名称']),
 'feedback':('门店确认回传','门店确认回传',['补货单号','商品编码','门店确认数量'],['商品名称','备注'])}

def write(kind,path,progress=lambda _:None,cancel=None):
    title,sheet,required,optional=TEMPLATES[kind];target=Path(path)
    if target.exists():raise FileExistsError('模板目标已存在，请另存为新文件')
    from parameter_tables import KINDS,write as write_classified
    if kind in KINDS:return write_classified(path,kind,progress=progress,cancel=cancel)
    if kind=='store_parameters':
        from store_parameter_import import write as write_parameters
        return write_parameters(path,progress=progress,cancel=cancel)
    wb=Workbook();ws=wb.active;ws.title=sheet;ws.append(required+optional);ws.freeze_panes='A2'
    for i,c in enumerate(ws[1]):
        c.font=Font(bold=True,color='FFFFFF');c.fill=PatternFill('solid',fgColor='126C63' if i<len(required) else '657B83');ws.column_dimensions[c.column_letter].width=22
        for row in range(2,202):ws.cell(row,i+1).number_format='@' if ('编码' in c.value or c.value=='补货单号') else 'General'
    note=wb.create_sheet('填写说明');note.append(['事项','要求'])
    for item in [('适用内容',title),('必需表头','、'.join(required)),('可选表头','、'.join(optional)),('编码','按文本保存；保留前导零，禁止科学计数法和模糊匹配'),('数量单位','库存单位；回传数量为非负数字，0表示明确不补，空白表示未确认'),('缺失值','保留空白，不能为了通过校验编造0；缺货天数空白按现有规则处理'),('分仓','在导入入口选择河北仓或山东仓；不要以文件名猜测仓库'),('关注','填写关注/未关注、是/否、1/0'),('回传关联','无修订信息的文件，导入时必须明确选择签发修订；也可直接改加盟商确认单Q列回传'),('数据区','第1张表从第2行填写；模板不附可误导入的示例商品')]:note.append(item)
    if kind=='feedback':note.append(('回传供货仓','新单填写原导出的仓子单号（-HB为河北仓，-SD为山东仓），按签发修订校验归属；两仓可一起导入。旧门店总单号仍兼容。不要手工改仓或按当前资料重新判断。'))
    if kind=='catalog':note.append(('商品标签','表头必须保留；内容为空表示无标签，多个标签原样填写。整列缺失只能留作待补资料，不能作为新执行安排依据；包含二类医疗字样须核验门店资质。'))
    if kind in ('store_qualification','store_refusals'):
        note.delete_rows(2,note.max_row)
        entries=[('必需表头','、'.join(required)),('可选表头','、'.join(optional)),('编码','按文本填写；保留前导零，不按名称猜测匹配'),('日期','YYYY-MM-DD；到期当天仍有效；到期日可不填'),('更新','导入预览后确认保存；缺行不删除或解除已有记录；原文件不改动'),('适用范围','同店两仓共用；只影响后续执行，历史签发和收货事实不改变')]
        entries+=([('资质状态','填写待核验、无资质或已核验；已核验必须填写核验人、核验日期'),('无记录','默认为待核验；二类医疗商品在核验完成前不能执行')] if kind=='store_qualification' else [('拒收原因','每条必填；商品不因必卖、保底或人工加量绕过拒收'),('到期日','留空表示持续拒收到手动解除；导入只新增或更新，解除请在门店拒收清单操作')])
        for item in entries:note.append(item)
    if kind=='aoxiang_stores':
        note.delete_rows(2,note.max_row)
        for item in [('必填字段','门店编码、门店名称；无需填写账号、仓供应商或系统版本'),('编码','按文本填写，保留前导零和长编码；按编码关联，不按相似名称合并'),('表头','按表头识别，列位置可调整；可直接使用翱象门店数据导出文件'),('重复','相同编码与名称去重并保留来源行；同码不同名阻断；同名不同码保留'),('核验','导入目录不代表已经核验平台权限；连接时按实际查询编码核对'),('原件','导入后保留原文件快照，不修改原文件')]:note.append(item)
    note.column_dimensions['A'].width=22;note.column_dimensions['B'].width=100
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:wb.save(tmp);os.link(tmp,target)
    finally:wb.close();Path(tmp).unlink(missing_ok=True)
    return str(target)
