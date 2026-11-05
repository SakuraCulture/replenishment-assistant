"""Small, versioned public command contract. JSON only; no executable payloads."""
import hashlib
import json
import math

VERSION = 'replenishment.tools.v1'
MAX_FRAME = 2 * 1024 * 1024
MAX_PAGE = 100


class CommandError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False, default=str).encode()).hexdigest()


def obj(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


TEXT = {'type': 'string', 'minLength': 1, 'maxLength': 4096}
LIST = {'type': 'array', 'items': TEXT, 'maxItems': 500, 'uniqueItems': True}
PAGE = {'offset': {'type': 'integer', 'minimum': 0}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': MAX_PAGE}}
TASK = {'task_id': TEXT}
CHANGE = obj({'store': TEXT, 'path': {'type': 'array', 'items': {'type': ['string', 'integer']}, 'minItems': 2, 'maxItems': 3},
              'value': {'type': ['number', 'string']}}, ['store', 'path', 'value'])
ANALYTICS_QUERY = obj({'from':TEXT,'to':TEXT,'stores':LIST,'orders':LIST,'warehouse':{'type':'string','enum':['','山东仓','河北仓']},
    'group':{'type':'string','enum':['','必采','非必采','属性待核']},'search':{'type':'string'},'category':TEXT,'rounds':{'type':'integer','minimum':1,'maximum':30}},['from','to'])
TOOLS = [
    ('store_history_review','只读复盘指定经营分析快照的连续多轮签发和履约证据。轮次按不同业务日期计数；缺货原因证据与参数假设分开。先query_order_funnel取得所需日期范围，不自动改参数。',
     obj({**TASK,'store':TEXT,**PAGE},['task_id'])),
    ('query_order_funnel','只读查询五段经营漏斗，按签发业务日期和精确门店/订单范围。异步返回task_id，task_status完成后用order_funnel_results分页。包含必采/非必采与资料待核，排除禁采/对应仓禁用/淘汰品。不执行计算。',
     obj({'context_token':TEXT,'operation_key':TEXT,'query':ANALYTICS_QUERY},['context_token','operation_key','query'])),
    ('order_funnel_results','读取已完成经营分析的门店、分仓、订单或商品分页。数量金额以服务为准，未导入不按0；next_offset为空才读完。返回中文业务口径。',
     obj({**TASK,'level':{'type':'string','enum':['store','warehouse','order','detail','pending']},'store':TEXT,'order':TEXT,**PAGE},['task_id'])),
    ('export_order_funnel','导出已核对范围的五段漏斗Excel，与原生经营分析相同。只写本地报表；directory可省略以使用配置导出位置。不签发、不修改订单、不发送文件。',
     obj({**TASK,'snapshot_id':TEXT,'context_token':TEXT,'operation_key':TEXT,'directory':TEXT,'level':{'type':'string','enum':['store','order','detail']},'orders':LIST},['task_id','snapshot_id','context_token','operation_key'])),
    ('workbench_context', '查看当前工作台、已启用范围、业务日期和任务。先调用以取得 context_token。', obj()),
    ('parameter_definitions', '查询参数业务含义、调整影响、合法范围、依赖与AI可改范围；调参前先读说明。模板路径须用实际门店参数路径替换。只读，不修改配置。',
     obj({'group': {'type':'string','enum':['all','warehouses','weight_profiles','profile_parameters','parameters','policies','category_profiles']}, 'search':{'type':'string'}, **PAGE})),
    ('find_resources', '查询应用登记的门店、预案、文件、仓资料、历史订单或本机 AI 任务；编码精确使用，不按名称猜。',
     obj({'kind': {'type': 'string', 'enum': ['stores', 'plans', 'files', 'catalogs', 'orders', 'tasks', 'schedules', 'scheduled_runs']}, 'search': {'type': 'string'}, **PAGE}, ['kind'])),
    ('effective_parameters', '读取本次作业(current)、已保存默认(defaults)或试算预览(preview，须preview_id)的参数与来源；按definition_id读取返回guidance.definitions中的解释和约束。默认和试算不代表本次已采用。', obj({'stores': LIST, 'scope': {'type': 'string', 'enum': ['current', 'defaults', 'preview']}, 'preview_id': TEXT}, ['stores'])),
    ('scheduled_run_status', '只读定时补货任务：分页门店进度、after之后的事件。不会启动、暂停、重试、取数或采用结果。',
     obj({'run_id': TEXT, 'after': {'type': 'integer', 'minimum': 0}, **PAGE}, ['run_id'])),
    ('prepare_calculation', '检查指定本地文件或当前已检查数据。形成预览，不签发、不保存默认。mode=trial 试算；current 明确采用到本次工作台。',
     obj({'context_token': TEXT, 'operation_key': TEXT, 'stores': LIST, 'paths': LIST, 'plan_id': TEXT,
          'mode': {'type': 'string', 'enum': ['trial', 'current']}, 'parameter_changes': {'type': 'array', 'items': CHANGE, 'maxItems': 500}},
         ['context_token', 'operation_key', 'mode'])),
    ('run_calculation', '执行已就绪预览，按冻结门店顺序计算；重复调用同预览返回原任务。', obj({'preview_id': TEXT}, ['preview_id'])),
    ('task_status', '读取任务进度和新增事件；断线后查询原任务，不重新发起。', obj({**TASK, 'after': {'type': 'integer', 'minimum': 0}}, ['task_id'])),
    ('stop_remaining', '停止原任务的后续步骤。正在计算的门店安全结束，已完成结果保留。', obj(TASK, ['task_id'])),
    ('retry_failed', '仅重试选中的失败或已停止门店，沿用原输入、参数和必卖快照。', obj({**TASK, 'stores': LIST, 'operation_key': TEXT}, ['task_id', 'stores', 'operation_key'])),
    ('calculation_results', '分页读取结果。数量使用库存单位，金额来自原计算，非 AI 估计。',
     obj({**TASK, 'store': TEXT, 'warehouse': TEXT, 'search': {'type': 'string'},
          'filter': {'type': 'string', 'enum': ['all', 'positive', 'issues']}, **PAGE}, ['task_id'])),
    ('explain_decision', '查询某商品的原参数、销量库存和已计算过程。历史缺项不推测。任务与历史单二选一。',
     obj({'task_id': TEXT, 'order_id': TEXT, 'store': TEXT, 'warehouse': TEXT, 'sku': TEXT}, ['sku'])),
    ('adopt_trial', '将试算结果采用到仍未变化的当前作业；不保存、不签发。若影响新品或人工调整，须在原界面核对。',
     obj({**TASK, 'context_token': TEXT}, ['task_id', 'context_token'])),
    ('open_workbench_view', '定位原业务页面，不触发保存、签发或导入确认。',
     obj({'view': {'type': 'string', 'enum': ['home', 'work', 'progress', 'results', 'tracking', 'review', 'parameters']}, 'store': TEXT}, ['view'])),
]
SCHEMAS = {name: schema for name, _, schema in TOOLS}


def validate(value, schema, path='参数'):
    kinds = schema.get('type'); kinds = [kinds] if isinstance(kinds, str) else kinds or []
    kind = 'null' if value is None else 'boolean' if isinstance(value, bool) else 'integer' if isinstance(value, int) else 'number' if isinstance(value, float) else 'string' if isinstance(value, str) else 'array' if isinstance(value, list) else 'object' if isinstance(value, dict) else 'invalid'
    if kinds and kind not in kinds and not (kind == 'integer' and 'number' in kinds):
        raise CommandError('INVALID_ARGUMENT', path + '类型不正确')
    if 'enum' in schema and value not in schema['enum']: raise CommandError('INVALID_ARGUMENT', path + '取值无效')
    if kind == 'object':
        props = schema.get('properties', {})
        missing = set(schema.get('required', [])) - value.keys()
        if missing: raise CommandError('INVALID_ARGUMENT', path + '缺少字段：' + ', '.join(sorted(missing)))
        if schema.get('additionalProperties') is False and value.keys() - props.keys():
            raise CommandError('INVALID_ARGUMENT', path + '包含未开放字段')
        for k, v in value.items():
            if k in props: validate(v, props[k], path + '.' + k)
    elif kind == 'array':
        if len(value) < schema.get('minItems', 0) or len(value) > schema.get('maxItems', MAX_PAGE * 100):
            raise CommandError('INVALID_ARGUMENT', path + '数量超出范围')
        if schema.get('uniqueItems') and len({digest(x) for x in value}) != len(value):
            raise CommandError('INVALID_ARGUMENT', path + '不能重复')
        for v in value: validate(v, schema.get('items', {}), path + '[]')
    elif kind == 'string':
        if len(value) < schema.get('minLength', 0) or len(value) > schema.get('maxLength', 4096) or '\0' in value:
            raise CommandError('INVALID_ARGUMENT', path + '文本长度或内容无效')
    elif kind in ('integer', 'number'):
        if not math.isfinite(value) or value < schema.get('minimum', -math.inf) or value > schema.get('maximum', math.inf):
            raise CommandError('INVALID_ARGUMENT', path + '数值超出范围')


def validate_call(name, arguments):
    if name not in SCHEMAS: raise CommandError('NOT_AVAILABLE', '首期未开放这个操作')
    validate(arguments, SCHEMAS[name])
    return arguments


def public_tools():
    readonly = {'workbench_context', 'parameter_definitions', 'find_resources', 'effective_parameters', 'task_status', 'scheduled_run_status', 'calculation_results', 'explain_decision', 'query_order_funnel', 'order_funnel_results', 'store_history_review'}
    return [{'name': n, 'description': d, 'inputSchema': s, 'annotations': {
        'readOnlyHint': n in readonly, 'destructiveHint': False, 'openWorldHint': False}}
        for n, d, s in TOOLS]


def decode(line):
    if len(line) > MAX_FRAME: raise CommandError('TOO_LARGE', '消息过大，请分页查询')
    def duplicate(pairs):
        out = {}
        for key, value in pairs:
            if key in out: raise ValueError('重复字段')
            out[key] = value
        return out
    try:
        return json.loads(line, object_pairs_hook=duplicate, parse_constant=lambda x: (_ for _ in ()).throw(ValueError('非有限数字')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CommandError('INVALID_JSON', '无法识别 JSON 消息') from exc
