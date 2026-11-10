"""Business-only historical calculation projection; no recursive technical tree."""
from contextlib import closing
import io
import openpyxl
import result_semantics as sem
from runtime_support import checkpoint

INPUTS=[('库存单位','G'),('日销（原14天日均）','H'),('近7天销量','I'),('近30天销量','J'),
        ('可用库存','K'),('在途库存','L'),('原在途库存天数','M'),('原缺货天数','N'),
        ('实际计算缺货天数','BA'),('周转天数','P'),('最小出库单位','BD'),('基本售价','V'),
        ('商品状态','S'),('模型权重分类','U'),('补货建议商品标签','T'),('商品等级','AV')]
STAGES=[('近7天参考日均','W'),('8至14天参考日均','Z'),('15至30天参考日均','AA'),
        ('近7天有货天数','AX'),('8至14天有货天数','AY'),('15至30天有货天数','AZ'),
        ('销售状态','BL'),('校正综合日均','AN'),('趋势修正系数','AM'),('安全库存量','AL'),
        ('保护期预测基值','AO'),('趋势后需求','AP'),('净需求','AQ'),('常规规整量','AR'),
        ('常规补货资格','BI'),('库存额度参考日均','BH'),('增长额度参考日均','BN'),
        ('库存额度目标量','库存额度目标量'),('额度允许数量','BK'),('资金金额上限','BT'),
        ('资金允许整箱数量','BU'),('最低恢复参考量','BQ'),('原计算建议量','AS')]
PARAMETERS=('cap_days','probability','single_sample_multiple','strict_monthly','exit_monthly',
            'monthly_extra_money','weak_extra_money','historical_reference_days','extreme_days',
            'minimum_reference_units','growth_reference_days')
SOURCE_FIELDS={'商品编码','商品标签','虚拟分类','供应商编码','供应商名称','可用数','实际库存数','采购在途数','采购状态'}


def master_snapshot(repo,order,warehouse,skus,cancel=None):
    """Read selected business fields from the retained original, never current data."""
    if repo is None:return {},'历史原表未读取；仅使用订单已留存字段'
    import history_forms,source_archive
    parts=history_forms.work_parts(repo,order,[('catalogs',)])
    catalog=parts.get(('catalogs',),{}).get(warehouse)
    if not catalog:return {},'历史未留存可独立识别的本仓原文件；缺失字段未补填'
    with closing(repo.work.connect()) as db:stored=source_archive.read(db,catalog.get('source_sha256'))
    if not stored:return {},'历史原文件未留存；缺失字段未用当前资料补填'
    book=openpyxl.load_workbook(io.BytesIO(stored[1]),read_only=True,data_only=True,keep_links=False)
    try:
        ws=book[catalog['sheet']];ws.reset_dimensions();it=iter(ws.values);heads=[str(v or '').strip() for v in next(it,())]
        if heads.count('商品编码')!=1:return {},'历史原表商品编码列不唯一；未猜测对应关系'
        used={h:heads.index(h) for h in SOURCE_FIELDS if heads.count(h)==1}
        from input_adapter import identifier
        result={};position=used['商品编码'];invalid=False
        for values in it:
            checkpoint(cancel)
            sku,error=identifier(values[position] if position<len(values) else None)
            if error:invalid=True;continue
            if sku not in skus:continue
            if sku in result:raise ValueError('历史仓原表商品编码重复，未猜测资料：'+sku)
            result[sku]={h:values[i] if i<len(values) else None for h,i in used.items()}
        duplicate=sorted(h for h in SOURCE_FIELDS if heads.count(h)>1)
        notes=[]
        if duplicate:notes.append('历史原表部分业务列重复，未取第一列：'+'、'.join(duplicate))
        if invalid:notes.append('历史原表有编码无效行，未猜测对应关系')
        return result,'；'.join(notes)
    finally:book.close()


def projected(order,warehouse,*,repo=None,cancel=None):
    import archive_orders as ao
    from parameter_schema import HELP
    result=order['payload']['result']
    rows=[d for d in result['rows'] if ao.warehouse(d)==warehouse]
    originals,note=master_snapshot(repo,order,warehouse,{d['E'] for d in rows},cancel)
    config=result.get('effective_settings',{}).get('parameter_config') or result.get('config') or {}
    params=config.get('parameters',{})
    def build(d):
        checkpoint(cancel);m=d.get('_meta',{});raw=originals.get(d['E'],{});route=m.get('routing',{})
        profile='快销' if d.get('U')=='无分类' else d.get('U')
        actual={**params,**config.get('profile_parameters',{}).get(profile,{})}
        if '_effective_probability' in d:actual['probability']=d['_effective_probability']
        audit=d.get('_mandatory_holding',{});manual=d.get('_preissue',{})
        q=sem.order_quantity(d)
        rate=d['AN']*d['AM'] if all(sem.numeric(d.get(k)) for k in ('AN','AM')) else None
        coverage=(max(d['K'],0)+max(d['L'],0)+q)/rate if rate and rate>0 and all(sem.numeric(v) for v in (d.get('K'),d.get('L'),q)) else None
        record={'业务日期':order['day'],'签发单号':ao.number(order['number'],warehouse),'签发修订':order['revision'],
                '门店编码':result.get('store',order.get('store')),'门店名称':result.get('store_name'),
                '商品编码':d['E'],'商品名称':d.get('D'),'供货仓':warehouse,
                '商品类目':d.get('_category',m.get('category')),
                '仓商品标签':m.get('product_tags') if m.get('product_tags_known') else raw.get('商品标签'),
                '虚拟分类':raw.get('虚拟分类'),'补货状态':m.get('export_status'),'采购状态':raw.get('采购状态'),
                '仓上游供应商编码':raw.get('供应商编码'),'仓上游供应商名称':raw.get('供应商名称'),
                '供货仓可用库存':m.get('warehouse_available',raw.get('可用数')),'供货仓实际库存':raw.get('实际库存数'),
                '供货仓采购在途':raw.get('采购在途数'),'主仓':route.get('primary'),'从仓':route.get('secondary'),
                '主仓可用库存':route.get('primary_stock'),'从仓可用库存':route.get('secondary_stock'),
                '资料匹配状态':d.get('_source'),'是否必采':d.get('_must_sell'),'排除原因':sem.exclusion_reason(d),
                '备货深度（天）':d.get('AG'),'到货周期（天）':d.get('AH'),
                '保护期（天）':d['AG']+d['AH'] if all(sem.numeric(d.get(k)) for k in ('AG','AH')) else None,
                '趋势敏感度':d.get('AB'),'近7天权重':d.get('AC'),'此前7天权重':d.get('AD'),'此前16天权重':d.get('AE'),
                '实际周转资格阈值':d.get('AI'),'实际低销价格门槛':d.get('AJ')}
        record.update({name:d.get(key) for name,key in INPUTS})
        record.update({'参数·'+HELP[key][0]:actual.get(key) for key in PARAMETERS})
        record.update({name:d.get(key) for name,key in STAGES})
        record.update({'原模型建议量':d.get('_base_qty'),'一箱例外追加量':d.get('_audit',{}).get('applied') and d.get('BD') or 0,
                       '必采最低持有基数':audit.get('minimum'),'必采最低补货量':audit.get('required_quantity'),
                       '必采保底追加量':audit.get('added_quantity'),'预算前候选量':d.get('_candidate_qty'),
                       '预算分配量':d.get('_allocated_qty') if d.get('_budget_active') else None,
                       '门店预算（全店共用）':result.get('budget',{}).get('limit'),'预算缺口数量':d.get('_budget_gap'),
                       '人工改量差额':manual.get('delta'),'人工改量原因':manual.get('reason'),
                       '补后预计持有天数':coverage,'执行状态':sem.order_status(d),'业务说明':sem.explanation(d),
                       '资料说明':'；'.join(filter(None,[note,sem.issue_text(d)])),
                       '模型版本':order['payload'].get('model_version',result.get('version',{}).get('model_version')),
                       '最终签发执行数':q,'最终签发金额':sem.order_amount(d),'核心决策理由':__import__('normal_exports').decision(d)})
        return record
    from itertools import chain
    records=(build(d) for d in rows);first=next(records,None)
    headers=list(first) if first is not None else ['业务日期','门店编码','门店名称','商品编码','商品名称','供货仓','最终签发执行数','最终签发金额']
    return headers,([row.get(key) for key in headers] for row in chain([first],records) if row is not None),len(rows)


def definitions():
    rows=[['范围','历史签发修订的业务分析表；不是回传、付款或平台采购模板。'],
          ['资料来源','只用签发快照与当时留存原表；不读取当前仓商品资料或当前参数补填历史。'],
          ['字段缺失','历史未记录的字段保持空白；未知不当作0。原始商品资料请使用独立原表下载入口。'],
          ['数量链路','原模型建议→一箱例外后的原计算建议→必采最低持有→预算候选/分配→人工调整→最终签发。'],
          ['周期与参数','采用商品当时实际仓周期、分类权重与分类覆盖；通用参数保留当时有效配置。'],
          ['预算','门店预算为全店共享金额，不能把两仓表中的预算相加。'],
          ['补后预计持有天数','按（非负可用＋非负在途＋最终签发执行数）÷当时预测日均；无正预测留空。'],
          ['业务字段','商品类目与模型权重分类分别保存；供货仓库存来自当时资料，非当前实时库存。'],
          ['技术信息','不展开文件路径、校验指纹、临时ID、进程、来源数组及未参与计算的旧参数。']]
    return ['项目','口径说明'],rows
