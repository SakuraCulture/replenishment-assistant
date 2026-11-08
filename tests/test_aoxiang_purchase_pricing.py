"""Business rounding cases, independent of Excel cell display formatting."""
import sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from aoxiang_purchase_pricing import priced_row, preview_rows

class PurchasePricingTests(unittest.TestCase):
    def test_half_up_ties_repeating_fractional_zero_and_large_quantity(self):
        for q, a, price, total, delta in [
            ('1', '1.005', '1.01', '1.01', '0.005'),
            ('1', '1.0049', '1.00', '1.00', '-0.0049'),
            ('2', '5.35', '2.68', '5.36', '0.01'),
            ('3', '10', '3.33', '9.99', '-0.01'),
            ('7', '.99', '0.14', '0.98', '-0.01'),
            ('.5', '.005', '0.01', '0.01', '0.005'),
            ('10000', '1', '0.00', '0.00', '-1.00'),
            ('2', '0', '0.00', '0.00', '0.00'),
            ('48', '.99', '0.02', '0.96', '-0.03'),
            ('200', '201', '1.01', '202.00', '1.00')]:
            with self.subTest(q=q, a=a):
                r = priced_row(q, a)
                self.assertEqual((r['unit_price'], r['amount'], r['rounding_delta']), (price,total,delta))

    def test_exact_ratio_just_below_half_does_not_round_up(self):
        self.assertEqual(priced_row('1', '1.004'+'9'*120)['unit_price'], '1.00')

    def test_negative_nonfinite_and_zero_quantity_rejected(self):
        for q,a in [('0','1'),('-1','1'),('NaN','1'),('1','-1'),('1','Infinity')]:
            with self.subTest(q=q,a=a), self.assertRaises(ValueError):priced_row(q,a)

    def test_preview_keeps_original_payment_and_legacy_history(self):
        row={'sku':'001','name':'合成品','quantity':'3',**priced_row('3','10')}
        self.assertEqual(preview_rows([row])[0],['001','合成品','3','10','3.33','9.99','-0.01'])
        self.assertEqual(preview_rows([{'sku':'001','name':'旧单','quantity':'3','amount':'10'}])[0],['001','旧单','3','10','—','10','0'])
