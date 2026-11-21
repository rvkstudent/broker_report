"""Тесты AI-сигналов: разбор ответа модели, вотчлист, рыночный контекст.

Запуск: python -m unittest tests.test_ai_signals -v
Сеть не используется: снимок скринера и история цен подменяются.
"""

import os
import tempfile
import unittest

_TMP_DIR = tempfile.mkdtemp(prefix='broker-ai-signals-test-')
os.environ.setdefault('BROKER_DB_PATH', os.path.join(_TMP_DIR, 'broker.db'))
os.environ.pop('ADVISOR_LLM_URL', None)
os.environ.pop('ADVISOR_LLM_KEY', None)
os.environ.pop('ADVISOR_LLM_MODEL', None)

from app import ai_signals  # noqa: E402


SNAPSHOT = {
    'bonds': [{'code': 'SU26243RMFS4', 'name': 'ОФЗ 26243', 'ytm': 16.99, 'duration': 5.72}],
    'fx_bonds': [{'code': 'RU000A10C3M0', 'name': 'Атомэнпр06', 'ytm': 9.09}],
    'stocks': [{'code': 'MTSS', 'name': 'МТС-ао', 'price': 175.1, 'div12': 20.03}],
}


class ParseTests(unittest.TestCase):
    def test_parses_valid_answer(self):
        text = ('{"signal": "покупать", "confidence": 72, "entry": "до 170 ₽", '
                '"target": "210 ₽", "stop": "160 ₽", "horizon": "6–12 мес", '
                '"why": ["YTM 17% при дюрации 5,7"], "risks": ["рост ставок"]}')
        data = ai_signals._parse(text)
        self.assertEqual(data['signal'], 'покупать')
        self.assertEqual(data['confidence'], 72)
        self.assertEqual(data['why'], ['YTM 17% при дюрации 5,7'])

    def test_parses_fenced_json(self):
        text = '```json\n{"signal": "ждать", "confidence": 40, "why": []}\n```'
        self.assertEqual(ai_signals._parse(text)['signal'], 'ждать')

    def test_rejects_unknown_signal(self):
        self.assertIsNone(ai_signals._parse('{"signal": "может быть"}'))

    def test_clamps_confidence(self):
        self.assertEqual(ai_signals._parse('{"signal": "ждать", "confidence": 500}')['confidence'], 100)
        self.assertEqual(ai_signals._parse('{"signal": "ждать", "confidence": -5}')['confidence'], 0)

    def test_garbage(self):
        self.assertIsNone(ai_signals._parse('не JSON'))
        self.assertIsNone(ai_signals._parse(''))


class FindTests(unittest.TestCase):
    def test_finds_bond_stock_and_fx(self):
        self.assertEqual(ai_signals._find(SNAPSHOT, 'mtss')[1], 'stock')
        self.assertEqual(ai_signals._find(SNAPSHOT, 'SU26243RMFS4')[1], 'bond')
        self.assertEqual(ai_signals._find(SNAPSHOT, 'RU000A10C3M0')[1], 'fx')

    def test_missing(self):
        self.assertIsNone(ai_signals._find(SNAPSHOT, 'XXXX'))


class WatchlistTests(unittest.TestCase):
    def setUp(self):
        from app import db
        db.init_db()
        self._orig_snapshot = ai_signals._snapshot
        ai_signals._snapshot = lambda: SNAPSHOT
        for item in ai_signals.items():
            ai_signals.remove_item(item['code'])

    def tearDown(self):
        ai_signals._snapshot = self._orig_snapshot

    def test_add_and_remove(self):
        ok, msg = ai_signals.add_item('mtss')
        self.assertTrue(ok, msg)
        codes = [i['code'] for i in ai_signals.items()]
        self.assertEqual(codes, ['MTSS'])

        ai_signals.remove_item('MTSS')
        self.assertEqual(ai_signals.items(), [])

    def test_add_unknown_rejected(self):
        ok, msg = ai_signals.add_item('НЕТТАКОГО')
        self.assertFalse(ok)
        self.assertIn('не найден', msg)

    def test_limit(self):
        original = ai_signals.MAX_ITEMS
        ai_signals.MAX_ITEMS = 1
        try:
            ai_signals.add_item('MTSS')
            ok, msg = ai_signals.add_item('SU26243RMFS4')
            self.assertFalse(ok)
            self.assertIn('предел', msg)
        finally:
            ai_signals.MAX_ITEMS = original

    def test_generate_requires_llm(self):
        ai_signals.add_item('MTSS')
        ok, msg = ai_signals.generate_async()
        self.assertFalse(ok)
        self.assertIn('не настроена', msg)

    def test_generate_requires_items(self):
        ok, msg = ai_signals.generate_async()
        self.assertFalse(ok)


class IssFactsTests(unittest.TestCase):
    """Сигналы не должны зависеть от скринера: фьючерсы берём с MOEX ISS."""

    def setUp(self):
        self._orig = ai_signals._get_json

    def tearDown(self):
        ai_signals._get_json = self._orig

    def _fake(self, path, params=None):
        if path == '/securities/MTZ6.json':
            return {'description': {'columns': ['name', 'value'], 'data': [
                ['SECID', 'MTZ6'], ['SHORTNAME', 'MTSI-12.26'], ['TYPE', 'futures'],
                ['GROUP', 'futures_forts'], ['ASSETCODE', 'MTSI']]}}
        if path.startswith('/engines/futures/markets/forts/securities/MTZ6'):
            return {'marketdata': {'columns': ['LAST', 'SETTLEPRICE', 'VALTODAY',
                                               'NUMTRADES', 'OPENPOSITION'],
                                   'data': [[17552, 17552, 17683388, 337, 35980]]}}
        return None

    def test_futures_facts(self):
        ai_signals._get_json = self._fake
        row, kind = ai_signals._iss_facts('MTZ6')
        self.assertEqual(kind, 'future')
        self.assertEqual(row['price'], 17552)
        self.assertEqual(row['asset_code'], 'MTSI')
        self.assertEqual(row['open_position'], 35980)
        self.assertIn('2026-12', row['expiry'])       # дата из имени контракта

    def test_unknown_returns_none(self):
        ai_signals._get_json = lambda path, params=None: None
        self.assertIsNone(ai_signals._iss_facts('НЕТТАКОГО'))


class MarketContextTests(unittest.TestCase):
    def setUp(self):
        self._orig_hist = ai_signals._history
        self._orig_price = ai_signals.current_price

    def tearDown(self):
        ai_signals._history = self._orig_hist
        ai_signals.current_price = self._orig_price

    def test_stock_context_from_history(self):
        series = [(f'2026-01-{i:02d}', 100.0 + i) for i in range(1, 29)]
        series += [('2026-11-20', 150.0)]
        ai_signals._history = lambda code, days=400: series
        ai_signals.current_price = lambda code: 150.0
        ctx = ai_signals.market_context({'code': 'MTSS'}, 'stock')
        self.assertEqual(ctx['year_high'], 150.0)
        self.assertEqual(ctx['year_low'], 101.0)
        self.assertEqual(ctx['from_high_pct'], 0.0)
        self.assertEqual(ctx['last_price'], 150.0)

    def test_stale_history_ignored(self):
        # История «из 2011 года» не должна подменять текущую цену
        ai_signals._history = lambda code, days=400: [('2012-01-01', 80.0), ('2012-03-01', 83.0)]
        ai_signals.current_price = lambda code: 286.0
        ctx = ai_signals.market_context({'code': 'SBER'}, 'stock')
        self.assertEqual(ctx['last_price'], 286.0)
        self.assertNotIn('year_high', ctx)

    def test_bond_context_is_empty(self):
        self.assertEqual(ai_signals.market_context({'code': 'SU26243RMFS4'}, 'bond'),
                         {'today': ai_signals.market_context({'code': 'x'}, 'bond')['today']})


if __name__ == '__main__':
    unittest.main()
