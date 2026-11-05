"""Frozen AI preparation/calculation, using the original business services."""
import copy
from pathlib import Path
import batch_service
import engine
import parameter_maintenance as pm
import parameter_schema as schema
import replenishment_plans as plans
from workbench_actions import plan_record, record_settings


def prepare(snapshot, arguments, progress=lambda _: None, cancel=None):
    import admission
    import store_parameter_import as spi
    from app_defaults import DEFAULT_WAREHOUSES
    engine.checkpoint(cancel)
    paths = arguments.get('paths')
    # When checking new files, the previous full input is irrelevant. Do not copy it.
    frozen = copy.deepcopy({k:v for k,v in snapshot.items() if k != 'loaded' or not paths})
    if paths:
        paths = list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
        for path in paths:
            if Path(path).suffix.lower() != '.xlsx' or not Path(path).is_file():
                raise ValueError('请选择现有 .xlsx 补货建议文件：' + Path(path).name)
        products = frozen['catalogs'] if not frozen['legacy'] else frozen['products']
        loaded = batch_service.load(paths, products, progress, cancel)
    else:
        loaded = frozen['loaded']
        if not loaded or frozen['input_dirty']:
            raise ValueError('当前输入尚未检查，请指定文件检查或先在准备数据页检查')
        # Reuse the frozen checked input, including historical source semantics.
    engine.checkpoint(cancel)
    plan = frozen.get('plan')
    codes = arguments.get('stores')
    if codes is None:
        codes = [x['code'] for x in plan['stores']] if plan else frozen['selected'] or list(loaded['stores'])
    if not codes: raise ValueError('请指定至少一家门店')
    if any(s not in loaded['stores'] for s in codes):
        raise ValueError('缺少所选门店文件：' + '、'.join(s for s in codes if s not in loaded['stores']))
    records = {}; jobs = {}; issues = []
    changes = {}
    for item in arguments.get('parameter_changes', []):
        if item['store'] not in codes: raise ValueError('参数修改包含本次未选门店')
        field = tuple(item['path']); key = (item['store'], field)
        if key in changes: raise ValueError('同一门店参数重复修改')
        changes[key] = item['value']
    for s in codes:
        engine.checkpoint(cancel)
        if loaded.get('blocked', {}).get(s):
            issues.append({'store': s, 'message': '存在未选择的重复冲突，请在工作台核对'}); continue
        try:
            record = copy.deepcopy(frozen['defaults'].get(s))
            if record is None:
                record = spi.base()
                budget = frozen['budgets'].get(s)
                if budget:
                    record['ui'].update(mode=2 if budget['enabled'] else 1, amount=f"{budget['amount']:.2f}")
                record['ui'].update(frozen['routes'].get(s, {}))
            if s in frozen['current_records']:
                import store_defaults
                record = store_defaults.reload_record(frozen['current_records'][s],
                    frozen['origins'].get(s), frozen['previous_defaults'].get(s), record) if paths else copy.deepcopy(frozen['current_records'][s])
            c = record['preset']['config']
            for d, _ in loaded['normalized']:
                if d['B'] == s and d.get('_source') == '资料匹配':
                    c['warehouses'].setdefault(d['AU'], DEFAULT_WAREHOUSES.get(d['AU'], (-1, -1)))
            # Current checked records retain their own frozen global mapping.
            if paths or s not in frozen['current_records'] or plan:
                record['preset'] = pm.bind(record['preset'], frozen['mapping'])
            if plan and s in {i['code'] for i in plan['stores']}:
                record = plan_record(plan, record, frozen['mapping'])
            chosen = {field: v for (store, field), v in changes.items() if store == s}
            if chosen: record['preset']['config'] = pm.patch(record['preset']['config'], chosen)
            records[s] = record
            jobs[s] = record_settings(record, frozen['profile'], loaded.get('catalogs'), admission.snapshot(frozen['dbpath'], s))
        except ValueError as exc:
            issues.append({'store': s, 'message': str(exc)})
    files = (loaded.get('intake') or {}).get('files', [])
    report = {'ready': not issues, 'stores': [{'code': s, 'name': loaded['stores'][s],
              'budget': jobs.get(s, {}).get('budget'), 'route': jobs.get(s, {}).get('supply_route'),
              'parameter_signature': jobs.get(s, {}).get('parameter_signature')} for s in codes],
              'issues': issues, 'mode': arguments['mode'],
              'files': [{**f, 'path': Path(f['path']).name} for f in files],
              'duplicates': (loaded.get('intake') or {}).get('duplicates', 0),
              'conflicts': len((loaded.get('intake') or {}).get('conflicts', [])),
              'plan': {k: plan[k] for k in ('id', 'name', 'revision')} if plan else None,
              'note': '只应用本次；不保存门店默认、不签发。'}
    return {'loaded': loaded, 'jobs': jobs, 'records': records, 'codes': codes, 'profile': frozen['profile'],
            'report': report, 'plan': plan, 'active_plan': frozen.get('active_plan'), 'workers': frozen['workers'], 'context_token': frozen['context_token'],
            'paths': paths, 'mode': arguments['mode'], 'dbpath': frozen['dbpath'], 'day': frozen['day'],
            'explicit_parameters': list(dict.fromkeys([item['store'] for item in arguments.get('parameter_changes', [])] + ([i['code'] for i in plan['stores']] if plan else [])))}


def retain_sources(prepared, progress, cancel):
    if prepared.get('paths'):
        from source_archive import retained_import
        retained_import(batch_service.load, (), prepared['loaded'], prepared['dbpath'], progress, cancel)


def explain_row(result, row):
    import business_presenter as bp
    return {'sku': row.get('E'), 'name': row.get('D'), 'warehouse': row.get('AU'),
            'input': [bp.field_row(row, k) for k in ('H', 'I', 'J', 'K', 'L', 'N', 'O', 'V')],
            'parameters': bp.parameter_rows(result, row), 'calculation': bp.stage_rows(row),
            'note': '原计算结果与当时参数；未重新执行模型，缺项不推测'}


def historical_explanation(path, head, arguments, cancel):
    from history_read import repository
    from archive_orders import require_active
    from contextlib import closing
    from automation_protocol import CommandError
    engine.checkpoint(cancel); repo = repository(path)
    order = repo.get(head['day'], head['store'], head['revision'])
    if not order: raise CommandError('ORDER_MISSING', '原订单已不存在')
    result = order['payload']['result']
    rows = [d for d in result['rows'] if d.get('E') == arguments['sku'] and (not arguments.get('warehouse') or d.get('AU') == arguments['warehouse'])]
    if len(rows) != 1: raise CommandError('AMBIGUOUS_PRODUCT', '请明确仓库和商品编码，匹配 ' + str(len(rows)) + ' 条')
    with closing(repo.work.connect()) as db: require_active(db, order['oid'], rows[0].get('AU'))
    engine.checkpoint(cancel)
    return {**explain_row(result, rows[0]), 'store': head['store'], 'order': {k: order.get(k) for k in ('oid', 'number', 'revision', 'saved')}}


def calculate(prepared, codes, notify, cancel):
    jobs = {s: prepared['jobs'][s] for s in codes}
    return batch_service.run(prepared['loaded'], jobs, prepared['profile'], prepared['workers'], notify, cancel)


def parameters(record):
    config = record['preset']['config']
    rows = []
    for field in pm.fields([config]) + pm.fields([config], 'common'):
        value = pm.value(config, field); inherited = value == '沿用门店'
        effective = config['parameters'][field[2]] if inherited else value
        unit = '天' if field[0] == 'warehouses' else '权重' if field[0] == 'weight_profiles' else schema.HELP[field[-1]][1]
        group = {'warehouses': '分仓周期', 'weight_profiles': '分类权重', 'profile_parameters': '分类参数', 'parameters': '门店通用参数'}[field[0]]
        rows.append({'parameter': pm.title(field), 'path': list(field), 'value': effective, 'unit': unit,
                     'configured_value': value, 'group': group, 'source': '门店通用参数' if inherited else group})
    return rows
