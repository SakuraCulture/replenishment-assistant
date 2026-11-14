"""Audited layouts on synthetic frozen facts; no business files or platforms."""
import copy
from decimal import Decimal
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import openpyxl
import analytics_template_export as export
from analytics_formula_cache import FormulaCache
from runtime_support import Cancelled
from xlsx_exports import stream_book


def fact(order='BH-001', sku='0001', group='必采', scope='纳入', values=(24, 20, 10, 25, 22, 18), price='2.5'):
    return {'day': '2026-11-14', 'store': 'STORE-1', 'store_name': '测试门店', 'warehouse': '河北仓',
            'order': order, 'sku': sku, 'name': '测试商品', 'unit': '瓶', 'group': group, 'scope': scope,
            'reasons': ['冻结准入缺失'] if scope == '待核' else ['对应仓禁用'] if scope == '排除' else [],
            'price': Decimal(price) if price is not None else None,
            'quantities': dict(zip(export.STAGES, [Decimal(str(v)) if v is not None else None for v in values])),
            'valid': dict.fromkeys(export.STAGES, True), 'states': dict.fromkeys(export.STAGES, '已确认'),
            'payment_amount': Decimal('999'), 'product_category': '测试三级类目', 'upstream_supplier': '冻结供应商',
            'references': {'signed_number': order, 'jst_numbers': ['00001234567890123456'], 'purchases': [{'number': 'AX-001'}]},
            'template_attributes': {'barcode': '0000000123', 'supply_state': '禁用', 'stock': Decimal(0),
                'monthly_sales': Decimal(30), 'in_transit': Decimal(0), 'target_holding': Decimal(50),
                'pack_size': Decimal(12), 'minimum_holding': Decimal(12), 'stock_snapshot_at': '2026-11-14 08:00',
                'warehouse_stock': Decimal(0)}}


class TemplateExportTests(unittest.TestCase):
    def write(self, rows, detail=False):
        root = tempfile.TemporaryDirectory(); self.addCleanup(root.cleanup)
        path = Path(root.name) / 'test.xlsx'
        with FormulaCache(root.name) as cache, stream_book() as book:
            book._analytics_formula_cache = cache.add
            names = (export.append_detail if detail else export.append_summary)(book, {}, rows)
            book.save(path); cache.apply(path)
        formula = openpyxl.load_workbook(path, data_only=False)
        values = openpyxl.load_workbook(path, data_only=True)
        self.addCleanup(formula.close); self.addCleanup(values.close)
        return names, formula, values

    def test_detail_exact_columns_order_six_stages_and_frozen_price(self):
        names, f, v = self.write([fact()], detail=True); s = v[names[0]]
        self.assertEqual(names, ['SKU完整明细'])
        self.assertEqual(s.max_column, 43)
        self.assertEqual([c.value for c in s[7]], export.DETAIL_HEADERS)
        self.assertEqual([s.cell(8, c).value for c in range(6, 12)], [24, 20, 10, 25, 22, 18])
        self.assertEqual(s['N8'].value, 2.5)  # operations/return, no 100% cap.
        self.assertEqual(s['Y8'].value, 55)  # 22 * frozen V; never actual payment 999.
        self.assertEqual(s['C8'].value, '0001'); self.assertEqual(s['AD8'].value, '0000000123')
        self.assertEqual(s['AF8'].value, '禁用'); self.assertEqual(s['AN8'].value, 0)
        self.assertEqual(s['AP8'].value, '00001234567890123456'); self.assertEqual(s['AQ8'].value, 'AX-001')
        self.assertIn('范围：纳入', s['E8'].value)
        self.assertEqual(f[names[0]]['U8'].value, '=IF(COUNT(F8,T8)<2,"n.a.",F8*T8)')
        self.assertIn('目标持有量为当时预测需求含安全库存', s['A4'].value)

    def test_summary_three_layouts_all_and_empty_subclass(self):
        names, f, v = self.write([fact()])
        self.assertEqual(names, ['SKU种类', '商品货值', '商品数量'])
        s = v['商品数量']
        self.assertEqual([c.value for c in s[16]], export.SUMMARY_HEADERS)
        self.assertEqual(s.max_column, 22)
        self.assertEqual([s.cell(r, 5).value for r in range(17, 20)], ['ALL', '必采', '非必采'])
        self.assertEqual([s.cell(19, c).value for c in range(7, 13)], [0] * 6)
        self.assertEqual(s['G17'].value, 24); self.assertEqual(s['O17'].value, 2.5)
        self.assertEqual(s['A12'].value, 24)  # ALL once, not ALL + child rows.
        self.assertEqual(v['商品货值']['G17'].value, 60)
        self.assertEqual(s['U17'].value, '00001234567890123456')
        self.assertIn('SUMIFS', f['商品数量']['A12'].value)
        self.assertNotIn('SUBTOTAL', f['商品数量']['A12'].value)

    def test_missing_and_invalid_stage_unknown_not_zero(self):
        row = fact(price=None); row['valid']['operations'] = False
        row['quantities']['receipt'] = Decimal(-1)
        row['template_attributes']['target_holding'] = None
        _, _, v = self.write([row], detail=True); s = v['SKU完整明细']
        self.assertEqual(s['I8'].value, export.NA); self.assertEqual(s['K8'].value, export.NA)
        self.assertEqual(s['T8'].value, export.NA); self.assertEqual(s['U8'].value, export.NA)
        self.assertEqual(s['N8'].value, export.NA); self.assertEqual(s['AJ8'].value, export.NA)
        self.assertEqual(s['AG8'].value, 0)
        row['quantities']['model'] = Decimal(0)
        _, _, v = self.write([row], detail=True)
        self.assertEqual(v['SKU完整明细']['U8'].value, export.NA)  # Exact template COUNT guard.

    def test_all_pending_and_all_excluded_orders_keep_identity(self):
        pending = fact('BH-PENDING', scope='待核')
        excluded = fact('BH-EXCLUDED', scope='排除')
        _, _, v = self.write([pending, excluded]); s = v['商品数量']
        self.assertEqual(s['B17'].value, 'BH-PENDING'); self.assertEqual(s['G17'].value, export.NA)
        self.assertEqual(s['G18'].value, export.NA); self.assertEqual(s['G19'].value, 0)
        self.assertEqual(s['B20'].value, 'BH-EXCLUDED'); self.assertEqual(s['G20'].value, 0)
        self.assertEqual(s['A12'].value, export.NA)
        _, _, d = self.write([pending, excluded], detail=True)
        self.assertEqual(d['SKU完整明细'].max_row, 9)
        self.assertIn('范围：待核', d['SKU完整明细']['E8'].value)
        self.assertIn('范围：排除', d['SKU完整明细']['E9'].value)

    def test_pending_with_included_does_not_understate_group(self):
        rows = [fact(), fact(sku='0002', scope='待核'), fact(sku='0003', group='非必采', scope='排除')]
        _, _, v = self.write(rows)
        self.assertEqual(v['商品数量']['G17'].value, export.NA)
        self.assertEqual(v['商品数量']['G19'].value, 0)

    def test_attribute_pending_group_and_cross_order_sku_count(self):
        rows = [fact(), fact(sku='0002', group='属性待核'), fact('BH-002')]
        _, _, v = self.write(rows); s = v['SKU种类']
        self.assertEqual(s['E20'].value, '属性待核'); self.assertEqual(s['G17'].value, 2)
        self.assertEqual(s['G21'].value, 1)
        self.assertEqual(s['A12'].value, 3)  # 0001 appears in two orders, counted twice.
        self.assertEqual(s['C12'].value, 3)

    def test_no_operations_backfill_and_invalid_stage_propagates(self):
        row = fact(); del row['valid']['operations']; del row['quantities']['operations']
        _, _, v = self.write([row]); s = v['商品数量']
        self.assertEqual(s['J17'].value, export.NA); self.assertEqual(s['G12'].value, export.NA)
        self.assertEqual(s['O17'].value, export.NA)
        self.assertEqual(s['I17'].value, 10)

    def test_real_order_status_priority_and_empty_query(self):
        row = fact(); row['order_status'] = '已收齐'; row['states']['receipt'] = '无需此环节'
        _, _, v = self.write([row], detail=True)
        self.assertEqual(v['SKU完整明细']['E8'].value, '已收齐；范围：纳入')
        _, _, v = self.write([row]); self.assertEqual(v['商品数量']['F17'].value, '已收齐')
        names, _, v = self.write([])
        self.assertEqual(len(names), 3); self.assertEqual(v['商品数量']['A12'].value, 0)
        self.assertEqual(v['商品数量']['A6'].value, export.NA)

    def test_store_code_and_name_and_signed_source_attributes_survive(self):
        row = fact(); row['store'] = '0000123'
        row['template_attributes'].update(stock=Decimal('-2.5'), monthly_sales=Decimal('-3'),
                                          in_transit=Decimal('-4'), warehouse_stock=Decimal('-5'))
        _, _, v = self.write([row], detail=True); s = v['SKU完整明细']
        self.assertEqual(s['B8'].value, '测试门店（0000123）')
        self.assertEqual([s[c].value for c in ('AG8', 'AH8', 'AI8', 'AN8')], [-2.5, -3, -4, -5])
        _, _, v = self.write([row]); self.assertEqual(v['商品数量']['C17'].value, '测试门店（0000123）')

    def test_native_layout_filters_freeze_safe_text_and_formats(self):
        row = fact(); row['name'] = '=HYPERLINK("x")'
        _, f, _ = self.write([row], detail=True); s = f['SKU完整明细']
        self.assertEqual(s['D8'].data_type, 's'); self.assertEqual(s.freeze_panes, 'E8')
        self.assertEqual(s.auto_filter.ref, 'A7:AQ8'); self.assertEqual(len(s.tables), 1)
        self.assertIn('AG6:AM6', {str(m) for m in s.merged_cells.ranges})
        self.assertEqual(s.column_dimensions['AO'].width, 27)
        self.assertEqual(s['L8'].number_format, '0.0%'); self.assertEqual(s['T8'].number_format, '#,##0.00')
        self.assertEqual(s['A7'].fill.fgColor.rgb, 'FF233B59')
        _, f, _ = self.write([row]); s = f['SKU种类']
        self.assertEqual(s.freeze_panes, 'E17'); self.assertEqual(s.auto_filter.ref, 'A16:V19')
        self.assertTrue(s['A17'].font.bold); self.assertEqual(s['A17'].fill.fgColor.rgb, 'FFEAF1F8')

    def test_template_card_colors_centering_and_numeric_unavailable_spacing(self):
        row = fact(); row['quantities']['return'] = None; row['template_attributes']['minimum_holding'] = None
        _, f, _ = self.write([row]); s = f['商品数量']
        for coordinate in ('A6', 'D6', 'G6', 'J6', 'A9', 'D9', 'G9', 'J9', 'A12'):
            self.assertEqual(s[coordinate].alignment.horizontal, 'center', coordinate)
        for coordinate in ('A5', 'D5', 'S15'):
            self.assertEqual(s[coordinate].fill.fgColor.rgb, 'FF207D77', coordinate)
        for coordinate in ('G5', 'J5', 'A8', 'D8', 'G8', 'J8'):
            self.assertEqual(s[coordinate].fill.fgColor.rgb, 'FF233B59', coordinate)
        for coordinate in ('H18', 'I18'):
            self.assertEqual(s[coordinate].alignment.horizontal, 'right', coordinate)
            self.assertEqual(s[coordinate].alignment.indent, 1, coordinate)
        self.assertIn('S15:T15', {str(m) for m in s.merged_cells.ranges})
        _, f, _ = self.write([row], detail=True); s = f['SKU完整明细']
        for coordinate in ('R6',):
            self.assertEqual(s[coordinate].fill.fgColor.rgb, 'FF207D77', coordinate)
        self.assertIn('R6:S6', {str(m) for m in s.merged_cells.ranges})
        for coordinate in ('G8', 'H8', 'AK8', 'AL8'):
            self.assertEqual(s[coordinate].alignment.horizontal, 'right', coordinate)
            self.assertEqual(s[coordinate].alignment.indent, 1, coordinate)
        self.assertEqual(s['AL8'].number_format, 'General')

    def test_cancel_before_sheet_writes(self):
        stop = threading.Event(); stop.set()
        with stream_book() as book:
            with self.assertRaises(Cancelled): export.append_summary(book, {}, [fact()], cancel=stop)
        with stream_book() as book:
            with self.assertRaises(Cancelled): export.append_detail(book, {}, [fact()], cancel=stop)


if __name__ == '__main__':
    unittest.main()
