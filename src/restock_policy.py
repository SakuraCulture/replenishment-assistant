"""Store-scoped optional policies. Missing fields retain historical enabled behavior."""
import copy

LABELS = {'extreme_restore': '极端缺货自动恢复', 'new_stocking': '新品铺设'}
HELP = {'extreme_restore': '有效缺货达到30天时，是否自动恢复一个出库单位。普通缺货、有销量双零最低持有不变。',
        'new_stocking': '是否允许新品首铺及新品清单补足。开启后符合资格的本月关注新品自动带量；往期新品仍手工选择。关闭不删除候选或历史单。'}
KEY = 'restock_switches'

def values(config):
    raw = config.get(KEY, {})
    if not isinstance(raw, dict) or set(raw)-set(LABELS) or any(type(v) is not bool for v in raw.values()):
        raise ValueError('补货策略须为开启或关闭，不能使用数字或文本冒充开关')
    return {k:raw.get(k, True) for k in LABELS}

def patch(config, changes):
    if set(changes)-set(LABELS) or any(type(v) is not bool for v in changes.values()):
        raise ValueError('补货策略值无效')
    out=copy.deepcopy(config); old=values(config); new={**old,**changes}
    # Opening a legacy form must not change its signature or archived content.
    if new==old:return out
    out[KEY]=new
    return out

def result_values(result):
    return values(result.get('config', {}))

def summary(config):
    return ' · '.join(LABELS[k]+'：'+('开启' if v else '关闭') for k,v in values(config).items())

def new_request(row, kind='manual'):
    from new_products import is_new
    return kind=='new' or is_new(row)

def guard_new(result, row=None, kind='manual', quantity=1):
    if quantity>0 and not result_values(result)['new_stocking'] and (row is None or new_request(row,kind)):
        raise ValueError('本店新品铺设已关闭；请先在“调整补货策略”开启，再安排新品数量')
