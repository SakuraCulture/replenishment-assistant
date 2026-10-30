"""原模型之后的一箱例外。只覆盖最终输出，不改预测、额度或资金计算。

apply_one_box_exception 返回 (结果, 审计)。关闭开关或未命中时原样返回结果。
元数据须来自可信输入适配器；缺少状态/匹配信息时不放行。
"""
from copy import deepcopy
from model import number

RULE_VERSION = 'one-box-arrival-exception-v1'


def assess(result, metadata):
    """保守检查资格，并确认只是在额度/资金层被挡住。"""
    d, m = result, metadata or {}
    audit = {'rule_version': RULE_VERSION, 'eligible': False, 'applied': False,
             'reason': '', 'original_quantity': d.get('AS'),
             'original_amount': d.get('BV')}
    def reject(reason):
        audit['reason'] = reason
        return audit
    if m.get('match') != '商品编码精确匹配' or m.get('blocking') != []:
        return reject('输入或商品资料未确认')
    if m.get('export_status') != '正常':
        return reject('导出补货状态不是正常')
    if d.get('S') != '启用':
        return reject('中心仓商品状态不是启用')
    if d.get('AW') != '可计算' or d.get('BI') != 1:
        return reject('未通过原计算或原补货资格')
    required = ['AS','BV','BD','V','H','I','J','K','L','AN','AM','AH','BJ','BK','AR','BR','BU']
    if not all(number(d.get(c)) for c in required):
        return reject('原结果或输入包含待核值')
    if d['AS'] != 0:
        return reject('原模型已有补货建议，不额外加箱')
    if d.get('_inbound_cap'):
        return reject('已有在途：追加保底受库存额度限制，不自动补回一箱')
    if d['BD'] <= 1:
        return reject('非箱规商品，不属于本次例外范围')
    if d['V'] <= 0 or min(d['H'],d['I'],d['J'],d['K'],d['L']) < 0:
        return reject('价格或销售库存值待核')
    if max(d['H'],d['I'],d['J']) <= 0:
        return reject('无真实销量，不扩张零销量恢复规则')
    rate = d['AN'] * d['AM']
    if rate <= 0 or d['AH'] <= 0:
        return reject('没有有效正预测或到货时间')
    days = (max(d['K'],0) + max(d['L'],0)) / rate
    audit.update(forecast_daily=rate, current_days=days, lead_days=d['AH'], step=d['BD'])
    if days >= d['AH'] - 1e-8:
        return reject('现有可用和在途足够覆盖到货时间')
    if d['BJ'] > 0 and d['BK'] == 0 and d['AR'] == 0:
        blocked = '库存额度'
    elif d['BR'] > 0 and d['BU'] == 0:
        blocked = '资金约束'
    else:
        return reject('原零补货并非由库存额度或资金约束造成')
    if blocked=='库存额度' and d['L']>0:
        # Also protect positive shelf stock + inbound: that row need not have
        # entered BP's zero-shelf floor, but the wrapper must respect its cap.
        audit.update(inbound_cap_blocked=True,blocked_by=blocked)
        return reject('已有在途：库存额度拦截，不自动补回一箱')
    audit.update(eligible=True, reason='符合一箱例外', blocked_by=blocked)
    return audit


def apply_one_box_exception(result, metadata, *, enabled=True):
    if not isinstance(enabled, bool):
        raise ValueError('enabled必须为布尔值')
    if not enabled:
        return result, {'rule_version': RULE_VERSION, 'eligible': False,
                        'applied': False, 'reason': '开关关闭'}
    audit = assess(result, metadata)
    if not audit['eligible']:
        return result, audit
    out = deepcopy(result)
    step = result['BD']
    out['AS'] = step
    out['BV'] = step * result['V']
    out['BM'] = (f"一箱例外：原建议0；最终补货{step:g}个库存单位；"
                 f"现有可售{audit['current_days']:.4f}天，小于到货{result['AH']:g}天；"
                 f"原拦截为{audit['blocked_by']}；仅一箱豁免库存额度和资金约束")
    out['BW'] = (result.get('BW') or '') + '一箱例外：豁免库存额度和资金约束；'
    audit.update(applied=True, final_quantity=step, final_amount=out['BV'],
                 incremental_amount=out['BV']-result['BV'],
                 baseline_reason=result.get('BM'),
                 post_days=(max(result['K'],0)+max(result['L'],0)+step)/audit['forecast_daily'])
    return out, audit
