"""Write a four-sheet operating daily report and cache its offline Excel formulas."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

from openpyxl.cell import WriteOnlyCell
from openpyxl.formatting.rule import ColorScaleRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from analytics_formula_cache import FormulaCache
from business_daily import (Cut, DETAIL_HEADERS, RULE, STORE_HEADERS, VERSION, WAREHOUSES, aggregate,
                            average, fulfillment)
from runtime_support import checkpoint
from xlsx_exports import stream_book


SHEETS = ('汇总', '门店缺货率', '门店缺货明细', '口径说明')
RATES = ('全量缺货率', '剔除缺货率', '原始缺货率', '地采缺货率')
RATE_COLUMNS = ('R', 'J', 'H', 'O')
TAGS = ('山东-维稳型', '河北-维稳型', '新店', '山东-问题型', '河北-问题型')
CUT_HEADERS = ('门店数', '动销数', '动销缺货数', '原始缺货率', '剔除后缺货数', '剔除后缺货率', '大仓缺货数')
ORDER_HEADERS = ('sku数', '门店数', '门店确认商品数', '门店确认商品货值', '实际下单商品数',
                 '实际下单商品货值', '实际下单/门店确认商品数比值（大仓满足率）')
PERCENT = '0.00%'
MONEY = '#,##0.00'
DARK = PatternFill('solid', fgColor='176C63')
WHITE = Font(name='微软雅黑', bold=True, color='FFFFFF', size=10)
TEXT = Font(name='微软雅黑', size=10)
HEADER_ALIGNMENT = Alignment(vertical='center', wrap_text=True)


@dataclass(frozen=True)
class Formula:
    expression: str
    cached: object


def quotient(row, numerator, denominator, value):
    return Formula(f'=IF({denominator}{row}>0,{numerator}{row}/{denominator}{row},"")', value)


class Sheet:
    def __init__(self, worksheet, cache, widths, freeze=None):
        self.ws, self.cache, self.row = worksheet, cache, 0
        for i, width in enumerate(widths, 1):
            worksheet.column_dimensions[get_column_letter(i)].width = width
        worksheet.freeze_panes = freeze

    def append(self, values=(), *, header=False, percentages=(), money=(), wrap=False):
        self.row += 1
        cells = []
        for i, value in enumerate(values, 1):
            style = PERCENT if i in percentages else MONEY if i in money else None
            if value is None and not header:
                cells.append(None)
                continue
            cell = WriteOnlyCell(self.ws, value=value.expression if isinstance(value, Formula) else value)
            if isinstance(value, Formula):
                self.cache.add(self.ws.title, get_column_letter(i) + str(self.row),
                               '' if value.cached is None else value.cached)
            elif isinstance(value, str):
                cell.data_type = 's'
            if style:
                cell.number_format = style
            cell.font = WHITE if header else TEXT
            if header:
                cell.fill = DARK
            if header or wrap:
                cell.alignment = HEADER_ALIGNMENT
            cells.append(cell)
        self.ws.append(cells)

    def colors(self, cols, start, stop):
        if start > stop:
            return
        for col in cols:
            self.ws.conditional_formatting.add(f'{col}{start}:{col}{stop}', ColorScaleRule(
                start_type='percentile', start_value=5, start_color='5B9BD5',
                mid_type='percentile', mid_value=50, mid_color='FFFFFF',
                end_type='percentile', end_value=95, end_color='F8696B'))


def store_sheet(sheet, stores, result):
    sheet.ws.row_dimensions[2].height = 30
    for area in ('A1:E1', 'F1:H1', 'I1:L1', 'M1:O1', 'P1:R1', 'S1:T1'):
        sheet.ws.merged_cells.add(area)
    sheet.append(('门店信息', None, None, None, None, '原始缺货', None, None, '剔除大仓缺货',
                  None, None, None, '地采', None, None, '全量', None, None, '参考指标', None), header=True)
    sheet.append(STORE_HEADERS, header=True)
    positions = {}
    for code in result['stores']:
        store, c = stores[code], result['counts'][code]
        row = sheet.row + 1
        whole, adjusted, raw, local = c.rates()
        sheet.append((code, store['name'], store['tag'], store['owner'], store['primary'][:2], c.active,
                      c.shortage, quotient(row, 'G', 'F', raw), c.remaining, quotient(row, 'I', 'F', adjusted),
                      c.warehouse_shortage, '待核' if c.inbound_unknown else c.inbound, c.local_active,
                      c.local_shortage, quotient(row, 'N', 'M', local), c.all_active, c.all_shortage,
                      quotient(row, 'Q', 'P', whole), c.rows, c.sales), percentages=(8, 10, 15, 18))
        positions[code] = row
    sheet.ws.auto_filter.ref = f'A2:T{sheet.row}'
    sheet.colors(RATE_COLUMNS, 3, sheet.row)
    return positions


def _term(area, selected):
    criteria = ''.join(f',{col},{value}' for col, value in selected)
    return f'=IFERROR(AVERAGEIFS({area}{criteria}),"")' if selected else f'=IF(COUNT({area}),AVERAGE({area}),"")'


def _area(name, col, start, stop):
    return f"'{name}'!${col}${start}:${col}${stop}"


def _daily_values(codes, result, positions, predicates):
    values = list(zip(*(result['counts'][code].rates() for code in codes))) if codes else [()] * 4
    span = lambda c: _area(SHEETS[1], c, 3, max(positions.values()))
    selected = [(span(col), value) for col, value in predicates]
    return [Formula(_term(span(col), selected), average(values[i])) for i, col in enumerate(RATE_COLUMNS)]


def _must_value(rows, predicates):
    span = lambda c: _area(SHEETS[3], c, 2, max(2, len(rows) + 1))
    values = [row['rate'] for row in rows if all(row[k] == v for k, _, v, _ in predicates)]
    return Formula(_term(span('I'), [(span(col), ref) for _, col, _, ref in predicates]), average(values))


def _selected(stores, codes, **fields):
    return [code for code in codes if all(stores[code][key] == value for key, value in fields.items())]


def _literal(value):
    return '"' + str(value).replace('"', '""') + '"'


def _upper(sheet, stores, result, positions, must, weeks, orders, day, partial):
    sheet.append(('北极星目标',), header=True)
    sheet.ws.merged_cells.add('A1:E1')
    for metric in ('全量缺货率', '原始缺货率', '必采缺货率'):
        prior = weeks['metrics'].get(metric, {})
        published = list(prior.get('weekly_values', []))[:4]
        published += [None] * (4 - len(published))
        business_day = date.fromisoformat(day)
        first = business_day.replace(day=1)
        first_monday = first + timedelta(days=(-first.weekday()) % 7)
        week = (business_day - first_monday).days // 7
        if 1 <= week <= 3 and not partial and metric != '必采缺货率':
            idx = 0 if metric == '全量缺货率' else 2
            published[week] = average(result['counts'][code].rates()[idx] for code in result['stores'])
        sheet.append((metric, None, '上月结果', '目标'), header=True)
        sheet.append((None, None, prior.get('previous'), prior.get('target')), percentages=(3, 4))
        sheet.append(('周期', 'W1', 'W2', 'W3', 'W4'), header=True)
        sheet.append(('目标', *prior.get('weekly_targets', [])))
        sheet.append(('实际', *published), percentages=(2, 3, 4, 5))
    sheet.append()
    sheet.append(('大盘数据（日维度）',), header=True)
    sheet.append((None, '全盘', *RATES, '必卖缺货率'), header=True)
    whole_codes = result['stores']
    sheet.append((None, None, *_daily_values(whole_codes, result, positions, []), _must_value(must, [])),
                 percentages=(3, 4, 5, 6, 7))
    sheet.append(('大盘数据——单一维度',), header=True)
    sheet.append(('仓维度', WAREHOUSES[0], *RATES, '必卖缺货率'), header=True)
    for i, warehouse in enumerate(WAREHOUSES):
        name = warehouse[:2]
        code_group = _selected(stores, whole_codes, primary=warehouse)
        sheet.append((None, warehouse if i else None,
                      *_daily_values(code_group, result, positions, [('E', _literal(name))]),
                      _must_value(must, [('region', 'H', '华东' if i == 0 else '华北',
                                          _literal('华东' if i == 0 else '华北'))])),
                     percentages=(3, 4, 5, 6, 7))
    for i, tag in enumerate(TAGS):
        code_group = [c for c in whole_codes if (stores[c]['tag'].endswith('-新店') if tag == '新店' else stores[c]['tag'] == tag)]
        criterion = '*-新店' if tag == '新店' else tag
        if tag == '新店':
            must_value = _must_value(must, [('tag_group', 'J', '新店', _literal('新店'))])
        else:
            must_value = _must_value(must, [('tag', 'G', tag, _literal(tag))])
        sheet.append(('标签维度' if i == 0 else None, tag,
                      *_daily_values(code_group, result, positions, [('C', _literal(criterion))]), must_value),
                     percentages=(3, 4, 5, 6, 7))
    sheet.append(('门店维度', '门店名称', '标签', '运营负责人', '主仓', *RATES), header=True)
    for code in whole_codes:
        s, c = stores[code], result['counts'][code]
        cells = [_area(SHEETS[1], col, positions[code], positions[code]).split(':')[0] for col in RATE_COLUMNS]
        rates = [Formula(f'=IF(ISNUMBER({cell}),{cell},"")', value) for cell, value in zip(cells, c.rates())]
        sheet.append((None, s['name'], s['tag'], s['owner'], s['primary'][:2], *rates), percentages=(6, 7, 8, 9))
    sheet.append()
    sheet.append(('大盘数据——组合维度',), header=True)
    sheet.append(('运营+标签维度', '运营负责人', '标签', '门店数', *RATES, '必卖缺货率'), header=True)
    groups = defaultdict(list)
    for code in whole_codes:
        s = stores[code]
        if s['owner'] != '自营':
            groups[s['owner'], s['tag']].append(code)
    for (owner, tag), codes in sorted(groups.items()):
        row = sheet.row + 1
        wants = [('owner', 'F', owner, f'B{row}'), ('tag', 'G', tag, f'C{row}')]
        sheet.append((None, owner, tag, len(codes),
                      *_daily_values(codes, result, positions, [('D', f'$B{row}'), ('C', f'$C{row}')]),
                      _must_value(must, wants)), percentages=(5, 6, 7, 8, 9))
    for kind, title, names in (('buyer', '采购维度', ('大仓', '采购')),
                               ('supplier', '供应商维度', ('供应商', '大仓'))):
        sheet.append((title, *names, *ORDER_HEADERS), header=True)
        for (warehouse, key), group in sorted(orders[kind].items(),
                                              key=lambda pair: (WAREHOUSES.index(pair[0][0]), -pair[1].demand, pair[0][1])):
            if not group.stores:
                continue
            row, values = sheet.row + 1, group.values()
            label = (warehouse, key) if kind == 'buyer' else (key, warehouse)
            sheet.append((None, *label, *values[:-1], quotient(row, 'H', 'F', values[-1])),
                         percentages=(10,), money=(7, 9))


def _cut_values(cut, row, num_cols):
    n, active, shortage, raw, remaining, adjusted, warehouse_shortage = cut.values()
    d, a, b = num_cols
    return n, active, shortage, quotient(row, a, d, raw), remaining, quotient(row, b, d, adjusted), warehouse_shortage


def _totals(groups):
    result = Cut()
    for cut in groups.values():
        result.add(cut)
    return result


def _lower(sheet, stores, data):
    for title, fields in (('仓维度', ('primary',)), ('标签维度', ('tag',)), ('运营+标签维度', ('owner', 'tag'))):
        groups = defaultdict(Cut)
        for code in data['stores']:
            store = stores[code]
            labels = tuple(store[key][:2] if key == 'primary' else store[key] for key in fields)
            groups[labels].store(code, data['counts'][code])
        width = len(fields)
        cols = ('C', 'D', 'F') if width == 1 else ('D', 'E', 'G')
        pct = (5, 7) if width == 1 else (6, 8)
        sheet.append((title,), header=True)
        labels = ('运营负责人', '标签') if width == 2 else ('主仓' if fields[0] == 'primary' else '标签',)
        sheet.append((*labels, *CUT_HEADERS), header=True)
        start = sheet.row + 1
        for key, group in sorted(groups.items(), key=lambda pair: (-pair[1].active, pair[0])):
            sheet.append((*key, *_cut_values(group, sheet.row + 1, cols)), percentages=pct)
        sheet.colors([get_column_letter(c) for c in pct], start, sheet.row)
        sheet.append(('合计', *([None] * (width - 1)), *_cut_values(_totals(groups), sheet.row + 1, cols)),
                     percentages=pct)
    cat, buyer, supplier = (data['cuts'][key] for key in ('category', 'buyer_category', 'supplier'))
    sheet.append(('类目维度', *[None] * 9, '采购+类目维度'), header=True)
    sheet.append(('大仓', '类目', *CUT_HEADERS, None, '采购负责人', '类目', *CUT_HEADERS), header=True)
    categories = sorted(cat, key=lambda pair: (WAREHOUSES.index(pair[0]), -cat[pair].active, pair[1]))
    owner_totals = defaultdict(int)
    for (owner, _), v in buyer.items():
        owner_totals[owner] += v.active
    buyers = sorted(buyer, key=lambda pair: (-owner_totals[pair[0]], pair[0], -buyer[pair].active, pair[1]))
    start = sheet.row + 1
    for i in range(max(len(categories), len(buyers)) + 1):
        row = sheet.row + 1
        if i < len(categories):
            key = categories[i]
            left = (key[0][:2], key[1], *_cut_values(cat[key], row, ('D', 'E', 'G')))
        elif i == len(categories):
            left = ('合计', None, *_cut_values(_totals(cat), row, ('D', 'E', 'G')))
        else:
            left = (None,) * 9
        if i < len(buyers):
            key = buyers[i]
            right = (*key, *_cut_values(buyer[key], row, ('N', 'O', 'Q')))
        elif i == len(buyers):
            right = ('合计', None, *_cut_values(_totals(buyer), row, ('N', 'O', 'Q')))
        else:
            right = ()
        sheet.append((*left, None, *right), percentages=(6, 8, 16, 18))
    sheet.colors(('F', 'H'), start, start + len(categories) - 1)
    sheet.colors(('P', 'R'), start, start + len(buyers) - 1)
    sheet.append(('供应商维度',), header=True)
    sheet.append(('大仓', '供应商', 'sku数', *CUT_HEADERS), header=True)
    start = sheet.row + 1
    for key, c in sorted(supplier.items(), key=lambda item: (WAREHOUSES.index(item[0][0]), -item[1].active, item[0][1])):
        sheet.append((key[0][:2], key[1], len(c.skus), *_cut_values(c, sheet.row + 1, ('E', 'F', 'H'))),
                     percentages=(7, 9))
    sheet.colors(('G', 'I'), start, sheet.row)
    c = _totals(supplier)
    sheet.append(('合计', None, len(c.skus), *_cut_values(c, sheet.row + 1, ('E', 'F', 'H'))), percentages=(7, 9))


def _about(sheet, basis, daily, orders, must, blacklist_outside_scope):
    sheet.append(('项目', '说明', None, '必卖门店编码', '门店名称', '运营负责人', '标签', '运营归属', '必卖剔除率', '合并标签'),
                 header=True)
    info = [
        ('数据日期', basis['day']), ('报表范围', '部分参考' if basis['partial'] else '所选业务日正式范围'),
        ('规则和模板', VERSION + ' · ' + RULE),
        ('统计范围', f'应采 {len(basis["expected_stores"])} 店，采用 {len(daily["stores"])} 店'),
        ('原始缺货', '正常且月销>0的有效仓品计动销。可用库存<1计缺货。'),
        ('剔除后缺货', '原始缺货扣除按当批仓缺货集归因的行。'),
        ('大仓缺货集', f'虚拟分类为空、启用、可用≤0。山东排除日配。本批 {daily["stockout_sizes"]}'),
        ('河北主从仓', RULE + '。河北命中禁用或标杆淘汰时标河北仓禁采。'),
        ('全量和明细', '全量按可用<1。明细按可用≤0并剔除黑名单。'),
        ('地采', '适用仓均无匹配且未命中本店黑名单。可用≤0计地采缺货。'),
        ('黑名单', '按原门店名称和商品编码精确匹配。'),
        ('比率', '上方缺货率为门店等权平均。下方按数量合计计算。空分母留空。'),
        ('在途', '原始缺货且在途大于零。缺在途数据的门店该项显示待核。'),
        ('必卖', '采用必卖清单·剔除大仓缺货率及冻结的门店分组。全盘沿用外表门店范围。'),
        ('冻结截至', basis['order_cutoff']),
        ('订单版本摘要', basis['order_snapshot']),
        ('订单满足率', '按冻结的门店回传和实际下单阶段汇总。两阶段都有数值的行参加已核对块。'),
        ('订单数量待核行', sum(c.pending for c in orders['supplier'].values())),
        ('订单货值', '采用阶段原件金额或有效零。阶段未留存金额时，对应组货值留空。'),
        ('明细行数', daily['details']),
    ]
    for role, ref in basis['files'].items():
        effective = f' · {ref["effective_day"]} 起采用' if ref.get('effective_day') else ''
        info.append((role, f'{ref["name"]} · SHA-256 {ref["sha256"]}{effective}'))
    if defaults := basis.get('week_defaults'):
        ref = defaults['source']
        info.append(('月度目标模板', f'{defaults["month"]} · {ref["name"]} · SHA-256 {ref["sha256"]}'))
        info.append(('模板采用范围', '采用上月结果、月目标和 W1–W4 周目标。周实际沿用本次实算或已登记的发布值。'))
    if rules := basis.get('store_rules'):
        info.append(('门店范围依据', f'{rules["source"]} · 版本 {rules["version"]}'))
        info.append(('主从仓依据', '保存的路由版本 ' + rules['route_source']))
    for warehouse, ref in basis['catalogs'].items():
        v = ref.get('file', ref)
        info.append((warehouse, f'{v.get("observed")} · {v.get("sha256") or v.get("source_sha")} · '
                                 f'账号 {v.get("account") or "未留存"} · 采集 {v.get("acquisition_batch") or ref.get("run") or "手工登记"}'))
        if 'adopted' in ref:
            info.append((warehouse + '原采用记录', f'{ref["adopted"]} · 仓资料版本 {ref["revision"]}'))
    for ref in basis['store_snapshots']:
        info.append(('门店快照 ' + ref['store'], f'{ref["observed"]} · {ref["id"]} · {ref["source_sha"]}'))
    for ref in basis['sales']:
        info.append(('门店原件', f'{ref["observed"]} · {ref["name"]} · {ref["sha256"]}'))
    for problem in blacklist_outside_scope:
        info.append(('范围外黑名单待核', problem))
    for h in basis['order_revisions']:
        info.append(('仓单 ' + h['number'], f'{h["warehouse"]} · 签发 {h["revision"]} · 仓版 {h["child_revision"]} · {h["digest"]}'))
    for (code, reason), count in sorted(daily['discrepancies'].items()):
        info.append(('全量与明细差额 ' + code, f'{reason} {count} 行'))
    for w, order, sku, category, demand, demand_value, ordered, ordered_value in orders['unmapped']:
        info.append(('负责人未映射', f'{w} · {order} · {sku} · {category} · {demand}/{demand_value} → {ordered}/{ordered_value}'))
    for i in range(max(len(info), len(must))):
        values = info[i] if i < len(info) else (None, None)
        if i < len(must):
            m = must[i]
            values = (*values, None, m['code'], m['name'], m['owner'], m['tag'], m['region'], m['rate'], m['tag_group'])
        if i < len(info):
            sheet.ws.row_dimensions[sheet.row + 1].height = 30
        sheet.append(values, percentages=(9,), wrap=True)


def write(target, basis, inputs, progress, cancel):
    target = Path(target)
    start = perf_counter()
    with stream_book() as book, FormulaCache(target.parent, cancel) as cache:
        summary, store, detail, about = [book.create_sheet(title) for title in SHEETS]
        ss = Sheet(summary, cache, (25, 42, 21, 22, 18, 19, 19, 19, 19, 23, 22, 23, 14, 14, 14, 15, 14, 15, 14))
        sr = Sheet(store, cache, (23, 46, 20, 16, 12, *(14,) * 15), 'A3')
        dr = Sheet(detail, cache, (22, 46, 23, 45, 20, 24, 65, 15, 12, 12, 12, 18, 12, 20, 15, 15, 15), 'A2')
        ar = Sheet(about, cache, (40, 115, 3, 24, 45, 17, 20, 16, 16, 20), 'A2')
        progress('计算缺货指标和逐店归因')
        dr.append(DETAIL_HEADERS, header=True)
        daily = aggregate(inputs, dr.append, cancel)
        orders = fulfillment(inputs['orders'], inputs['catalogs'], inputs['owners'], cancel)
        calculated = perf_counter()
        dr.ws.auto_filter.ref = f'A1:Q{dr.row}'
        progress('写入经营日报和离线公式')
        stores = inputs['stores']
        positions = store_sheet(sr, stores, daily)
        must = []
        for code, row in inputs['must_sell'].items():
            if basis['partial'] and code not in stores:
                continue
            tag = row.get('tag') or stores.get(code, {}).get('tag', '')
            must.append({**row, 'code': code, 'tag': tag, 'tag_group': '新店' if tag.endswith('-新店') else tag,
                         'region': row['group']})
        _upper(ss, stores, daily, positions, must, inputs['weeks'], orders, basis['day'], basis['partial'])
        _lower(ss, stores, daily)
        _about(ar, basis, daily, orders, must, inputs.get('blacklist_outside_scope', ()))
        cache.save(book, target)
    progress('核验四页与发布前文件')
    with ZipFile(target) as archive:
        checkpoint(cancel)
        if archive.testzip() or any(name.startswith('xl/externalLinks/') for name in archive.namelist()):
            raise ValueError('日报文件校验失败或含外部链接')
    return {'stores': len(positions), 'details': daily['details'], 'must_sell_stores': len(must),
            'stockout_sizes': daily['stockout_sizes'], 'warehouse_shortage': sum(c.warehouse_shortage for c in daily['counts'].values()),
            'supplier_groups': sum(bool(group.stores) for group in orders['supplier'].values()),
            'buyer_groups': sum(bool(group.stores) for group in orders['buyer'].values()),
            'calculate_and_details_seconds': calculated - start, 'write_and_verify_seconds': perf_counter() - calculated}
