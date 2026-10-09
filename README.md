# Options Scanner — paper agent MVP

This is a **paper-only** signal, risk, and simulated-execution service. It does
not import a broker trading SDK, submit broker orders, or hold broker credentials.
The current simulator uses the underlying stock price as an explicit
`underlying_equity_proxy`; it does not claim to model option-contract fills,
greeks, spreads, assignment, or early exercise.

## What changed in v6

- Entry selection rejects low-priced, thin, and parabolic momentum candidates.
- Risk is 0.5% of equity per entry by default, with 12–15% per-position caps.
- The agent allows at most 3 open positions, 3 new entries/day, and 45% total
  deployed capital.
- A 1.5% daily loss limit and a 3R daily limit disable new entries.
- `POST /kill-switch` (PIN protected) disables all new paper entries. Existing
  simulated positions keep being monitored and can be closed manually.
- Each entry, exit, and kill-switch action is recorded in `trade_journal.jsonl`.
- `GET /risk` exposes the mode, current breaker state, and enforced limits.

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

## Test checklist

1. Open `/ping` and confirm `mode` is `PAPER_ONLY` and version is
   `v6-paper-agent`.
2. Open `/risk` and confirm the displayed limits before enabling entries.
3. Use the dashboard PIN to track only a reviewed candidate; verify a matching
   `entry` record is written to the journal.
4. Set the kill switch with `POST /kill-switch` and confirm `/enter` returns a
   paper-entry-disabled message.
5. Let a stopped/targeted position close and review `exit_reason`, P&L, and
   the journal record. Do not evaluate strategy quality until at least 30–50
   closed paper trades across multiple market regimes.

## Later broker integration

Keep broker execution in a separate adapter with a second explicit deployment
gate. The adapter must consume approved `OrderIntent` records only after this
paper journal shows a repeatable edge. It must not be added to this service by
changing a configuration value.
