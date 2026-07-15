"""
Market Scanner Pro v5 — full rebuild
=====================================
- MARKET-WIDE scan: Alpaca movers/most-actives + Yahoo screeners (thousands of
  stocks, not a fixed watchlist), merged, filtered, deep-scored.
- Two modes: DAY candidates (momentum + RVOL + intraday confirmation) and
  SWING candidates (multi-day trend / breakout, held up to 10 trading days).
- Signal + simulated tracking only (no broker orders). Persistent capital —
  no daily reset, so records compound and history is kept.
- Telegram with real diagnostics: failures are logged, /telegram-test endpoint
  reports the exact error (bad token, wrong chat id, etc).
"""

import os, json, time, uuid, threading, urllib.request, urllib.error
from urllib.parse import urlencode
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request
import pytz

try:
    import yfinance as yf
    import numpy as np
    import pandas as pd
except Exception:
    yf = None; np = None; pd = None

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except Exception:
    BackgroundScheduler = None

try:
    import websocket as ws_client   # websocket-client package
except Exception:
    ws_client = None

# ── Config ────────────────────────────────────────────────────────────
PORT           = int(os.environ.get('PORT', 10000))
ALPACA_KEY     = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET  = os.environ.get('ALPACA_SECRET_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
GROQ_KEY       = os.environ.get('GROQ_API_KEY', '')
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
APP_URL        = os.environ.get('RENDER_EXTERNAL_URL', '')
PIN            = str(os.environ.get('ABIY_PIN', '1702'))
ET             = pytz.timezone('US/Eastern')
UA             = 'MarketScannerPro/5.0'

STARTING_CAPITAL = float(os.environ.get('STARTING_CAPITAL', 1000))
RISK_PER_TRADE   = 0.01     # 1% of equity risked per trade
DAY_POS_CAP      = 0.30     # max 30% of equity in one day trade
SWING_POS_CAP    = 0.25     # max 25% of equity in one swing trade
MIN_PRICE        = 2.0
MAX_PRICE        = 2000.0
MIN_DOLLAR_VOL   = 5e6      # $5M+ traded today = liquid enough
DAILY_LOSS_LIMIT = 0.03     # stop auto-trading if down 3% on the day
SWING_MAX_DAYS   = 10       # trading days a swing may be held

# ── Position sizing is now uncapped by count — no MAX_DAY_TRADES / ─────
# MAX_SWING_TRADES. Instead, "how many at once" is governed by capital:
MAX_DEPLOYED_PCT     = float(os.environ.get('MAX_DEPLOYED_PCT', 0.90))  # never deploy more than 90% of equity across all open positions
SYMBOL_COOLDOWN_SEC  = int(os.environ.get('SYMBOL_COOLDOWN_SEC', 900))  # 15 min before re-entering a symbol just exited (win or loss) — stops VEEE-style churn
MAX_ENTRIES_PER_SYMBOL_DAY = int(os.environ.get('MAX_ENTRIES_PER_SYMBOL_DAY', 2))  # hard cap on repeat entries into one symbol per day

ALPACA_DATA_FEED = os.environ.get('ALPACA_DATA_FEED', 'iex')   # 'iex' = free real-time feed, 'sip' = paid full-tape feed
# NOTE: this build is SIMULATED TRACKING ONLY — no live or paper orders are sent to
# Alpaca's Trading API. Real-money execution is a separate, deliberate next step.

# Core liquid names — fallback universe if all screeners fail
CORE = ['NVDA','TSLA','AMD','AAPL','MSFT','META','AMZN','GOOGL','PLTR','COIN',
        'HOOD','SOFI','MSTR','SMCI','AVGO','MU','ARM','APP','RKLB','HIMS',
        'SOXL','TQQQ','ASTS','SMR','UPST']

app = Flask(__name__, static_folder='.', static_url_path='')

# ── State (persistent) ────────────────────────────────────────────────
STATE_FILE = 'state.json'
state = {
    'capital': STARTING_CAPITAL,   # free cash
    'trades': {},                  # open positions (day + swing)
    'completed': [],               # closed positions — never wiped
    'equity_history': [],          # [{date, equity}] daily snapshots
    'logs': [],
    'daily_pnl': 0.0,
    'daily_date': '',
    'scan': {'day': [], 'swing': [], 'ts': 0, 'universe': 0},
    'prices': {}, 'price_ts': 0,
    'tg_status': {'ok': None, 'error': 'not tested yet', 't': ''},
    'symbol_cooldown': {},          # {symbol: last_exit_epoch}
    'symbol_entries': {'date': '', 'counts': {}},  # entries per symbol today
    'stream_status': {'connected': False, 'authed': False, 'error': '', 'last_tick': 0, 'symbols': 0},
}
_lock = threading.Lock()

def now_et():   return datetime.now(ET)
def today():    return now_et().strftime('%Y-%m-%d')
def now_str():  return now_et().strftime('%Y-%m-%d %H:%M ET')

def market_open():
    n = now_et(); t = n.hour * 60 + n.minute
    return n.weekday() < 5 and 570 <= t < 960          # 9:30–16:00

def save_state():
    try:
        with _lock:
            with open(STATE_FILE, 'w') as f:
                json.dump({
                    'capital': state['capital'],
                    'trades': state['trades'],
                    'completed': state['completed'][-500:],
                    'equity_history': state['equity_history'][-365:],
                    'daily_pnl': state['daily_pnl'],
                    'daily_date': state['daily_date'],
                }, f)
    except Exception as e:
        print(f'save_state: {e}')

def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        state['capital']        = d.get('capital', STARTING_CAPITAL)
        state['trades']         = d.get('trades', {})
        state['completed']      = d.get('completed', [])
        state['equity_history'] = d.get('equity_history', [])
        state['daily_pnl']      = d.get('daily_pnl', 0.0)
        state['daily_date']     = d.get('daily_date', '')
    except Exception:
        pass
    if state['daily_date'] != today():
        state['daily_date'] = today()
        state['daily_pnl']  = 0.0

def equity():
    """Cash + market value of open positions."""
    val = state['capital']
    for t in state['trades'].values():
        val += t.get('current', t['entry']) * t['shares']
    return round(val, 2)

# ── Telegram (with real diagnostics) ──────────────────────────────────
def tg(msg):
    """Send a Telegram message. Returns (ok, detail). Failures are recorded."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        missing = [n for n, v in [('TELEGRAM_TOKEN', TELEGRAM_TOKEN),
                                  ('TELEGRAM_CHAT_ID', TELEGRAM_CHAT)] if not v]
        err = f'Missing env var(s): {", ".join(missing)}'
        state['tg_status'] = {'ok': False, 'error': err, 't': now_str()}
        return False, err
    try:
        body = json.dumps({'chat_id': str(TELEGRAM_CHAT), 'text': str(msg)[:4000],
                           'disable_web_page_preview': True}).encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type': 'application/json', 'User-Agent': UA})
        urllib.request.urlopen(req, timeout=10)
        state['tg_status'] = {'ok': True, 'error': '', 't': now_str()}
        return True, 'sent'
    except urllib.error.HTTPError as e:
        try: detail = e.read().decode()[:300]
        except Exception: detail = ''
        hint = ''
        if e.code == 401: hint = ' → TELEGRAM_TOKEN is invalid. Get a new one from @BotFather.'
        if e.code == 400 and 'chat not found' in detail:
            hint = ' → TELEGRAM_CHAT_ID is wrong, or you never pressed Start on the bot.'
        if e.code == 403: hint = ' → The bot was blocked/removed. Open the bot and press Start.'
        err = f'HTTP {e.code}: {detail}{hint}'
        state['tg_status'] = {'ok': False, 'error': err, 't': now_str()}
        return False, err
    except Exception as e:
        err = f'{type(e).__name__}: {e}'
        state['tg_status'] = {'ok': False, 'error': err, 't': now_str()}
        return False, err

def log(msg, alert=False):
    state['logs'].insert(0, {'t': now_str(), 'msg': str(msg)})
    state['logs'] = state['logs'][:300]
    print(msg)
    if alert:
        ok, detail = tg(f'📡 Scanner\n{msg}')
        if not ok:
            state['logs'].insert(0, {'t': now_str(), 'msg': f'⚠️ Telegram failed: {detail}'})

# ── AI helper (optional) ──────────────────────────────────────────────
def ai_call(system, user, max_tokens=300):
    if GROQ_KEY:
        try:
            body = json.dumps({'model': 'llama-3.3-70b-versatile', 'max_tokens': max_tokens,
                               'messages': [{'role': 'system', 'content': system},
                                            {'role': 'user', 'content': user}]}).encode()
            req = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions',
                data=body, headers={'Authorization': f'Bearer {GROQ_KEY}',
                                    'Content-Type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            return r['choices'][0]['message']['content']
        except Exception: pass
    if ANTHROPIC_KEY:
        try:
            body = json.dumps({'model': 'claude-haiku-4-5-20251001', 'max_tokens': max_tokens,
                               'system': system,
                               'messages': [{'role': 'user', 'content': user}]}).encode()
            req = urllib.request.Request('https://api.anthropic.com/v1/messages',
                data=body, headers={'x-api-key': ANTHROPIC_KEY,
                                    'anthropic-version': '2023-06-01',
                                    'Content-Type': 'application/json'})
            r = json.loads(urllib.request.urlopen(req, timeout=15).read())
            return r['content'][0]['text']
        except Exception: pass
    return None

# ══════════════════════════════════════════════════════════════════════
# UNIVERSE — market-wide candidate gathering
# ══════════════════════════════════════════════════════════════════════
def _alpaca_get(path, params=None):
    if not ALPACA_KEY or not ALPACA_SECRET: return None
    try:
        url = f'https://data.alpaca.markets{path}'
        if params: url += '?' + urlencode(params)
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': ALPACA_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'User-Agent': UA})
        return json.loads(urllib.request.urlopen(req, timeout=12).read())
    except Exception as e:
        print(f'alpaca {path}: {e}')
        return None

def _clean_symbol(s):
    s = (s or '').upper().strip()
    if not s or len(s) > 5 or not s.isalpha(): return None
    return s

def gather_universe():
    """Collect candidate symbols from every available market-wide source.
    Returns dict {sym: {'pct': float|None, 'price': float|None, 'src': str}}"""
    cands = {}

    def add(sym, pct=None, price=None, src=''):
        sym = _clean_symbol(sym)
        if not sym: return
        cur = cands.setdefault(sym, {'pct': None, 'price': None, 'src': src})
        if pct is not None:   cur['pct'] = pct
        if price is not None: cur['price'] = price

    # 1. Alpaca top movers (real-time SIP)
    d = _alpaca_get('/v1beta1/screener/stocks/movers', {'top': 50})
    if d:
        for g in d.get('gainers', []):
            add(g.get('symbol'), g.get('percent_change'), g.get('price'), 'alpaca_gainers')

    # 2. Alpaca most-actives by volume
    d = _alpaca_get('/v1beta1/screener/stocks/most-actives', {'by': 'volume', 'top': 50})
    if d:
        for g in d.get('most_actives', []):
            add(g.get('symbol'), None, None, 'alpaca_active')

    # 3. Yahoo predefined screeners
    if yf:
        for scr in ('day_gainers', 'most_actives', 'small_cap_gainers'):
            try:
                r = yf.screen(scr, count=100)
                for q in (r or {}).get('quotes', []):
                    add(q.get('symbol'), q.get('regularMarketChangePercent'),
                        q.get('regularMarketPrice'), scr)
            except Exception as e:
                print(f'yf.screen {scr}: {e}')

    # 4. Core liquid names always considered
    for s in CORE: add(s, src='core')

    return cands

# ══════════════════════════════════════════════════════════════════════
# DEEP SCAN — score candidates on daily history
# ══════════════════════════════════════════════════════════════════════
def deep_scan():
    """Full pipeline: gather universe → filter → score day & swing setups."""
    if not yf or pd is None:
        return [], [], 0
    cands = gather_universe()
    universe_n = len(cands)
    if not cands:
        return [], [], 0

    # Rank raw candidates by % change; deep-scan top 45 + all core
    ranked = sorted(cands.items(), key=lambda kv: kv[1]['pct'] or 0, reverse=True)
    syms = [s for s, _ in ranked[:45]]
    for s in CORE:
        if s not in syms: syms.append(s)
    syms = syms[:70]

    day_list, swing_list = [], []
    try:
        df = yf.download(syms, period='1y', interval='1d', auto_adjust=True,
                         progress=False, group_by='ticker', threads=True)
    except Exception as e:
        print(f'deep_scan download: {e}')
        return [], [], universe_n

    multi = isinstance(df.columns, pd.MultiIndex)
    for sym in syms:
        try:
            if multi:
                sub = df[sym].dropna()
            else:
                sub = df.dropna()
            if len(sub) < 30: continue
            c = sub['Close'].values.astype(float)
            h = sub['High'].values.astype(float)
            l = sub['Low'].values.astype(float)
            v = sub['Volume'].values.astype(float)

            price = float(c[-1]); prev = float(c[-2])
            if not (MIN_PRICE <= price <= MAX_PRICE): continue

            pct_today = (price - prev) / prev * 100
            # Prefer real-time % from screener source when available
            live = cands.get(sym, {})
            if live.get('pct') is not None:
                lp = float(live['pct'])
                # yahoo returns %, alpaca returns % too
                if abs(lp) < 80: pct_today = lp
            if live.get('price'): price = float(live['price'])

            dollar_vol = float(v[-1]) * price
            if dollar_vol < MIN_DOLLAR_VOL: continue

            avg_vol = float(v[-21:-1].mean()) if len(v) >= 21 else float(v.mean())
            rvol = float(v[-1]) / avg_vol if avg_vol > 0 else 1.0

            # ATR(14)
            tr = np.maximum(h[1:] - l[1:],
                 np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
            atr = float(tr[-14:].mean())

            sma20 = float(c[-20:].mean())
            sma50 = float(c[-50:].mean()) if len(c) >= 50 else sma20
            hi52  = float(h.max())
            chg5  = (price - float(c[-6]))  / float(c[-6])  * 100 if len(c) >= 6  else 0
            chg20 = (price - float(c[-21])) / float(c[-21]) * 100 if len(c) >= 21 else 0
            near_hi   = price >= hi52 * 0.95
            breakout  = price >= float(h[-21:-1].max())          # new 20-day high
            uptrend   = price > sma20 > sma50
            pullback  = uptrend and abs(price - sma20) / sma20 < 0.03 and pct_today > -1

            base = {
                'symbol': sym, 'price': round(price, 2),
                'pct_today': round(pct_today, 2), 'rvol': round(rvol, 1),
                'atr': round(atr, 2), 'chg5d': round(chg5, 1), 'chg20d': round(chg20, 1),
                'near_52w_hi': near_hi, 'uptrend': uptrend,
                'dollar_vol_m': round(dollar_vol / 1e6, 1),
            }

            # ── DAY score ────────────────────────────────────────────
            ds, dr = 0, []
            if   pct_today >= 8: ds += 30; dr.append(f'+{pct_today:.1f}% today 🔥')
            elif pct_today >= 4: ds += 22; dr.append(f'+{pct_today:.1f}% today')
            elif pct_today >= 2: ds += 14; dr.append(f'+{pct_today:.1f}% today')
            elif pct_today >= 1: ds += 7
            if   rvol >= 5: ds += 25; dr.append(f'RVOL {rvol:.1f}x 🔥')
            elif rvol >= 3: ds += 18; dr.append(f'RVOL {rvol:.1f}x')
            elif rvol >= 2: ds += 12; dr.append(f'RVOL {rvol:.1f}x')
            elif rvol >= 1.5: ds += 6
            if near_hi:  ds += 10; dr.append('near 52w high')
            if uptrend:  ds += 10; dr.append('uptrend')
            if breakout: ds += 10; dr.append('20d breakout')
            if dollar_vol > 5e7: ds += 5
            if ds >= 35 and pct_today > 0.5:
                stop   = round(max(price - 0.8 * atr, price * 0.975), 2)
                risk   = price - stop
                target = round(price + 2.0 * risk, 2)
                day_list.append({**base, 'mode': 'day', 'score': ds, 'reasons': dr,
                                 'entry': round(price, 2), 'stop': stop, 'target': target})

            # ── SWING score ──────────────────────────────────────────
            ss, sr = 0, []
            if   chg20 >= 30: ss += 25; sr.append(f'+{chg20:.0f}% in 20d 🔥')
            elif chg20 >= 15: ss += 18; sr.append(f'+{chg20:.0f}% in 20d')
            elif chg20 >= 8:  ss += 10; sr.append(f'+{chg20:.0f}% in 20d')
            if   chg5 >= 10:  ss += 10; sr.append(f'+{chg5:.0f}% in 5d')
            elif chg5 >= 5:   ss += 6
            if uptrend:  ss += 15; sr.append('price>20SMA>50SMA')
            if near_hi:  ss += 12; sr.append('near 52w high')
            if breakout: ss += 12; sr.append('20d breakout')
            if pullback: ss += 10; sr.append('pullback to 20SMA')
            if rvol >= 2: ss += 8; sr.append(f'RVOL {rvol:.1f}x')
            if dollar_vol > 5e7: ss += 5
            if ss >= 40:
                stop   = round(max(price - 1.5 * atr, price * 0.93), 2)
                risk   = price - stop
                target = round(price + 2.5 * risk, 2)
                swing_list.append({**base, 'mode': 'swing', 'score': ss, 'reasons': sr,
                                   'entry': round(price, 2), 'stop': stop, 'target': target})
        except Exception:
            continue

    day_list.sort(key=lambda x: x['score'], reverse=True)
    swing_list.sort(key=lambda x: x['score'], reverse=True)
    return day_list[:12], swing_list[:12], universe_n

def run_scan(announce=False):
    day, swing, n = deep_scan()
    state['scan'] = {'day': day, 'swing': swing, 'ts': time.time(), 'universe': n}
    stream_set_symbols()
    log(f'Scan done: universe {n} → {len(day)} day / {len(swing)} swing candidates')
    if announce and (day or swing):
        msg = f'🔎 Scan {now_str()} — universe {n} stocks\n'
        if day:
            msg += '\n📅 DAY picks:\n'
            for x in day[:4]:
                msg += f"• {x['symbol']} ${x['price']} ({x['pct_today']:+.1f}%) score {x['score']} — {', '.join(x['reasons'][:2])}\n"
        if swing:
            msg += '\n📈 SWING picks:\n'
            for x in swing[:4]:
                msg += f"• {x['symbol']} ${x['price']} score {x['score']} — {', '.join(x['reasons'][:2])}\n"
        tg(msg)

# ══════════════════════════════════════════════════════════════════════
# INTRADAY TECHNICALS (confirmation for day entries + analyze box)
# ══════════════════════════════════════════════════════════════════════
def compute_technicals(sym):
    if not yf or np is None: return None
    try:
        df = yf.download(sym, period='1d', interval='5m', auto_adjust=True, progress=False)
        if df is None or len(df) < 8: return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        c = df['Close'].dropna().values.astype(float)
        h = df['High'].dropna().values.astype(float)
        l = df['Low'].dropna().values.astype(float)
        v = df['Volume'].dropna().values.astype(float)
        price = float(c[-1])
        tp = (h + l + c) / 3
        vwap = float((tp * v).sum() / v.sum()) if v.sum() > 0 else price

        def ema(arr, n):
            out = np.zeros_like(arr); out[0] = arr[0]; k = 2 / (n + 1)
            for i in range(1, len(arr)): out[i] = arr[i] * k + out[i-1] * (1-k)
            return out
        ema9, ema20 = float(ema(c, 9)[-1]), float(ema(c, 20)[-1])
        delta = np.diff(c)
        gain = np.where(delta > 0, delta, 0); loss = np.where(delta < 0, -delta, 0)
        ag = gain[-14:].mean() if len(gain) >= 14 else gain.mean()
        al = loss[-14:].mean() if len(loss) >= 14 else loss.mean()
        rsi = 100 - 100 / (1 + ag / al) if al > 0 else 50
        macd_bull = float(ema(c, 12)[-1]) - float(ema(c, 26)[-1] if len(c) >= 26 else ema(c, 12)[-1]) > 0

        above_vwap = price > vwap
        bull = sum([above_vwap, ema9 > ema20, macd_bull, 35 < rsi < 70])
        signal = 'BUY' if (bull >= 3 and above_vwap and rsi < 70) else \
                 ('SELL' if (bull <= 1 or rsi > 78) else 'WAIT')
        return {'signal': signal, 'price': round(price, 2), 'vwap': round(vwap, 2),
                'ema9': round(ema9, 2), 'ema20': round(ema20, 2), 'rsi': round(rsi, 1),
                'macd_bull': macd_bull, 'above_vwap': above_vwap, 'bull_pts': bull,
                'support': round(float(l[-12:].min()), 2),
                'resistance': round(float(h[-12:].max()), 2), 'bars': len(c)}
    except Exception as e:
        print(f'technicals {sym}: {e}')
        return None

# ══════════════════════════════════════════════════════════════════════
# PRICES for open positions + indices
# ══════════════════════════════════════════════════════════════════════
def refresh_prices():
    syms = {t['symbol'] for t in state['trades'].values()} | {'SPY', 'QQQ'}
    prices = {}
    # Alpaca snapshots first (real-time)
    if ALPACA_KEY:
        d = _alpaca_get('/v2/stocks/snapshots', {'symbols': ','.join(sorted(syms))})
        if d:
            for sym, s in d.items():
                try:
                    lt = (s.get('latestTrade') or {}).get('p') or \
                         (s.get('minuteBar') or {}).get('c')
                    db = s.get('dailyBar') or {}
                    if lt:
                        o = db.get('o') or lt
                        prices[sym] = {'price': round(float(lt), 2),
                                       'pct': round((float(lt) - o) / o * 100, 2) if o else 0}
                except Exception: pass
    # yfinance for anything missing
    missing = syms - set(prices)
    if missing and yf:
        try:
            df = yf.download(list(missing), period='2d', interval='1d',
                             auto_adjust=True, progress=False, group_by='ticker')
            multi = isinstance(df.columns, pd.MultiIndex)
            for sym in missing:
                try:
                    cl = (df[sym]['Close'] if multi else df['Close']).dropna()
                    p = float(cl.iloc[-1]); pv = float(cl.iloc[-2]) if len(cl) > 1 else p
                    prices[sym] = {'price': round(p, 2),
                                   'pct': round((p - pv) / pv * 100, 2) if pv else 0}
                except Exception: pass
        except Exception: pass
    if prices:
        state['prices'].update(prices)
        state['price_ts'] = time.time()

def cp(sym): return state['prices'].get(sym)

# ══════════════════════════════════════════════════════════════════════
# REAL-TIME STREAM — Alpaca market-data WebSocket (push prices, no polling)
# Replaces "wait up to a minute for a price" with sub-second trade ticks for
# every open position + current scan candidates. Falls back silently to the
# existing refresh_prices() polling if no Alpaca keys are configured.
# ══════════════════════════════════════════════════════════════════════
_stream_lock = threading.Lock()
_stream_ws = None
_stream_subs = set()
_stream_authed = False

def _stream_symbols_wanted():
    syms = {t['symbol'] for t in state['trades'].values()}
    for c in state['scan'].get('day', [])[:15]:   syms.add(c['symbol'])
    for c in state['scan'].get('swing', [])[:15]: syms.add(c['symbol'])
    syms |= {'SPY', 'QQQ'}
    return syms

def stream_set_symbols():
    """Diff current vs. wanted subscriptions and (un)subscribe on the open socket."""
    if not (ws_client and ALPACA_KEY and ALPACA_SECRET): return
    wanted = _stream_symbols_wanted()
    with _stream_lock:
        if not (_stream_ws and _stream_authed): return
        add = wanted - _stream_subs
        rem = _stream_subs - wanted
        try:
            if add:
                _stream_ws.send(json.dumps({'action': 'subscribe', 'trades': sorted(add)}))
                _stream_subs.update(add)
            if rem:
                _stream_ws.send(json.dumps({'action': 'unsubscribe', 'trades': sorted(rem)}))
                _stream_subs.difference_update(rem)
            state['stream_status']['symbols'] = len(_stream_subs)
        except Exception as e:
            print(f'stream_set_symbols: {e}')

def _on_stream_open(wsapp):
    global _stream_authed
    _stream_authed = False
    wsapp.send(json.dumps({'action': 'auth', 'key': ALPACA_KEY, 'secret': ALPACA_SECRET}))

def _on_stream_message(wsapp, message):
    global _stream_authed
    try:
        msgs = json.loads(message)
    except Exception:
        return
    for m in msgs:
        t = m.get('T')
        if t == 'success' and m.get('msg') == 'authenticated':
            _stream_authed = True
            state['stream_status'].update({'connected': True, 'authed': True, 'error': ''})
            stream_set_symbols()
        elif t == 'error':
            state['stream_status']['error'] = f"{m.get('code')}: {m.get('msg')}"
            print(f'stream error: {m}')
        elif t == 't':   # trade tick: {"T":"t","S":"AAPL","p":190.12,...}
            sym, price = m.get('S'), m.get('p')
            if not sym or price is None: continue
            prev = state['prices'].get(sym, {})
            state['prices'][sym] = {'price': round(float(price), 2), 'pct': prev.get('pct', 0)}
            state['price_ts'] = time.time()
            state['stream_status']['last_tick'] = time.time()
            if any(tr['symbol'] == sym for tr in state['trades'].values()):
                try: monitor_trades()
                except Exception as e: print(f'monitor on tick: {e}')

def _on_stream_error(wsapp, error):
    state['stream_status'].update({'connected': False, 'authed': False, 'error': str(error)})

def _on_stream_close(wsapp, *a):
    global _stream_authed, _stream_subs
    _stream_authed = False
    _stream_subs = set()
    state['stream_status'].update({'connected': False, 'authed': False})

def start_stream():
    """Open the Alpaca real-time trade stream in a background thread with
    auto-reconnect. No-ops safely if Alpaca keys aren't configured."""
    global _stream_ws
    if not (ws_client and ALPACA_KEY and ALPACA_SECRET):
        log('Realtime stream: no Alpaca keys set — using price polling instead')
        return
    url = f'wss://stream.data.alpaca.markets/v2/{ALPACA_DATA_FEED}'
    def _run():
        global _stream_ws
        backoff = 2
        while True:
            try:
                _stream_ws = ws_client.WebSocketApp(
                    url, on_open=_on_stream_open, on_message=_on_stream_message,
                    on_error=_on_stream_error, on_close=_on_stream_close)
                _stream_ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f'stream loop: {e}')
            state['stream_status']['connected'] = False
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
    threading.Thread(target=_run, daemon=True).start()
    log(f'Realtime stream: connecting ({ALPACA_DATA_FEED} feed)…')

# ══════════════════════════════════════════════════════════════════════
# TRADES (simulated tracking)
# ══════════════════════════════════════════════════════════════════════
def open_count(mode):
    return sum(1 for t in state['trades'].values() if t['mode'] == mode)

def deployed_value():
    """Market value currently tied up in open positions (cost basis)."""
    return sum(t.get('cost', t['entry'] * t['shares']) for t in state['trades'].values())

def _reset_symbol_entries_if_new_day():
    se = state['symbol_entries']
    if se.get('date') != today():
        se['date'] = today()
        se['counts'] = {}

def enter_trade(cand, manual=False):
    sym, mode = cand['symbol'], cand.get('mode', 'day')
    if any(t['symbol'] == sym for t in state['trades'].values()):
        return False, f'Already holding {sym}'

    if not manual:
        # No re-entry same symbol same day after a loss (revenge-trade guard)
        for t in state['completed'][-30:]:
            if t['symbol'] == sym and t.get('exit_time', '').startswith(today()) and t.get('pnl', 0) <= 0:
                return False, f'{sym} lost earlier today — skipping'
        # Cooldown after ANY exit (win or loss) — stops rapid-fire re-entry into the same
        # volatile name (e.g. entering/exiting VEEE nine times in 40 minutes)
        last_exit = state['symbol_cooldown'].get(sym)
        if last_exit and (time.time() - last_exit) < SYMBOL_COOLDOWN_SEC:
            wait = int(SYMBOL_COOLDOWN_SEC - (time.time() - last_exit))
            return False, f'{sym} on cooldown ({wait}s left)'
        # Hard cap on repeat entries into one symbol per day, regardless of cooldown
        _reset_symbol_entries_if_new_day()
        if state['symbol_entries']['counts'].get(sym, 0) >= MAX_ENTRIES_PER_SYMBOL_DAY:
            return False, f'{sym} already traded {MAX_ENTRIES_PER_SYMBOL_DAY}x today — skipping'
        # Daily loss circuit-breaker
        if state['daily_pnl'] <= -DAILY_LOSS_LIMIT * equity():
            return False, 'Daily loss limit hit — auto-entries paused until tomorrow'

    price  = float(cand['entry'])
    stop   = float(cand['stop'])
    target = float(cand['target'])
    if price <= stop: return False, 'Bad stop'

    eq = equity()
    # No hard count limit on concurrent positions — capital does the limiting instead
    if deployed_value() >= eq * MAX_DEPLOYED_PCT:
        return False, f'Deployed capital at {MAX_DEPLOYED_PCT*100:.0f}% cap — no new entries until something closes'

    risk_dollars = eq * RISK_PER_TRADE
    shares = risk_dollars / (price - stop)
    cap = eq * (DAY_POS_CAP if mode == 'day' else SWING_POS_CAP)
    shares = min(shares, cap / price)
    room = max(eq * MAX_DEPLOYED_PCT - deployed_value(), 0)
    shares = min(shares, room / price)
    shares = round(shares, 4)
    cost = round(shares * price, 2)
    if shares <= 0 or cost > state['capital']:
        return False, f'Insufficient free cash (${state["capital"]:.0f})'

    tid = str(uuid.uuid4())[:8]
    state['trades'][tid] = {
        'id': tid, 'symbol': sym, 'mode': mode, 'entry': round(price, 2),
        'shares': shares, 'cost': cost, 'stop': stop, 'target': target,
        'current': round(price, 2), 'peak': round(price, 2),
        'pnl': 0.0, 'pnl_pct': 0.0,
        'entry_time': now_str(), 'entry_date': today(), 'entered_at': time.time(),
        'manual': manual, 'score': cand.get('score', 0),
        'reasons': cand.get('reasons', []),
    }
    state['capital'] = round(state['capital'] - cost, 2)
    _reset_symbol_entries_if_new_day()
    state['symbol_entries']['counts'][sym] = state['symbol_entries']['counts'].get(sym, 0) + 1
    save_state()
    stream_set_symbols()
    rr = round((target - price) / (price - stop), 1)
    log(f"ENTER {mode.upper()} {sym} {shares}sh @${price:.2f}\n"
        f"Stop ${stop:.2f} | Target ${target:.2f} | R:R {rr}:1 | Risk ${shares*(price-stop):.2f}\n"
        f"Why: {', '.join(cand.get('reasons', [])[:3])}", alert=True)
    return True, f'Entered {sym} ({mode})'

def close_trade(tid, reason):
    t = state['trades'].pop(tid, None)
    if not t: return
    q = cp(t['symbol'])
    exit_price = q['price'] if q else t['current']
    pnl = round((exit_price - t['entry']) * t['shares'], 2)
    pnl_pct = round((exit_price - t['entry']) / t['entry'] * 100, 2)
    state['capital'] = round(state['capital'] + exit_price * t['shares'], 2)
    state['daily_pnl'] = round(state['daily_pnl'] + pnl, 2)
    label = {'target': '🎯 TARGET', 'stop': '🛑 STOP', 'trail': '📈 TRAIL',
             'eod': '🌙 EOD', 'time': '⏰ TIME EXIT', 'manual': '👤 MANUAL'}.get(reason, reason)
    rec = {**t, 'exit_price': round(exit_price, 2), 'exit_reason': reason,
           'exit_time': now_str(), 'exit_date': today(), 'pnl': pnl, 'pnl_pct': pnl_pct}
    state['completed'].append(rec)
    state['symbol_cooldown'][t['symbol']] = time.time()
    save_state()
    stream_set_symbols()
    log(f"CLOSE {t['mode'].upper()} {t['symbol']} {label} ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
        f"${t['entry']:.2f} → ${exit_price:.2f} | Day P&L ${state['daily_pnl']:+.2f} | Equity ${equity():.2f}",
        alert=True)

def monitor_trades():
    if not state['trades']: return
    for tid, t in list(state['trades'].items()):
        q = cp(t['symbol'])
        if not q: continue
        price = q['price']
        entry, mode = t['entry'], t['mode']
        peak = max(price, t.get('peak', price))
        pnl_pct = (price - entry) / entry * 100
        peak_pct = (peak - entry) / entry * 100
        t.update({'current': round(price, 2), 'peak': round(peak, 2),
                  'pnl': round((price - entry) * t['shares'], 2),
                  'pnl_pct': round(pnl_pct, 2)})

        if price >= t['target']: close_trade(tid, 'target'); continue
        if price <= t['stop']:   close_trade(tid, 'stop');   continue

        if mode == 'day':
            if peak_pct >= 5 and price <= peak * 0.97:
                close_trade(tid, 'trail'); continue
            if peak_pct >= 3:
                t['stop'] = max(t['stop'], round(peak * 0.985, 2))
            # EOD close for day trades
            n = now_et()
            if market_open() and (16 * 60 - (n.hour * 60 + n.minute)) <= 10:
                close_trade(tid, 'eod'); continue
        else:  # swing
            if peak_pct >= 12 and price <= peak * 0.94:
                close_trade(tid, 'trail'); continue
            if peak_pct >= 8:
                t['stop'] = max(t['stop'], round(entry * 1.01, 2))  # lock breakeven+
            # Time exit after SWING_MAX_DAYS trading days
            held_days = (time.time() - t.get('entered_at', time.time())) / 86400
            if held_days >= SWING_MAX_DAYS * 1.5:   # calendar approximation
                close_trade(tid, 'time'); continue

def auto_entry():
    """Enter top-scored candidates automatically during market hours."""
    if not market_open(): return
    n = now_et(); mins = n.hour * 60 + n.minute
    if mins < 575: return                      # skip first 5 minutes
    scan = state['scan']
    # No hard position-count limit — enter_trade() itself stops new entries once
    # deployed capital hits MAX_DEPLOYED_PCT of equity, so we just offer every
    # qualifying candidate and let the capital math do the gating.
    # DAY entries: 9:35–15:00, need intraday confirmation
    if mins <= 900:
        for c in scan.get('day', []):
            if deployed_value() >= equity() * MAX_DEPLOYED_PCT: break
            if c['score'] < 60: continue
            if any(t['symbol'] == c['symbol'] for t in state['trades'].values()): continue
            tech = compute_technicals(c['symbol'])
            if not tech or tech['signal'] != 'BUY': continue
            fresh = {**c, 'entry': tech['price'],
                     'stop': round(max(tech['price'] - (c['entry'] - c['stop']),
                                       tech.get('vwap', tech['price'] * 0.98) * 0.995), 2)}
            fresh['target'] = round(tech['price'] + 2 * (tech['price'] - fresh['stop']), 2)
            if fresh['entry'] <= fresh['stop']: continue
            enter_trade(fresh)
    # SWING entries: 9:35–15:45, daily setup is enough
    if mins <= 945:
        for c in scan.get('swing', []):
            if deployed_value() >= equity() * MAX_DEPLOYED_PCT: break
            if c['score'] < 65: continue
            if any(t['symbol'] == c['symbol'] for t in state['trades'].values()): continue
            enter_trade(c)

# ══════════════════════════════════════════════════════════════════════
# CANDLES (for the UI chart)
# ══════════════════════════════════════════════════════════════════════
def get_candles(sym, period='1d', interval='5m'):
    if not yf: return []
    try:
        df = yf.download(sym, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        if df is None or len(df) == 0: return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        bars = []
        for idx, row in df.iterrows():
            try:
                dt = idx.astimezone(ET) if hasattr(idx, 'astimezone') else idx
                bars.append({'t': str(dt),
                             'hm': dt.strftime('%m/%d %H:%M') if hasattr(dt, 'strftime') else str(dt),
                             'o': round(float(row['Open']), 2), 'h': round(float(row['High']), 2),
                             'l': round(float(row['Low']), 2), 'c': round(float(row['Close']), 2),
                             'v': int(row['Volume']) if not pd.isna(row['Volume']) else 0})
            except Exception: pass
        return bars
    except Exception:
        return []

# ══════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════
def _cors(data, status=200):
    r = jsonify(data); r.headers['Access-Control-Allow-Origin'] = '*'
    r.status_code = status
    return r

PUBLIC = {'/', '/ping', '/scan', '/trades', '/performance', '/logs',
          '/candles', '/technicals', '/analysis-result', '/prices'}

@app.before_request
def auth():
    if request.method == 'OPTIONS': return _cors({})
    if request.path in PUBLIC or request.path.startswith('/static'): return
    pin = request.headers.get('X-PIN') or request.args.get('pin') or ''
    if not pin and request.is_json:
        pin = (request.get_json(silent=True) or {}).get('pin', '')
    if str(pin) != PIN:
        return _cors({'locked': True, 'ok': False, 'msg': 'PIN required'}, 403)

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/ping')
def ping():
    return _cors({'ok': True, 'time': now_str(), 'market': market_open(),
                  'version': 'v5', 'tg': state['tg_status'],
                  'stream': state['stream_status']})

@app.route('/scan')
def scan_route():
    return _cors({**state['scan'], 'market_open': market_open()})

@app.route('/rescan', methods=['POST'])
def rescan():
    threading.Thread(target=lambda: run_scan(announce=False), daemon=True).start()
    return _cors({'ok': True, 'msg': 'Scan started — refresh in ~30s'})

@app.route('/prices')
def prices_route():
    return _cors({'data': state['prices'], 'ts': state['price_ts']})

@app.route('/trades')
def trades_route():
    return _cors({'trades': list(state['trades'].values()),
                  'capital': state['capital'], 'equity': equity(),
                  'daily_pnl': state['daily_pnl'],
                  'limits': {'max_deployed_pct': MAX_DEPLOYED_PCT,
                             'deployed_pct': round(deployed_value() / equity() * 100, 1) if equity() else 0}})

@app.route('/performance')
def performance():
    c = state['completed']
    wins = [t for t in c if t.get('pnl', 0) > 0]
    losses = [t for t in c if t.get('pnl', 0) <= 0]
    by_day, by_mode = {}, {'day': 0.0, 'swing': 0.0}
    for t in c:
        d = t.get('exit_date') or (t.get('exit_time', '')[:10])
        by_day[d] = round(by_day.get(d, 0) + t.get('pnl', 0), 2)
        by_mode[t.get('mode', 'day')] = round(by_mode.get(t.get('mode', 'day'), 0) + t.get('pnl', 0), 2)
    gross_w = sum(t['pnl'] for t in wins); gross_l = abs(sum(t['pnl'] for t in losses))
    return _cors({
        'trades': len(c), 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(len(wins) / len(c) * 100, 1) if c else 0,
        'total_pnl': round(sum(t.get('pnl', 0) for t in c), 2),
        'avg_win': round(gross_w / len(wins), 2) if wins else 0,
        'avg_loss': round(-gross_l / len(losses), 2) if losses else 0,
        'profit_factor': round(gross_w / gross_l, 2) if gross_l > 0 else None,
        'best': max((t.get('pnl', 0) for t in c), default=0),
        'worst': min((t.get('pnl', 0) for t in c), default=0),
        'by_mode': by_mode, 'by_day': by_day,
        'capital': state['capital'], 'equity': equity(),
        'starting_capital': STARTING_CAPITAL,
        'daily_pnl': state['daily_pnl'],
        'equity_history': state['equity_history'][-120:],
        'history': list(reversed(c[-100:])),
    })

@app.route('/logs')
def logs_route(): return _cors({'logs': state['logs'][:80]})

@app.route('/candles')
def candles_route():
    sym = request.args.get('symbol', 'SPY').upper()
    return _cors({'symbol': sym,
                  'bars': get_candles(sym, request.args.get('period', '1d'),
                                      request.args.get('interval', '5m'))})

@app.route('/technicals')
def technicals_route():
    sym = request.args.get('symbol', 'SPY').upper()
    return _cors({'symbol': sym, 'technicals': compute_technicals(sym)})

_analysis = {}
@app.route('/analyze', methods=['POST'])
def analyze():
    sym = (request.get_json(silent=True) or {}).get('symbol', '').upper().strip()
    if not sym: return _cors({'error': 'No symbol'}, 400)
    def run():
        tech = compute_technicals(sym)
        bars = get_candles(sym, '5d', '15m')
        ai = ''
        if tech:
            ai = ai_call('You are a disciplined professional trader. Be concise and honest about risk.',
                         f"Symbol {sym} | Price ${tech['price']} | Signal {tech['signal']} | "
                         f"RSI {tech['rsi']} | VWAP ${tech['vwap']} | Above VWAP: {tech['above_vwap']} | "
                         f"Support ${tech['support']} | Resistance ${tech['resistance']}\n"
                         '4 lines: 1) BUY/WAIT/AVOID 2) Entry/Stop/Target 3) Key reason 4) Main risk',
                         200) or ''
        _analysis[sym] = {'done': True, 'tech': tech, 'bars': bars, 'ai': ai}
    _analysis[sym] = {'done': False}
    threading.Thread(target=run, daemon=True).start()
    return _cors({'ok': True, 'symbol': sym})

@app.route('/analysis-result')
def analysis_result():
    sym = request.args.get('symbol', '').upper()
    r = _analysis.get(sym, {})
    if not r.get('done'): return _cors({'ready': False})
    return _cors({'ready': True, 'symbol': sym, **{k: r[k] for k in ('tech', 'bars', 'ai')}})

@app.route('/enter', methods=['POST'])
def enter_route():
    d = request.get_json(silent=True) or {}
    sym = d.get('symbol', '').upper().strip()
    mode = d.get('mode', 'day')
    if not sym: return _cors({'ok': False, 'msg': 'No symbol'}, 400)
    # Use scan data if we have it, else build from technicals
    cand = next((c for c in state['scan'].get(mode, []) if c['symbol'] == sym), None)
    if not cand:
        tech = compute_technicals(sym)
        if not tech: return _cors({'ok': False, 'msg': f'No data for {sym}'})
        p = tech['price']
        stop = round(p * (0.975 if mode == 'day' else 0.93), 2)
        cand = {'symbol': sym, 'mode': mode, 'entry': p, 'stop': stop,
                'target': round(p + (2 if mode == 'day' else 2.5) * (p - stop), 2),
                'score': 0, 'reasons': ['manual']}
    refresh_prices()
    ok, msg = enter_trade(cand, manual=True)
    return _cors({'ok': ok, 'msg': msg})

@app.route('/close', methods=['POST'])
def close_route():
    tid = (request.get_json(silent=True) or {}).get('tid', '')
    if tid in state['trades']:
        refresh_prices()
        close_trade(tid, 'manual')
        return _cors({'ok': True})
    return _cors({'ok': False, 'msg': 'Trade not found'}, 404)

@app.route('/telegram-test', methods=['POST'])
def telegram_test():
    """Full Telegram diagnostic."""
    diag = {'token_set': bool(TELEGRAM_TOKEN), 'chat_id_set': bool(TELEGRAM_CHAT)}
    if TELEGRAM_TOKEN:
        try:
            r = json.loads(urllib.request.urlopen(urllib.request.Request(
                f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe',
                headers={'User-Agent': UA}), timeout=10).read())
            diag['bot'] = r.get('result', {}).get('username', '?')
            diag['token_valid'] = True
        except urllib.error.HTTPError as e:
            diag['token_valid'] = False
            diag['token_error'] = f'HTTP {e.code} — token is invalid. Create a new one with @BotFather and update TELEGRAM_TOKEN on Render.'
        except Exception as e:
            diag['token_valid'] = False
            diag['token_error'] = str(e)
    ok, detail = tg(f'✅ Test message from Market Scanner Pro — {now_str()}')
    diag['send_ok'] = ok
    diag['send_detail'] = detail
    return _cors({'ok': ok, 'diag': diag})

# ══════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════
def job_premarket():
    if now_et().weekday() >= 5: return
    state['daily_date'] = today(); state['daily_pnl'] = 0.0
    save_state()
    run_scan(announce=True)

def job_intraday_scan():
    if not market_open(): return
    run_scan(announce=False)

def job_minute():
    if now_et().weekday() >= 5: return
    if state['daily_date'] != today():
        state['daily_date'] = today(); state['daily_pnl'] = 0.0
    if market_open():
        refresh_prices()
        monitor_trades()
        auto_entry()

def job_eod():
    if now_et().weekday() >= 5: return
    refresh_prices()
    # Snapshot equity for the curve
    eq = equity()
    hist = state['equity_history']
    if not hist or hist[-1].get('date') != today():
        hist.append({'date': today(), 'equity': eq})
    else:
        hist[-1]['equity'] = eq
    save_state()
    open_swings = [t['symbol'] for t in state['trades'].values() if t['mode'] == 'swing']
    tg(f"📊 EOD {today()}\nDay P&L: ${state['daily_pnl']:+.2f}\nEquity: ${eq:.2f} "
       f"(started ${STARTING_CAPITAL:.0f})\nOpen swings: {', '.join(open_swings) or 'none'}")

def job_keepalive():
    try:
        url = APP_URL or f'http://localhost:{PORT}'
        urllib.request.urlopen(urllib.request.Request(f'{url}/ping',
            headers={'User-Agent': UA}), timeout=8)
    except Exception: pass

# ══════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════
load_state()
log('Market Scanner Pro v5 starting…')

def _boot():
    try:
        refresh_prices()
        run_scan(announce=False)
        start_stream()
    except Exception as e:
        print(f'boot scan: {e}')
threading.Thread(target=_boot, daemon=True).start()

if BackgroundScheduler:
    import atexit
    try:
        sched = BackgroundScheduler(timezone=ET)
        sched.add_job(job_premarket,     'cron', day_of_week='mon-fri', hour=8, minute=45)
        sched.add_job(job_premarket,     'cron', day_of_week='mon-fri', hour=9, minute=25)
        # Full re-scan every 3 minutes during market hours — fully automatic,
        # nobody needs to hit "Rescan". job_intraday_scan no-ops itself when
        # the market's closed, so it's safe to schedule broadly.
        sched.add_job(job_intraday_scan, 'interval', minutes=3, id='intraday_scan')
        sched.add_job(job_minute,        'cron', day_of_week='mon-fri', hour='9-16', minute='*')
        sched.add_job(job_eod,           'cron', day_of_week='mon-fri', hour=15, minute=56)
        sched.add_job(job_keepalive,     'interval', minutes=8)
        sched.start()
        atexit.register(lambda: sched.shutdown(wait=False))
        print(f'Scheduler: {len(sched.get_jobs())} jobs ✅')
    except Exception as e:
        print(f'Scheduler: {e}')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
