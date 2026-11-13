"""Тесты скринера: нормализация API, дедупликация ленты, объёмная аналитика.

Запуск: python -m unittest discover -s tests -v
Внешние вызовы не делаются: TinkoffClient подменяется заглушкой.
"""

import dataclasses
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

# Отдельная БД на весь прогон — до импорта app.db, где путь читается один раз.
_TMP_DIR = tempfile.mkdtemp(prefix='broker-screener-test-')
os.environ['BROKER_DB_PATH'] = os.path.join(_TMP_DIR, 'broker.db')
os.environ['SCREENER_DB_PATH'] = os.path.join(_TMP_DIR, 'screener.db')
os.environ.pop('TINKOFF_TOKEN', None)
os.environ.pop('SCREENER_ENABLED', None)

from app import screener  # noqa: E402
from app import signal_journal  # noqa: E402
from app.db import init_db  # noqa: E402

get_connection = screener.get_connection
from app.tinkoff_api import (RateLimiter, TinkoffClient, _normalize_direction,  # noqa: E402
                             _quotation_to_float, _timestamp_to_ns, _to_rfc3339)


class QuotationTests(unittest.TestCase):
    def test_quotation_to_float(self):
        self.assertAlmostEqual(_quotation_to_float({'units': '123', 'nano': 450000000}), 123.45)
        self.assertAlmostEqual(_quotation_to_float({'units': -5, 'nano': -500000000}), -5.5)
        self.assertEqual(_quotation_to_float(None), 0.0)

    def test_timestamp_to_ns(self):
        self.assertEqual(_timestamp_to_ns({'seconds': 10, 'nanos': 500}), 10_000_000_500)
        self.assertEqual(_timestamp_to_ns(None), 0)

    def test_rfc3339(self):
        dt = datetime(2026, 11, 8, 10, 30, tzinfo=timezone.utc)
        self.assertEqual(_to_rfc3339(dt), '2026-11-08T10:30:00.000Z')

    def test_direction_normalization(self):
        self.assertEqual(_normalize_direction('TRADE_DIRECTION_BUY'), 'buy')
        self.assertEqual(_normalize_direction('TRADE_DIRECTION_SELL'), 'sell')
        self.assertEqual(_normalize_direction('TRADE_DIRECTION_UNSPECIFIED'), '')
        self.assertEqual(_normalize_direction(None), '')


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = ''

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None, verify=None):
        self.calls.append({'url': url, 'json': json, 'headers': headers, 'verify': verify})
        return FakeResponse(self.payload)


class ClientParsingTests(unittest.TestCase):
    def test_last_trades_parsing(self):
        payload = {'trades': [
            {'direction': 'TRADE_DIRECTION_BUY',
             'price': {'units': '250', 'nano': 500000000},
             'quantity': '10',
             'time': {'seconds': '1794000000', 'nanos': 0},
             'tradeSource': 'TRADE_SOURCE_EXCHANGE'},
            {'direction': 'TRADE_DIRECTION_UNSPECIFIED',
             'price': {'units': '251', 'nano': 0},
             'quantity': '4',
             'time': {'seconds': '1794000060', 'nanos': 0}},
            {'direction': 'TRADE_DIRECTION_SELL',       # нулевой объём — мусор
             'price': {'units': '250', 'nano': 0}, 'quantity': '0',
             'time': {'seconds': '1794000120', 'nanos': 0}},
        ]}
        session = FakeSession(payload)
        client = TinkoffClient('t', session=session)
        trades = client.last_trades('uid-1',
                                    datetime(2026, 11, 8, tzinfo=timezone.utc),
                                    datetime(2026, 11, 8, 23, tzinfo=timezone.utc))
        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]['direction'], 'buy')
        self.assertAlmostEqual(trades[0]['price'], 250.5)
        self.assertEqual(trades[0]['lots'], 10)
        self.assertEqual(trades[1]['direction'], '')
        self.assertEqual(session.calls[0]['headers']['Authorization'], 'Bearer t')
        self.assertEqual(session.calls[0]['json']['instrumentId'], 'uid-1')

    def test_shares_parsing(self):
        payload = {'instruments': [
            {'uid': 'u1', 'figi': 'f1', 'ticker': 'SBER', 'classCode': 'TQBR',
             'name': 'Сбербанк', 'lot': 10, 'currency': 'RUB',
             'apiTradeAvailableFlag': True},
            {'figi': 'f2', 'ticker': 'OLD', 'classCode': 'TQBR', 'lot': 1},
        ]}
        client = TinkoffClient('t', session=FakeSession(payload))
        shares = client.shares()
        self.assertEqual(len(shares), 2)
        self.assertEqual(shares[0]['ticker'], 'SBER')
        self.assertEqual(shares[0]['lot'], 10)
        self.assertEqual(shares[1]['uid'], 'f2')  # fallback на figi

    def test_candles_parsing(self):
        payload = {'candles': [
            {'open': {'units': '100'}, 'close': {'units': '101'},
             'volume': '1000', 'isComplete': True, 'time': {'seconds': '1794000000'}},
            {'open': {'units': '101'}, 'close': {'units': '102'},
             'volume': '500', 'isComplete': False, 'time': {'seconds': '1794086400'}},
        ]}
        client = TinkoffClient('t', session=FakeSession(payload))
        candles = client.candles('u1', datetime(2026, 11, 1, tzinfo=timezone.utc),
                                 datetime(2026, 11, 8, tzinfo=timezone.utc))
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[0]['volume_lots'], 1000)
        self.assertFalse(candles[1]['is_complete'])


class RateLimiterTests(unittest.TestCase):
    def test_respects_window(self):
        limiter = RateLimiter(per_minute=3)
        for _ in range(3):
            limiter.acquire()
        self.assertEqual(len(limiter._stamps), 3)


class TickRuleTests(unittest.TestCase):
    def test_resolve_sides(self):
        trades = [
            {'price': 100.0, 'direction': ''},
            {'price': 101.0, 'direction': ''},   # выше предыдущей → покупка
            {'price': 100.5, 'direction': ''},   # ниже → продажа
            {'price': 100.5, 'direction': 'buy'},  # явное направление важнее
            {'price': 100.5, 'direction': ''},   # та же цена → как предыдущая
        ]
        screener._resolve_sides(trades, prev_price=99.0)
        self.assertEqual([t['side'] for t in trades],
                         ['buy', 'buy', 'sell', 'buy', 'buy'])

    def test_first_trade_without_previous_price(self):
        trades = [{'price': 100.0, 'direction': ''}]
        screener._resolve_sides(trades, prev_price=0.0)
        self.assertEqual(trades[0]['side'], 'buy')


def _trade(uid, ticker, ns, price, lots, side, lot=1, direction=None):
    local = datetime.fromtimestamp(ns / 1_000_000_000, tz=screener.MSK)
    shares = lots * lot
    return {
        'instrument_uid': uid, 'ticker': ticker, 'class_code': 'TQBR',
        'ts_ns': ns, 'trade_date': local.strftime('%Y-%m-%d'),
        'trade_time': local.strftime('%H:%M:%S.%f')[:-3],
        'price': price, 'lots': lots, 'lot_size': lot, 'shares': shares,
        'value': round(price * shares, 2), 'side': side,
        'direction': direction if direction is not None else side.upper(),
        'trade_source': 'TRADE_SOURCE_EXCHANGE',
    }


class CollectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        screener.get_connection().close()

    def setUp(self):
        conn = get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM screener_instrument')
        conn.execute('DELETE FROM screener_state')
        conn.commit()
        conn.close()

    def _add_instrument(self, uid, ticker, lot=1, avg_daily=0.0):
        conn = get_connection()
        conn.execute(
            'INSERT INTO screener_instrument (instrument_uid, ticker, class_code, lot, '
            'avg_daily_value, rank, selected) VALUES (?,?,?,?,?,?,1)',
            (uid, ticker, 'TQBR', lot, avg_daily, 1))
        conn.commit()
        conn.close()

    def test_overlapping_polls_do_not_duplicate(self):
        self._add_instrument('u-sber', 'SBER', lot=10)
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        batch = [
            {'ts_ns': now_ns - 120 * 10 ** 9, 'price': 250.0, 'lots': 3,
             'direction': 'buy', 'trade_source': 'TRADE_SOURCE_EXCHANGE'},
            {'ts_ns': now_ns - 60 * 10 ** 9, 'price': 250.5, 'lots': 2,
             'direction': 'sell', 'trade_source': 'TRADE_SOURCE_EXCHANGE'},
        ]

        class FakeClient:
            def last_trades(self, uid, from_dt, to_dt, trade_source=''):
                return [dict(t) for t in batch]

        inst = {'instrument_uid': 'u-sber', 'ticker': 'SBER', 'class_code': 'TQBR',
                'lot': 10, 'last_ts_ns': 0, 'last_price': None}
        cfg = screener.get_config()

        self.assertEqual(screener.collect_instrument(FakeClient(), inst, cfg), 2)

        conn = get_connection()
        inst['last_ts_ns'] = conn.execute(
            'SELECT last_ts_ns FROM screener_instrument WHERE instrument_uid=?',
            ('u-sber',)).fetchone()['last_ts_ns']
        conn.close()
        self.assertEqual(inst['last_ts_ns'], batch[-1]['ts_ns'])

        # Повторный опрос с тем же перекрытием не должен добавить строк
        self.assertEqual(screener.collect_instrument(FakeClient(), inst, cfg), 0)
        conn = get_connection()
        total = conn.execute('SELECT COUNT(*) c FROM market_trade').fetchone()['c']
        shares = conn.execute('SELECT SUM(shares) s FROM market_trade').fetchone()['s']
        conn.close()
        self.assertEqual(total, 2)
        self.assertEqual(shares, 50)  # (3 + 2) лота × 10 акций

    def test_prune_removes_old_trades(self):
        conn = get_connection()
        old_ns = int((datetime.now(timezone.utc) - timedelta(days=10)).timestamp() * 10 ** 9)
        conn.execute(
            'INSERT INTO market_trade (instrument_uid, ticker, ts_ns, trade_date, trade_time,'
            ' price, lots, shares, value, side) VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('u-old', 'OLD', old_ns, '2026-01-01', '10:00:00.000', 1.0, 1, 1, 1.0, 'buy'))
        conn.commit()
        conn.close()
        screener.prune_old_trades(screener.get_config())
        conn = get_connection()
        left = conn.execute('SELECT COUNT(*) c FROM market_trade').fetchone()['c']
        conn.close()
        self.assertEqual(left, 0)


class AnalyticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        screener.get_connection().close()

    def setUp(self):
        conn = get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM screener_instrument')
        conn.commit()
        conn.close()
        with screener._metrics_cache_lock:
            screener._metrics_cache.clear()

    def _seed(self):
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        conn = get_connection()
        conn.execute(
            'INSERT INTO screener_instrument (instrument_uid, ticker, class_code, lot, '
            'avg_daily_value, rank, selected) VALUES (?,?,?,?,?,?,1)',
            ('u-buy', 'BUYER', 'TQBR', 1, 50_000.0, 1))
        conn.execute(
            'INSERT INTO screener_instrument (instrument_uid, ticker, class_code, lot, '
            'avg_daily_value, rank, selected) VALUES (?,?,?,?,?,?,1)',
            ('u-sell', 'SELLER', 'TQBR', 1, 50_000.0, 2))
        rows = []
        for i in range(20):
            ns = now_ns - (20 - i) * 30 * 10 ** 9
            rows.append(_trade('u-buy', 'BUYER', ns, 100.0 + i * 0.01, 1000, 'buy'))
            rows.append(_trade('u-sell', 'SELLER', ns, 200.0 - i * 0.01, 1000, 'sell'))
        conn.executemany(
            'INSERT INTO market_trade (instrument_uid, ticker, class_code, ts_ns, trade_date,'
            ' trade_time, price, lots, lot_size, shares, value, side, direction, trade_source)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            [(r['instrument_uid'], r['ticker'], r['class_code'], r['ts_ns'], r['trade_date'],
              r['trade_time'], r['price'], r['lots'], r['lot_size'], r['shares'], r['value'],
              r['side'], r['direction'], r['trade_source']) for r in rows])
        conn.commit()
        conn.close()

    def test_metrics_and_signals(self):
        self._seed()
        metrics = screener.compute_metrics(window='session')
        by_ticker = {m['ticker']: m for m in metrics}
        self.assertEqual(set(by_ticker), {'BUYER', 'SELLER'})

        buyer = by_ticker['BUYER']
        self.assertAlmostEqual(buyer['delta_ratio'], 1.0)
        self.assertEqual(buyer['delta_shares'], 20000)
        self.assertGreater(buyer['score'], 0)
        self.assertEqual(buyer['signal'], 'strong_buy')

        seller = by_ticker['SELLER']
        self.assertAlmostEqual(seller['delta_ratio'], -1.0)
        self.assertLess(seller['score'], 0)
        self.assertEqual(seller['signal'], 'strong_sell')

        # Сортировка по оценке: покупатель выше продавца
        self.assertEqual(metrics[0]['ticker'], 'BUYER')

    def test_min_turnover_filter(self):
        self._seed()
        rows = screener.compute_metrics(window='session', min_turnover=10 ** 9)
        self.assertEqual(rows, [])

    def test_large_trades_split(self):
        self._seed()
        metrics = screener.compute_metrics(window='session', large_rub=1000.0)
        buyer = next(m for m in metrics if m['ticker'] == 'BUYER')
        # 1000 акций × ~100 ₽ = 100 000 ₽ — каждая сделка крупная
        self.assertAlmostEqual(buyer['large_delta_ratio'], 1.0)
        self.assertGreater(buyer['large_buy'], 0)

    def test_instrument_detail(self):
        self._seed()
        detail = screener.instrument_detail('buyer', window='session')
        self.assertIsNotNone(detail)
        self.assertEqual(detail['instrument']['ticker'], 'BUYER')
        self.assertEqual(detail['metrics']['signal'], 'strong_buy')
        self.assertTrue(detail['series'])
        self.assertTrue(detail['tape'])
        self.assertEqual(detail['tape'][0]['side'], 'buy')
        self.assertIsNone(screener.instrument_detail('NOPE', window='session'))


class MarketHoursTests(unittest.TestCase):
    def test_weekday_session(self):
        # среда 12:00 МСК
        self.assertTrue(screener._market_is_open(datetime(2026, 11, 11, 9, 0, tzinfo=timezone.utc)))

    def test_weekday_late_evening(self):
        # среда 23:59 МСК — уже закрыто
        self.assertFalse(screener._market_is_open(datetime(2026, 11, 11, 21, 0, tzinfo=timezone.utc)))

    def test_weekend_session(self):
        # воскресенье 14:00 МСК — идут выходные торги
        self.assertTrue(screener._market_is_open(datetime(2026, 11, 8, 11, 0, tzinfo=timezone.utc)))

    def test_weekend_after_close(self):
        # воскресенье 19:30 МСК
        self.assertFalse(screener._market_is_open(datetime(2026, 11, 8, 16, 30, tzinfo=timezone.utc)))


class StorageTests(unittest.TestCase):
    """Отдельная БД, свёртка в минутки, сроки хранения и выгрузка."""

    @classmethod
    def setUpClass(cls):
        screener.get_connection().close()

    def setUp(self):
        conn = screener.get_connection()
        conn.execute('DELETE FROM market_trade')
        conn.execute('DELETE FROM market_bar_1m')
        conn.execute('DELETE FROM screener_state')
        conn.commit()
        conn.close()
        with screener._metrics_cache_lock:
            screener._metrics_cache.clear()

    def test_separate_database_file(self):
        # Тики не должны попадать в базу отчётов
        self.assertNotEqual(os.path.abspath(screener.screener_db_path()),
                            os.path.abspath(os.environ['BROKER_DB_PATH']))
        self.assertTrue(screener.screener_db_path().endswith('screener.db'))

    def test_rollup_builds_minute_bars(self):
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        minute_ns = (now_ns // 60_000_000_000) * 60_000_000_000 - 120_000_000_000
        conn = screener.get_connection()
        rows = []
        for i, (price, lots, side) in enumerate([(100.0, 10, 'buy'), (100.5, 20, 'sell'),
                                                 (101.0, 5, 'buy'), (99.5, 15, 'buy')]):
            trade = _trade('u-roll', 'ROLL', minute_ns + i * 1_000_000_000, price, lots, side)
            rows.append((trade['instrument_uid'], trade['ticker'], trade['class_code'],
                         trade['ts_ns'], trade['trade_date'], trade['trade_time'],
                         trade['price'], trade['lots'], trade['lot_size'], trade['shares'],
                         trade['value'], trade['side'], trade['direction'],
                         trade['trade_source']))
        conn.executemany(
            'INSERT INTO market_trade (instrument_uid, ticker, class_code, ts_ns, trade_date,'
            ' trade_time, price, lots, lot_size, shares, value, side, direction, trade_source)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
        conn.commit()
        conn.close()

        screener.rollup_bars(until_ns=minute_ns + 60_000_000_000,
                             from_ns=minute_ns)
        conn = screener.get_connection()
        bar = conn.execute('SELECT * FROM market_bar_1m WHERE instrument_uid=?', ('u-roll',)).fetchone()
        conn.close()
        self.assertIsNotNone(bar)
        self.assertEqual(bar['open'], 100.0)
        self.assertEqual(bar['close'], 99.5)
        self.assertEqual(bar['high'], 101.0)
        self.assertEqual(bar['low'], 99.5)
        self.assertEqual(bar['volume'], 50)           # 10+20+5+15 лотов, лот = 1
        self.assertEqual(bar['buy_volume'], 30)       # 10+5+15
        self.assertEqual(bar['sell_volume'], 20)
        self.assertEqual(bar['trades'], 4)
        self.assertAlmostEqual(bar['turnover'],
                               100.0*10 + 100.5*20 + 101.0*5 + 99.5*15)

    def test_prune_keeps_bars(self):
        cfg = screener.get_config()
        old_ns = int((datetime.now(timezone.utc) - timedelta(days=cfg.retention_days + 2))
                     .timestamp() * 1_000_000_000)
        minute_ns = (old_ns // 60_000_000_000) * 60_000_000_000
        conn = screener.get_connection()
        trade = _trade('u-old', 'OLD', minute_ns + 1_000_000_000, 10.0, 5, 'buy')
        conn.execute(
            'INSERT INTO market_trade (instrument_uid, ticker, class_code, ts_ns, trade_date,'
            ' trade_time, price, lots, lot_size, shares, value, side, direction, trade_source)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (trade['instrument_uid'], trade['ticker'], trade['class_code'], trade['ts_ns'],
             trade['trade_date'], trade['trade_time'], trade['price'], trade['lots'],
             trade['lot_size'], trade['shares'], trade['value'], trade['side'],
             trade['direction'], trade['trade_source']))
        conn.commit()
        conn.execute("DELETE FROM screener_state WHERE key='bar_rollup_ts'")
        conn.commit()
        conn.close()

        screener.prune_old_trades(cfg)

        conn = screener.get_connection()
        raw_left = conn.execute('SELECT COUNT(*) c FROM market_trade').fetchone()['c']
        bars = conn.execute('SELECT COUNT(*) c FROM market_bar_1m').fetchone()['c']
        conn.close()
        self.assertEqual(raw_left, 0, 'старый тик должен быть удалён')
        self.assertEqual(bars, 1, 'минутка должна остаться после чистки тика')

    def test_export_writes_file(self):
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        conn = screener.get_connection()
        for i in range(50):
            trade = _trade('u-exp', 'EXP', now_ns - i * 1_000_000_000, 10.0 + i * 0.01, 3, 'buy')
            conn.execute(
                'INSERT INTO market_trade (instrument_uid, ticker, class_code, ts_ns, trade_date,'
                ' trade_time, price, lots, lot_size, shares, value, side, direction, trade_source)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (trade['instrument_uid'], trade['ticker'], trade['class_code'], trade['ts_ns'],
                 trade['trade_date'], trade['trade_time'], trade['price'], trade['lots'],
                 trade['lot_size'], trade['shares'], trade['value'], trade['side'],
                 trade['direction'], trade['trade_source']))
        conn.commit()
        conn.close()

        out = os.path.join(_TMP_DIR, 'export_test')
        info = screener.export_trades(out, 0, now_ns + 1_000_000_000)
        self.assertEqual(info['rows'], 50)
        self.assertGreater(info['bytes'], 0)
        self.assertTrue(os.path.exists(info['path']))
        self.assertIn(info['format'], ('parquet+zstd', 'csv+zstd', 'csv+gzip'))
        self.assertEqual(info['source'], 'trades')
        os.remove(info['path'])


class DiskGuardTests(unittest.TestCase):
    def test_guard_stops_collection(self):
        cfg = screener.get_config()
        original = screener.free_disk_mb
        screener.free_disk_mb = lambda path=None: 10.0   # «диск кончился»
        try:
            cfg_low = dataclasses.replace(cfg, min_free_disk_mb=500.0)
            with self.assertRaises(screener.ScreenerStorageError):
                screener.run_cycle(None, cfg_low)
        finally:
            screener.free_disk_mb = original

    def test_status_reports_free_space(self):
        self.assertIn('free_disk_mb', screener.collector_status())


class SignalThresholdTests(unittest.TestCase):
    def test_accumulation_when_volume_without_edge(self):
        metrics = {'delta_ratio': 0.05, 'large_delta_ratio': 0.0, 'activity_ratio': 2.0,
                   'price_change_pct': 0.05, 'large_turnover': 0}
        self.assertEqual(screener._classify(metrics), 'accumulation')

    def test_distribution_when_price_holds_up(self):
        metrics = {'delta_ratio': -0.1, 'large_delta_ratio': 0.0, 'activity_ratio': 2.0,
                   'price_change_pct': 0.5, 'large_turnover': 0}
        self.assertEqual(screener._classify(metrics), 'distribution')

    def test_large_prints_dominate(self):
        metrics = {'delta_ratio': 0.1, 'large_delta_ratio': 0.7, 'activity_ratio': 0.5,
                   'price_change_pct': 0.1, 'large_turnover': 5_000_000, 'large_count': 12}
        self.assertEqual(screener._classify(metrics), 'large_buy')

    def test_tiny_turnover_is_neutral(self):
        # Оборот ниже порога: дельта 100% на паре сделок — не сигнал
        metrics = {'delta_ratio': 1.0, 'large_delta_ratio': 0.0, 'activity_ratio': 0.1,
                   'price_change_pct': 0.0, 'large_turnover': 0,
                   'turnover': 3854.0, 'trades': 25, 'signal_turnover_floor': 1_000_000.0}
        self.assertEqual(screener._classify(metrics), 'neutral')

    def test_single_large_print_is_not_a_signal(self):
        metrics = {'delta_ratio': 0.1, 'large_delta_ratio': 1.0, 'activity_ratio': 0.3,
                   'price_change_pct': 0.1, 'large_turnover': 2_000_000, 'large_count': 1,
                   'turnover': 30_000_000}
        self.assertNotIn(screener._classify(metrics), ('large_buy', 'large_sell'))

    def test_neutral(self):
        metrics = {'delta_ratio': 0.02, 'large_delta_ratio': 0.0, 'activity_ratio': 0.4,
                   'price_change_pct': 0.0, 'large_turnover': 0}
        self.assertEqual(screener._classify(metrics), 'neutral')


class SignalJournalTests(unittest.TestCase):
    """Журнал сигналов: первая запись фиксирует статус навсегда."""

    def setUp(self):
        conn = signal_journal._conn()
        conn.execute('DELETE FROM strategy_signal_log')
        conn.commit()
        conn.close()

    @staticmethod
    def signal(ts, *, taken=True, rr=2.0, kind='reversal'):
        return {'time': ts, 'date': '15.11.2023', 'time_label': '01:13',
                'kind': kind,
                'kind_label': 'разворот' if kind == 'reversal' else 'пробой',
                'zone_price': 100.0, 'zone_low': 99.9, 'zone_high': 100.1,
                'price': 100.05, 'delta': 0.2, 'volume_ratio': 1.3,
                'taken': taken, 'rr': rr}

    def test_first_status_wins(self):
        self.assertEqual(signal_journal.record('SBER', [self.signal(1700010800)]), 1)
        # Повторный расчёт с другим статусом не переписывает историю.
        changed = self.signal(1700010800, taken=False, rr=0.4)
        self.assertEqual(signal_journal.record('SBER', [changed]), 0)
        rows = signal_journal.rows(ticker='SBER')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['taken'])
        self.assertEqual(rows[0]['rr'], 2.0)
        self.assertEqual(rows[0]['date'], '15.11.2023')
        self.assertEqual(rows[0]['day'], '2023-11-15')
        self.assertTrue(rows[0]['first_seen_at'])

    def test_rows_sorted_and_filtered(self):
        signal_journal.record('SBER', [self.signal(1700014400),
                                       self.signal(1700010800)])
        signal_journal.record('GAZP', [self.signal(1700012600, taken=False,
                                                   rr=0.5, kind='breakout')])
        rows = signal_journal.rows(day='2023-11-15')
        self.assertEqual([r['signal_ts'] for r in rows],
                         [1700010800, 1700012600, 1700014400])
        self.assertEqual(len(signal_journal.rows(ticker='GAZP')), 1)
        self.assertEqual(signal_journal.rows(day='2023-11-14'), [])

    def test_counts(self):
        signal_journal.record('SBER', [self.signal(1700010800),
                                       self.signal(1700014400, taken=False, rr=0.5)])
        info = signal_journal.counts(day='2023-11-15')['SBER']
        self.assertEqual(info['count'], 2)
        self.assertEqual(info['taken'], 1)
        self.assertEqual(info['skipped'], 1)
        self.assertEqual(info['last'], 1700014400)
        self.assertEqual(info['kinds'], ['разворот'])


if __name__ == '__main__':
    unittest.main()
