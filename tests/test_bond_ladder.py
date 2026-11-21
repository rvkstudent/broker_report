"""Тесты лестницы погашений: расчёт YTM/дюрации и сводка по портфелю.

Запуск: python -m unittest tests.test_bond_ladder -v
Сеть не используется: ISS-справочник подменяется готовыми словарями.
"""

import os
import tempfile
import unittest
from datetime import date

_TMP_DIR = tempfile.mkdtemp(prefix='broker-bond-ladder-test-')
os.environ.setdefault('BROKER_DB_PATH', os.path.join(_TMP_DIR, 'broker.db'))

from app import bond_ladder  # noqa: E402

AS_OF = date(2026, 11, 20)


def _ref(**kwargs):
    base = {
        'kind': 'bond', 'isin': '', 'currency': 'RUB', 'matdate': '2032-11-17',
        'coupon_pct': 9.5, 'coupon_value': 47.37, 'coupon_freq': 2,
        'facevalue': 1000.0, 'price_pct': 77.279, 'ytm': None, 'duration_days': None,
    }
    base.update(kwargs)
    return base


class YieldMathTests(unittest.TestCase):
    def test_ytm_matches_exchange_for_long_ofz(self):
        # ОФЗ 26241: цена 77,279%, купон 9,5% (2 раза в год), погашение 17.11.2032.
        # Биржевая YTM на 20.11.2026 — 16,05%.
        ytm = bond_ladder.ytm_from_price(77.279, 9.5, 2, '2032-11-17', AS_OF)
        self.assertAlmostEqual(ytm * 100, 16.05, delta=0.15)

    def test_ytm_matches_exchange_for_high_coupon_ofz(self):
        # ОФЗ 26243: цена 66,628%, купон 9,8%, погашение 19.05.2038 — биржа 16,99%.
        ytm = bond_ladder.ytm_from_price(66.628, 9.8, 2, '2038-05-19', AS_OF)
        self.assertAlmostEqual(ytm * 100, 16.99, delta=0.15)

    def test_duration_close_to_exchange_value(self):
        # Биржевая дюрация 26241 — 1547 дней ≈ 4,24 года. Запасной расчёт идёт по
        # сетке от погашения и потому немного отличается от биржевой методики:
        # в карточке используется биржевое значение, здесь проверяем порядок.
        duration = bond_ladder.duration_from_price(77.279, 9.5, 2, '2032-11-17', AS_OF)
        self.assertAlmostEqual(duration, 4.24, delta=0.35)

    def test_ytm_undefined_without_price(self):
        self.assertIsNone(bond_ladder.ytm_from_price(0, 9.5, 2, '2032-11-17', AS_OF))


class BuildRowsTests(unittest.TestCase):
    def test_price_from_market_pipeline_not_iss(self):
        # Цена берётся из конвейера QUIK/T-Invest: 682,97 ₽ за бумагу, а не
        # котировка ISS 66,63% (66,63 × 1000 / 100 = 666,30 ₽) — она с задержкой.
        positions = [{'code': 'SU26243RMFS4', 'name': 'ОФЗ 26243', 'qty': 1530}]
        refs = {'SU26243RMFS4': _ref(matdate='2038-05-19', coupon_pct=9.8,
                                     coupon_value=48.87, coupon_freq=2,
                                     price_pct=66.63, ytm=16.99, duration_days=2090)}
        rows = bond_ladder._build_rows(positions, refs, {'SU26243RMFS4': 682.97},
                                       'quik', AS_OF, usd_rate=None)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['price_src'], 'quik')
        self.assertAlmostEqual(row['value'], 1530 * 682.97, places=2)
        self.assertAlmostEqual(row['price_pct'], 68.297, places=2)
        # Купон: 1530 × 48,87 × 2 = 149 542 ₽
        self.assertAlmostEqual(row['coupon_year'], 1530 * 48.87 * 2, delta=1.0)
        self.assertEqual(row['ytm'], None) if row['ytm'] is None else None
        self.assertAlmostEqual(row['ytm'], 16.6, delta=1.5)   # по фактической цене
        self.assertAlmostEqual(row['duration'], 2090 / 365.25, places=2)

    def test_iss_price_only_as_fallback(self):
        positions = [{'code': 'SU26253RMFS3', 'name': 'ОФЗ 26253', 'qty': 237}]
        refs = {'SU26253RMFS3': _ref(matdate='2038-10-06', coupon_pct=13.0,
                                     coupon_value=64.82, coupon_freq=2,
                                     price_pct=82.67, ytm=17.01, duration_days=2100)}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=None)

        self.assertEqual(rows[0]['price_src'], 'iss')
        self.assertAlmostEqual(rows[0]['value'], 237 * 826.7, delta=5.0)

    def test_currency_bond_converted_with_rate(self):
        positions = [{'code': 'RU000A10C3M0', 'name': 'Атомэнпр06', 'qty': 9}]
        refs = {'RU000A10C3M0': _ref(currency='USD', matdate='2029-07-13',
                                     coupon_pct=7.2, coupon_value=17.95, coupon_freq=4,
                                     facevalue=1000.0, price_pct=96.2548,
                                     ytm=9.09, duration_days=877)}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=80.0)

        self.assertEqual(rows[0]['kind'], 'Валютная')
        # 9 × 96,2548% × 1000 USD × 80 ₽/$ = 693 034 ₽
        self.assertAlmostEqual(rows[0]['value'], 9 * 962.548 * 80, delta=2.0)
        # Купон: 9 × 17,95 USD × 4 × 80 = 51 696 ₽
        self.assertAlmostEqual(rows[0]['coupon_year'], 9 * 17.95 * 4 * 80, delta=2.0)

    def test_ruble_bond_not_multiplied_by_fx_rate(self):
        # Курс USD передаётся для валютного выпуска в портфеле, но к рублёвой
        # бумаге применяться не должен — иначе стоимость вырастает в 80 раз.
        positions = [{'code': 'SU26243RMFS4', 'name': 'ОФЗ 26243', 'qty': 1530}]
        refs = {'SU26243RMFS4': _ref(matdate='2038-05-19', coupon_pct=9.8,
                                     coupon_value=48.87, coupon_freq=2,
                                     price_pct=66.63, duration_days=2090)}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=80.13)

        self.assertAlmostEqual(rows[0]['value'], 1530 * 666.3, delta=5.0)
        self.assertAlmostEqual(rows[0]['coupon_year'], 1530 * 48.87 * 2, delta=1.0)

    def test_currency_bond_skipped_without_rate(self):
        positions = [{'code': 'RU000A10C3M0', 'name': 'Атомэнпр06', 'qty': 9}]
        refs = {'RU000A10C3M0': _ref(currency='USD', matdate='2029-07-13')}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=None)
        self.assertEqual(rows, [])

    def test_same_isin_merged(self):
        # ОФЗ 26254 приходит двумя кодами (26254RMFS и RU000A10D533) — это одна бумага.
        positions = [
            {'code': '26254RMFS', 'name': 'ОФЗ-ПД254', 'qty': 69},
            {'code': 'RU000A10D533', 'name': 'ОФЗ 26254', 'qty': 1},
        ]
        ref = _ref(isin='RU000A10D533', matdate='2040-10-03', coupon_pct=13.0,
                   coupon_value=64.82, coupon_freq=2, price_pct=81.755,
                   ytm=17.03, duration_days=2192)
        refs = {'26254RMFS': ref, 'RU000A10D533': ref}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=None)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['qty'], 70)

    def test_name_taken_from_ref_when_position_name_is_code(self):
        positions = [{'code': 'SU26243RMFS4', 'name': 'SU26243RMFS4', 'qty': 10}]
        refs = {'SU26243RMFS4': _ref(name='ОФЗ 26243', matdate='2038-05-19',
                                     price_pct=66.63, duration_days=2090)}
        rows = bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=None)

        self.assertEqual(rows[0]['name'], 'ОФЗ 26243')
        self.assertEqual(rows[0]['kind'], 'ОФЗ')

    def test_non_bond_skipped(self):
        positions = [{'code': 'MTZ6', 'name': 'MTZ6', 'qty': 8},
                     {'code': 'SBER', 'name': 'Сбербанк', 'qty': 204}]
        refs = {'MTZ6': {'kind': 'other'}, 'SBER': {'kind': 'other'}}
        self.assertEqual(bond_ladder._build_rows(positions, refs, {}, '', AS_OF, None), [])

    def test_bond_detected_by_name_without_iss_ref(self):
        self.assertTrue(bond_ladder._looks_like_bond({'name': 'ОФЗ 26241'}, 'SU26241RMFS8'))
        self.assertFalse(bond_ladder._looks_like_bond({'name': 'МТС'}, 'MTSS'))


class RefLookupTests(unittest.TestCase):
    def test_ref_found_by_isin_secid_or_quik_code(self):
        ref = _ref(isin='RU000A106E90', matdate='2038-05-19')
        ref['secid'] = 'SU26243RMFS4'
        refs = {'RU000A106E90': ref}          # запись сохранена под ISIN

        self.assertEqual(bond_ladder._ref_for('RU000A106E90', refs)['matdate'], '2038-05-19')
        self.assertEqual(bond_ladder._ref_for('SU26243RMFS4', refs)['matdate'], '2038-05-19')

    def test_ref_found_by_regnumber(self):
        ref = _ref(isin='RU000A10D533', matdate='2040-10-03')
        ref['secid'] = 'SU26254RMFS1'
        refs = {'26254RMFS': ref}             # код QUIK из отчёта

        self.assertEqual(bond_ladder._ref_for('26254RMFS', refs)['matdate'], '2040-10-03')

    def test_unknown_code_returns_empty(self):
        self.assertEqual(bond_ladder._ref_for('XXXX', {'RU000A106E90': _ref()}), {})


class SummarizeTests(unittest.TestCase):
    def _rows(self):
        positions = [
            {'code': 'SU26243RMFS4', 'name': 'ОФЗ 26243', 'qty': 1530},
            {'code': 'SU26241RMFS8', 'name': 'ОФЗ 26241', 'qty': 1081},
            {'code': 'SU26253RMFS3', 'name': 'ОФЗ 26253', 'qty': 237},
        ]
        refs = {
            'SU26243RMFS4': _ref(matdate='2038-05-19', coupon_pct=9.8,
                                 coupon_value=48.87, price_pct=66.63,
                                 ytm=16.99, duration_days=2090),
            'SU26241RMFS8': _ref(matdate='2032-11-17', coupon_pct=9.5,
                                 coupon_value=47.37, price_pct=77.279,
                                 ytm=16.05, duration_days=1547),
            'SU26253RMFS3': _ref(matdate='2038-10-06', coupon_pct=13.0,
                                 coupon_value=64.82, price_pct=82.67,
                                 ytm=17.01, duration_days=2100),
        }
        return bond_ladder._build_rows(positions, refs, {}, '', AS_OF, usd_rate=None)

    def test_weighted_metrics(self):
        data = bond_ladder.summarize(self._rows(), total=3_000_000)
        total = 1530 * 666.3 + 1081 * 772.79 + 237 * 826.7   # цена = price_pct × 10

        self.assertAlmostEqual(data['bond_value'], total, places=2)
        self.assertAlmostEqual(data['bonds_pct'], total / 3_000_000 * 100, places=1)
        ytm = (1530 * 666.3 * 16.99 + 1081 * 772.79 * 16.05 + 237 * 826.7 * 17.01) / total
        self.assertAlmostEqual(data['ytm_avg'], ytm, delta=0.02)
        self.assertAlmostEqual(
            data['dv01'],
            (1530 * 666.3 * 2090 + 1081 * 772.79 * 1547 + 237 * 826.7 * 2100) / 365.25 / 100,
            delta=50.0)  # дюрация в строках округлена до сотых
        self.assertAlmostEqual(data['pct_sum'] if 'pct_sum' in data
                               else sum(r['pct'] for r in data['rows']), 100.0, delta=0.2)

    def test_ladder_buckets_by_maturity_year(self):
        data = bond_ladder.summarize(self._rows(), total=None)
        ladder = {b['year']: b for b in data['ladder']}

        self.assertEqual(set(ladder), {'2032', '2038'})
        self.assertAlmostEqual(ladder['2038']['value'], 1530 * 666.3 + 237 * 826.7, places=2)
        self.assertEqual(data['next_maturity'], '2032-11-17')

    def test_concentration_warning(self):
        data = bond_ladder.summarize(self._rows(), total=None)
        # 2038 год: ~73% облигационной части — это выше порога 30%.
        self.assertTrue(data['warn'])
        self.assertEqual(data['max_year']['year'], '2038')

    def test_coupon_flow_and_tax(self):
        data = bond_ladder.summarize(self._rows(), total=None)
        expected = sum(r['coupon_year'] for r in data['rows'])
        self.assertAlmostEqual(data['coupon_year'], expected, delta=1.0)
        self.assertAlmostEqual(data['coupon_year_net'], expected * 0.87, delta=1.0)
        self.assertAlmostEqual(data['coupon_month'], expected / 12, delta=0.5)


if __name__ == '__main__':
    unittest.main()
