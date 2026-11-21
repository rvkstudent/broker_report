"""Тесты рекомендательной системы: правила стратегии и разбор ответа LLM.

Запуск: python -m unittest tests.test_advisor -v
Сеть не используется: контекст портфеля — словарь, ответ нейросети — строка.
"""

import os
import tempfile
import unittest

_TMP_DIR = tempfile.mkdtemp(prefix='broker-advisor-test-')
os.environ.setdefault('BROKER_DB_PATH', os.path.join(_TMP_DIR, 'broker.db'))
os.environ.pop('ADVISOR_LLM_URL', None)
os.environ.pop('ADVISOR_LLM_KEY', None)
os.environ.pop('ADVISOR_LLM_MODEL', None)

from app import advisor  # noqa: E402


def _ctx(**kw):
    base = {
        'bond_value': 2_800_000, 'bond_share_pct': 78.0, 'fx_share_pct': 24.0,
        'risky_share_pct': 5.0, 'duration': 4.5, 'horizon': 15, 'gap': -10.5,
        'irr_net': 14.0, 'future': 25_000_000,
        'maturity_years': {'2029': 700_000, '2032': 840_000, '2038': 1_200_000},
        'rvi': {'available': True, 'current': 32.3, 'percentile': 57,
                'regime': 'норма', 'median': 31.0, 'p75': 38.0},
        'risk_weight_pct': 25.0,
        'kelly': {'full': -61.8, 'half': -30.9, 'positive': False},
        'monte_carlo': {'cagr_p10': 9.0, 'cagr_p50': 13.0, 'cagr_p90': 16.0,
                        'p_target': 0.0, 'equity_pct': 10.0},
        'bond_candidates': [{'code': 'RU1', 'name': 'ГТЛК 2P-13', 'ytm': 19.4,
                             'duration': 2.4, 'matdate': '2030-05-05',
                             'score': 81.2, 'flags': []}],
        'stock_candidates': [{'code': 'TRNFP', 'name': 'Транснефть', 'div12': 19.6,
                              'div3y': 18.6, 'pe': 0.71, 'score': 94.8,
                              'flags': ['P/E ниже 1 — проверить отчётность']}],
    }
    base.update(kw)
    return base


def _ids(rows):
    return [r['id'] for r in rows]


class RulesTests(unittest.TestCase):
    def test_immunization_flagged_when_duration_short(self):
        rows = advisor.rules(_ctx(gap=-10.5))
        rec = next(r for r in rows if r['id'] == 'immunization')
        self.assertEqual(rec['severity'], 'action')
        self.assertIn('удлинить', rec['action'].lower())

    def test_no_immunization_advice_when_matched(self):
        rows = advisor.rules(_ctx(gap=0.3, duration=14.7, horizon=15))
        self.assertNotIn('immunization', _ids(rows))

    def test_concentration_over_limit(self):
        rows = advisor.rules(_ctx(maturity_years={'2038': 1_200_000, '2032': 800_000,
                                                  '2029': 700_000}))
        rec = next(r for r in rows if r['id'] == 'concentration')
        self.assertIn('2038', rec['title'])

    def test_ladder_gaps(self):
        rows = advisor.rules(_ctx(maturity_years={'2029': 1, '2032': 1, '2038': 1}))
        self.assertIn('ladder_gaps', _ids(rows))

    def test_credit_risk_above_threshold(self):
        rows = advisor.rules(_ctx(risky_share_pct=22.0))
        self.assertIn('credit_risk', _ids(rows))

    def test_fx_low_and_high(self):
        low = advisor.rules(_ctx(fx_share_pct=3.0))
        self.assertIn('fx_low', _ids(low))
        high = advisor.rules(_ctx(fx_share_pct=45.0))
        self.assertIn('fx_high', _ids(high))

    def test_kelly_negative_note(self):
        rows = advisor.rules(_ctx(kelly={'full': -50, 'positive': False}))
        self.assertIn('kelly_negative', _ids(rows))

    def test_target_30_unreachable(self):
        rows = advisor.rules(_ctx(monte_carlo={'cagr_p10': 9, 'cagr_p50': 13,
                                               'cagr_p90': 16, 'p_target': 0.0}))
        rec = next(r for r in rows if r['id'] == 'target_30')
        self.assertIn('0.0', rec['title'])

    def test_candidates_reported(self):
        rows = advisor.rules(_ctx())
        self.assertIn('bond_candidate', _ids(rows))
        self.assertIn('stock_candidate', _ids(rows))

    def test_sorted_by_priority(self):
        rows = advisor.rules(_ctx())
        priorities = [r['priority'] for r in rows]
        self.assertEqual(priorities, sorted(priorities))


class LlmParsingTests(unittest.TestCase):
    def test_parses_plain_json(self):
        text = '[{"title":"Тест","why":"потому","action":"сделать","risk":"риск","severity":"action"}]'
        rows = advisor._parse_json(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Тест')
        self.assertEqual(rows[0]['severity'], 'action')
        self.assertEqual(rows[0]['source'], 'нейросеть')

    def test_parses_fenced_json(self):
        text = '```json\n[{"title":"В заборе","why":"w","action":"a"}]\n```'
        rows = advisor._parse_json(text)
        self.assertEqual(rows[0]['title'], 'В заборе')

    def test_rejects_garbage(self):
        self.assertIsNone(advisor._parse_json('извините, не могу'))
        self.assertIsNone(advisor._parse_json('[{"why": "нет title"}]'))

    def test_disabled_without_config(self):
        self.assertIsNone(advisor.llm_config())
        self.assertIsNone(advisor.llm_recommend(_ctx(), []))


if __name__ == '__main__':
    unittest.main()
