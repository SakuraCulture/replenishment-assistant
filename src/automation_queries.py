"""Read-only AI projections. Use existing repositories, never start scheduler work."""
import copy
from pathlib import Path
from automation_protocol import CommandError


def page(rows, offset, limit):
    return {'total': len(rows), 'offset': offset, 'items': rows[offset:offset + limit],
            'next_offset': offset + limit if offset + limit < len(rows) else None}


def schedule_summary(s):
    return {k: s.get(k) for k in ('id', 'name', 'plan_id', 'revision', 'kind', 'time',
            'weekdays', 'date', 'enabled', 'next_at', 'workers', 'timezone')}


def run_summary(r):
    from scheduled_replenishment import STATES
    return {**{k: r.get(k) for k in ('id', 'day', 'trigger', 'created', 'updated', 'state', 'stage', 'revision', 'message')},
            'state_name': STATES.get(r['state'], r['state']), 'schedule': schedule_summary(r['schedule']),
            'store_count': len(r['stores']), 'ready_stores': sum(s['state'] == 'READY' for s in r['stores']),
            'note': '完成表示计算完成等待核对，不表示已签发。仅查询，不会启动或改变任务。'}


def scheduled_resources(path, args):
    from scheduled_replenishment import Repository
    repo = Repository(path); offset = args.get('offset', 0); limit = args.get('limit', 50)
    if args['kind'] == 'scheduled_runs':
        result = repo.list(offset=offset, limit=limit, query=args.get('search', ''))
        return {**result, 'offset': offset, 'items': [run_summary(r) for r in result['items']]}
    rows = [schedule_summary(s) for s in repo.schedules()]
    search = args.get('search', '').casefold()
    if search: rows = [s for s in rows if any(search in str(s.get(k, '')).casefold() for k in ('id', 'name', 'plan_id'))]
    return page(rows, offset, limit)


def scheduled_status(path, args):
    from scheduled_replenishment import Repository
    repo = Repository(path)
    if not repo.exists(): raise CommandError('TASK_NOT_FOUND', '没有定时补货任务，请先查询运行记录')
    try: r = repo.get(args['run_id'])
    except ValueError: raise CommandError('TASK_NOT_FOUND', '该定时补货任务已不存在，请重新查询') from None
    offset, limit, after = args.get('offset', 0), args.get('limit', 50), args.get('after', 0)
    names = {'PENDING': '等待处理', 'RUNNING': '计算中', 'READY': '计算完成', 'FAILED': '未完成，需处理'}
    stores = [{**{k: s.get(k) for k in ('code', 'name', 'state', 'message')},
               'state_name': names.get(s['state'], s['state'])} for s in r['stores']]
    events = repo.events(r['id'], after=after, limit=limit)
    cursor = events[-1]['seq'] if events else after
    return {'run': run_summary(r), 'stores': page(stores, offset, limit), 'events': events,
            'next_after': cursor, 'has_more_events': len(events) == limit or r.get('event_seq', 0) > cursor,
            'note': '门店按 next_offset 翻页，事件按 next_after 续查；任务运行中可能继续产生进展。'}


def file_resources(host):
    reports = {str(Path(f['path']).resolve()): f for f in ((host.loaded or {}).get('intake') or {}).get('files', [])}
    rows = []
    for path in host.sales.paths:
        report = reports.get(str(Path(path).resolve()))
        rows.append({'path': path, 'name': Path(path).name,
            'check_scope': '输入已变化，需重新检查' if host.input_dirty else '本次检查报告' if report else '仅登记，尚无检查依据',
            'check_report': copy.deepcopy(report) if report and not host.input_dirty else None,
            'note': '单文件检查通过不代表跨文件冲突、门店配置和商品匹配均已就绪；准备预览仍须检查。'})
    return rows


def parameter_result(host, args, preview=None):
    import automation_jobs
    import parameter_maintenance as pm
    import parameter_schema as schema
    import restock_policy
    scope = args.get('scope', 'current'); rows = []
    defaults = host.work_repository.stores() if scope == 'defaults' else {}
    mapping = pm.mapping_snapshot(host.work_repository.path) if scope == 'defaults' else None
    for code in args['stores']:
        if scope == 'current':
            if code not in host.store_widgets: raise CommandError('STORE_NOT_FOUND', '门店不在当前工作台：' + code)
            record = host.current_parameter_record(code); name = host.states[code]['name']
        elif scope == 'preview':
            if code not in preview['records']: raise CommandError('STORE_NOT_FOUND', '该门店不在指定试算预览中：' + code)
            record = preview['records'][code]; name = preview['loaded']['stores'][code]
        else:
            if code not in defaults: raise CommandError('STORE_DEFAULT_NOT_FOUND', '该门店没有已保存默认，未推测门店编码或设置：' + code)
            record = copy.deepcopy(defaults[code]); name = record.get('name')
            try: record['preset'] = pm.bind(record['preset'], mapping)
            except ValueError as exc: raise CommandError('NEEDS_REVIEW', str(exc)) from None
        config = record['preset']['config']; ui = record['ui']
        implicit=bool(record['preset'].get('implicit_default'))
        fields = automation_jobs.parameters(record)
        for field in fields:
            field['scope'] = '无保存参数，准备时自动填写' if scope=='defaults' and implicit else {'current':'当前作业快照', 'defaults':'已保存门店默认', 'preview':'试算冻结参数'}[scope]
        rows.append({'store': code, 'name': name, 'parameters': fields, 'route_and_budget': copy.deepcopy(ui),
            'budget': {'mode': {0: '未设置', 1: '不限预算', 2: '限额补货'}.get(ui.get('mode'), '待核'), 'configured_amount': ui.get('amount')},
            'supply': {'主仓': ui.get('primary'), '从仓': ui.get('secondary')},
            'policies': {'一箱例外': config['exception'], **{restock_policy.LABELS[k]: v for k, v in restock_policy.values(config).items()}},
            'mapping': record['preset'].get('mapping_source'), 'category_mapping': copy.deepcopy(config['category_profiles']),
            'parameter_signature': schema.signature(config),
            'preset': {k: record['preset'].get(k) for k in ('id', 'name', 'version')},
            'parameter_source':copy.deepcopy(record['preset'].get('parameter_source')),
            'default_preset_binding':copy.deepcopy(record['preset'].get('default_binding')),
            'implicit_parameter_fallback':implicit,
            'note': '分类参数覆盖通用值，分仓只维护周期；方案名是快照标识，不代表每个字段的编辑来源。' +
                    {'current':'当前作业快照，未保存默认。', 'defaults':('门店未保存默认参数，所列为准备时自动填充值及全局映射，不代表已保存或本次已采用。' if implicit else '新作业可采用的已保存默认及当前全局映射，不代表本次已应用。'),
                     'preview':'预案及本次试算修改装配后的冻结参数；不代表当前工作台已采用。'}[scope]})
    from parameter_guidance import referenced
    guidance = referenced(rows)
    return {'scope': scope, 'stores': rows, 'guidance':guidance,
            **({'preview_id':args['preview_id'], 'preview_context_token':preview['context_token']} if preview else {})}
