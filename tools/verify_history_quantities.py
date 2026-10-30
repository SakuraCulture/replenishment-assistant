"""Compare business form execution facts with signed semantics, never raw new-item fields."""
import argparse
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import history_forms as forms
import result_semantics as sem
import work_repository as wr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expect-existing-defect', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = []
    p = wr.loads(args.fixture.read_bytes())
    for store, result in p['results'].items():
        for row in result['rows']:
            cases.append((store + '/' + row['E'], row, sem.order_quantity(row), sem.order_amount(row)))
    base = dict(A='合成店', B='T', D='合成商品', E='synthetic', AS=15, BV=30, J=20, K=0, L=0, N=0,
                _meta={'blocking': []}, _source='资料匹配', _budget_active=False)
    cases.extend([
        ('unlimited regular: no new-item fields', base, 15, 30),
        ('partial budget regular', {**base, '_budget_active': True, '_allocated_qty': 5, '_allocated_amount': 10}, 5, 10),
        ('new item budget caps pre-budget request', {**base, 'AS': 0, 'BV': 0, 'J': 0, '_new_requested': 6, '_execution_qty': 6,
          '_execution_amount': 12, '_budget_active': True, '_allocated_qty': 2, '_allocated_amount': 4}, 2, 4),
        ('excluded retains original calculation but execution zero', {**base, 'S': '禁用'}, 0, 0),
        # e45b9d0 quantity()/amount_value() expose missing raw AS/BV as 待核,
        # not numeric zero. The raw AS still remains None in the same form.
        ('unknown quantity retains baseline pending state', {**base, 'AS': None, 'BV': None}, '待核', '待核'),
    ])
    differences = []
    for name, row, quantity, amount in cases:
        before = wr.dumps(row)
        order = {'payload': {'result': {'rows': [row]}}}
        rendered = forms.form(None, order, '商品建议与销量库存', row['E'])
        values = {r[0]: r[1:] for r in rendered}
        if row.get('AS') is None:assert values['原计算建议量']==['未提供','缺失']
        expected = dict((r[0], r[1:]) for r in forms.fields({'签发执行量': quantity, '签发金额': amount}))
        for label, value in expected.items():
            if values.get(label) != value:
                differences.append({'case': name, 'field': label, 'before': values.get(label), 'expected': value})
        assert wr.dumps(row) == before, 'view mutated signed record'
    report = {'passed': bool(differences) if args.expect_existing_defect else not differences,
              'expected_existing_defect': args.expect_existing_defect, 'cases': len(cases),
              'differences': differences, 'signed_records_unchanged': True}
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
    print(json.dumps({'passed': report['passed'], 'cases': len(cases), 'differences': len(differences)}))
    assert report['passed']


if __name__ == '__main__':
    main()
