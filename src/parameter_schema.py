"""Complete editable configurations around the immutable model, with business help."""
import copy,hashlib,json
import engine,model

# key: Chinese name, unit, explanation and adjustment guidance
HELP={
 'depth':('备货深度','天','与到货天数组成保护期；调高通常增加需求和资金。按补货节奏维护，别重复加到货时间。仓设置优先于通用值。'),
 'lead':('到货时间','天','影响保护期、一箱例外及预算紧急程度。调高可能增加需求与紧急商品，按实际到货周期维护。仓设置优先。'),
 'cap_days':('普通库存额度天数','天','调低收紧常规补货，调高放宽；0为关闭额度。按BH/BN额度日均计算，并非预测持有天数硬上限；恢复和一箱例外仍存在。'),
 'probability':('库存目标分位概率','概率','调高通常增加安全库存，调低减少；不是实际不断货率承诺，也不是固定安全天数。先试算再应用。'),
 'single_sample_multiple':('单段样本预测倍数','倍','仅剩早期样本时调高放宽预测与部分额度，调低更保守。不是所有商品放大倍数；安全库存和整箱另计。'),
 'strict_monthly':('严格约束月销上界','个','调高使更多低销量商品严格受单品资金约束；调低更早释放。必须小于退出月销，不替代门店预算。'),
 'exit_monthly':('完全退出约束月销','个','达到此月销退出低销量资金层。调高延后退出，调低更早放宽；必须大于严格月销。'),
 'monthly_extra_money':('销量侧额外金额','元','单品基准中月销量×基本售价+此金额。调高可能放宽；若弱证据分支更小则无变化，不是每SKU固定加钱。'),
 'weak_extra_money':('弱证据额外金额','元','单品基准中扣掉最大一段的销量×基本售价+此金额。调高可能放宽，调低收紧；受另一分支及最低出库成本影响，也影响成本过高提示。'),
 'historical_reference_days':('单段额度参考天数','天','单段BH分母参考；调高通常收紧额度，调低放宽。不等于实际保护期，也不会自动跟随仓周期。'),
 'extreme_days':('极端缺货边界','天','固定30；当前核心同时有硬编码30，仅展示。改变需单独修改模型并验收。'),
 'minimum_reference_units':('最低覆盖判断量','库存单位','在途与min(此值,最小出库单位)比较。调高可能增加恢复触发；不是补货目标数量，触发后仍按最小出库单位补。'),
 'growth_reference_days':('增长额度确认天数','天','有效有货天数/此值形成最高1的系数。调小更快认可增长、调大更慢；有效天数足够后可能无变化。'),
 'moq_days':('常规资格周转阈值','天','周转不超过此值可过原资格之一；调高放宽、调低收紧。一箱例外也要求原资格；不是最小起订量。'),
 'moq_price':('低月销基本售价门槛','元','月销≤5且基本售价V严格小于此值可过另一资格；调高扩大范围。用基本售价，不是采购价。')}
WEIGHT_HELP={'alpha':'趋势敏感度：调高通常更响应近期涨跌，调低更稳；仍受趋势支撑与限幅影响，0不等于关闭全部趋势条件。',
 'w1':'最近7天权重：调高更重视最新涨跌，需要减少其他段权重；缺货时按有效有货天数修正。',
 'w2':'此前7天权重：调高更重视中间一周，变化方向取决于该段销量。w3=1-w1-w2自动计算。'}
FIXED='固定规则：7/14/30天窗口；低月销资格≤5；周转在途折算0.7；趋势中间限幅0.3～2及增长支撑上限2；不是最终AM绝对范围。AF=1.28不参与当前安全库存；AK/CV为诊断量。未增加安全余量天数上限；无分类跟随快销，缺失箱规补1，禁用/停采排除、必卖优先及无货仍保留从仓需求保持不变。'

def standard():
    c=engine.config()
    return {'parameters':c['parameters'],'weight_profiles':c['weight_profiles'],'category_profiles':c['category_profiles'],'warehouses':copy.deepcopy(engine.DEFAULT_WAREHOUSES),'exception':True}

def signature(config):return hashlib.sha256(json.dumps(config,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()

def validate(config):
    if not isinstance(config,dict) or set(config)!=set(standard()):raise ValueError('参数方案必须包含完整模型、权重、分类映射、仓周期和一箱例外设置')
    p=config['parameters']
    if not isinstance(p,dict) or set(p)!=set(HELP):raise ValueError('模型参数不完整')
    if not model.Rules(**p).valid() or p['moq_days']<0 or p['moq_price']<0:raise ValueError('参数无效：概率0.50～0.99、参考值为正、退出月销>严格月销、极端缺货固定30、资格阈值非负')
    profiles=config['weight_profiles']
    if not isinstance(profiles,dict) or not {'快销','日百'}<=profiles.keys():raise ValueError('至少保留快销和日百权重方案')
    for name,w in profiles.items():
        if not isinstance(name,str) or not name.strip() or name=='无分类' or not isinstance(w,dict) or set(w)!=set(WEIGHT_HELP):raise ValueError('权重格式错误；无分类自动跟随快销')
        if not all(model.number(x) and x>=0 for x in w.values()) or w['w1']+w['w2']>1:raise ValueError(name+'：alpha、w1、w2须非负，w1+w2不能超过1')
    mapping=config['category_profiles']
    if not isinstance(mapping,dict) or any(not isinstance(k,str) or not k.strip() or v not in profiles for k,v in mapping.items()):raise ValueError('分类映射必须指向存在的权重方案')
    if not isinstance(config['warehouses'],dict) or not config['warehouses']:raise ValueError('至少配置一个仓的周期')
    for wh,values in config['warehouses'].items():
        if not isinstance(wh,str) or not wh.strip() or len(values)!=2 or not all(model.number(v) and v>=0 for v in values) or sum(values)<=0:raise ValueError('仓周期须非负且合计大于0')
    if not isinstance(config['exception'],bool):raise ValueError('一箱例外须为开关值')
    return config

def apply(loaded,config):
    """Rebind normalized weights per store AFTER routing; no cross-store mutations."""
    validate(config);out={**loaded,'config':copy.deepcopy(loaded['config']),'normalized':copy.deepcopy(loaded['normalized'])}
    for key in ['parameters','weight_profiles','category_profiles']:out['config'][key]=copy.deepcopy(config[key])
    out['config']['parameter_labels']={k:HELP[k][0]+'（'+HELP[k][1]+'）' for k in HELP}
    weights=out['config']['weight_profiles'];weights['无分类']=copy.deepcopy(weights['快销'])
    for d,m in out['normalized']:
        if d['_source']!='资料匹配':continue
        override=m.get('profile_override')
        name=str(override).strip() if override not in (None,'') else config['category_profiles'].get(str(m.get('category')),'无分类')
        m['blocking']=[s for s in m['blocking'] if not s.startswith('未配置的商品分类：')]
        d['U']=name
        if name in weights:
            w=weights[name];d.update(AB=w['alpha'],AC=w['w1'],AD=w['w2'])
        else:d.update(AB=None,AC=None,AD=None);m['blocking'].append('未配置的商品分类：'+name)
        d.update(AI=config['parameters']['moq_days'],AJ=config['parameters']['moq_price'])
    return out
