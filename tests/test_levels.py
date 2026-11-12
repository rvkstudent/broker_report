"""Тесты уровней: профиль объёма по цене, зоны, вердикт по перевесу."""

import os
import tempfile
import unittest
from datetime import datetime, timezone

_TMP_DIR = tempfile.mkdtemp(prefix='broker-levels-test-')
os.environ['BROKER_DB_PATH'] = os.path.join(_TMP_DIR, 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(_TMP_DIR, 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)

from app import levels, screener  # noqa: E402
from app.db import init_db  # noqa: E402

START = int(datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc).timestamp()) * 10 ** 9
MINUTE = 60 * 10 ** 9  # минута в наносекундах

INSERT_TRADE = '''INSERT OR IGNORE INTO market_trade
    (instrument_uid, ticker, ts_ns, trade_date, trade_time, price, lots, lot_size,
     shares, value, side)
    VALUES ('u-z', 'ZONE', ?, '2026-11-09', '10:00:00.000', ?, 1, 1, 1, ?, ?)'''

INSERT_BAR = '''INSERT OR REPLACE INTO market_bar_1m
    (instrument_uid, ticker, minute_ns, trade_date, trade_time, open, high, low, close,
     vwap, volume, turnover, buy_volume, sell_volume, buy_turnover, sell_turnover,
     trades, large_turnover, large_count)
    VALUES ('u-z', 'ZONE', ?, '2026-11-09', '10:00', ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?, ?, 10, 0, 0)'''


class LevelsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM market_bar_1m')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-z', 'ZONE', 'Зоны', 1, 1e9, 1, 1)")

        rows = []
        # Поддержка на 100.00: много покупок (в т.ч. крупных)
        for i in range(400):
            value = 2_000_000.0 if i < 40 else 50_000.0
            rows.append(('ZONE', START + i * 10 ** 9, 100.0, value, 'buy'))
        # Сопротивление на 110.00: много продаж
        for i in range(400):
            value = 1_500_000.0 if i < 30 else 40_000.0
            rows.append(('ZONE', START + 400 * 10 ** 9 + i * 10 ** 9, 110.0, value, 'sell'))
        # Текущая зона у 105.00 — тонкий рынок
        for i in range(40):
            rows.append(('ZONE', START + 800 * 10 ** 9 + i * 10 ** 9, 105.0, 30_000.0, 'buy'))
        conn.executemany(INSERT_TRADE, [row[1:] for row in rows])

        # Минутки идут в тех же единицах времени (секунды), иначе окно
        # анализа и бары разъезжаются.
        bars = []
        for i in range(30):
            price = 105.0
            bars.append((START + 800 * 10 ** 9 + i * MINUTE, price, price + 0.05,
                         price - 0.05, price, price, price * 1000, 900, 100,
                         price * 900, price * 100))
        conn.executemany(INSERT_BAR, bars)
        conn.commit()
        conn.close()

    def _analyze(self, **kwargs):
        return levels.analyze('ZONE', START, START + 2900 * 10 ** 9, **kwargs)

    def test_profile_has_two_zones(self):
        data = self._analyze()
        self.assertEqual(data['source'], 'trades')
        self.assertGreaterEqual(len(data['zones']), 2)
        prices = [z['price'] for z in data['zones']]
        self.assertTrue(any(abs(p - 100) < 1.5 for p in prices), prices)
        self.assertTrue(any(abs(p - 110) < 1.5 for p in prices), prices)

    def test_zone_side_and_flow(self):
        data = self._analyze()
        support = next(z for z in data['zones'] if abs(z['price'] - 100) < 1.5)
        resistance = next(z for z in data['zones'] if abs(z['price'] - 110) < 1.5)
        self.assertEqual(support['side'], 'support')
        self.assertEqual(resistance['side'], 'resistance')
        self.assertGreater(support['buy_share'], 0.9)      # на поддержке покупали
        self.assertLess(resistance['buy_share'], 0.1)      # на сопротивлении продавали
        self.assertGreater(support['large_flow'], 0.5)     # крупные покупки

    def test_support_and_resistance_brackets_price(self):
        data = self._analyze()
        self.assertIsNotNone(data['support'])
        self.assertIsNotNone(data['resistance'])
        self.assertLess(data['support']['price'], data['price'])
        self.assertGreater(data['resistance']['price'], data['price'])

    def test_verdict_direction_uses_flow(self):
        data = self._analyze()
        verdict = data['verdict']
        self.assertIn(verdict['direction'], ('up', 'down', 'neutral'))
        self.assertIn(verdict['state'], ('in_zone', 'between'))
        self.assertTrue(verdict['title'])
        self.assertGreaterEqual(verdict['confidence'], 0)
        # дельта за 30 минут положительная (900 против 100) → не «вниз»
        self.assertNotEqual(verdict['direction'], 'down')

    def test_profile_payload(self):
        data = self._analyze(buckets=40)
        self.assertEqual(len(data['profile']), 40)
        total = sum(p['total'] for p in data['profile'])
        self.assertGreater(total, 0)
        self.assertTrue(all(p['price'] > 0 for p in data['profile']))

    def test_bars_fallback_when_no_trades(self):
        # окно только с минутками: сделки заканчиваются на 840-й секунде
        data = levels.analyze('ZONE', START + 1500 * 10 ** 9, START + 2500 * 10 ** 9)
        self.assertEqual(data['source'], 'bars')
        self.assertGreater(len(data['zones']), 0)

    def test_empty_period(self):
        with self.assertRaises(levels.LevelsError):
            levels.analyze('ZONE', START + 10 ** 13, START + 2 * 10 ** 13)

    def test_unknown_ticker(self):
        with self.assertRaises(levels.LevelsError):
            levels.analyze('NOPE', START, START + 2900 * 10 ** 9)


if __name__ == '__main__':
    unittest.main()
