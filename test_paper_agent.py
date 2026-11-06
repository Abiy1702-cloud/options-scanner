import unittest

from paper_agent import RiskEngine, RiskLimits


class RiskEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(RiskLimits(
            risk_per_trade=0.01,
            daily_loss_limit=0.025,
            max_open_positions=5,
            max_entries_per_day=0,
            max_deployed_pct=0.90,
        ))

    def test_zero_daily_limit_means_no_arbitrary_entry_count(self):
        ok, reason, quantity = self.engine.assess(
            kill_switch=False, market_open=True, open_positions=0, entries_today=999,
            daily_pnl=0, equity=1000, deployed_value=0, available_cash=1000,
            entry=100, stop=98,
        )
        self.assertTrue(ok)
        self.assertEqual(reason, 'approved')
        self.assertGreater(quantity, 0)

    def test_daily_loss_circuit_breaker_rejects_new_entry(self):
        ok, reason, _ = self.engine.assess(
            kill_switch=False, market_open=True, open_positions=0, entries_today=0,
            daily_pnl=-25, equity=1000, deployed_value=0, available_cash=1000,
            entry=100, stop=98,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'daily loss limit reached')

    def test_deployment_cap_rejects_new_entry(self):
        ok, reason, _ = self.engine.assess(
            kill_switch=False, market_open=True, open_positions=1, entries_today=1,
            daily_pnl=0, equity=1000, deployed_value=900, available_cash=100,
            entry=100, stop=98,
        )
        self.assertFalse(ok)
        self.assertEqual(reason, 'capital deployment cap reached')


if __name__ == '__main__':
    unittest.main()
