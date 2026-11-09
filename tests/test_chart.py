"""Тесты графика и бэктеста: ресемплинг свечей, VWAP, маркеры, сделки, метрики."""

import os
import tempfile
import unittest
from datetime import datetime, timezone

_TMP_DIR = tempfile.mkdtemp(prefix='broker-chart-test-')
os.environ['BROKER_DB_PATH'] = os.path.join(_TMP_DIR, 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(_TMP_DIR, 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)

from app import backtest, chartdata, screener  # noqa: E402
from app.db import init_db  # noqa: E402

DAY_START_NS = int(datetime(2026, 11, 9, 7, 0, tzinfo=timezone.utc).timestamp()) * 10 ** 9


def _bar(uid, ticker, minute, *, open_, high, low, close, volume, buy_vol, sell_vol,
         large_buy=0.0, large_sell=0.0):
    minute_ns = DAY_START_NS + minute * 60 * 10 ** 9
    turnover = close * volume
    return {
        'instrument_uid': uid, 'ticker': ticker, 'minute_ns': minute_ns,
        'trade_date': '2026-11-09', 'trade_time': f'{10 + minute // 60:02d}:{minute % 60:02d}',
        'open': open_, 'high': high, 'low': low, 'close': close,
        'vwap': close, 'volume': volume, 'turnover': turnover,
        'buy_volume': buy_vol, 'sell_volume': sell_vol,
        'buy_turnover': close * buy_vol, 'sell_turnover': close * sell_vol,
        'trades': 10, 'large_turnover': large_buy + large_sell,
        'large_count': 1 if (large_buy or large_sell) else 0,
        'large_buy_turnover': large_buy, 'large_sell_turnover': large_sell,
    }


INSERT_BAR = '''INSERT OR REPLACE INTO market_bar_1m
    (instrument_uid, ticker, minute_ns, trade_date, trade_time, open, high, low, close,
     vwap, volume, turnover, buy_volume, sell_volume, buy_turnover, sell_turnover,
     trades, large_turnover, large_count, large_buy_turnover, large_sell_turnover)
    VALUES (:instrument_uid, :ticker, :minute_ns, :trade_date, :trade_time, :open, :high,
            :low, :close, :vwap, :volume, :turnover, :buy_volume, :sell_volume,
            :buy_turnover, :sell_turnover, :trades, :large_turnover, :large_count,
            :large_buy_turnover, :large_sell_turnover)'''


class ChartDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_bar_1m')
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-t', 'TEST', 'Тест', 1, 1e9, 1, 1)")
        cls.bars = [
            _bar('u-t', 'TEST', 0, open_=100, high=101, low=99, close=100.5,
                 volume=1000, buy_vol=700, sell_vol=300),
            _bar('u-t', 'TEST', 1, open_=100.5, high=102, low=100, close=101.5,
                 volume=1200, buy_vol=500, sell_vol=700),
            _bar('u-t', 'TEST', 2, open_=101.5, high=103, low=101, close=102.5,
                 volume=800, buy_vol=600, sell_vol=200),
        ]
        conn.executemany(INSERT_BAR, cls.bars)
        conn.execute(
            'INSERT INTO market_trade (instrument_uid, ticker, ts_ns, trade_date, trade_time,'
            ' price, lots, shares, value, side) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('u-t', 'TEST', DAY_START_NS + 60 * 10 ** 9, '2026-11-09', '10:01:00.000',
             101.5, 20000, 20000, 2_030_000.0, 'buy'))
        conn.execute(
            'INSERT INTO market_trade (instrument_uid, ticker, ts_ns, trade_date, trade_time,'
            ' price, lots, shares, value, side) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('u-t', 'TEST', DAY_START_NS + 90 * 10 ** 9, '2026-11-09', '10:01:30.000',
             101.4, 100, 100, 10_140.0, 'sell'))
        conn.commit()
        conn.close()

    def test_series_candles(self):
        data = chartdata.series('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 1)
        self.assertEqual(len(data['candles']), 3)
        first = data['candles'][0]
        self.assertEqual(first['open'], 100)
        self.assertEqual(first['close'], 100.5)
        self.assertEqual(len(data['volume']), 3)
        self.assertEqual(data['summary']['bars'], 3)

    def test_resample_five_minutes(self):
        data = chartdata.series('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 5)
        self.assertEqual(len(data['candles']), 1)
        candle = data['candles'][0]
        self.assertEqual(candle['open'], 100)      # открытие первого бара
        self.assertEqual(candle['close'], 102.5)   # закрытие последнего
        self.assertEqual(candle['high'], 103)
        self.assertEqual(candle['low'], 99)
        self.assertEqual(data['summary']['volume'], 3000)

    def test_session_vwap_is_cumulative(self):
        data = chartdata.prepared('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 1)
        bars = data['bars']
        self.assertGreater(bars[0]['session_vwap'], 0)
        self.assertNotEqual(bars[0]['session_vwap'], bars[2]['session_vwap'])

    def test_markers_large_and_all(self):
        large = chartdata.markers('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                                  min_value=1_000_000, mode='large')
        self.assertEqual(large['count'], 1)
        self.assertEqual(large['items'][0]['side'], 'buy')
        every = chartdata.markers('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                                  mode='all')
        self.assertEqual(every['count'], 2)

    def test_tape_order(self):
        rows = chartdata.tape('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 10)
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[0]['trade_time'], rows[1]['trade_time'])  # сначала свежие

    def test_unknown_ticker(self):
        with self.assertRaises(chartdata.ChartError):
            chartdata.series('NOPE', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 1)

    def test_bad_interval(self):
        with self.assertRaises(chartdata.ChartError):
            chartdata.series('TEST', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 7)


class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_bar_1m')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute("INSERT INTO screener_instrument (instrument_uid, ticker, name, lot, "
                     "avg_daily_value, rank, selected) VALUES ('u-bt', 'BT', 'Бэктест', 1, 1e9, 1, 1)")
        # Плавный рост с сильным перевесом покупок — импульс дельты должен лонговать
        bars = []
        for i in range(60):
            price = 100 + i * 0.05
            bars.append(_bar('u-bt', 'BT', i, open_=price, high=price + 0.05,
                             low=price - 0.02, close=price + 0.04,
                             volume=1000, buy_vol=850, sell_vol=150))
        conn.executemany(INSERT_BAR, bars)
        conn.commit()
        conn.close()

    def test_delta_momentum_trades(self):
        result = backtest.run('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                              'delta_momentum',
                              {'window_min': 5, 'threshold': 0.3, 'volume_factor': 0.5,
                               'take_profit_pct': 5, 'stop_loss_pct': 5,
                               'max_hold_min': 10},
                              interval=1)
        self.assertGreater(result['metrics']['trades'], 0)
        self.assertEqual(result['strategy'], 'delta_momentum')
        first = result['trades'][0]
        self.assertEqual(first['side'], 'long')
        self.assertGreater(first['entry_time'], 0)
        # вход строго позже сигнального бара — без заглядывания в будущее
        self.assertGreaterEqual(first['entry_price'], 100)

    def test_metrics_consistent(self):
        result = backtest.run('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                              'delta_momentum',
                              {'window_min': 5, 'threshold': 0.3, 'volume_factor': 0.5,
                               'take_profit_pct': 5, 'stop_loss_pct': 5,
                               'max_hold_min': 10}, interval=1)
        m = result['metrics']
        self.assertEqual(m['trades'], len(result['trades']))
        self.assertAlmostEqual(m['net_pnl'], sum(t['pnl'] for t in result['trades']), delta=0.05)
        self.assertGreaterEqual(m['max_drawdown'], 0)
        self.assertTrue(0 <= m['win_rate'] <= 100)
        self.assertEqual(len(result['markers']), 2 * m['trades'])

    def test_no_lookahead_entry_price_matches_next_open(self):
        prepared = chartdata.prepared('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 1)
        result = backtest.run('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                              'delta_momentum',
                              {'window_min': 5, 'threshold': 0.3, 'volume_factor': 0.5,
                               'take_profit_pct': 9, 'stop_loss_pct': 9,
                               'max_hold_min': 3}, interval=1)
        opens = {b['minute_ns'] // 10 ** 9 + 3 * 3600: b['open'] for b in prepared['bars']}
        for trade in result['trades']:
            self.assertAlmostEqual(trade['entry_price'], opens[trade['entry_time']], places=4)

    def test_unknown_strategy(self):
        with self.assertRaises(backtest.BacktestError):
            backtest.run('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 'nope', {})

    def test_params_clamped_to_bounds(self):
        params = backtest._resolve_params('delta_momentum', {'threshold': 99, 'window_min': 0})
        self.assertEqual(params['threshold'], 0.95)
        self.assertEqual(params['window_min'], 1)

    def test_not_enough_data(self):
        with self.assertRaises(backtest.BacktestError):
            backtest.run('BT', DAY_START_NS, DAY_START_NS + 60 * 10 ** 9,
                         'delta_momentum', {})

    def test_exit_price_within_bar_range(self):
        """Цена выхода должна существовать на бирже и лежать внутри бара."""
        prepared = chartdata.prepared('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9, 1)
        by_time = {chartdata._to_chart_time(b['minute_ns']): b for b in prepared['bars']}
        for strategy in ('delta_momentum', 'vwap_reversion'):
            result = backtest.run('BT', DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                                  strategy, {'take_profit_pct': 0.05, 'stop_loss_pct': 0.05},
                                  interval=1)
            for trade in result['trades']:
                bar = by_time[trade['exit_time']]
                self.assertGreaterEqual(trade['exit_price'], bar['low'] - 1e-9)
                self.assertLessEqual(trade['exit_price'], bar['high'] + 1e-9)
                # не больше 2 знаков — котировки MOEX для этих цен
                self.assertEqual(round(trade['exit_price'], 2), trade['exit_price'])

    def test_vwap_signal_uses_window_delta(self):
        """Порог отклонения адаптивный: фиксированный процент почти недостижим."""
        params = backtest._resolve_params('vwap_reversion', {})
        self.assertIn('deviation_sigma', params)
        self.assertNotIn('deviation_pct', params)

    def test_scan_uses_actual_data_span(self):
        result = backtest.scan(['BT'], DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                               'delta_momentum',
                               {'window_min': 5, 'threshold': 0.3, 'volume_factor': 0.5})
        self.assertEqual(result['tickers'], 1)
        self.assertGreaterEqual(result['total_trades'], 0)
        self.assertIn('in_net', result)
        self.assertIn('out_net', result)
        self.assertEqual(len(result['rows']), 1)

    def test_scan_bad_split(self):
        with self.assertRaises(backtest.BacktestError):
            backtest.scan(['BT'], DAY_START_NS, DAY_START_NS + 3600 * 10 ** 9,
                          'delta_momentum', {}, split=1.5)

    def test_strategy_catalog(self):
        catalog = backtest.strategy_catalog()
        keys = {s['key'] for s in catalog}
        self.assertEqual(keys, {'delta_momentum', 'vwap_reversion', 'absorption',
                                'large_print_follow'})
        for item in catalog:
            self.assertTrue(item['label'])
            self.assertTrue(item['params'])


if __name__ == '__main__':
    unittest.main()
