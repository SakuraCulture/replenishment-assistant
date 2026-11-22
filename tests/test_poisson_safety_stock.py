"""The Poisson buffer and the trended forecast have separate demand inputs."""
from math import exp, floor, sqrt
from pathlib import Path
from statistics import NormalDist
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import model
from result_semantics import pre_round_target


YITANG = dict(H=.29, I=3, J=6, K=3, L=0, O=1, N=0, V=8.5, AB=.4, AC=.5, AD=.25)


class PoissonSafetyStock(unittest.TestCase):
    def test_yitang_three_protection_periods(self):
        for depth, quantile, quantity in ((3, 4, 2), (4, 4, 2), (5, 4, 2)):
            with self.subTest(depth=depth):
                row = model.calculate(YITANG, model.Rules(depth=depth, lead=2, probability=.95))
                self.assertEqual((row['AN'], row['AM'], row['BF'], row['BG']), (.28125, 1.3564, quantile, '通过'))
                self.assertEqual(row['AO'], .28125 * (depth + 2))
                self.assertAlmostEqual(row['AP'], row['AO'] * 1.3564)
                self.assertAlmostEqual(row['AL'], quantile - row['AO'])
                self.assertAlmostEqual(row['AQ'], row['AP'] + row['AL'] - 3)
                self.assertEqual(row['AS'], quantity)
        row = model.calculate(YITANG, model.Rules(depth=3, lead=2, probability=.95))
        self.assertAlmostEqual(pre_round_target(row), 4.5011875)
        self.assertAlmostEqual(row['AQ'], 1.5011875)

    def test_fixed_mean_and_probability_under_rising_falling_and_zero_trend(self):
        rules = model.Rules(depth=3, lead=2, probability=.95)
        # These sales windows have the same weighted daily demand and different recent sales.
        rising = model.calculate(YITANG, rules)
        flat = model.calculate(dict(YITANG, AB=0), rules)
        falling = model.calculate(dict(YITANG, H=.43, I=1, J=8), rules)
        zero = model.calculate(dict(YITANG, H=.50, I=0, J=9), rules)
        self.assertGreater(rising['AM'], 1)
        self.assertEqual(flat['AM'], 1)
        self.assertTrue(0 < falling['AM'] < 1)
        self.assertEqual((zero['AM'], zero['AP']), (0, 0))
        for row in (rising, flat, falling, zero):
            self.assertEqual((row['AO'], row['BF'], row['AL'], row['BG']), (1.40625, 4, 2.59375, '通过'))
        self.assertGreater(rising['AP'], flat['AP'])
        self.assertGreater(flat['AP'], falling['AP'])
        self.assertGreater(falling['AP'], zero['AP'])
        self.assertEqual(pre_round_target(zero), 2.59375)
        self.assertEqual(pre_round_target(flat), flat['BF'])

    def test_quantile_start_and_result_use_base_demand(self):
        rules = model.Rules(depth=5, lead=2, probability=.95)
        row = model.calculate(dict(YITANG, H=29, I=300, J=600), rules)
        mean = row['AO']
        z = NormalDist().inv_cdf(.95)
        self.assertEqual(row['BE'], max(0, floor(mean + z * sqrt(mean) + (z*z - 1)/6) - 3))
        term = cumulative = exp(-mean)
        quantile = 0
        while cumulative < .95:
            quantile += 1
            term *= mean / quantile
            cumulative += term
        self.assertEqual((row['BF'], row['BG']), (quantile, '通过'))
        self.assertAlmostEqual(row['AL'], quantile - mean)

    def test_only_inbound_and_carton_rounding(self):
        row = model.calculate(dict(YITANG, K=0, L=3, O=3), model.Rules(depth=3, lead=2, probability=.95))
        self.assertAlmostEqual(row['AQ'], 1.5011875)
        self.assertEqual((row['BJ'], row['BQ'], row['AS']), (3, 0, 3))

    def test_invalid_quantile_keeps_target_and_result_pending(self):
        with patch('model.poisson_cdf', return_value=0.5) as cdf:
            row = model.calculate(YITANG, model.Rules(depth=3, lead=2, probability=.95))
        self.assertTrue(cdf.call_args_list)
        self.assertTrue(all(call.args[1] == row['AO'] for call in cdf.call_args_list))
        self.assertEqual((row['BG'], row['AL'], row['AQ'], row['AS']),
                         ('分位计算待复核', model.PENDING, model.PENDING, model.PENDING))
        self.assertIsNone(pre_round_target(row))
