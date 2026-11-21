"""Тесты моделей: IRR, иммунизация, таргет по RVI, Монте-Карло, Келли.

Запуск: python -m unittest tests.test_models -v
Сеть не используется: входные данные — словари позиций.
"""

import os
import tempfile
import unittest
from datetime import date

_TMP_DIR = tempfile.mkdtemp(prefix='broker-models-test-')
os.environ.setdefault('BROKER_DB_PATH', os.path.join(_TMP_DIR, 'broker.db'))

from app import models  # noqa: E402


def _bond(qty=1000, price_rub=800.0, face=1000.0, coupon_pct=10.0, freq=2,
          matdate='2031-11-21', duration=5.0, currency='RUB'):
    return {'qty': qty, 'value': qty * price_rub, 'facevalue': face,
            'coupon_pct': coupon_pct, 'coupon_freq': freq, 'matdate': matdate,
            'duration': duration, 'currency': currency,
            'price_pct': price_rub / face * 100}


class XirrTests(unittest.TestCase):
    def test_simple_known_rate(self):
        today = date(2026, 1, 1)
        one_year = date(2027, 1, 1)
        flows = [(today, -1000.0), (one_year, 1100.0)]
        rate = models.xirr(flows, today)
        self.assertAlmostEqual(rate, 0.10, places=2)

    def test_negative_result(self):
        today = date(2026, 1, 1)
        flows = [(today, -1000.0), (date(2027, 1, 1), 900.0)]
        self.assertLess(models.xirr(flows, today), 0)

    def test_empty(self):
        self.assertIsNone(models.xirr([]))


class CouponMachineTests(unittest.TestCase):
    def test_after_tax_below_gross(self):
        rows = [_bond()]
        result = models.coupon_machine(rows, horizon=5)
        self.assertIsNotNone(result['irr_net'])
        self.assertGreater(result['irr_gross'], result['irr_net'])
        self.assertGreater(result['tax_drag'], 0)
        self.assertGreater(result['future'], result['value'])

    def test_high_yield_grows_faster(self):
        low = models.coupon_machine([_bond(coupon_pct=5.0, price_rub=950.0)], 10)
        high = models.coupon_machine([_bond(coupon_pct=15.0, price_rub=700.0)], 10)
        self.assertGreater(high['irr_net'], low['irr_net'])

    def test_no_positions(self):
        result = models.coupon_machine([], 10)
        self.assertIsNone(result['irr_net'])
        self.assertIsNone(result['future'])


class ImmunizationTests(unittest.TestCase):
    def test_gap_computed(self):
        result = models.immunization([_bond(duration=4.5)], horizon=10)
        self.assertAlmostEqual(result['duration'], 4.5, places=2)
        self.assertAlmostEqual(result['gap'], -5.5, places=2)
        self.assertFalse(result['immunized'])
        self.assertIn('удлинить', result['advice'])

    def test_immunized_when_close(self):
        result = models.immunization([_bond(duration=10.0)], horizon=10)
        self.assertTrue(result['immunized'])

    def test_rate_rise_lowers_price(self):
        result = models.immunization([_bond(duration=5.0)], horizon=5)
        up = next(s for s in result['shocks'] if s['dy'] == 2)
        down = next(s for s in result['shocks'] if s['dy'] == -2)
        self.assertLess(up['price_pct'], 0)
        self.assertGreater(down['price_pct'], 0)
        # при D = H итог к горизонту не зависит от сдвига (первый порядок)
        self.assertAlmostEqual(up['total_pct'], 0.0, places=6)
        self.assertAlmostEqual(down['total_pct'], 0.0, places=6)


class VolTargetTests(unittest.TestCase):
    RVI = {'available': True, 'current': 30.0, 'median': 31.0, 'p25': 25.0,
           'p75': 38.0, 'p90': 46.0}

    def test_weight_inverse_to_fear(self):
        calm = models.vol_target({**self.RVI, 'current': 20.0}, sigma_star=10.0, cap=1.0)
        fear = models.vol_target({**self.RVI, 'current': 40.0}, sigma_star=10.0, cap=1.0)
        self.assertGreater(calm['weight'], fear['weight'])
        self.assertAlmostEqual(calm['weight'], 50.0, places=1)

    def test_cap_applied(self):
        result = models.vol_target({**self.RVI, 'current': 10.0}, sigma_star=30.0, cap=0.25)
        self.assertEqual(result['weight'], 25.0)
        self.assertTrue(result['capped'])

    def test_unavailable(self):
        self.assertFalse(models.vol_target({'available': False}, 15)['available'])


class MonteCarloTests(unittest.TestCase):
    def test_percentiles_ordered_and_deterministic(self):
        first = models.monte_carlo(1_000_000, 0.2, 0.14, years=10, paths=1000, seed=7)
        second = models.monte_carlo(1_000_000, 0.2, 0.14, years=10, paths=1000, seed=7)
        self.assertEqual(first['p50'], second['p50'])      # фиксированный seed
        self.assertLess(first['p10'], first['p50'])
        self.assertLess(first['p50'], first['p90'])
        self.assertLess(first['cagr_p10'], first['cagr_p50'])
        self.assertGreaterEqual(first['p_target'], 0.0)

    def test_all_bonds_less_dispersion(self):
        bonds_only = models.monte_carlo(1_000_000, 0.0, 0.14, years=10, paths=1500, seed=3)
        mixed = models.monte_carlo(1_000_000, 0.5, 0.14, years=10, paths=1500, seed=3)
        self.assertLess(bonds_only['p90'] - bonds_only['p10'], mixed['p90'] - mixed['p10'])

    def test_no_capital(self):
        self.assertFalse(models.monte_carlo(0, 0.2, 0.14)['available'])


class KellyTests(unittest.TestCase):
    def test_negative_when_bonds_beat_equities(self):
        result = models.kelly(0.16)
        self.assertFalse(result['positive'])
        self.assertLess(result['full'], 0)

    def test_positive_when_equity_premium(self):
        result = models.kelly(0.05)
        self.assertTrue(result['positive'])
        self.assertGreater(result['full'], 0)
        self.assertAlmostEqual(result['half'], result['full'] / 2, places=1)


if __name__ == '__main__':
    unittest.main()
