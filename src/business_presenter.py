"""Read-only business projections. Never write snapshots or run the model.

Legacy Excel labels are intentionally not changed by this display vocabulary.
"""
from decimal import Decimal
from pathlib import PureWindowsPath
from functools import lru_cache
import re
import result_semantics as sem

MISSING = '历史未留存'
NAMES = {
    'AB':'趋势敏感度','AC':'最近7天权重','AD':'此前7天权重','AE':'此前16天权重',
    'AF':'旧安全系数（当前未使用）','AN':'校正综合日均','AM':'趋势修正系数',
    'AS':'例外后计算建议量','_base_qty':'原模型建议量','_base_amount':'原模型建议金额',
    '_candidate_qty':'预算前候选量','_candidate_amount':'预算前候选金额',
    '_allocated_qty':'预算分配量','_allocated_amount':'预算分配金额',
    '_execution_qty':'执行覆盖数量','_execution_amount':'执行覆盖金额',
    '_budget_gap':'预算未满足量','BQ':'最低恢复参考量','BD':'最小出库数量',
    'BH':'库存上限参考日均','BN':'增长额度参考日均','AG':'备货天数','AH':'到货天数',
    'CV':'阶段波动诊断','_category':'商品分类','_source':'资料匹配结果',
    '_meta':'来源与校验','_audit':'一箱例外依据','_inbound_cap':'在途保底限制',
    '_preissue':'签发前调整','_budget_overrun':'预算末尾补足','_new_requested':'新品首铺申请量',
    'effective_settings':'实际设置','parameter_config':'参数配置','parameters':'模型参数',
    'weight_profiles':'分类权重','profile_parameters':'分类超参','category_profiles':'分类映射',
    'warehouses':'仓周期','exception':'一箱例外','budget':'门店预算','supply_route':'供货设置',
    'primary':'主仓','secondary':'从仓','preset':'参数方案','config':'配置',
    'sources':'输入来源','hashes':'文件校验值','new_focus':'新品关注清单','new_products':'新品选择',
    'alpha':'趋势敏感度','w1':'最近7天权重','w2':'此前7天权重','w3':'此前16天权重',
    'store':'门店编码','name':'名称','sku':'商品编码','quantity':'数量','amount':'金额',
    'before_qty':'原数量','after_qty':'新数量','before_amount':'原金额','after_amount':'新金额',
    'before_candidate':'原候选量','after_candidate':'新候选量','holding_gap':'最低持有缺口',
    'reason':'原因','applied':'是否生效','enabled':'是否启用','limit':'设定预算',
    'source':'来源','path':'文件位置','file':'文件','sheet':'工作表','row':'来源行',
    'export_file':'补货建议文件','export_sheet':'补货建议工作表','export_row':'补货建议来源行',
    'product_file':'商品资料文件','product_row':'商品资料来源行','product_revision':'商品资料版本',
    'notes':'说明','blocking':'待核原因','routing':'供货判断','match':'匹配结果',
    'primary_stock':'主仓可用库存','secondary_stock':'从仓可用库存','primary_state':'主仓判断',
    'secondary_state':'从仓判断','assigned':'归属仓','sha256':'文件校验值','revision':'记录版本',
    'old_revision':'原记录版本','new_revision':'新记录版本','previous_revision':'前次记录版本',
    'gid':'生成记录标识','bid':'库存基准标识','summary':'数量金额汇总','positive':'安排商品款数',
    'shortages':'缺口商品款数','issues':'待核商品款数','type':'类型','old':'原值','new':'新值',
    'source_path':'原文件位置','source_sha256':'原文件校验值','imported_at':'导入时间',
}
PERCENT={'AC','AD','AE','w1','w2','w3','probability','_effective_probability'}
DAYS={'AG','AH','N','BA','M','P','AX','AY','AZ','depth','lead','cap_days','extreme_days','historical_reference_days','growth_reference_days','moq_days'}
QUANTITIES=set('I J K L O AL AO AP AQ AR AS BD BF BJ BK BO BP BQ BR BS BU BX'.split())|{'_base_qty','_candidate_qty','_allocated_qty','_execution_qty','_budget_gap','_new_requested'}
RATES=set('H W X Y Z AA AN BC BH BN'.split())
MONEY={'V','BV','BT','amount','before_amount','after_amount','_base_amount','_candidate_amount','_allocated_amount','_execution_amount','monthly_extra_money','weak_extra_money','moq_price'}

@lru_cache(maxsize=1)
def names():
    # Import lazily: these modules also expose immutable legacy export contracts.
    from archive_exports import LETTER_LABELS
    from parameter_schema import HELP
    return {**LETTER_LABELS,**{k:v[0] for k,v in HELP.items()},**NAMES}

def title(key):
    return names().get(key,key)

def path_title(path):
    return ' / '.join(title(p) for p in str(path).split(' / '))

def business_help(text):
    return re.sub(r'(?<![A-Za-z0-9_])(?:BH|BN|AM|AF|AK|CV|V|SKU|w1|w2|w3)(?![A-Za-z0-9_])',lambda m:'商品' if m[0]=='SKU' else title(m[0]),str(text)).replace('min(此值,最小出库单位)','本值与最小出库数量中的较小值').replace('固定30；当前核心同时有硬编码30，仅展示。改变需单独修改模型并验收。','目前固定为30天，仅供查看；不能通过此表单修改。')

def value_text(value, key='', *, unit='', missing='未提供'):
    if value is None:return '不限预算' if key=='budget' else missing
    if value is True:return '是'
    if value is False:return '否'
    if value=='':return '空白'
    if isinstance(value,(int,float,Decimal)) and not isinstance(value,bool):
        if key in PERCENT:return format(Decimal(str(value))*100,'f').rstrip('0').rstrip('.')+'%' if Decimal(str(value))*100%1 else str(int(Decimal(str(value))*100))+'%'
        if key in MONEY:return f'{value:,.2f}'
        return str(value)
    if isinstance(value,(list,tuple)):return '、'.join(value_text(v) for v in value) or '空清单'
    if isinstance(value,dict):return '；'.join(title(k)+'：'+value_text(v,k) for k,v in value.items()) or '空记录'
    return str(value)

def field_unit(key,d=None):
    u=str((d or {}).get('G') or '库存单位')
    if key in DAYS:return '天'
    if key in QUANTITIES:return u
    if key in RATES:return u+'/天'
    if key in MONEY:return '元/'+u if key=='V' else '元'
    if key in PERCENT:return '%'
    if key in ('AB','alpha','AM','single_sample_multiple'):return '倍'
    from parameter_schema import HELP
    return '库存单位' if key in ('strict_monthly','exit_monthly') else HELP.get(key,('','',''))[1]

def field_row(d,key,why=''):
    return [title(key),value_text(d.get(key),key,missing=MISSING) if key in d else MISSING,field_unit(key,d),why]

def flat_rows(value,prefix='',technical=False):
    """Technical fallback retains every key; never interpreted as a business step."""
    out=[]
    def walk(v,parts):
        if isinstance(v,dict) and v:
            for k,x in v.items():walk(x,parts+[str(k)])
        elif isinstance(v,(list,tuple)) and v:
            for i,x in enumerate(v):walk(x,parts+[str(i+1)])
        else:
            key=parts[-1] if parts else ''
            out.append([' / '.join(title(k) for k in parts),value_text(v,key),field_unit(key),('原字段：'+' / '.join(parts)) if technical else ''])
    walk(value,[prefix] if prefix else [])
    return out

def stage_rows(d,final_label='最终签发量'):
    """Show persisted stages, not a counterfactual rerun of any rule."""
    unit=str(d.get('G') or '库存单位');adjust=d.get('_preissue') or {}
    original=adjust.get('original_fields',{})
    def prior(key):
        entry=original.get(key)
        return entry.get('value') if entry and entry.get('present') else None if entry else d.get(key)
    candidate=prior('_candidate_qty')
    automatic=adjust.get('automatic_quantity',sem.order_quantity(d))
    stages=[('原模型建议量',d.get('_base_qty'),'模型内部包含预测、约束与恢复规则；不把最低恢复参考量当作实际增量'),
            ('一箱例外后',d.get('AS'),(d.get('_audit') or {}).get('reason','历史未留存例外判断')),
            ('预算前候选量',candidate,'包含执行排除及已选择的新品；按当时保存值'),
            ('自动安排量',automatic,'预算与排除后的自动安排；不含签发前人工调整'),
            (final_label,sem.order_quantity(d),adjust.get('reason') or sem.explanation(d))]
    out=[];previous=None
    for label,v,why in stages:
        delta='；较上一阶段 '+format(Decimal(str(v))-Decimal(str(previous)),'+f')+unit if sem.numeric(v) and sem.numeric(previous) else ''
        out.append([label,value_text(v,missing=MISSING),unit,str(why)+delta]);previous=v
    return out

def parameter_rows(result,d):
    from parameter_schema import HELP
    cfg=(result.get('effective_settings') or {}).get('parameter_config') or result.get('config') or {}
    params=cfg.get('parameters',{});profile='快销' if d.get('U')=='无分类' else d.get('U')
    overrides=cfg.get('profile_parameters',{}).get(profile,{})
    rows=[field_row(d,k,'商品当时实际取值') for k in ('AU','U','AG','AH','AB','AC','AD','AE')]
    for k,(name,u,helptext) in HELP.items():
        if k in ('depth','lead'):continue
        val=overrides.get(k,params.get(k));why='分类覆盖：'+str(profile) if k in overrides else '当时门店通用值'
        if k=='probability' and '_effective_probability' in d:val=d['_effective_probability'];why='商品留存的实际值'
        rows.append([name,value_text(val,k,missing=MISSING),field_unit(k,d),why+'；'+business_help(helptext)])
    effective=result.get('effective_settings') or {}
    budget=effective['budget'] if 'budget' in effective else result['budget'].get('limit') if 'budget' in result else MISSING
    rows.extend([['门店预算',value_text(budget,'budget'),'元','门店共用预算，不是单仓预算'],['一箱例外',value_text(result.get('enabled'),missing=MISSING),'','当时开关']])
    return rows

HEADERS=['项目','当时值','单位','用途 / 变化说明']
def historical(repo,order,scope,sku,cancel=None):
    from runtime_support import checkpoint
    import history_forms
    r=order['payload']['result'];wh=order.get('warehouse')
    d=next((x for x in r['rows'] if x.get('E')==sku and (not wh or x.get('AU')==wh)),None)
    checkpoint(cancel)
    if scope=='完整参数与技术资料':return flat_rows({'实际设置':r.get('effective_settings'),'方案':order['payload'].get('preset'),'配置':r.get('config'),'来源':r.get('sources')},technical=True)
    if scope in ('单据概况','当时商品资料快照','签发前调整'):
        return [[path_title(k),v,'' if t in ('文字','数字','开关','缺失') else t,''] for k,v,t in history_forms.form(repo,order,scope,sku,cancel)]
    if d is None:return [['商品','请选择本单商品','','未使用其他订单或当前资料补填']]
    if scope=='当时参数':return parameter_rows(r,d)
    if scope=='商品完整计算依据':return flat_rows(d,technical=True)
    if scope=='数量形成过程':return stage_rows(d)
    if scope=='商品建议与销量库存':return [field_row(d,k) for k in ('D','E','AU','H','I','J','K','L','N','BA','BD','V')]
    import normal_exports
    if scope=='决策总览':
        rows=[field_row(d,k) for k in ('D','AU')]
        rows += [['最终签发量',value_text(sem.order_quantity(d),missing='待核'),str(d.get('G') or '库存单位'),'当前记录的最终数量'],
                 ['最终签发金额',value_text(sem.order_amount(d),'BV',missing='待核'),'元','按当时基本售价'],
                 ['核心理由',normal_exports.decision(d),'','详见数量形成过程']]
        a=d.get('_preissue')
        if a:rows += [['人工 / 清单调整',value_text(a.get('delta')),str(d.get('G') or '库存单位'),a.get('reason','')]]
        rows += [field_row(d,k,'当时快照') for k in ('J','K','L','AG','AH','BD')]
        rows += [['复查提示','所有数据来自当时记录','','未留存的阶段不推测；原字母与完整资料在详细依据中']]
        return rows
    raise ValueError('未知核对范围')

def file_name(path):return PureWindowsPath(str(path)).name

def readable_changes(rows):
    for row in rows:
        v=list(row)
        if len(v)==7:
            v[3]=path_title(v[3]);v[4]=path_title(v[4])
        yield v
