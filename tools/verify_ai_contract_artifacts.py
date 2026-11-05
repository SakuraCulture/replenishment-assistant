"""Check 0.40 additive AI contracts in real-client logs (synthetic fixture only).

Run after serve_analytics_ai_acceptance has exited. Natural-language assessment
is a separate reviewed artifact; this script checks the data actually returned.
"""
import argparse
import copy
from decimal import Decimal
import json
from pathlib import Path

from verify_ai_analytics_artifacts import inspect, require


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--workspace', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path)
    a = p.parse_args()
    workspace, out = a.workspace.resolve(), a.out.resolve()
    inspect(workspace, out)
    calls = [json.loads(x) for x in (workspace / 'tool-calls.jsonl').read_text('utf-8').splitlines()]
    days = sorted({r['day'] for r in json.loads((workspace / 'oracle.json').read_text('utf-8'))['rows']})
    risks, parameters, histories = [], [], []
    for call in calls:
        v = call.get('result')
        if not isinstance(v, dict):
            continue
        if call['tool'] == 'stock_diagnosis_results':
            r = v['risk_summary']
            expected = {'before_gap_skus': 4, 'after_gap_skus': 4, 'unknown_gap_skus': 0,
                        'arrival_gap_skus': 4, 'new_or_worsened_skus': 0, 'reduced_gap_skus': 0,
                        'hard_issue_count': 0}
            require(all(type(r[k]) is int and r[k] == n for k, n in expected.items()), 'Risk totals mismatch')
            require([s['store'] for s in r['stores']] == ['S1', 'S2'], 'Risk store order mismatch')
            for row in v['page']['items']:
                deficit = 16 if row['sku'] == 'B' else 26
                require(Decimal(row['deficit_before']) == Decimal(row['deficit_after']) == deficit, 'Existing gap lost')
                require(Decimal(row['arrival_gap_without_timed_inbound']) == 10, 'Arrival gap mismatch')
            risks.append({'task_id': call['arguments']['task_id'], 'candidate': v['candidate'], **expected})
        elif call['tool'] == 'stock_diagnosis_parameters':
            expected_probability = {1: .875, 2: .925}[v['candidate']]
            before, after = v['baseline']['stores'], v['candidate_parameters']['stores']
            require([s['store'] for s in before] == [s['store'] for s in after] == call['arguments']['stores'], 'Parameter store order mismatch')
            require(not v['adopted'] and not v['stale'], 'Candidate context changed')
            for left, right in zip(before, after):
                require(len(left['parameters']) == len(right['parameters']) == 28, 'Incomplete parameters')
                x, y = copy.deepcopy(left), copy.deepcopy(right)
                x.pop('parameter_signature'); y.pop('parameter_signature')
                changed = []
                for old, new in zip(x['parameters'], y['parameters']):
                    require(old['path'] == new['path'], 'Parameter order changed')
                    if old == new:
                        continue
                    require(old['path'] == ['profile_parameters', '快销', 'probability'], 'Unexpected parameter changed')
                    require(old['value'] == .9 and new['value'] == expected_probability, 'Probability mismatch')
                    require(old['configured_value'] == '沿用门店' and old['source'] == '门店通用参数', 'Baseline source mismatch')
                    # Only this parameter's explicit classification override and its
                    # description may differ; all other values/metadata stay exact.
                    for key in ('value', 'configured_value', 'source'):
                        new[key] = old[key]
                    require(old == new, 'Unexpected probability metadata changed')
                    changed.append(old['path'])
                require(len(changed) == 1 and x == y, 'Other candidate settings changed')
            parameters.append({'task_id': v['task_id'], 'candidate': v['candidate'], 'stores': call['arguments']['stores'],
                               'parameters_per_store': 28, 'only_fast_probability_changed': True})
        elif call['tool'] == 'store_history_review':
            for row in v['page']['items']:
                obs, basis = row['observation_basis'], row['parameter_basis']
                require(obs['business_date'] == basis['business_date'] == row['day'], 'Historical date replaced')
                require(Decimal(basis['protection_days']) == 12, 'Current protection period substituted for history')
                detail = row['evidence_details']
                if row['day'] == days[1] and row['sku'] == '0001':
                    require(len(detail) == 3, 'Missing source difference evidence')
                    require([Decimal(d['delta']) for d in detail] == [-12, -6, -3], 'Evidence quantity mismatch')
                    require(all(d['business_date'] == days[0] and d['order_number'] == 'BH-' + days[0].replace('-', '') + '-S1-ZC-HB' for d in detail), 'Evidence assigned to observation date')
                else:
                    require(detail == [], 'Invented historical difference')
                histories.append({'day': row['day'], 'sku': row['sku'], 'evidence_count': len(detail)})
    require({x['candidate'] for x in risks} == {1, 2}, 'Missing risk candidate')
    require({x['candidate'] for x in parameters} == {1, 2}, 'Missing complete candidate parameter query')
    require({(x['day'], x['sku']) for x in histories} == {(d, sku) for d in days for sku in ('0001', '0002')}, 'Incomplete history evidence')
    task_failures = [{'task_id': c['result']['task_id'], 'state': c['result']['state'],
                      'message': c['result'].get('message'), 'error': c['result'].get('error')}
                     for c in calls if c['tool'] == 'task_status'
                     and c.get('result', {}).get('state') in ('failed', 'stopped')]
    result = {'passed': True, 'synthetic': True, 'risks': risks, 'parameters': parameters,
              'history_rows_returned': len(histories), 'unique_history_rows': 6,
              'observed_task_failures': task_failures,
              'natural_language_checked_separately': True}
    (out / 'contract.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'contract_passed': True, 'parameter_calls': len(parameters), 'risk_calls': len(risks)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
