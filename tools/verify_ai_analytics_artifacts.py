"""Independently inspect artifacts from real-client synthetic analytics runs.

Run after the host has stopped. This does not invoke an AI or mutate the source DB.
Numerical fixture expectations are explicit; Excel parity uses the native exporter.
"""
import argparse
import copy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import openpyxl
import order_analytics as analytics
import order_analytics_exports as exports


def require(value, message):
    if not value:
        raise AssertionError(message)


def compare_excel(actual, expected):
    a, b = (openpyxl.load_workbook(p) for p in (actual, expected))
    differences = []
    require(a.sheetnames == b.sheetnames, 'worksheet order changed')
    checked = 0
    for left, right in zip(a, b):
        require(left.max_row == right.max_row and left.max_column == right.max_column, 'dimensions changed')
        require(left.freeze_panes == right.freeze_panes, 'freeze panes changed')
        require(left.auto_filter.ref == right.auto_filter.ref, 'filter changed')
        require(list(left.merged_cells) == list(right.merged_cells), 'merged cells changed')
        for lr, rr in zip(left, right):
            for x, y in zip(lr, rr):
                dynamic = left.title == '统计口径' and x.column == 2 and left.cell(x.row, 1).value in ('数据截至', '生成时间')
                if not dynamic and (type(x.value) is not type(y.value) or x.value != y.value or x.data_type != y.data_type):
                    differences.append([left.title, x.coordinate, str(x.value), str(y.value)])
                for field in ('font', 'fill', 'border', 'alignment', 'number_format', 'protection'):
                    require(copy.copy(getattr(x, field)) == copy.copy(getattr(y, field)), 'style mismatch: ' + left.title + '/' + x.coordinate + '/' + field)
                checked += 1
    a.close(); b.close()
    require(not differences, str(differences))
    return {'cells_checked': checked, 'value_type_order_style_differences': differences,
            'dynamic_cell_whitelist': ['统计口径/数据截至/说明', '统计口径/生成时间/说明']}


def inspect(workspace, target):
    target.mkdir(parents=True, exist_ok=False)
    calls = [json.loads(x) for x in (workspace / 'tool-calls.jsonl').read_text('utf-8').splitlines()]
    final = json.loads((workspace / 'final.json').read_text('utf-8'))
    require(final['formal_unchanged'] and final['current_unchanged'], 'business state mutated')
    db = workspace / 'settings/workbench.sqlite3'
    original_hash = hashlib.sha256(db.read_bytes()).hexdigest()
    scope = json.loads((workspace / 'ready.json').read_text('utf-8'))['query']
    report = analytics.collect(db, scope)
    days = sorted({r['day'] for r in report['rows']})
    require(len(days) == 3 and len(report['rows']) == 6, 'unexpected fixture size')
    expected = {
        (days[0], '0001'): [24, 24, 12, 6, 3],
        (days[1], '0001'): [24, 24, 24, 24, 24],
        (days[2], '0001'): [24, 0, None, None, None],
        **{(d, '0002'): [24, 24, None, None, None] for d in days},
    }
    for row in report['rows']:
        quantities = [row['quantities'][s] if row['valid'][s] else None for s in analytics.STAGES]
        require(quantities == expected[(row['day'], row['sku'])], 'quantity fixture mismatch')
        require(row['group'] == ('必采' if row['sku'] == '0001' else '非必采'), 'mandatory split mismatch')
    diagnosis_checks = []
    for call in calls:
        if call['tool'] != 'stock_diagnosis_results' or 'result' not in call:
            continue
        v = call['result']
        require(v['adopted'] is False and v['stale'] is False, 'trial was adopted or stale')
        expected_model = {1: 64, 2: 68}[v['candidate']]
        require([s['store'] for s in v['stores']] == ['S1', 'S2'], 'store order changed')
        for store in v['stores']:
            for field, expected_value in {'before':10, 'after':10, 'delta':0, 'model_before':66,
                                          'model_after':expected_model, 'must_before':10, 'must_after':10}.items():
                require(Decimal(store[field]) == expected_value, 'trial amount mismatch: ' + field)
        require(v['page']['total'] == 4 and v['page']['next_offset'] is None, 'incomplete trial page')
        require([(r['store'],r['sku']) for r in v['page']['items']] == [('S1','A'),('S1','B'),('S2','A'),('S2','B')], 'SKU order mismatch')
        for row in v['page']['items']:
            quantity = 10 if row['sku'] == 'B' else 0
            require(Decimal(row['before']) == quantity and Decimal(row['after']) == quantity, 'trial execution mismatch')
            require(Decimal(row['coverage_before']) == quantity / 2 and Decimal(row['coverage_after']) == quantity / 2, 'trial coverage mismatch')
            require(Decimal(row['box']) == 1 and Decimal(row['amount_delta']) == 0, 'box or delta mismatch')
        diagnosis_checks.append({'candidate':v['candidate'], 'rows':4, 'two_store_execution':20, 'saving':0})
    requests = {}
    artifacts = []
    seen = set()
    forbidden = {'prepare_calculation','adopt_calculation','adopt_stock_diagnosis','undo_stock_diagnosis'}
    require(not forbidden.intersection(c['tool'] for c in calls), 'unexpected mutation tool')
    for c in calls:
        value = c.get('result') or {}
        if c['tool'] in ('query_order_funnel', 'export_order_funnel') and value.get('task_id'):
            requests[value['task_id']] = c
        if c['tool'] == 'task_status' and value.get('state') == 'completed':
            task = requests.get(value['task_id'])
            if not task or task['tool'] != 'export_order_funnel' or value['task_id'] in seen:
                continue
            seen.add(value['task_id'])
            request = task['arguments']
            query = requests[request['task_id']]['arguments']['query']
            fresh = analytics.collect(db, query)
            require(fresh['snapshot_id'] == request['snapshot_id'], 'export snapshot mismatch')
            receipt = value.get('export') or value.get('receipt') or value.get('result')
            if receipt is None:
                # Keep the service contract explicit; fail instead of guessing a path.
                raise AssertionError('Unknown export task receipt keys: ' + str(list(value)))
            actual = Path(receipt['path'])
            require(hashlib.sha256(actual.read_bytes()).hexdigest() == receipt['sha256'], 'export file hash mismatch')
            native = target / ('native-' + value['task_id'] + '.xlsx')
            native_receipt = exports.export(db, fresh, native, request.get('level', 'store'), request.get('orders'))
            artifacts.append({'path': str(actual), 'level': request.get('level', 'store'),
                              'sheets': receipt['sheets'], 'sha256': receipt['sha256'],
                              **compare_excel(actual, native)})
    require(hashlib.sha256(db.read_bytes()).hexdigest() == original_hash, 'verification changed DB')
    result = {'workspace': str(workspace), 'synthetic': True, 'fixture_rows_checked': 6,
              'formal_unchanged': True, 'current_unchanged': True, 'calls': len(calls),
              'tools': sorted({c['tool'] for c in calls}), 'artifacts': artifacts, 'diagnosis_checks':diagnosis_checks,
              'exceptions': [{'tool': c['tool'], 'error': c['error']} for c in calls if c.get('error')],
              'passed': True}
    (target / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    inspect(args.workspace.resolve(), args.out.resolve())
