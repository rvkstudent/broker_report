"""Тесты выбора инструмента T-Invest для цен дашборда."""

import unittest

from app import dashboard_prices


class FakeClient:
    def __init__(self, instruments):
        self._instruments = instruments

    def find_instrument(self, query):
        return list(self._instruments)


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


if __name__ == '__main__':
    unittest.main()
