"""Broker-independent paper-agent contracts.

Nothing in this module can transmit an order. A future broker adapter must be
implemented separately and consume a reviewed OrderIntent only.
"""
from dataclasses import dataclass

@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float
    daily_loss_limit: float
    max_open_positions: int
    max_entries_per_day: int
    max_deployed_pct: float

@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    mode: str
    entry: float
    stop: float
    target: float
    quantity: float
    execution: str = 'simulated'
    instrument: str = 'underlying_equity_proxy'

class RiskEngine:
    """Pure entry gate; no network, persistence, or broker side effects."""
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def assess(self, *, kill_switch, market_open, open_positions, entries_today,
               daily_pnl, equity, deployed_value, available_cash, entry, stop):
        if kill_switch: return False, 'kill switch is on', 0.0
        if not market_open: return False, 'market is closed', 0.0
        if open_positions >= self.limits.max_open_positions: return False, 'maximum concurrent positions reached', 0.0
        if entries_today >= self.limits.max_entries_per_day: return False, 'maximum daily entries reached', 0.0
        if daily_pnl <= -self.limits.daily_loss_limit * max(equity, 1): return False, 'daily loss limit reached', 0.0
        if entry <= stop: return False, 'invalid stop', 0.0
        if deployed_value >= equity * self.limits.max_deployed_pct: return False, 'capital deployment cap reached', 0.0
        risk_budget = equity * self.limits.risk_per_trade
        qty = risk_budget / (entry - stop)
        remaining = max(equity * self.limits.max_deployed_pct - deployed_value, 0)
        qty = min(qty, remaining / entry, available_cash / entry)
        return (True, 'approved', round(qty, 4)) if qty > 0 else (False, 'insufficient buying power', 0.0)
