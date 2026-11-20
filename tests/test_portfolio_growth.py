"""Тесты доходности портфеля: дата XIRR и стартовый капитал в USD."""

import unittest
from datetime import date
from unittest import mock

from app import portfolio_growth as pg


class XirrTests(unittest.TestCase):
    def test_terminal_day_sets_period(self):
        cash = [(date(2022, 1, 1), -1000.0)]
        # 1000 -> 1210 за два года = ровно 10% годовых.
        self.assertAlmostEqual(pg._xirr(cash, 1210.0, '2024-01-01'), 10.0, delta=0.2)

    def test_later_terminal_day_lowers_rate(self):
        cash = [(date(2022, 1, 1), -1000.0)]
        short = pg._xirr(cash, 1210.0, '2024-01-01')
        long = pg._xirr(cash, 1210.0, '2028-01-01')
        self.assertIsNotNone(long)
        self.assertLess(long, short)


class ValueAsOfTests(unittest.TestCase):
    def test_asof_keeps_old_snapshot_without_expiry(self):
        families = {'old': [('2020-12-31', 100.0)], 'new': [('2023-01-01', 50.0)]}
        # _value_at отбрасывает «просроченный» последний снимок, _value_asof — нет.
        self.assertEqual(pg._value_at(families, '2022-06-01'), 0.0)
        self.assertEqual(pg._value_asof(families, '2022-06-01'), 100.0)


class UsdOpeningCapitalTests(unittest.TestCase):
    """Стоимость портфеля на начало валютного окна должна входить в XIRR."""

    def setUp(self):
        pg._cache.clear()
        self.snaps = [
            {'filename': 'open_2020.pdf', 'snapshot_date': '2020-12-31',
             'assets': 591606.0, 'cash': 0.0},
            {'filename': 'vtb_2022.xlsx', 'snapshot_date': '2022-12-31',
             'assets': 900000.0, 'cash': 0.0},
            {'filename': 'vtb_2026.xlsx', 'snapshot_date': '2026-09-30',
             'assets': 3433280.37, 'cash': 0.0},
        ]
        self.flows = [
            {'date': '2022-03-01', 'amount': 300000.0, 'filename': 'vtb_2022.xlsx'},
            {'date': '2023-05-10', 'amount': 200000.0, 'filename': 'vtb_2022.xlsx'},
            {'date': '2025-01-15', 'amount': -150000.0, 'filename': 'vtb_2026.xlsx'},
        ]
        self.rates = {
            '2022-01-03': 75.0, '2022-03-01': 110.0, '2023-05-10': 78.0,
            '2025-01-15': 102.0, '2026-09-30': 83.0,
        }

    def _run(self):
        with mock.patch.object(pg.report_snapshots, 'all_snapshots', return_value=list(self.snaps)), \
             mock.patch.object(pg.report_snapshots, 'all_flows',
                               side_effect=lambda: [dict(f) for f in self.flows]), \
             mock.patch.object(pg.report_snapshots, 'all_income', return_value=[]), \
             mock.patch('app.db.get_manual_flows', return_value=[]), \
             mock.patch('app.db.get_trade_lots', return_value=[]), \
             mock.patch.object(pg, '_daily_series', lambda *a, **k: dict(self.rates)):
            return pg.invested_summary()

    def test_opening_capital_is_counted(self):
        inv = self._run()
        self.assertAlmostEqual(inv['usd_start_value'], round(591606.0 / 75.0, 2), places=2)
        self.assertIsNotNone(inv['usd_xirr'])
        self.assertIsNotNone(inv['usd_profit'])

    def test_opening_capital_lowers_xirr(self):
        inv = self._run()
        cash = [(date.fromisoformat(f['date']), -f['amount'] / self.rates[f['date']])
                for f in self.flows]
        without_opening = pg._xirr(cash, inv['usd_value'], inv['as_of'])
        self.assertLess(inv['usd_xirr'], without_opening)


class CurrentPortfolioSummaryTests(unittest.TestCase):
    """Структура текущего портфеля, доходность по выплатам и скользящая XIRR."""

    def setUp(self):
        pg._cache.clear()

    def _patched(self, positions, prices, snaps, income, flows=([], 0.0)):
        return [
            mock.patch('app.db.get_instrument_summary', return_value=positions),
            mock.patch('app.db.get_current_prices', return_value=prices),
            mock.patch.object(pg.report_snapshots, 'all_snapshots', return_value=snaps),
            mock.patch.object(pg.report_snapshots, 'all_income', return_value=income),
            mock.patch.object(pg, 'external_flows', return_value=flows),
        ]

    def test_structure_income_and_rolling(self):
        inv = {'value': 1_000_000.0, 'as_of': '2026-11-19'}
        positions = [
            {'security_code': 'SU26243', 'security_name': 'ОФЗ 26243',
             'qty': 1000, 'avg_price': 600.0},
            {'security_code': 'SBER', 'security_name': 'Сбербанк',
             'qty': 100, 'avg_price': 300.0},
        ]
        prices = [
            {'sec_code': 'SU26243', 'class_code': 'TQOB', 'price': 673.69},
            {'sec_code': 'SBER', 'class_code': 'TQBR', 'price': 320.0},
        ]
        snaps = [
            {'filename': 'vtb_2025.xlsx', 'snapshot_date': '2025-11-19', 'assets': 800000.0, 'cash': 0.0},
            {'filename': 'vtb_2026.xlsx', 'snapshot_date': '2026-11-19', 'assets': 1000000.0, 'cash': 0.0},
        ]
        income = [{'date': '2026-03-01', 'amount': 50000.0, 'kind': 'income'}]
        patches = self._patched(positions, prices, snaps, income)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            out = pg.current_portfolio_summary(inv)

        cats = {r['label']: r['value'] for r in out['structure']}
        self.assertAlmostEqual(cats['ОФЗ'], 673690.0, places=0)
        self.assertAlmostEqual(cats['Акции'], 32000.0, places=0)
        self.assertIn('Кэш', cats)               # остаток до стоимости портфеля
        self.assertAlmostEqual(out['cash_value'], 294310.0, places=0)
        self.assertAlmostEqual(out['income_12m'], 50000.0, places=2)
        self.assertAlmostEqual(out['yield_total_pct'], 5.0, places=2)
        self.assertIsNotNone(out['rolling_xirr_12m'])
        self.assertGreater(out['rolling_xirr_12m'], 0)

    def test_rolling_none_without_start_snapshot(self):
        inv = {'value': 1000.0, 'as_of': '2026-11-19'}
        patches = self._patched([], [], [], [])
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            out = pg.current_portfolio_summary(inv)
        self.assertIsNone(out['rolling_xirr_12m'])
        self.assertIsNone(out['rolling_xirr_36m'])
        self.assertEqual(out['income_12m'], 0.0)
        # без позиций остаток до стоимости портфеля весь уходит в «Кэш»
        self.assertEqual([r['label'] for r in out['structure']], ['Кэш'])


    def test_rolling_ignores_stale_closed_accounts(self):
        """Старт окна — ближайший снимок по активным счетам, без закрытых."""
        inv = {'value': 3582996.07, 'as_of': '2026-11-19'}
        snaps = [
            {'filename': 'old_2020.pdf', 'snapshot_date': '2020-11-08',
             'assets': 591605.70, 'cash': 0.0},     # давно закрытый счёт
            {'filename': 'vtb_2024.xlsx', 'snapshot_date': '2024-12-31',
             'assets': 2600583.60, 'cash': 0.0},
            {'filename': 'vtb_2025.xlsx', 'snapshot_date': '2025-12-31',
             'assets': 3311948.99, 'cash': 0.0},
        ]
        flows = [{'date': '2026-03-01', 'amount': 100000.0, 'filename': 'vtb_2025.xlsx'}]
        patches = self._patched([], [], snaps, [], flows=(flows, 0.0))
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            out = pg.current_portfolio_summary(inv)
        # Старт 12-мес окна = 2025-12-31 (3.31М), а НЕ 3.31М + 591k закрытого счёта.
        # Если бы старый счёт прибавлялся, XIRR ушла бы в минус; проверяем, что нет.
        self.assertIsNotNone(out['rolling_xirr_12m'])
        self.assertGreater(out['rolling_xirr_12m'], 0)


if __name__ == '__main__':
    unittest.main()
