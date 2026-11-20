"""Тесты единственной стратегии: сигналы по уровням и лонг-симуляция."""

import os
import tempfile
import unittest

os.environ['BROKER_DB_PATH'] = os.path.join(tempfile.mkdtemp(prefix='broker-strat-'), 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(tempfile.mkdtemp(prefix='broker-strat-'), 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)

from app import strategy_levels as sl  # noqa: E402

MINUTE = 60 * 10 ** 9
START = 1_700_000_000 * 10 ** 9


def bar(index, close, *, low=None, high=None, open_=None, volume=1000, buy_share=0.7):
    buy = int(volume * buy_share)
    return {
        'minute_ns': START + index * MINUTE,
        'open': open_ if open_ is not None else close,
        'high': high if high is not None else close * 1.0002,
        'low': low if low is not None else close * 0.9998,
        'close': close, 'volume': volume, 'turnover': close * volume,
        'buy_volume': buy, 'sell_volume': volume - buy,
        'large_buy_turnover': 0.0, 'large_sell_turnover': 0.0,
    }


def support(price=100.0, width=0.05):
    return {'price': price, 'low': price - width, 'high': price + width,
            'side': 'support', 'hold_rate': 0.6, 'touches': 5, 'volume_rub': 1e8}


def resistance(price=101.0, width=0.05):
    return {'price': price, 'low': price - width, 'high': price + width,
            'side': 'resistance', 'hold_rate': 0.5, 'touches': 4, 'volume_rub': 8e7}


PARAMS = sl._resolve({})


class SignalTests(unittest.TestCase):
    def test_reversal_at_support(self):
        bars = [bar(i, 100.6) for i in range(6)]
        bars.append(bar(6, 100.2, low=99.95, high=100.25, open_=100.5, buy_share=0.85))
        bars += [bar(i, 100.4) for i in range(7, 12)]
        found = sl.signals('TEST', bars, [support()], PARAMS, start_index=1)
        self.assertTrue(found)
        self.assertEqual(found[0]['kind'], 'reversal')
        self.assertEqual(found[0]['zone']['price'], 100.0)

    def test_no_reversal_without_confirmation(self):
        # бар просел в зону, но закрылся ниже открытия, и в окне продавцы
        bars = [bar(i, 100.6, buy_share=0.2) for i in range(6)]
        bars.append(bar(6, 100.1, low=99.95, high=100.2, open_=100.18, buy_share=0.2))
        bars += [bar(i, 100.3, buy_share=0.2) for i in range(7, 12)]
        found = sl.signals('TEST', bars, [support()], PARAMS, start_index=1)
        self.assertEqual(found, [])

    def test_breakout_with_volume(self):
        bars = [bar(i, 100.5) for i in range(6)]
        bars.append(bar(6, 101.2, high=101.3, low=100.4, open_=100.6, volume=4000, buy_share=0.9))
        found = sl.signals('TEST', bars, [resistance()], PARAMS, start_index=1)
        self.assertTrue(found)
        self.assertEqual(found[0]['kind'], 'breakout')

    def test_wide_zone_ignored(self):
        wide = support(price=100.0, width=1.5)   # ширина 3 % — не уровень для входа
        bars = [bar(i, 100.6) for i in range(6)]
        bars.append(bar(6, 100.2, low=99.9, high=100.3, open_=100.5, buy_share=0.9))
        found = sl.signals('TEST', bars, [wide], PARAMS, start_index=1)
        self.assertEqual(found, [])

    def test_cooldown_limits_repeats(self):
        bars = [bar(i, 100.6) for i in range(5)]
        for _ in range(4):
            bars.append(bar(len(bars), 100.2, low=99.95, high=100.25, open_=100.5, buy_share=0.9))
            bars.append(bar(len(bars), 100.6))
        params = dict(PARAMS, cooldown_min=30)
        found = sl.signals('TEST', bars, [support()], params, start_index=1)
        self.assertEqual(len(found), 1)


class SimulateTests(unittest.TestCase):
    def _bars_for_trade(self):
        bars = [bar(i, 100.0, low=99.8, high=100.2) for i in range(5)]
        bars.append(bar(5, 105.0, low=99.5, high=105.5, open_=100.0))   # стоп внутри бара
        bars += [bar(i, 104.0) for i in range(6, 12)]
        return bars

    def test_stop_triggered(self):
        bars = self._bars_for_trade()
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        result = sl.simulate(bars, [signal], dict(PARAMS, min_rr=0.0), zones=[support()],
                             ticker='TEST')
        self.assertEqual(len(result['trades']), 1)
        self.assertEqual(result['trades'][0]['reason'], 'stop')
        self.assertTrue(result['stop_enabled'])

    def test_no_stop_mode(self):
        bars = self._bars_for_trade()
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        result = sl.simulate(bars, [signal], dict(PARAMS, use_stop=0, min_rr=0.0),
                             zones=[support()], ticker='TEST')
        self.assertFalse(result['stop_enabled'])
        self.assertEqual(len(result['trades']), 1)
        self.assertNotEqual(result['trades'][0]['reason'], 'stop')

    def test_dividend_tickers_skip_stop(self):
        os.environ['SCREENER_NO_STOP_TICKERS'] = 'DIV,GAZP'
        try:
            bars = self._bars_for_trade()
            signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                      'kind': 'reversal', 'kind_label': 'разворот'}
            result = sl.simulate(bars, [signal], PARAMS, zones=[support()], ticker='DIV')
            self.assertFalse(result['stop_enabled'])
            self.assertEqual(sl.no_stop_tickers(), {'DIV', 'GAZP'})
        finally:
            os.environ.pop('SCREENER_NO_STOP_TICKERS', None)

    def test_long_only_markers(self):
        bars = self._bars_for_trade()
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        result = sl.simulate(bars, [signal], dict(PARAMS, min_rr=0.0), zones=[support()],
                             ticker='TEST')
        self.assertTrue(all(m['position'] == 'belowBar' for m in result['markers'][:1]))
        self.assertEqual(len(result['markers']), 2 * len(result['trades']))

    def test_metrics_consistent(self):
        bars = self._bars_for_trade()
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        result = sl.simulate(bars, [signal], dict(PARAMS, min_rr=0.0), zones=[support()],
                             ticker='TEST')
        metrics = result['metrics']
        self.assertEqual(metrics['trades'], len(result['trades']))
        self.assertAlmostEqual(metrics['net_pnl'],
                               sum(t['pnl'] for t in result['trades']), delta=0.05)


class CatalogTests(unittest.TestCase):
    def test_single_strategy(self):
        catalog = sl.catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]['key'], 'levels_long')
        keys = {p['key'] for p in catalog[0]['params']}
        self.assertIn('use_stop', keys)
        self.assertIn('max_zone_width_pct', keys)

    def test_params_have_bounds(self):
        params = sl._resolve({'take_profit_pct': 99, 'cooldown_min': -5})
        self.assertEqual(params['take_profit_pct'], 20.0)
        self.assertEqual(params['cooldown_min'], 0)


if __name__ == '__main__':
    unittest.main()


class RiskRewardTests(unittest.TestCase):
    def _bars(self):
        # вход по 100, стоп далеко (98), цель очень близко (100.2)
        bars = [bar(i, 100.0, low=99.9, high=100.1) for i in range(5)]
        bars += [bar(i, 100.0, low=99.9, high=100.1) for i in range(5, 12)]
        return bars

    def test_trade_skipped_when_target_too_close(self):
        bars = self._bars()
        near_resistance = {'price': 100.2, 'low': 100.15, 'high': 100.25,
                           'side': 'resistance', 'hold_rate': 0.5, 'touches': 3,
                           'volume_rub': 1e8}
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        params = dict(PARAMS, min_rr=1.5, atr_mult=0.0, take_profit_pct=0.1)
        result = sl.simulate(bars, [signal], params, zones=[support(), near_resistance],
                             ticker='TEST')
        self.assertEqual(len(result['trades']), 0)
        self.assertEqual(result['metrics']['skipped_rr'], 1)

    def test_rr_recorded_per_signal(self):
        bars = self._bars()
        near_resistance = {'price': 100.2, 'low': 100.15, 'high': 100.25,
                           'side': 'resistance', 'hold_rate': 0.5, 'touches': 3,
                           'volume_rub': 1e8}
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        params = dict(PARAMS, min_rr=1.5, atr_mult=0.0, take_profit_pct=0.1)
        result = sl.simulate(bars, [signal], params,
                             zones=[support(), near_resistance], ticker='TEST')
        rr = result['rr_by_time'][signal['time']]
        self.assertLess(rr, 1.5)

    def test_signal_moment_is_msk(self):
        # START = 1_700_000_000 с — 2023-11-15 01:13 по Москве
        self.assertEqual(sl._signal_moment(START), ('15.11.2023', '01:13'))

    def test_trade_taken_with_good_rr(self):
        bars = [bar(i, 100.0, low=99.9, high=100.1) for i in range(5)]
        bars += [bar(i, 100.4, low=100.0, high=100.5) for i in range(5, 12)]
        far_resistance = {'price': 102.0, 'low': 101.9, 'high': 102.1,
                          'side': 'resistance', 'hold_rate': 0.5, 'touches': 3,
                          'volume_rub': 1e8}
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        params = dict(PARAMS, min_rr=1.5, atr_mult=0.0, take_profit_pct=5.0)
        result = sl.simulate(bars, [signal], params,
                             zones=[support(), far_resistance], ticker='TEST')
        self.assertEqual(len(result['trades']), 1)
        self.assertEqual(result['metrics']['skipped_rr'], 0)

    def test_atr_stop_wider_than_percent(self):
        # волатильные бары: ATR больше 0.25 % от цены
        bars = [bar(i, 100.0, low=99.0, high=101.0) for i in range(20)]
        bars += [bar(i, 100.0, low=99.0, high=101.0) for i in range(20, 24)]
        signal = {'time': bars[0]['minute_ns'], 'price': 100.0, 'zone': support(),
                  'kind': 'reversal', 'kind_label': 'разворот'}
        # цель далеко (тейк 10 %), чтобы сделка прошла фильтр R:R
        result = sl.simulate(bars, [signal],
                             dict(PARAMS, atr_mult=1.0, min_rr=0.5, take_profit_pct=10.0,
                                  max_hold_min=3),
                             zones=[support()], ticker='TEST')
        self.assertTrue(result['trades'])
        trade = result['trades'][0]
        # стоп шире 0.25 % от входа, потому что ATR = 2 ₽
        self.assertGreaterEqual(trade['entry_price'] - trade['stop'], 1.5)
