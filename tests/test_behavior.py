"""Тесты поведенческих факторов: накопление, касания, исходы, сигнал."""

import os
import tempfile
import unittest
from datetime import datetime, timezone

_TMP_DIR = tempfile.mkdtemp(prefix='broker-behavior-test-')
os.environ['BROKER_DB_PATH'] = os.path.join(_TMP_DIR, 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(_TMP_DIR, 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)

from app import behavior, screener  # noqa: E402
from app.db import init_db  # noqa: E402

DAY_NS = 86400 * 10 ** 9
START = int(datetime(2026, 11, 6, 7, 0, tzinfo=timezone.utc).timestamp()) * 10 ** 9
MINUTE = 60 * 10 ** 9


def _bars(prices, *, buy_ratio=0.6, volume=1000, start=START, step_min=1):
    """Последовательность баров из списка цен закрытия."""
    bars = []
    for i, price in enumerate(prices):
        buy = int(volume * buy_ratio)
        bars.append({
            'minute_ns': start + i * step_min * MINUTE,
            'open': price, 'high': price * 1.0005, 'low': price * 0.9995, 'close': price,
            'volume': volume, 'turnover': price * volume,
            'buy_volume': buy, 'sell_volume': volume - buy,
            'large_buy_turnover': price * buy * 100 if buy_ratio > 0.5 else 0,
            'large_sell_turnover': price * (volume - buy) * 100 if buy_ratio < 0.5 else 0,
        })
    return bars


class TouchEventTests(unittest.TestCase):
    zone = {'low': 99.5, 'high': 100.5, 'price': 100.0, 'side': 'support'}

    def test_three_touches_two_held(self):
        # три подхода к зоне: два отскока и одно падение ниже
        prices = ([105, 104, 103, 102, 101, 100.2, 100.1, 100.3, 101, 102, 104, 105]
                  + [105, 103, 101, 100.2, 100.0, 100.4, 101, 103, 105]
                  + [105, 103, 101, 100.2, 99.0, 98.5, 98.0, 97.5])
        events = behavior.touch_events(_bars(prices), self.zone)
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual(events[0]['outcome'], 'held')
        self.assertEqual(events[1]['outcome'], 'held')
        self.assertIn(events[2]['outcome'], ('break_down', 'pierce_down'))

    def test_flow_inside_event(self):
        prices = [105, 103, 101, 100.2, 100.1, 101, 103, 105]
        events = behavior.touch_events(_bars(prices, buy_ratio=0.9), self.zone)
        self.assertTrue(events)
        self.assertGreater(events[0]['flow'], 0.5)
        self.assertGreater(events[0]['large_flow'], 0.5)

    def test_no_touch_when_far(self):
        events = behavior.touch_events(_bars([120, 121, 122]), self.zone)
        self.assertEqual(events, [])


class AttachHistoryTests(unittest.TestCase):
    def test_hold_rate_and_behavior(self):
        prices = ([105, 103, 101, 100.2, 100.1, 101, 103, 105]
                  + [105, 103, 101, 100.3, 100.2, 100.4, 101, 104]
                  + [105, 103, 101, 100.2, 100.1, 100.5, 102, 105])
        zones = [{'low': 99.5, 'high': 100.5, 'price': 100.0, 'side': 'support',
                  'volume_rub': 1e8, 'buy_share': 0.7, 'flow': 0.4,
                  'large_flow': 0.5, 'large_rub': 1e7, 'trades': 100,
                  'distance_pct': -0.5}]
        behavior.attach_history(zones, _bars(prices))
        zone = zones[0]
        self.assertEqual(zone['touches'], 3)
        self.assertEqual(zone['held'], 3)
        self.assertEqual(zone['hold_rate'], 1.0)
        self.assertIsNotNone(zone['behavior'])
        self.assertEqual(zone['behavior']['kind'], 'absorption')


class SignalTests(unittest.TestCase):
    def _zone(self, **over):
        zone = {'low': 99.5, 'high': 100.5, 'price': 100.0, 'side': 'support',
                'volume_rub': 1e8, 'buy_share': 0.7, 'flow': 0.4,
                'large_flow': 0.5, 'large_rub': 1e7, 'trades': 100,
                'distance_pct': -0.3, 'touches': 4, 'held': 3, 'hold_rate': 0.75,
                'behavior': {'kind': 'absorption', 'label': 'уровень держат',
                             'flow': 0.4, 'large_flow': 0.6, 'large_rub': 1e7,
                             'turnover': 1e8, 'bars': 10, 'start': START,
                             'end': START + MINUTE, 'active': True}}
        zone.update(over)
        return zone

    def test_absorption_on_support_is_long(self):
        zone = self._zone()
        resistance = {'price': 103.0}
        signal = behavior.build_signal([zone], 100.0, next_up=resistance,
                                       next_down=None, fee_pct=0.05)
        self.assertEqual(signal['direction'], 'up')
        self.assertGreater(signal['confidence'], 0)
        self.assertFalse(signal['blocked_by_cost'])
        self.assertTrue(any('держал' in r for r in signal['reasons']))

    def test_break_down_is_short(self):
        zone = self._zone(side='resistance', behavior={
            'kind': 'break_down', 'label': 'пробой вниз', 'flow': -0.5,
            'large_flow': -0.6, 'large_rub': 1e7, 'turnover': 1e8, 'bars': 8,
            'start': START, 'end': START + MINUTE, 'active': True})
        signal = behavior.build_signal([zone], 100.0, next_up=None,
                                       next_down={'price': 97.0}, fee_pct=0.05)
        self.assertEqual(signal['direction'], 'down')

    def test_small_expected_move_blocked_by_cost(self):
        zone = self._zone()
        # цель в 0.05 % при издержках 0.1 % — сделка не оправдана
        resistance = {'price': 100.05}
        signal = behavior.build_signal([zone], 100.0, next_up=resistance,
                                       next_down=None, fee_pct=0.05)
        self.assertTrue(signal['blocked_by_cost'])
        self.assertTrue(any('не оправдана' in r for r in signal['reasons']))

    def test_no_active_level(self):
        signal = behavior.build_signal([], 100.0, next_up=None, next_down=None)
        self.assertEqual(signal['direction'], 'neutral')
        self.assertEqual(signal['confidence'], 0)


class AccumulateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM level_stats')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-b', 'BEH', 'Поведение', 1, 1e9, 1, 1)")
        rows = []
        for day, price in (('2026-11-06', 100.0), ('2026-11-07', 100.02), ('2026-11-08', 100.04)):
            ts = int(datetime.fromisoformat(day + 'T07:00:00+00:00').timestamp()) * 10 ** 9
            for i in range(50):
                rows.append(('u-b', 'BEH', ts + i * 10 ** 9, day, '10:00:00.000',
                             price, 1, 1, 1, price, 'buy' if i % 2 else 'sell'))
        conn.executemany(
            'INSERT OR IGNORE INTO market_trade (instrument_uid, ticker, ts_ns, trade_date,'
            ' trade_time, price, lots, lot_size, shares, value, side)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?)', rows)
        conn.commit()
        conn.close()

    def test_accumulates_and_is_idempotent(self):
        start = int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()) * 10 ** 9
        end = start + 10 * DAY_NS
        first = behavior.accumulate_level_stats('u-b', 'BEH', start, end, 1_000_000)
        second = behavior.accumulate_level_stats('u-b', 'BEH', start, end, 1_000_000)
        self.assertGreater(first, 0)
        self.assertEqual(first, second)     # повтор не дублирует строки
        profile = behavior.history_profile('u-b')
        self.assertTrue(profile)
        self.assertGreater(sum(p['buy_value'] + p['sell_value'] for p in profile), 0)

    def test_level_stats_survive_trade_deletion(self):
        start = int(datetime(2026, 11, 5, tzinfo=timezone.utc).timestamp()) * 10 ** 9
        behavior.accumulate_level_stats('u-b', 'BEH', start, start + 10 * DAY_NS, 1_000_000)
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.commit()
        conn.close()
        self.assertTrue(behavior.history_profile('u-b'))


if __name__ == '__main__':
    unittest.main()
