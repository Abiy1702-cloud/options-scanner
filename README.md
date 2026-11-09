# Options Scanner — paper agent MVP

This is a **paper-only** signal, risk, and simulated-execution service. It does
not import a broker trading SDK, submit broker orders, or hold broker credentials.
The current simulator uses the underlying stock price as an explicit
`underlying_equity_proxy`; it does not claim to model option-contract fills,
greeks, spreads, assignment, or early exercise.

## What changed in v7

- Entry selection rejects low-priced, thin, and parabolic momentum candidates,
  while treating incomplete daily volume as a score input rather than an
  intraday hard gate.
- Risk is 0.75% of equity per entry by default, with 20% per-position caps.
- The agent allows at most 5 open positions and up to 100% total simulated deployment.
  Daily entries are not arbitrarily capped; duplicate-position, re-entry,
  capital, and loss controls still apply.
- A hard $1,000 daily paper-notional budget limits the combined value of new
  simulated entries in one session. It includes the configured paper slippage
  and commission assumptions, and resets at the next trading session.
- A 2.5% daily loss limit and a 3R daily limit disable new entries.
- `POST /kill-switch` (PIN protected) disables all new paper entries. Existing
  simulated positions keep being monitored. The public dashboard deliberately
  has no manual scan, stop, entry, or close controls; it runs automatically.
- Each entry, exit, and kill-switch action is recorded in `trade_journal.jsonl`.
- `GET /risk` exposes the mode, current breaker state, and enforced limits.
- Candidate rows include a real, active Alpaca call contract when one is
  available in the selected strike and expiry window. This is read-only research
  data; the service does not paper-trade or submit option orders.
- The trader dashboard now provides an explainable market pulse (SPY/QQQ),
  deployment and daily-loss room, and a pre-entry simulated share/cost/risk plan
  for every candidate. The risk engine re-checks all values at the actual entry.
- Performance reporting includes expectancy, average R, maximum drawdown, win/loss
  streaks, and a breakdown of exits. These statistics are drawn only from closed
  simulated trades and should be evaluated over a broad sample, not a single day.
- Scans are serialized so a slow data download cannot overlap the next scheduled
  scan. Auto-entry refuses stale scan results.
- The service uses Alpaca's market clock when credentials are present, with a
  weekday/time fallback only when the clock is unavailable. This handles market
  holidays and early closes better than a hard-coded session window.
- Simulated entries and exits apply configurable adverse price slippage (5 bps by
  default) and optional per-share commissions. The dashboard identifies these as
  simulation assumptions, not actual broker fills.
- Background workers can be disabled with `ENABLE_BACKGROUND_JOBS=false` for
  safe local testing. The included unit tests cover the core risk-engine gates.

## Render setup

Deploy from this repository using `render.yaml`, or set the service command to:

`gunicorn --workers 1 --threads 4 --timeout 120 server:app`

Set secrets in Render (never commit them): `ALPACA_API_KEY`,
`ALPACA_SECRET_KEY`, `ABIY_PIN`, and optional Telegram credentials. Set
`PAPER_TRADING_ENABLED=true` to permit only simulated entries; setting it to
anything else starts with the kill switch engaged.

The included Render blueprint attaches a persistent disk at `/var/data` and
stores both state and journals there. Render's default filesystem is ephemeral,
so do not remove those two environment variables when deploying manually.

Useful non-secret settings in the blueprint are `AUTO_ENTRY_ENABLED`,
`SCAN_STALE_SECONDS`, `PAPER_SLIPPAGE_BPS`, `MAX_DAILY_NOTIONAL`, and
`PAPER_COMMISSION_PER_SHARE`. Do not set the Alpaca trading base URL to a live
endpoint in this application: it is intentionally read-only and paper-only.

## Test checklist

1. Open `/ping` and confirm `mode` is `PAPER_ONLY` and version is
   `v6-paper-agent`.
2. Open `/risk` and confirm the displayed limits before enabling entries.
3. Confirm the Daily Trading Budget card never exceeds $1,000 in accumulated
   paper entry value.
4. Let a stopped/targeted position close and review `exit_reason`, P&L, and
   the journal record. Do not evaluate strategy quality until at least 30–50
   closed paper trades across multiple market regimes.
5. Use the market pulse and paper plan as a review aid, not a forecast. Check
   that capital deployment, open-position risk, and the daily-loss circuit breaker
   remain within your comfort level before allowing new simulated entries.
6. Run `py -3 -m unittest -v test_paper_agent.py test_daily_budget.py` from the repository root.
   For a no-background-worker app smoke test, set
   `ENABLE_BACKGROUND_JOBS=false` first.

## Later broker integration

Keep broker execution in a separate adapter with a second explicit deployment
gate. The adapter must consume approved `OrderIntent` records only after this
paper journal shows a repeatable edge. It must not be added to this service by
changing a configuration value.
