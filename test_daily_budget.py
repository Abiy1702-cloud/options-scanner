import copy
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ['ENABLE_BACKGROUND_JOBS'] = 'false'
os.environ['STATE_FILE'] = os.path.join(tempfile.gettempdir(), 'options-scanner-test-state.json')
os.environ['JOURNAL_FILE'] = os.path.join(tempfile.gettempdir(), 'options-scanner-test-journal.jsonl')

import server


class DailyBudgetTests(unittest.TestCase):
    def setUp(self):
        self.original = copy.deepcopy(server.state)
        self.original_limit = server.MAX_DAILY_NOTIONAL
        server.state['capital'] = 1000.0
        server.state['trades'] = {}
        server.state['completed'] = []
        server.state['daily_pnl'] = 0.0
        server.state['risk'].update({
            'kill_switch': False, 'kill_reason': '', 'entries_today': 0,
            'realized_r_today': 0.0, 'daily_notional': 0.0,
        })
        server.MAX_DAILY_NOTIONAL = 151.0

    def tearDown(self):
        server.state.clear()
        server.state.update(self.original)
        server.MAX_DAILY_NOTIONAL = self.original_limit

    def test_daily_notional_blocks_second_entry_after_budget_is_used(self):
        first = {'symbol': 'BUDG', 'mode': 'day', 'entry': 100, 'stop': 99,
                 'target': 102, 'score': 70, 'reasons': []}
        second = {**first, 'symbol': 'NEXT'}
        with patch.object(server, 'market_open', return_value=True):
            ok, _ = server.enter_trade(first)
            self.assertTrue(ok)
            self.assertLessEqual(server.state['risk']['daily_notional'], 151.0)
            ok, message = server.enter_trade(second)
        self.assertFalse(ok)
        self.assertIn('Daily paper-trading budget', message)


if __name__ == '__main__':
    unittest.main()
