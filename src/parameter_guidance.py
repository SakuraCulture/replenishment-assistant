"""Shared advisory descriptions for UI/MCP/CLI; never validate or change values.

Business effects come from parameter_schema/restock_policy. Bounds document the
existing validators and are regression-checked against them, not a second validator.
"""
import copy
import parameter_schema as schema
import restock_policy as policy

VERSION = 'parameter-guidance.v1'
GROUPS = {'warehouses':'分仓周期', 'weight_profiles':'分类权重', 'profile_parameters':'分类参数',
          'parameters':'门店通用参数', 'policies':'补货策略', 'category_profiles':'全局分类映射'}


def definition_id(path):
    f = tuple(path)
    if len(f)==3 and f[0]=='warehouses' and type(f[2]) is int and f[2] in (0,1):
        return 'warehouses.'+('depth','lead')[f[2]]
    if len(f)==3 and f[0] in ('weight_profiles','profile_parameters'):
        return f[0]+'.'+str(f[2])
    if len(f)==2 and f[0] in ('parameters',policy.KEY):return '.'.join(f)
    if f==('exception',):return 'exception'
    if len(f)==2 and f[0]=='category_profiles':return 'category_profiles'
    raise ValueError('没有该参数路径的说明')


def describe(path):
    """Accept a concrete path or a dictionary template; no dynamic config reads."""
    f=tuple(path); identifier=definition_id(f); group=f[0]; key=f[-1]
    dependencies=[]; alternatives=[]; trial=True
    bounds={'type':'number','finite':True,'minimum':0}
    source='parameter_schema.HELP'
    if group=='warehouses':
        key=('depth','lead')[f[2]]; name=('备货天数','到货天数')[f[2]]
        unit='天'; explanation=schema.HELP[key][2]
        dependencies=['同一仓备货天数与到货天数之和必须大于0。','优先使用对应仓周期；两仓周期可不同。']
        template=['warehouses','{仓库}',f[2]]
    elif group in ('parameters','profile_parameters'):
        if key not in schema.HELP or group=='profile_parameters' and key not in schema.PROFILE_KEYS:raise ValueError('没有该参数的说明')
        name,unit,explanation=schema.HELP[key];template=list(f) if group=='parameters' else [group,'{分类组}',key]
        if key in ('strict_monthly','exit_monthly'):unit='库存单位'
        if key=='probability':bounds.update(minimum=.5,maximum=.99)
        elif key in ('single_sample_multiple','historical_reference_days','minimum_reference_units','growth_reference_days','exit_monthly'):
            bounds.pop('minimum');bounds['exclusiveMinimum']=0
        elif key=='extreme_days':bounds={'type':'number','finite':True,'const':30}
        if key in ('strict_monthly','exit_monthly'):dependencies.append('完全退出约束月销必须严格大于严格约束月销上界。')
        if key in ('depth','lead'):dependencies.append('通用周期之和必须大于0；维护时应改对应仓的周期。')
        if group=='profile_parameters':
            alternatives=['沿用门店'];dependencies+=['填写分类值时覆盖门店通用值；传“沿用门店”取消该分类覆盖。','同店两仓共用分类参数；无分类使用快销参数。']
        elif key in schema.PROFILE_KEYS:dependencies.append('该商品分类已设置专用值时，分类值优先，修改通用值可能不影响它。')
        trial=group!='parameters' or key not in ('depth','lead','extreme_days')
    elif group=='weight_profiles':
        if key not in (*schema.WEIGHT_HELP,'w3'):raise ValueError('没有该权重的说明')
        name={'alpha':'趋势敏感度','w1':'最近7天权重','w2':'此前7天权重','w3':'此前16天权重'}[key]
        unit='系数' if key=='alpha' else '比例';source='parameter_schema.WEIGHT_HELP'
        explanation=schema.WEIGHT_HELP.get(key,'由1减去最近7天权重与此前7天权重自动计算，不能直接修改。')
        template=[group,'{分类组}',key]
        if key!='alpha':bounds['maximum']=1
        trial=key!='w3'
        dependencies=['最近7天权重与此前7天权重均须非负，合计不能超过1；此前16天权重=1-两者之和。',
                      '商品按分类映射选取权重组；同店两仓共用，无分类使用快销。']
    elif group==policy.KEY or f==('exception',):
        group='policies';template=list(f);trial=False;unit='开关';bounds={'type':'boolean'}
        if f==('exception',):
            name='启用一箱例外';source='parameter_guidance.exception'
            explanation='启用一箱例外，仍受准入与门店总预算约束；开启不代表所有商品都触发，具体原因以原计算及例外审计为准。'
        else:
            if key not in policy.LABELS:raise ValueError('没有该策略的说明')
            name=policy.LABELS[key];explanation=policy.HELP[key];source='restock_policy.HELP'
        dependencies=['策略在原业务页面维护；AI只读取现有开关，不通过数值试算接口修改。','资质、拒收、禁用和停采等硬排除不能由此关闭。']
    elif group=='category_profiles':
        name='全局分类映射';unit='分类组';template=[group,'{商品分类}'];trial=False;bounds={'type':'string'}
        explanation='决定商品分类使用哪个分类参数组；所有门店共用映射，各店分类参数仍独立。新作业或明确采用后生效，不回写历史快照。'
        dependencies=['目标分类组须在门店参数中存在；无分类按快销。','通过全局分类映射入口维护，不能放进数值试算修改。'];source='parameter_guidance.mapping'
    else:raise ValueError('没有该参数的说明')
    from business_presenter import business_help
    percent=key in ('probability','w1','w2','w3')
    encoding='接口用0到1小数表示比例，例如95%传0.95；不要传95或“95%”。' if percent else '接口数量使用库存单位；金额使用元；数值不做隐式单位换算。' if bounds['type']=='number' else '按返回类型理解；本项不通过AI数值试算修改。'
    return {'id':identifier,'group':group,'group_name':GROUPS[group],'name':name,'unit':unit,
            'path_template':template,'explanation':business_help(explanation),'constraints':bounds,
            'dependencies':dependencies,'input_encoding':encoding,'alternatives':alternatives,
            'ai_trial_editable':trial,'description_source':source,
            'constraint_scope':'既有参数服务校验范围；不扩大各表单自身输入范围。',
            'result_caution':'调参方向是规则解释，不保证每款商品数量单调变化；箱规、库存、资格、准入和预算可能使最终数量不变。实际影响须同输入试算核对。'}


def definitions():
    paths=[('warehouses','{仓库}',i) for i in (0,1)]
    paths += [('weight_profiles','{分类组}',k) for k in (*schema.WEIGHT_HELP,'w3')]
    paths += [('profile_parameters','{分类组}',k) for k in schema.PROFILE_KEYS]
    paths += [('parameters',k) for k in schema.HELP]
    paths += [('exception',),*[(policy.KEY,k) for k in policy.LABELS],('category_profiles','{商品分类}')]
    return [describe(p) for p in paths]


def catalog(args):
    import json
    rows=definitions();group=args.get('group','all');search=args.get('search','').casefold()
    if group!='all':rows=[r for r in rows if r['group']==group]
    if search:rows=[r for r in rows if search in json.dumps(r,ensure_ascii=False).casefold()]
    offset=args.get('offset',0);limit=args.get('limit',50)
    return {'version':VERSION,'total':len(rows),'offset':offset,'items':rows[offset:offset+limit],
            'next_offset':offset+limit if offset+limit<len(rows) else None,
            'note':'模板中的仓库/分类组不是可提交的编码；实际路径和值从effective_parameters取得。定义不代表已应用配置；预算、主从仓和策略不是数值试算超参。'}


def help_text(path):
    d=describe(path);c=d['constraints'];parts=[d['explanation']]
    percent=d['id'].split('.')[-1] in ('probability','w1','w2','w3')
    def display(value):return f'{value*100:g}%' if percent else str(value)+' '+d['unit']
    if 'const' in c:limit='固定为'+display(c['const'])
    elif c['type']=='number':
        limit=('大于'+display(c['exclusiveMinimum'])) if 'exclusiveMinimum' in c else '不小于'+display(c['minimum'])
        if 'maximum' in c:limit+='，不大于'+display(c['maximum'])
    else:limit='开启或关闭' if c['type']=='boolean' else '已存在的分类组名称'
    parts+=['有效范围：'+limit,'依赖：'+'；'.join(d['dependencies']) if d['dependencies'] else '',d['result_caution']]
    return '\n'.join(p for p in parts if p)


def referenced(rows):
    """Attach descriptions once per response, never once per store or model row."""
    by_id={d['id']:d for d in definitions()};used=[]
    for r in rows:
        for f in r['parameters']:
            identifier=definition_id(f['path']);f['definition_id']=identifier
            if identifier not in used:used.append(identifier)
        for identifier in ('exception',*[policy.KEY+'.'+k for k in policy.LABELS],'category_profiles'):
            if identifier not in used:used.append(identifier)
    return {'version':VERSION,'definitions':{k:copy.deepcopy(by_id[k]) for k in used}}
