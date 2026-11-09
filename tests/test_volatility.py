"""Тесты индикатора волатильности: ATR, σ, размах, приведение к дню."""

import math
import os
import tempfile
import unittest
from datetime import datetime, timezone

_TMP_DIR = tempfile.mkdtemp(prefix='broker-vol-test-')
os.environ['BROKER_DB_PATH'] = os.path.join(_TMP_DIR, 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(_TMP_DIR, 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)

from app import screener, volatility  # noqa: E402
from app.db import init_db  # noqa: E402

DAY_START_NS = int(datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc).timestamp()) * 10 ** 9

INSERT_BAR = '''INSERT OR REPLACE INTO market_bar_1m
    (instrument_uid, ticker, minute_ns, trade_date, trade_time, open, high, low, close,
     vwap, volume, turnover, buy_volume, sell_volume, buy_turnover, sell_turnover,
     trades, large_turnover, large_count)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''


def _bar(uid, ticker, minute, open_, high, low, close, volume=1000):
    ns = DAY_START_NS + minute * 60 * 10 ** 9
    return (uid, ticker, ns, '2026-11-09', f'{minute // 60 + 10:02d}:{minute % 60:02d}',
            open_, high, low, close, close, volume, close * volume,
            volume // 2, volume // 2, close * volume / 2, close * volume / 2, 10, 0, 0)


class VolatilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_bar_1m')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-vol', 'VOL', 'Волатильный', 1, 1e9, 1, 1)")
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-cal', 'CALM', 'Спокойный', 1, 1e9, 2, 1)")
        # Размах каждого бара 2 ₽ при цене ~100 → ATR = 2 ₽ = 2 %
        vol_bars = [
            _bar('u-vol', 'VOL', 0, 100, 101, 99, 100),
            _bar('u-vol', 'VOL', 1, 100, 102, 100, 101),
            _bar('u-vol', 'VOL', 2, 101, 103, 101, 102),
        ]
        # Размах 0.1 ₽ по той же траектории → ATR в 20 раз меньше
        calm_bars = [
            _bar('u-cal', 'CALM', 0, 100, 100.05, 99.95, 100),
            _bar('u-cal', 'CALM', 1, 100, 100.15, 100.05, 100.1),
            _bar('u-cal', 'CALM', 2, 100.1, 100.25, 100.15, 100.2),
        ]
        conn.executemany(INSERT_BAR, vol_bars + calm_bars)
        conn.commit()
        conn.close()

    def test_atr_absolute_and_percent(self):
        m = volatility.metrics('VOL', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        # TR: 2, max(2, |102-100|, |100-100|)=2, max(2, |103-101|, |101-101|)=2
        self.assertAlmostEqual(m['atr_rub'], 2.0, places=4)
        self.assertAlmostEqual(m['atr_pct'], 2 / 102 * 100, places=3)
        self.assertEqual(m['bars'], 3)

    def test_sigma_matches_manual(self):
        m = volatility.metrics('VOL', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        rets = [math.log(101 / 100), math.log(102 / 101)]
        mean = sum(rets) / 2
        expected = math.sqrt(sum((r - mean) ** 2 for r in rets) / 2) * 100
        self.assertAlmostEqual(m['sigma_bar_pct'], expected, places=3)
        # приведение к часу/дню — по корню из времени
        self.assertAlmostEqual(m['sigma_hour_pct'], m['sigma_bar_pct'] * math.sqrt(60), places=2)
        self.assertAlmostEqual(m['sigma_day_pct'],
                               m['sigma_bar_pct'] * math.sqrt(840), places=2)

    def test_range_and_absolute(self):
        m = volatility.metrics('VOL', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        self.assertAlmostEqual(m['range_rub'], 4.0, places=4)      # 103 − 99
        self.assertAlmostEqual(m['range_pct'], 4 / 99 * 100, places=2)
        self.assertGreater(m['sigma_day_rub'], 0)

    def test_relative_comparison(self):
        vol = volatility.metrics('VOL', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        calm = volatility.metrics('CALM', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        # проценты информативнее рублей: у спокойной бумаги ATR в 20 раз меньше
        self.assertGreater(vol['atr_rub'], calm['atr_rub'] * 5)
        self.assertGreater(vol['sigma_day_pct'], calm['sigma_day_pct'])

    def test_universe_returns_all(self):
        result = volatility.universe(DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)
        self.assertEqual(set(result), {'VOL', 'CALM'})
        self.assertGreater(result['VOL']['sigma_day_pct'], 0)
        self.assertGreater(result['VOL']['atr_pct'], result['CALM']['atr_pct'])

    def test_empty_range(self):
        m = volatility.metrics('VOL', DAY_START_NS + 10 ** 12, DAY_START_NS + 2 * 10 ** 12)
        self.assertEqual(m['bars'], 0)
        self.assertEqual(m['atr_pct'], 0.0)

    def test_unknown_ticker(self):
        from app import chartdata
        with self.assertRaises(chartdata.ChartError):
            volatility.metrics('NOPE', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9)


if __name__ == '__main__':
    unittest.main()
