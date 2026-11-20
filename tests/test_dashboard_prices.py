"""Тесты выбора инструмента T-Invest для цен дашборда."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from app import dashboard_prices


class FakeClient:
    def __init__(self, instruments):
        self._instruments = instruments

    def find_instrument(self, query):
        return list(self._instruments)


class CandleClient:
    """Клиент со «свечами»: нужен только метод candles()."""

    def __init__(self, candles):
        self._candles = candles

    def candles(self, instrument_id, from_dt, to_dt, interval='CANDLE_INTERVAL_DAY'):
        return list(self._candles)


def ns(dt):
    return int(dt.timestamp() * 1_000_000_000)


def candle(day, close):
    return {'ts_ns': ns(day), 'open': close, 'high': close, 'low': close,
            'close': close, 'volume_lots': 1, 'is_complete': True}


def inst(ticker, class_code, uid=None):
    return {'ticker': ticker, 'classCode': class_code, 'uid': uid or f'{ticker}-{class_code}'}


class FindInstrumentTests(unittest.TestCase):
    def setUp(self):
        dashboard_prices._uid_cache.clear()

    def tearDown(self):
        dashboard_prices._uid_cache.clear()

    def test_prefers_moex_class_when_class_unknown(self):
        """BELU ищется без класса: берём TQBR, а не первую запись (PTEQ)."""
        client = FakeClient([inst('BELU', 'PTEQ'), inst('BELU', 'TQBR')])
        found = dashboard_prices._find(client, 'BELU', '')
        self.assertEqual(found['classCode'], 'TQBR')

    def test_exact_class_match_wins(self):
        client = FakeClient([inst('BELU', 'PTEQ'), inst('BELU', 'TQBR')])
        found = dashboard_prices._find(client, 'BELU', 'TQBR')
        self.assertEqual(found['classCode'], 'TQBR')

    def test_no_foreign_listing_when_class_known(self):
        """Класса TQBR нет — цену чужой листинговой записи не подставляем."""
        client = FakeClient([inst('BELU', 'PTEQ')])
        self.assertIsNone(dashboard_prices._find(client, 'BELU', 'TQBR'))

    def test_no_foreign_listing_when_class_unknown(self):
        client = FakeClient([inst('BELU', 'PTEQ')])
        self.assertIsNone(dashboard_prices._find(client, 'BELU', ''))

    def test_ignores_unrelated_search_results(self):
        """FindInstrument — поиск: чужие тикеры в выдаче не должны попадать в цену."""
        client = FakeClient([inst('BELU', 'TQBR'), inst('BELU2', 'TQBR')])
        self.assertIsNone(dashboard_prices._find(client, 'BELUX', ''))

    def test_result_is_cached(self):
        client = FakeClient([inst('SBER', 'TQBR')])
        first = dashboard_prices._find(client, 'SBER', '')
        second = dashboard_prices._find(FakeClient([]), 'SBER', '')
        self.assertIs(first, second)


class PrevCloseTests(unittest.TestCase):
    """Закрытие предыдущего дня для процента изменения цены."""

    def test_uses_last_candle_before_today(self):
        """Текущая (незакрытая) дневная свеча не должна становиться базой."""
        now = datetime.now(timezone.utc)
        client = CandleClient([
            candle(now - timedelta(days=3), 280.0),
            candle(now - timedelta(days=1), 300.0),
            candle(now, 320.0),
        ])
        self.assertEqual(dashboard_prices._prev_close(client, {'uid': 'u', 'instrumentType': 'share'}), 300.0)

    def test_no_candles_before_today(self):
        """История начинается с сегодняшнего дня — базы для сравнения нет."""
        client = CandleClient([candle(datetime.now(timezone.utc), 320.0)])
        self.assertIsNone(dashboard_prices._prev_close(client, {'uid': 'u', 'instrumentType': 'share'}))

    def test_no_candles_at_all(self):
        self.assertIsNone(dashboard_prices._prev_close(CandleClient([]), {'uid': 'u'}))

    def test_candle_day_is_moscow_date(self):
        """23:30 UTC — это уже следующий день по Москве."""
        late_utc = datetime(2026, 11, 18, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(dashboard_prices._candle_day(ns(late_utc)), '2026-11-19')


class AccruedInterestTests(unittest.TestCase):
    """НКД из карточки T-Invest — для оценки стоимости «с НКД»."""

    def setUp(self):
        dashboard_prices._aci_cache.clear()

    def tearDown(self):
        dashboard_prices._aci_cache.clear()

    def test_bond_aci_returned(self):
        detail = {'aci': {'units': 12, 'nano': 340000000, 'currency': 'rub'}}
        with mock.patch.object(dashboard_prices, 'enabled', return_value=True), \
             mock.patch.object(dashboard_prices, '_get_client', return_value=object()), \
             mock.patch.object(dashboard_prices, '_find',
                               lambda c, code, cc: {'instrumentType': 'bond', 'uid': code}), \
             mock.patch.object(dashboard_prices, '_details', lambda c, ins: detail):
            out = dashboard_prices.accrued_interests({'SU26243': ''})
        self.assertAlmostEqual(out['SU26243'], 12.34, places=2)

    def test_non_bond_skipped(self):
        with mock.patch.object(dashboard_prices, 'enabled', return_value=True), \
             mock.patch.object(dashboard_prices, '_get_client', return_value=object()), \
             mock.patch.object(dashboard_prices, '_find',
                               lambda c, code, cc: {'instrumentType': 'share', 'uid': code}):
            out = dashboard_prices.accrued_interests({'SBER': 'TQBR'})
        self.assertEqual(out, {})

    def test_disabled_returns_empty(self):
        with mock.patch.object(dashboard_prices, 'enabled', return_value=False):
            self.assertEqual(dashboard_prices.accrued_interests({'SU26243': ''}), {})


if __name__ == '__main__':
    unittest.main()
