"""Тесты скринера инструментов: нормализация, скоринг, подсветка кандидатов.

Запуск: python -m unittest tests.test_instruments -v
Сеть не используется: строки ISS подменяются словарями тех же полей.
"""

import os
import tempfile
import unittest
from datetime import date, timedelta

_TMP_DIR = tempfile.mkdtemp(prefix='broker-instruments-test-')
os.environ.setdefault('BROKER_DB_PATH', os.path.join(_TMP_DIR, 'broker.db'))
os.environ.pop('TINKOFF_TOKEN', None)

from app import instruments  # noqa: E402


def _iss_bond(**kw):
    """Строка доски ISS: securities + marketdata в '_md'."""
    md = {'YIELD': 17.0, 'DURATION': 2000, 'LAST': 80.0, 'VALTODAY': 5_000_000,
          'NUMTRADES': 50}
    md.update(kw.pop('md', {}))
    row = {'SECID': 'RU000TEST01', 'SHORTNAME': 'Тест 01', 'MATDATE': '2035-01-01',
           'COUPONPERCENT': 12.0, 'COUPONPERIOD': 182, 'FACEVALUE': 1000.0,
           'FACEUNIT': 'SUR', 'LISTLEVEL': 1, 'ISSUESIZE': 1_000_000,
           'ACCRUEDINT': 20.0, 'ISIN': 'RU000TEST01', '_md': md}
    row.update(kw)
    return row


class BondRowTests(unittest.TestCase):
    def test_parses_main_fields(self):
        row = instruments._bond_row(_iss_bond(), 'TQOB')
        self.assertEqual(row['type'], 'ОФЗ')
        self.assertEqual(row['ytm'], 17.0)
        self.assertAlmostEqual(row['duration'], 2000 / 365.25, places=2)
        self.assertEqual(row['coupon_freq'], 2)      # 365 / 182
        self.assertEqual(row['matdate'], '2035-01-01')
        self.assertIsNotNone(row['years'])

    def test_skips_bond_without_yield(self):
        self.assertIsNone(instruments._bond_row(_iss_bond(md={'YIELD': None}), 'TQOB'))

    def test_skips_illiquid(self):
        self.assertIsNone(instruments._bond_row(
            _iss_bond(md={'VALTODAY': 100_000}), 'TQOB'))

    def test_skips_implausible_yield(self):
        # 70% на рублёвом рынке — артефакт расчёта ISS, а не доходность
        self.assertIsNone(instruments._bond_row(_iss_bond(md={'YIELD': 70.0}), 'TQOB'))

    def test_currency_type(self):
        self.assertEqual(instruments._bond_type(_iss_bond(FACEUNIT='USD')), 'Валютная')

    def test_corporate_type(self):
        self.assertEqual(
            instruments._bond_type({'SHORTNAME': 'Сбер Sb56R', 'FACEUNIT': 'SUR'}),
            'Корпоративная')

    def test_offer_date_detected(self):
        row = instruments._bond_row(_iss_bond(PUTOPTIONDATE='2028-08-16'), 'TQCB')
        self.assertIn('2028-08-16', row['offer'])


class ScoringTests(unittest.TestCase):
    def _bond(self, **kw):
        base = {'ytm': 17.0, 'duration': 5.0, 'turnover': 20_000_000,
                'listlevel': 1, 'type': 'ОФЗ', 'currency': 'RUB',
                'coupon_pct': 10.0, 'spread': None, 'risky': False}
        base.update(kw)
        return base

    def test_higher_yield_scores_higher(self):
        low = instruments._score_bond(self._bond(ytm=14.0))
        high = instruments._score_bond(self._bond(ytm=20.0))
        self.assertGreater(high, low)

    def test_risky_bond_penalized(self):
        clean = instruments._score_bond(self._bond())
        risky = instruments._score_bond(self._bond(risky=True))
        self.assertLess(risky, clean)

    def test_bond_without_duration_scores_zero(self):
        self.assertEqual(instruments._score_bond(self._bond(duration=None)), 0.0)

    def test_ofz_spread_computed_against_curve(self):
        # Кривая ОФЗ: 2 года → 15%, 6 лет → 17%; корпорат с дюрацией 4 и YTM 18%
        # должен получить спред +1 п.п. относительно 16% в этой точке.
        bonds = [
            {'type': 'ОФЗ', 'duration': 2.0, 'ytm': 15.0, 'currency': 'RUB',
             'turnover': 9e6, 'listlevel': 1, 'coupon_pct': 7.0},
            {'type': 'ОФЗ', 'duration': 6.0, 'ytm': 17.0, 'currency': 'RUB',
             'turnover': 9e6, 'listlevel': 1, 'coupon_pct': 7.0},
            {'type': 'Корпоративная', 'duration': 4.0, 'ytm': 18.0, 'currency': 'RUB',
             'turnover': 9e6, 'listlevel': 1, 'coupon_pct': 13.0},
        ]
        instruments.add_risk_flags(bonds)
        self.assertAlmostEqual(bonds[2]['spread'], 2.0, delta=0.01)

    def test_stock_scoring_uses_dividends(self):
        rich = instruments._score_stock({'div12': 14.0, 'div3y': 11.0, 'pe': 4.0,
                                         'turnover': 200e6, 'years_paid': 6})
        poor = instruments._score_stock({'div12': 1.0, 'div3y': 0.5, 'pe': 30.0,
                                         'turnover': 200e6, 'years_paid': 1})
        self.assertGreater(rich, poor)

    def test_stock_without_data_scores_low(self):
        empty = instruments._score_stock({'div12': None, 'div3y': None, 'pe': None,
                                          'turnover': 5e6, 'years_paid': 0})
        self.assertLess(empty, 20)


class QualityGuardTests(unittest.TestCase):
    def test_anomalous_dividend_capped_in_score(self):
        base = {'div3y': 12.0, 'pe': 5.0, 'turnover': 100e6, 'years_paid': 5}
        normal = instruments._score_stock({**base, 'div12': 15.0})
        anomaly = instruments._score_stock({**base, 'div12': 190.0})
        capped = instruments._score_stock({**base, 'div12': instruments.DIV_FOR_SCORE})
        self.assertLess(anomaly, capped)          # штраф за аномалию
        self.assertLess(anomaly, normal)           # стабильные 15% ценнее разовых 190%
        self.assertGreater(anomaly, 70)            # но бумага всё равно заметная

    def test_anomaly_flagged(self):
        flags = instruments._stock_flags({'div12': 190.0, 'pe': 2.0, 'roe': 10.0})
        self.assertTrue(any('аномальная ДД' in f for f in flags))

    def test_roe_outlier_dropped(self):
        self.assertIsNone(instruments._clean_roe(-1987.2))
        self.assertEqual(instruments._clean_roe(22.88), 22.9)


class ReliabilityTests(unittest.TestCase):
    def _bond(self, **kw):
        base = {'type': 'ОФЗ', 'currency': 'RUB', 'spread': None, 'listlevel': 1,
                'turnover': 50e6, 'issuesize': 500e6, 'offer': '', 'coupon_pct': 9.5}
        base.update(kw)
        return base

    def test_ofz_is_most_reliable(self):
        ofz = instruments.reliability_bond(self._bond())
        corp = instruments.reliability_bond(self._bond(type='Корпоративная', spread=3.0))
        self.assertGreater(ofz['score'], corp['score'])
        self.assertEqual(ofz['letter'], 'A')

    def test_wide_spread_lowers_rating(self):
        tight = instruments.reliability_bond(self._bond(type='Корпоративная', spread=1.0))
        wide = instruments.reliability_bond(self._bond(type='Корпоративная', spread=11.0))
        self.assertLess(wide['score'], tight['score'])

    def test_third_level_and_high_coupon_penalized(self):
        clean = instruments.reliability_bond(self._bond(type='Корпоративная'))
        bad = instruments.reliability_bond(self._bond(type='Корпоративная',
                                                       listlevel=3, coupon_pct=25.0))
        self.assertLess(bad['score'], clean['score'])

    def test_score_bounds_and_reasons(self):
        for row in (self._bond(), self._bond(type='Корпоративная', spread=50.0,
                                             listlevel=3, coupon_pct=40.0,
                                             turnover=0, issuesize=0)):
            rel = instruments.reliability_bond(row)
            self.assertGreaterEqual(rel['score'], 1.0)
            self.assertLessEqual(rel['score'], 10.0)
            self.assertIn(rel['letter'], ('A', 'B', 'C', 'D'))
            self.assertTrue(rel['reasons'])

    def test_stock_reliability_prefers_healthy(self):
        healthy = instruments.reliability_stock({
            'years_paid': 6, 'roe': 25.0, 'pe': 8.0, 'pb': 1.2,
            'listlevel': 1, 'turnover': 500e6})
        weak = instruments.reliability_stock({
            'years_paid': 0, 'roe': -5.0, 'pe': 60.0, 'pb': 8.0,
            'listlevel': 3, 'turnover': 5e6})
        self.assertGreater(healthy['score'], weak['score'])
        self.assertGreaterEqual(healthy['score'] - weak['score'], 2.0)


class DividendStatsTests(unittest.TestCase):
    def test_dividends_12m_and_3y(self):
        today = date.today()
        divs = [
            {'value': 10.0, 'pay': (today - timedelta(days=180)).isoformat()},
            {'value': 5.0, 'pay': (today - timedelta(days=420)).isoformat()},
            {'value': 7.0, 'pay': (today - timedelta(days=800)).isoformat()},
        ]
        stats = instruments._div_stats(divs, price=100.0)
        self.assertAlmostEqual(stats['div12'], 10.0)          # только последняя
        self.assertAlmostEqual(stats['div3y'], (10 + 5 + 7) / 3, places=2)  # средняя за 3 года
        self.assertEqual(stats['years_paid'], 3)
        self.assertEqual(stats['last_div'], 10.0)

    def test_empty_history(self):
        stats = instruments._div_stats([], price=100.0)
        self.assertIsNone(stats['div12'])
        self.assertEqual(stats['years_paid'], 0)


class CandidatesTests(unittest.TestCase):
    def test_marks_top_n_and_sorts(self):
        bonds = [{'ytm': y, 'duration': 3.0, 'turnover': 10e6, 'listlevel': 1,
                  'type': 'ОФЗ', 'currency': 'RUB', 'coupon_pct': 10.0,
                  'risky': False, 'spread': None} for y in (12, 14, 16)]
        stocks = [{'div12': d, 'div3y': d, 'pe': 5.0, 'turnover': 50e6,
                   'years_paid': 5} for d in (3, 9)]
        instruments.mark_candidates(bonds, stocks)

        self.assertTrue(bonds[0]['candidate'])
        self.assertGreater(bonds[0]['score'], bonds[-1]['score'])
        self.assertEqual(sum(1 for b in bonds if b['candidate']), len(bonds))
        self.assertEqual(sum(1 for s in stocks if s['candidate']), len(stocks))
        self.assertIn('YTM', bonds[0]['reason'])

    def test_fx_bonds_not_ranked(self):
        fx = [{'ytm': 8.0, 'duration': 1.0, 'turnover': 9e6, 'listlevel': 1,
               'type': 'Валютная', 'currency': 'USD', 'coupon_pct': 7.0,
               'risky': False, 'spread': None}]
        instruments.mark_candidates([], [], fx)
        self.assertIsNone(fx[0]['score'])
        self.assertFalse(fx[0]['candidate'])


if __name__ == '__main__':
    unittest.main()
