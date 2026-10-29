"""Ordered UI/export field definitions; single and batch report scopes remain distinct."""
from result_semantics import *

COLUMNS=[('商品名称',lambda d:d.get('D')),('商品编码',lambda d:d.get('E')),('来源',lambda d:d['_source']),('所属仓',lambda d:d.get('AU')),
 ('分类',lambda d:d.get('_category')),('月销',lambda d:d.get('J')),('可用库存',lambda d:d.get('K')),('在途',lambda d:d.get('L')),
 ('箱规',lambda d:d.get('BD',d.get('O'))),('执行补货',order_quantity),('执行金额',order_amount),('例外',lambda d:'一箱放行' if d.get('_audit',{}).get('applied') else ''),
 ('补货建议状态',lambda d:d['_meta'].get('export_status')),('原因',explanation),('商品资料状态',lambda d:d.get('S')),
 ('执行状态',order_status),('排除原因',exclusion_reason),('计算建议',quantity),('计算金额',amount_value),
 ('优先清单',lambda d:'必卖' if d.get('_must_sell') else '普通'),('预算前执行量',lambda d:d.get('_candidate_qty',quantity(d))),
 ('预算前金额',lambda d:d.get('_candidate_amount',amount_value(d))),('预算缺口数量',lambda d:d.get('_budget_gap',0)),
 ('最低持有缺口',lambda d:'有销量且无可用/在途，本次资金未覆盖' if d.get('_holding_gap') else ''),('资金分配说明',lambda d:d.get('_budget_reason','未启用总预算'))]

COLUMNS.extend([('主仓',lambda d:routing(d).get('primary')),('从仓',lambda d:routing(d).get('secondary')),('需求归属说明',lambda d:routing(d).get('reason')),('主仓可用数',lambda d:routing(d).get('primary_stock')),('从仓可用数',lambda d:routing(d).get('secondary_stock')),('主仓判断',lambda d:routing(d).get('primary_state')),('从仓判断',lambda d:routing(d).get('secondary_state')),('商品资料来源',lambda d:d.get('_meta',{}).get('product_file')),('仓资料版本',lambda d:d.get('_meta',{}).get('product_revision'))])

IMPORT_HEADERS=['类型','门店编码','商品编码','来源文件','工作表','源行 / 行数','保留选择 / 状态','SHA256']
def import_audit_rows(result):
    audit=result.get('import_audit',{})
    yield ['一致重复去重数量','','','','',audit.get('identical_duplicates',0),'','']
    for f in audit.get('files',[]):yield ['读取文件','','',f['path'],'',f['rows'],f['error'] or '成功',result.get('sources',{}).get(f['path'],'')]
    for c in audit.get('conflicts',[]):
        for i,source in enumerate(c['sources']):yield ['重复冲突',c['store'],c['sku'],source['file'],source['sheet'],source['row'],'保留' if c['choice']==i else '未处理' if c['choice'] is None else '未选','']


def single_headers():
    return [h for h,f in COLUMNS]+['周销','14天日均','缺货天数','基本售价','原模型建议','原模型金额','例外新增金额','预测日均','BH额度日均','BN增长额度日均','库存额度总量','按计算建议持有天数','备注','导出源行','资料源行']

def single_values(d):
    r=d['AN']*d['AM'] if numeric(d.get('AN')) and numeric(d.get('AM')) else None
    return [f(d) for h,f in COLUMNS]+[d.get('I'),d.get('H'),d.get('N'),d.get('V'),d.get('_base_qty'),d.get('_base_amount'),d.get('_audit',{}).get('incremental_amount',0),r,d.get('BH'),d.get('BN'),d.get('库存额度目标量'),coverage(d),'；'.join(d['_meta']['notes']),d['_meta']['export_row'],d['_meta'].get('product_row')]

def batch_headers():
    return ["门店编码","门店名称"]+[h for h,f in COLUMNS]+["来源文件","来源工作表","来源行"]
