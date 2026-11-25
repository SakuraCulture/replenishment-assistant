"""Audit the published reference against independent order and inventory evidence."""
import argparse
from collections import Counter
from contextlib import closing
import csv
from pathlib import Path
import re
import sys

from openpyxl import load_workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from business_daily import average, decimal, fulfillment, ratio, stockout_sets
from business_daily_sources import CATALOG_COLUMNS, sheet_rows, text, warehouse
from export_files import sha256
from input_adapter import identifier
from stockout_common import atomic_json


def exact(value):
    code, issue = identifier(value)
    if issue:
        raise ValueError(issue)
    return code


def close_enough(left, right):
    if left is None or right is None:
        return left is right
    return abs(decimal(left) - decimal(right)) < decimal('0.00000001')


def catalog(path):
    return {exact(r['sku']): {'stock': decimal(r['stock']), 'status': text(r['status']),
                             'virtual': text(r['virtual']), 'category': text(r['category']),
                             'supplier': text(r['supplier'])}
            for r, _ in sheet_rows(path, CATALOG_COLUMNS)}


def audit(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    with closing(load_workbook(args.sample, read_only=True, keep_links=False, data_only=True)) as book:
        summary = list(book['汇总'].values)
        report_sheets = book.sheetnames
        day = str(book['口径说明']['B2'].value)
        day = f'{day[:4]}-{day[4:6]}-{day[6:8]}'
        stores = list(book['门店缺货率'].iter_rows(min_row=3, values_only=True))
        details = list(book['门店缺货明细'].iter_rows(min_row=2, values_only=True))
    rates = [(ratio(s[16], s[15]), ratio(s[8], s[5]), ratio(s[6], s[5]), ratio(s[13], s[12])) for s in stores]
    rate_diffs = [(row[0], i, expected, row[col]) for row, expected_rates in zip(stores, rates)
                  for i, (col, expected) in enumerate(zip((17, 9, 7, 14), expected_rates), 1)
                  if not close_enough(expected, row[col])]
    store_averages = [average(values) for values in zip(*rates)]
    catalogs = {'山东仓': catalog(args.shandong), '河北仓': catalog(args.hebei)}
    observed_short = stockout_sets(catalogs)
    changes, latest = [], 0
    for row in details:
        if row[14] != '是':
            continue
        sku = exact(row[5])
        new = sku in observed_short['山东仓'] or row[11] == '主仓（河北）' and sku in observed_short['河北仓']
        old = row[15] == '是'
        latest += new
        if old != new:
            records = [catalogs[w].get(sku, {}) for w in ('山东仓', '河北仓')]
            changes.append((row[2], row[3], sku, row[11], old, new,
                            *[v for rec in records for v in (rec.get('status'), rec.get('stock'),
                                                            rec.get('virtual'), rec.get('category'))]))
    with (out / '库存归因逐店逐SKU.csv').open('x', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('门店编码', '门店', 'SKU', '样表仓归属', '样表归因', '上午快照归因',
                         '山东状态', '山东可用数', '山东虚拟分类', '山东类目',
                         '河北状态', '河北可用数', '河北虚拟分类', '河北类目'))
        writer.writerows(changes)
    month = str(int(day[5:7])) + '月'
    owners = {text(row['category']): text(row['owner']) for row, _ in sheet_rows(
        args.owners, {'category': ('分类',), 'owner': (month,)})}
    fields = ('订单编号', 'SKU编码', '订单日期', '供货仓', '门店回传量', '门店回传货值', '实际下单量', '实际下单货值')
    keys = ('order', 'sku', 'day', 'warehouse', 'demand', 'demand_value', 'ordered', 'ordered_value')

    def order_rows():
        for row, line in sheet_rows(args.orders, dict(zip(keys, ((name,) for name in fields)))):
            if str(row.pop('day'))[:10] != day:
                continue
            row['warehouse'] = warehouse(row['warehouse'])
            match = re.fullmatch(r'BH-\d{8}-(.+)-ZC-(?:SD|HB)', row['order'])
            if not match or not row['warehouse']:
                raise ValueError(f'{line} 原仓单身份不可核对')
            yield {**row, 'sku': exact(row['sku']), 'store': match[1],
                   'revision': '原经营明细文件未单列修订'}

    actual = fulfillment(order_rows(), catalogs, owners)
    expected = {'supplier': {}, 'buyer': {}}
    block = None
    for r in summary:
        if r[0] in ('采购维度', '供应商维度') and r[3] == 'sku数':
            block = 'buyer' if r[0] == '采购维度' else 'supplier'
            continue
        if block and r[1] and r[2] and isinstance(r[3], (int, float)):
            wh, group = (r[1], r[2]) if block == 'buyer' else (r[2], r[1])
            expected[block][warehouse(wh), group] = r[3:10]
        else:
            block = None
    group_counts, group_diff = {}, []
    for kind in expected:
        fresh = {k: v.values() for k, v in actual[kind].items() if v.stores}
        group_counts[kind] = {'expected': len(expected[kind]), 'observed': len(fresh),
                              'by_warehouse': dict(Counter(w for w, _ in fresh))}
        for key in sorted(set(expected[kind]) | set(fresh)):
            a, b = expected[kind].get(key), fresh.get(key)
            if a is None or b is None or not all(close_enough(x, y) for x, y in zip(a, b)):
                group_diff.append([kind, *key, a, b])
    with closing(load_workbook(args.must_sell, read_only=True, keep_links=False, data_only=True)) as book:
        it = book['门店明细'].values
        heads = next(it)
        values = {exact(r[0]): decimal(r[heads.index('必卖清单·剔除大仓缺货率')]) for r in it if r[0]}
    overlap = set(values) & {r[0] for r in stores}
    evidence = {
        'day': day, 'sheets': report_sheets, 'stores': len(stores), 'store_rate_cells': len(stores) * 4,
        'store_rate_differences': rate_diffs, 'detail_rows_in_reference': len(details),
        'store_average': store_averages, 'totals': {name: sum(r[col] for r in stores) for name, col in (
            ('all_active', 15), ('all_shortage', 16), ('active', 5), ('shortage', 6), ('remaining', 8),
            ('warehouse_shortage', 10), ('inbound', 11), ('local_active', 12), ('local_shortage', 13))},
        'morning_stockout_sku_counts': {w: len(s) for w, s in observed_short.items()},
        'warehouse_attribution_on_reference_rows_with_morning_snapshot': latest,
        'warehouse_attribution_changed_rows': len(changes),
        'warehouse_attribution_gained': sum(r[5] for r in changes),
        'must_sell': {'stores': len(values), 'average': average(values.values()),
                      'overlap': len(overlap), 'overlap_average': average(values[k] for k in overlap)},
        'orders': {'group_counts': group_counts, 'group_differences': group_diff,
                   'unmapped_sku_rows': actual['unmapped']},
        'source_sha256': {str(p): sha256(p) for p in (args.sample, args.orders, args.must_sell, args.owners,
                                                   args.shandong, args.hebei)},
        'scope': '样表核口径和结构。库存差异仅替换当日上午仓资料，使用样表原始缺货行及其归属。'
                 '订单块独立从经营明细及实仓商品资料重算。'
    }
    import json
    atomic_json(out / '复核.json', json.loads(json.dumps(evidence, ensure_ascii=False, default=str)))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for key in ('sample', 'orders', 'must-sell', 'owners', 'shandong', 'hebei', 'output'):
        parser.add_argument('--' + key, type=Path, required=True)
    audit(parser.parse_args())
