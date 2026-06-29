"""
AutoTrade Pro — Rebuilt from scratch
Strategy: Morning momentum + news catalyst
- Scan premarket for gap-up stocks with news
- Enter 9:30-10:30 AM on first pullback to VWAP/9EMA
- 2:1 R:R minimum, 1% risk per trade
- Exit: target hit, stop hit, or signal reversal
- 2 trades max per day
"""

import os, sys, json, time, uuid, threading, re, urllib.request, urllib.error
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_from_directory, request
import pytz

# ── Optional imports ──────────────────────────────────────────────────
try: import yfinance as yf
except: yf = None
try: import numpy as np; import pandas as pd
except: np = None; pd = None
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest, StockLatestTradeRequest
    alpaca_sdk = True
except: alpaca_sdk = False
try:
    from apscheduler.schedulers.background import BackgroundScheduler
except: BackgroundScheduler = None

# ── Config ────────────────────────────────────────────────────────────
PORT            = int(os.environ.get('PORT', 10000))
ALPACA_KEY      = os.environ.get('ALPACA_API_KEY','')
ALPACA_SECRET   = os.environ.get('ALPACA_SECRET_KEY','')
TELEGRAM_TOKEN  = os.environ.get('TELEGRAM_TOKEN','')
TELEGRAM_CHAT   = os.environ.get('TELEGRAM_CHAT_ID','')
GROQ_KEY        = os.environ.get('GROQ_API_KEY','')
ANTHROPIC_KEY   = os.environ.get('ANTHROPIC_API_KEY','')
APP_URL         = os.environ.get('RENDER_EXTERNAL_URL','')
ET_TZ           = pytz.timezone('US/Eastern')
UA              = 'AutoTradePro/3.0'

# ── Capital config ────────────────────────────────────────────────────
STARTING_CAPITAL = 1000.0  # Total account
RISK_PER_TRADE   = 0.01    # 1% risk per trade = $10 max loss
MAX_TRADES       = 2       # Max concurrent positions
MIN_PRICE        = 5.0     # No penny stocks
ENTRY_START      = 570     # 9:30 AM ET
ENTRY_END        = 630     # 10:30 AM ET (best window)
RR_RATIO         = 2.0     # 2:1 reward:risk minimum

# ── Watchlist — quality mid/large caps only ───────────────────────────
WATCHLIST = [
    # Mega cap tech (high volume, reliable technicals)
    'AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA',
    # Semis + AI
    'AMD','AVGO','MU','PLTR','ARM','SMCI',
    # Fintech/growth
    'SOFI','COIN','HOOD','UPST',
    # Momentum ETFs (3x leverage — bigger moves)
    'SOXL','TQQQ','UPRO','TECL',
    # Biotech catalyst plays
    'HIMS','RKLB','APP',
    # Memory/semis ETF
    'DRAM','NVDL',
    # High beta names
    'MSTR','ASTS','SMR',
]

app = Flask(__name__, static_folder='.', static_url_path='')

# ── State ─────────────────────────────────────────────────────────────
state = {
    'capital': STARTING_CAPITAL,
    'trades': {},       # open trades
    'completed': [],    # closed trades
    'logs': [],
    'date': '',
    'daily_pnl': 0.0,
    'scan_cache': {},   # premarket scan results
    'scan_ts': 0,
    'price_cache': {},
    'price_ts': 0,
}
_state_lock = threading.Lock()
STATE_FILE = 'state.json'

def save_state():
    try:
        with open(STATE_FILE,'w') as f:
            json.dump({
                'capital': state['capital'],
                'completed': state['completed'][-200:],
                'date': state['date'],
                'daily_pnl': state['daily_pnl'],
            }, f)
    except: pass

def load_state():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
        today = now_et().strftime('%Y-%m-%d')
        if d.get('date') == today:
            state['capital']   = d.get('capital', STARTING_CAPITAL)
            state['daily_pnl'] = d.get('daily_pnl', 0.0)
            state['completed'] = d.get('completed', [])
        else:
            # New day — reset capital
            state['capital']   = STARTING_CAPITAL
            state['daily_pnl'] = 0.0
        state['date'] = today
    except: pass

# ── Time helpers ──────────────────────────────────────────────────────
def now_et(): return datetime.now(ET_TZ)
def today_str(): return now_et().strftime('%Y-%m-%d')
def now_str(): return now_et().strftime('%Y-%m-%d %H:%M ET')

def market_open():
    n = now_et(); t = n.hour*60+n.minute
    return n.weekday() < 5 and 570 <= t < 960

def in_entry_window():
    t = now_et().hour*60+now_et().minute
    return ENTRY_START <= t <= ENTRY_END

# ── Alpaca init ───────────────────────────────────────────────────────
alpaca_trade = None
alpaca_data  = None
if alpaca_sdk and ALPACA_KEY and ALPACA_SECRET:
    try:
        alpaca_trade = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data  = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("Alpaca ✅")
    except Exception as e:
        print(f"Alpaca: {e}")

# ── Telegram ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return
    try:
        body = json.dumps({'chat_id':str(TELEGRAM_CHAT),'text':str(msg)[:4000],'disable_web_page_preview':True}).encode()
        req  = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type':'application/json','User-Agent':UA}
        )
        urllib.request.urlopen(req, timeout=8)
    except: pass

# ── Logging ───────────────────────────────────────────────────────────
def log(msg, alert=False):
    entry = {'t': now_str(), 'msg': msg}
    state['logs'].insert(0, entry)
    state['logs'] = state['logs'][:200]
    print(msg)
    if alert: tg(f"🚨 AutoTrade Pro\n{msg}")

# ── AI call ───────────────────────────────────────────────────────────
def ai_call(system, user, max_tokens=300):
    # Try Groq first (fast), then Anthropic
    if GROQ_KEY:
        try:
            body = json.dumps({'model':'llama-3.3-70b-versatile','max_tokens':max_tokens,
                               'messages':[{'role':'system','content':system},{'role':'user','content':user}]}).encode()
            req  = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions',
                data=body, headers={'Authorization':f'Bearer {GROQ_KEY}','Content-Type':'application/json'})
            r = json.loads(urllib.request.urlopen(req,timeout=15).read())
            return r['choices'][0]['message']['content']
        except: pass
    if ANTHROPIC_KEY:
        try:
            body = json.dumps({'model':'claude-haiku-4-5-20251001','max_tokens':max_tokens,
                               'system':system,'messages':[{'role':'user','content':user}]}).encode()
            req  = urllib.request.Request('https://api.anthropic.com/v1/messages',
                data=body, headers={'x-api-key':ANTHROPIC_KEY,'anthropic-version':'2023-06-01','Content-Type':'application/json'})
            r = json.loads(urllib.request.urlopen(req,timeout=15).read())
            return r['content'][0]['text']
        except: pass
    return None

# ══════════════════════════════════════════════════════════════════════
# PRICES
# ══════════════════════════════════════════════════════════════════════
def get_prices_alpaca(symbols):
    """Get snapshot prices from Alpaca."""
    if not alpaca_data or not symbols: return {}
    try:
        req  = StockSnapshotRequest(symbol_or_symbols=list(symbols))
        snaps = alpaca_data.get_stock_snapshot(req)
        result = {}
        for sym, s in snaps.items():
            try:
                price = float(s.latest_trade.price if s.latest_trade else
                              s.minute_bar.close if s.minute_bar else 0)
                prev  = float(s.daily_bar.open if s.daily_bar else price)
                pct   = round((price-prev)/prev*100,2) if prev else 0
                if price > 0:
                    result[sym] = {'price':round(price,2),'pct':pct,'src':'alpaca'}
            except: pass
        return result
    except: return {}

def get_prices_yf(symbols):
    """Fallback: yfinance prices."""
    if not yf or not symbols: return {}
    try:
        tickers = yf.download(list(symbols), period='2d', interval='1d',
                              auto_adjust=True, progress=False,
                              group_by='ticker' if len(symbols)>1 else None)
        result  = {}
        multi   = isinstance(tickers.columns, pd.MultiIndex) if pd else False
        for sym in symbols:
            try:
                cl = tickers['Close'][sym].dropna() if multi else tickers['Close'].dropna()
                if len(cl) < 1: continue
                price = float(cl.iloc[-1])
                prev  = float(cl.iloc[-2]) if len(cl)>1 else price
                pct   = round((price-prev)/prev*100,2) if prev else 0
                if price > 0:
                    result[sym] = {'price':round(price,2),'pct':pct,'src':'yfinance'}
            except: pass
        return result
    except: return {}

_price_lock = threading.Lock()
def refresh_prices():
    """Refresh all watchlist prices."""
    syms = set(WATCHLIST) | {'SPY','QQQ','^VIX','BTC-USD'}
    stock_syms = {s for s in syms if not s.startswith('^') and s != 'BTC-USD'}
    special    = {'^VIX','BTC-USD'}
    prices = {}
    # Alpaca for stocks
    ap = get_prices_alpaca(stock_syms)
    prices.update(ap)
    # yfinance for missing + VIX + BTC
    missing = (stock_syms - set(prices.keys())) | special
    yp = get_prices_yf(missing)
    for k,v in yp.items():
        if k == '^VIX': prices['VIX'] = v
        elif k == 'BTC-USD': prices['BTC'] = v
        else: prices[k] = v
    with _price_lock:
        state['price_cache'] = prices
        state['price_ts']    = time.time()

def cp(sym):
    """Current price dict for a symbol."""
    return state['price_cache'].get(sym)

# ══════════════════════════════════════════════════════════════════════
# MORNING SCAN — The heart of the system
# ══════════════════════════════════════════════════════════════════════
def morning_scan():
    """
    Premarket scan — run at 8:30 AM ET.
    Strategy: Find stocks with catalysts + gap + relative volume.
    Uses yfinance for data, news headlines for catalyst detection.

    Scoring (0-100):
    - Gap % today:      up to 30 pts
    - Relative volume:  up to 25 pts
    - News catalyst:    up to 25 pts
    - Technical setup:  up to 20 pts
    """
    if not yf or not pd: return []
    print("  Morning scan running...")
    results = []
    try:
        # Download 30 days for proper RVOL calculation
        df = yf.download(WATCHLIST, period='30d', interval='1d',
                         auto_adjust=True, progress=False, group_by='ticker')
        multi = isinstance(df.columns, pd.MultiIndex)

        for sym in WATCHLIST:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                vl = (df['Volume'][sym] if multi else df['Volume']).dropna()
                hi = (df['High'][sym]   if multi else df['High']).dropna()
                lo = (df['Low'][sym]    if multi else df['Low']).dropna()
                if len(cl) < 20: continue
                price  = float(cl.iloc[-1])
                prev   = float(cl.iloc[-2])
                if price < MIN_PRICE: continue

                # ── Gap % ─────────────────────────────────────────────
                gap    = (price - prev) / prev * 100
                if gap < 0.5: continue  # Only positive momentum

                # ── Relative Volume ───────────────────────────────────
                avg_vol = float(vl.iloc[-21:-1].mean())
                vol_td  = float(vl.iloc[-1])
                rvol    = vol_td / avg_vol if avg_vol > 0 else 1.0

                # ── 52-week context ────────────────────────────────────
                hi_52w  = float(hi.max())
                near_hi = (price / hi_52w) > 0.95  # within 5% of 52w high

                # ── 1-month momentum ──────────────────────────────────
                chg_20d = (price - float(cl.iloc[-21])) / float(cl.iloc[-21]) * 100

                # ── Support/Resistance from last 10 days ──────────────
                recent_hi = float(hi.iloc[-10:].max())
                recent_lo = float(lo.iloc[-10:].min())

                # ── Scoring ───────────────────────────────────────────
                score = 0
                reasons = []

                # Gap points (0-30)
                if   gap >= 8: score += 30; reasons.append(f'Gap +{gap:.1f}% 🔥')
                elif gap >= 5: score += 22; reasons.append(f'Gap +{gap:.1f}%')
                elif gap >= 3: score += 15; reasons.append(f'Gap +{gap:.1f}%')
                elif gap >= 1: score += 8;  reasons.append(f'Up +{gap:.1f}%')

                # RVOL points (0-25)
                if   rvol >= 5: score += 25; reasons.append(f'RVOL {rvol:.1f}x 🔥')
                elif rvol >= 3: score += 18; reasons.append(f'RVOL {rvol:.1f}x')
                elif rvol >= 2: score += 12; reasons.append(f'RVOL {rvol:.1f}x')
                elif rvol >= 1.5: score += 6

                # Near 52-week high (0-10)
                if near_hi:
                    score += 10; reasons.append('Near 52W high')

                # Monthly momentum (0-10)
                if chg_20d >= 20: score += 10; reasons.append(f'+{chg_20d:.0f}% monthly')
                elif chg_20d >= 10: score += 6
                elif chg_20d >= 5:  score += 3

                # Technical setup bonus (0-20 — checked separately)
                # Will be added after technicals are computed at open

                if score < 20: continue  # Too weak

                results.append({
                    'symbol':  sym,
                    'price':   round(price, 2),
                    'gap_pct': round(gap, 2),
                    'rvol':    round(rvol, 1),
                    'score':   score,
                    'reasons': reasons,
                    'near_52w_hi': near_hi,
                    'chg_20d': round(chg_20d, 1),
                    'support': round(recent_lo, 2),
                    'resistance': round(recent_hi, 2),
                })
            except: pass

        results.sort(key=lambda x: x['score'], reverse=True)
        top = results[:8]  # Top 8 candidates
        print(f"  Morning scan: {len(top)} candidates (from {len(WATCHLIST)} stocks)")
        for r in top:
            print(f"    {r['symbol']:6} score:{r['score']:3} gap:{r['gap_pct']:+.1f}% RVOL:{r['rvol']:.1f}x {r['reasons']}")
        return top

    except Exception as e:
        print(f"  Morning scan error: {e}")
        return []

# ══════════════════════════════════════════════════════════════════════
# TECHNICALS — Compute for a specific symbol
# ══════════════════════════════════════════════════════════════════════
def compute_technicals(sym):
    """
    Compute intraday technicals from 5-min bars.
    Returns: signal, vwap, ema9, rsi, macd_bull, above_vwap, bull_pts, support, resistance
    """
    if not yf or not np: return None
    try:
        df = yf.download(sym, period='1d', interval='5m',
                         auto_adjust=True, progress=False)
        if df is None or len(df) < 10: return None

        c = df['Close'].dropna().values.astype(float)
        h = df['High'].dropna().values.astype(float)
        l = df['Low'].dropna().values.astype(float)
        v = df['Volume'].dropna().values.astype(float)
        price = float(c[-1])
        if price <= 0: return None

        # ── VWAP ──────────────────────────────────────────────────────
        tp   = (h + l + c) / 3
        vwap = float(np.cumsum(tp * v)[-1] / np.cumsum(v)[-1]) if np.sum(v) > 0 else price

        # ── EMA 9 and EMA 20 ──────────────────────────────────────────
        def ema(arr, n):
            result = np.zeros_like(arr); result[0] = arr[0]
            k = 2/(n+1)
            for i in range(1,len(arr)): result[i] = arr[i]*k + result[i-1]*(1-k)
            return result

        ema9  = float(ema(c,9)[-1])
        ema20 = float(ema(c,20)[-1])

        # ── RSI 14 ────────────────────────────────────────────────────
        delta = np.diff(c)
        gain  = np.where(delta>0,delta,0)
        loss  = np.where(delta<0,-delta,0)
        ag    = np.mean(gain[-14:]) if len(gain)>=14 else np.mean(gain)
        al    = np.mean(loss[-14:]) if len(loss)>=14 else np.mean(loss)
        rsi   = 100 - 100/(1+ag/al) if al > 0 else 50

        # ── MACD ──────────────────────────────────────────────────────
        ema12 = float(ema(c,12)[-1])
        ema26 = float(ema(c,26)[-1]) if len(c)>=26 else ema12
        macd  = ema12 - ema26
        macd_bull = macd > 0

        # ── Support / Resistance from today's bars ────────────────────
        support    = round(float(np.min(l)), 2)
        resistance = round(float(np.max(h)), 2)
        # Use tighter S/R from last 10 bars if enough data
        if len(h) >= 10:
            support    = round(float(np.min(l[-10:])), 2)
            resistance = round(float(np.max(h[-10:])), 2)

        # ── Bull points (0-4) ─────────────────────────────────────────
        above_vwap  = price > vwap
        ema_bullish = ema9 > ema20
        rsi_ok      = 35 < rsi < 68
        bull_pts = sum([above_vwap, macd_bull, ema_bullish, rsi_ok])

        # ── Signal ────────────────────────────────────────────────────
        if bull_pts >= 3 and above_vwap and rsi < 68:
            signal = 'BUY'
        elif bull_pts <= 1 or rsi > 75 or rsi < 25:
            signal = 'SELL'
        else:
            signal = 'WAIT'

        # ── Prediction (linear regression) ────────────────────────────
        x    = np.arange(len(c))
        coeffs = np.polyfit(x, c, 1)
        slope  = coeffs[0]
        pred_series = [round(float(np.polyval(coeffs, i)), 2) for i in range(len(c))]
        # Project 30D ahead (simple: extrapolate slope * 30 5-min bars)
        pred_30d = round(float(price + slope * 30), 2)
        pred_5d  = round(float(price + slope * 6),  2)

        return {
            'signal':signal, 'price':round(price,2),
            'vwap':round(vwap,2), 'ema9':round(ema9,2), 'ema20':round(ema20,2),
            'rsi':round(rsi,1), 'macd_bull':macd_bull,
            'above_vwap':above_vwap, 'ema_bullish':ema_bullish, 'rsi_ok':rsi_ok,
            'bull_pts':bull_pts, 'support':support, 'resistance':resistance,
            'slope':round(float(slope),4),
            'pred_series':pred_series, 'pred_5d':pred_5d, 'pred_30d':pred_30d,
            'bars':len(c),
        }
    except Exception as e:
        return None

# ══════════════════════════════════════════════════════════════════════
# TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════
def enter_trade(sym, manual=False, tech=None, scan_data=None):
    """
    Enter a trade with strict quality gates.
    Risk: 1% of capital per trade = $10 max loss
    Position: capital * 30% (concentrated in 2 trades max)
    """
    # Gate 1: Max positions
    if len(state['trades']) >= MAX_TRADES:
        return False, f'Max {MAX_TRADES} trades open'

    # Gate 2: Already in this symbol
    if any(t['symbol']==sym for t in state['trades'].values()):
        return False, f'Already in {sym}'

    # Gate 3: Entry window (only 9:30-10:30 for auto)
    if not manual and not in_entry_window():
        return False, 'Outside entry window (9:30-10:30 AM)'

    # Gate 4: Need technicals
    if not tech:
        tech = compute_technicals(sym)
    if not tech:
        return False, f'Cannot get data for {sym}'

    price = tech['price']
    if price < MIN_PRICE:
        return False, f'{sym} price ${price} below ${MIN_PRICE} minimum'

    # Gate 5: Signal must be BUY
    if tech['signal'] != 'BUY' and not manual:
        return False, f'{sym} signal is {tech["signal"]}, not BUY'

    # Gate 6: 2-hour cooldown on same symbol (no re-entry)
    cutoff = time.time() - 7200
    recent = [t for t in state['completed'][-20:]
              if t['symbol']==sym and t.get('entered_at',0)>cutoff]
    if recent and not manual:
        return False, f'{sym} cooldown (traded {len(recent)}x in last 2h)'

    # ── Calculate position size ────────────────────────────────────────
    # Risk = 1% of capital = $10
    # Stop = ATR-based or 2% below entry (whichever is tighter)
    risk_dollars = state['capital'] * RISK_PER_TRADE  # $10
    stop_pct = 0.02  # 2% stop
    stop = round(price * (1 - stop_pct), 2)

    # Position: risk / stop_distance
    risk_per_share = price - stop
    shares = max(1, int(risk_dollars / risk_per_share)) if risk_per_share > 0 else 1

    # Cap position at 30% of capital
    max_cost = state['capital'] * 0.30
    shares = min(shares, int(max_cost / price))
    shares = max(1, shares)
    cost   = round(shares * price, 2)

    if cost > state['capital']:
        return False, f'Insufficient capital: ${state["capital"]:.0f}'

    # Target: 2:1 R:R minimum
    risk_per_share = price - stop
    target = round(price + risk_per_share * RR_RATIO, 2)

    # Use resistance as target if it's reasonable
    if tech.get('resistance') and tech['resistance'] > price:
        res_gain = tech['resistance'] - price
        min_gain = risk_per_share * RR_RATIO
        if res_gain >= min_gain:
            target = tech['resistance']

    tid = str(uuid.uuid4())[:8]
    trade = {
        'id':tid, 'symbol':sym, 'entry':round(price,2), 'shares':shares,
        'cost':cost, 'stop':stop, 'target':round(target,2),
        'current':round(price,2), 'peak':round(price,2), 'peak_pct':0.0,
        'pnl':0.0, 'pnl_pct':0.0,
        'entry_time':now_str(), 'entered_at':time.time(),
        'manual':manual, 'tech_at_entry':tech.get('signal','?'),
        'scan_score':scan_data.get('score',0) if scan_data else 0,
        'scan_reasons':scan_data.get('reasons',[]) if scan_data else [],
    }

    state['trades'][tid] = trade
    state['capital'] -= cost
    state['capital']  = round(state['capital'], 2)
    save_state()

    rr = round((target-price)/(price-stop),1) if price>stop else 0
    msg = (f"ENTER {sym} {shares}sh @${price:.2f}\n"
           f"Stop: ${stop:.2f} | Target: ${target:.2f} | R:R {rr}:1\n"
           f"Risk: ${round(shares*(price-stop),2):.2f} | Capital left: ${state['capital']:.0f}")
    log(msg, alert=True)

    if alpaca_trade:
        try:
            alpaca_trade.submit_order(MarketOrderRequest(
                symbol=sym, qty=shares, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY
            ))
        except Exception as e:
            log(f"Alpaca order {sym}: {e}")

    return True, f'Entered {sym}'

def close_trade(tid, reason):
    """Close a trade and record P&L."""
    t = state['trades'].pop(tid, None)
    if not t: return
    sym = t['symbol']
    q   = cp(sym)
    exit_price = q['price'] if q else t['current']
    pnl = round((exit_price - t['entry']) * t['shares'], 2)
    pnl_pct = round((exit_price - t['entry']) / t['entry'] * 100, 1)

    # Return capital
    proceeds = round(exit_price * t['shares'], 2)
    state['capital'] += proceeds
    state['capital']  = round(state['capital'], 2)
    state['daily_pnl'] = round(state['daily_pnl'] + pnl, 2)

    label = {'target':'🎯 TARGET','stop':'🛑 STOP','trail_3':'📈 TRAIL',
             'trail_5':'💰 TRAIL+','quick_stop':'⚡ QUICK STOP','eod':'🌙 EOD',
             'manual':'👤 MANUAL','signal':'🔄 SIGNAL FLIP'}.get(reason, reason)

    record = {**t, 'exit_price':round(exit_price,2), 'exit_reason':reason,
              'exit_time':now_str(), 'pnl':pnl, 'pnl_pct':pnl_pct}
    state['completed'].append(record)
    save_state()

    msg = (f"CLOSE {sym} {label} ${pnl:+.2f} ({pnl_pct:+.1f}%)\n"
           f"Entry: ${t['entry']:.2f} → Exit: ${exit_price:.2f}\n"
           f"Daily P&L: ${state['daily_pnl']:+.2f} | Capital: ${state['capital']:.0f}")
    log(msg, alert=True)

    if alpaca_trade:
        try:
            alpaca_trade.submit_order(MarketOrderRequest(
                symbol=sym, qty=t['shares'], side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            ))
        except Exception as e:
            log(f"Alpaca close {sym}: {e}")

# ══════════════════════════════════════════════════════════════════════
# EXIT MONITOR — Runs every minute
# ══════════════════════════════════════════════════════════════════════
def monitor_trades():
    """
    Exit logic — cut losses fast, let winners run.
    - Quick stop: down 1% in first 15 min = bad entry, exit immediately
    - Hard stop: down 2% = exit
    - Trail 3%: when up 3%, trail at 1.5% below peak
    - Trail 5%: when up 5%, trail at 3% below peak (locks in ~2%)
    - Target: take profit
    - EOD: exit all day trades 10 min before close
    """
    if not market_open(): return
    now_ts = time.time()

    for tid, t in list(state['trades'].items()):
        q = cp(t['symbol'])
        if not q: continue
        price = q['price']
        entry = t['entry']
        peak  = max(price, t.get('peak', price))
        pnl_pct  = (price - entry) / entry * 100
        peak_pct = (peak  - entry) / entry * 100
        held_min = (now_ts - t.get('entered_at', now_ts)) / 60

        # Update live state
        state['trades'][tid].update({
            'current':round(price,2), 'peak':round(peak,2),
            'peak_pct':round(peak_pct,2),
            'pnl':round((price-entry)*t['shares'],2),
            'pnl_pct':round(pnl_pct,2)
        })

        # ── TARGET ────────────────────────────────────────────────────
        if price >= t['target']:
            close_trade(tid,'target'); continue

        # ── HARD STOP ─────────────────────────────────────────────────
        if price <= t['stop']:
            close_trade(tid,'stop'); continue

        # ── QUICK STOP: down 1% in first 20 min = wrong setup ─────────
        if pnl_pct <= -1.0 and held_min < 20 and not t.get('manual'):
            close_trade(tid,'quick_stop'); continue

        # ── TRAIL at 5% peak: trail 3% below peak ─────────────────────
        if peak_pct >= 5.0:
            trail = round(peak * 0.97, 2)
            if price <= trail:
                close_trade(tid,'trail_5'); continue
            state['trades'][tid]['stop'] = max(t['stop'], trail)

        # ── TRAIL at 3% peak: trail 1.5% below peak ───────────────────
        elif peak_pct >= 3.0:
            trail = round(peak * 0.985, 2)
            if price <= trail:
                close_trade(tid,'trail_3'); continue
            state['trades'][tid]['stop'] = max(t['stop'], trail)

        # ── SIGNAL REVERSAL: if up and signal flips to SELL ───────────
        if pnl_pct >= 1.0 and held_min > 20:
            tech = compute_technicals(t['symbol'])
            if tech and tech.get('signal') == 'SELL':
                close_trade(tid,'signal'); continue

        # ── EOD: close day trades 10 min before 4 PM ──────────────────
        et = now_et()
        mins_to_close = (16*60) - (et.hour*60 + et.minute)
        if mins_to_close <= 10:
            close_trade(tid,'eod'); continue

# ══════════════════════════════════════════════════════════════════════
# AUTO ENTRY — The strategy
# ══════════════════════════════════════════════════════════════════════
def auto_entry():
    """
    Entry logic:
    1. Take top candidates from morning scan
    2. At market open, check live technicals
    3. Enter if: BUY signal + above VWAP + 3+ bull points + in entry window
    4. Max 2 trades per day
    """
    if not market_open() or not in_entry_window(): return
    if len(state['trades']) >= MAX_TRADES: return

    # Get morning scan candidates
    candidates = state['scan_cache'].get('candidates', [])
    if not candidates:
        # Fallback: scan on the fly
        candidates = morning_scan()

    entered = {t['symbol'] for t in state['trades'].values()}

    for c in candidates:
        if len(state['trades']) >= MAX_TRADES: break
        sym = c['symbol']
        if sym in entered: continue

        # Check 2-hour cooldown
        cutoff = time.time() - 7200
        if any(t['symbol']==sym and t.get('entered_at',0)>cutoff
               for t in state['completed'][-10:]):
            continue

        # Get fresh technicals
        tech = compute_technicals(sym)
        if not tech: continue
        if tech['signal'] != 'BUY': continue
        if tech['bull_pts'] < 3: continue
        if not tech['above_vwap']: continue
        if tech['rsi'] > 65 or tech['rsi'] < 30: continue

        # Boost scan score with live technical score
        tech_score = tech['bull_pts'] * 10 + (65 - tech['rsi'])
        total = c['score'] + tech_score

        log(f"📡 SIGNAL: {sym} score:{total} gap:{c['gap_pct']:+.1f}% RVOL:{c['rvol']:.1f}x BUY RSI:{tech['rsi']:.0f} {c['reasons']}")
        ok, msg = enter_trade(sym, manual=False, tech=tech, scan_data=c)
        if ok:
            entered.add(sym)

# ══════════════════════════════════════════════════════════════════════
# CANDLES
# ══════════════════════════════════════════════════════════════════════
def get_candles(sym, period='1d', interval='5m'):
    """Fetch OHLCV bars for charting."""
    if not yf: return []
    try:
        yf_sym = sym if not sym.startswith('^') else sym
        if sym == 'DRAM': yf_sym = 'DRAM'
        df = yf.download(yf_sym, period=period, interval=interval,
                         auto_adjust=True, progress=False)
        if df is None or len(df) == 0: return []
        bars = []
        for idx, row in df.iterrows():
            try:
                dt = idx.astimezone(ET_TZ) if hasattr(idx,'astimezone') else idx
                bars.append({
                    't': str(dt), 'hm': dt.strftime('%H:%M') if hasattr(dt,'strftime') else str(dt),
                    'o':round(float(row['Open']),2),  'h':round(float(row['High']),2),
                    'l':round(float(row['Low']),2),   'c':round(float(row['Close']),2),
                    'v':int(row['Volume']) if pd and not pd.isna(row['Volume']) else 0
                })
            except: pass
        return bars
    except Exception as e:
        print(f"  candles {sym}: {e}")
        return []

# ══════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════
def _cors(data, status=200):
    r = jsonify(data)
    r.headers['Access-Control-Allow-Origin'] = '*'
    r.status_code = status
    return r

PUBLIC = {'/','/ping','/prices','/candles','/technicals','/trades',
          '/scan','/performance','/logs','/analyze','/analysis-result'}

@app.before_request
def auth():
    if request.method == 'OPTIONS': return _cors({})
    if request.path in PUBLIC: return
    if request.path.startswith('/static'): return
    pin = (request.headers.get('X-PIN') or
           request.args.get('pin') or
           (request.json or {}).get('pin','') if request.is_json else '')
    if str(pin) != str(os.environ.get('ABIY_PIN','1702')): return _cors({'locked':True,'ok':False},403)

@app.route('/')
def index(): return send_from_directory('.','index.html')

@app.route('/ping')
def ping(): return _cors({'ok':True,'time':now_str(),'market':market_open()})

@app.route('/prices')
def prices():
    data = {}
    pc   = state['price_cache']
    for sym in ['SPY','QQQ','VIX','BTC'] + WATCHLIST:
        if sym in pc: data[sym] = pc[sym]
    return _cors({'data':data,'ts':state['price_ts'],'src':'live'})

@app.route('/candles')
def candles():
    sym      = request.args.get('symbol','NVDA').upper()
    period   = request.args.get('period','1d')
    interval = request.args.get('interval','5m')
    bars     = get_candles(sym, period, interval)
    return _cors({'bars':bars,'symbol':sym,'count':len(bars)})

@app.route('/technicals')
def technicals():
    sym  = request.args.get('symbol','NVDA').upper()
    tech = compute_technicals(sym)
    return _cors({'technicals':tech,'symbol':sym})

@app.route('/scan')
def scan():
    """Return morning scan candidates with live scores."""
    candidates = state['scan_cache'].get('candidates',[])
    # Enrich with live prices
    for c in candidates:
        q = cp(c['symbol'])
        if q:
            c['price']     = q['price']
            c['price_pct'] = q['pct']
    return _cors({'candidates':candidates,'ts':state['scan_ts'],
                  'entry_window':in_entry_window(),'market_open':market_open()})

@app.route('/trades')
def trades_route():
    open_t = list(state['trades'].values())
    return _cors({
        'trades':    open_t,
        'completed': state['completed'][-50:],
        'capital':   state['capital'],
        'daily_pnl': state['daily_pnl'],
        'max_trades': MAX_TRADES,
    })

@app.route('/performance')
def performance():
    c = state['completed']
    if not c: return _cors({'trades':0,'win_rate':0,'total_pnl':0})
    wins   = [t for t in c if t.get('pnl',0)>0]
    losses = [t for t in c if t.get('pnl',0)<=0]
    return _cors({
        'trades':    len(c),
        'wins':      len(wins),
        'losses':    len(losses),
        'win_rate':  round(len(wins)/len(c)*100,1) if c else 0,
        'total_pnl': round(sum(t.get('pnl',0) for t in c),2),
        'avg_win':   round(sum(t.get('pnl',0) for t in wins)/len(wins),2) if wins else 0,
        'avg_loss':  round(sum(t.get('pnl',0) for t in losses)/len(losses),2) if losses else 0,
        'capital':   state['capital'],
        'daily_pnl': state['daily_pnl'],
        'all_trades':[{'sym':t['symbol'],'pnl':round(t.get('pnl',0),2),
                       'pct':round(t.get('pnl_pct',0),1),'reason':t.get('exit_reason',''),
                       'entry':t.get('entry',0),'exit':t.get('exit_price',0)} for t in c[-20:]],
    })

@app.route('/logs')
def logs_route():
    return _cors({'logs': state['logs'][:50]})

_analysis_cache = {}
@app.route('/analyze', methods=['POST'])
def analyze():
    sym = (request.json or {}).get('symbol','').upper().strip()
    if not sym: return _cors({'error':'No symbol'},400)
    def run():
        tech = compute_technicals(sym)
        bars = get_candles(sym,'1d','5m')
        # Get news via Alpaca or skip
        news_txt = ''
        if alpaca_data:
            try:
                import alpaca.data.requests as adr
                nr = adr.NewsRequest(symbols=[sym], limit=3)
                news_items = alpaca_data.get_news(nr) if hasattr(alpaca_data,'get_news') else []
                news_txt = ' | '.join(n.headline for n in news_items[:3])
            except: pass
        # AI analysis
        ai_txt = ''
        if tech:
            sys_p = "You are a professional day trader. Give a concise trading analysis."
            usr_p = (f"Symbol: {sym}\nPrice: ${tech['price']}\nSignal: {tech['signal']}\n"
                     f"RSI: {tech['rsi']:.0f} | VWAP: ${tech['vwap']:.2f} | "
                     f"Above VWAP: {tech['above_vwap']} | Bull pts: {tech['bull_pts']}/4\n"
                     f"Support: ${tech['support']} | Resistance: ${tech['resistance']}\n"
                     f"News: {news_txt or 'None'}\n\n"
                     "In 4 lines: 1) SIGNAL (BUY/WAIT/AVOID) 2) Entry/Target/Stop 3) Key reason 4) Risk")
            ai_txt = ai_call(sys_p, usr_p, 200) or ''
        _analysis_cache[sym] = {'tech':tech,'bars':bars,'ai':ai_txt,'done':True}
    _analysis_cache[sym] = {'done':False}
    threading.Thread(target=run, daemon=True).start()
    return _cors({'ok':True,'symbol':sym})

@app.route('/analysis-result')
def analysis_result():
    sym = request.args.get('symbol','').upper()
    r   = _analysis_cache.get(sym,{})
    if not r.get('done'): return _cors({'ready':False})
    return _cors({'ready':True,'symbol':sym,'tech':r.get('tech'),
                  'bars':r.get('bars',[]),'ai':r.get('ai','')})

@app.route('/enter', methods=['POST'])
def enter_route():
    data = request.json or {}
    sym  = data.get('symbol','').upper()
    if not sym: return _cors({'ok':False,'msg':'No symbol'},400)
    tech = compute_technicals(sym)
    ok, msg = enter_trade(sym, manual=True, tech=tech)
    return _cors({'ok':ok,'msg':msg})

@app.route('/close', methods=['POST'])
def close_route():
    tid = (request.json or {}).get('tid','')
    if tid in state['trades']:
        close_trade(tid,'manual')
        return _cors({'ok':True})
    return _cors({'ok':False,'msg':'Trade not found'},404)

@app.route('/whale-refresh', methods=['POST'])
def whale_refresh():
    """Trigger a fresh morning scan."""
    def run():
        c = morning_scan()
        state['scan_cache'] = {'candidates':c}
        state['scan_ts']    = time.time()
    threading.Thread(target=run,daemon=True).start()
    return _cors({'ok':True,'msg':'Scan triggered'})

# ══════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════
def job_morning():
    """8:30 AM — Reset capital, run morning scan, alert."""
    if now_et().weekday() >= 5: return
    state['date']      = today_str()
    state['capital']   = STARTING_CAPITAL
    state['daily_pnl'] = 0.0
    state['trades']    = {}
    save_state()
    log(f"🌅 New day — Capital: ${STARTING_CAPITAL:.0f}")
    # Run morning scan
    c = morning_scan()
    state['scan_cache'] = {'candidates':c}
    state['scan_ts']    = time.time()
    if c:
        top3 = c[:3]
        msg  = f"🌅 AutoTrade Pro — {today_str()}\nCapital: ${STARTING_CAPITAL:.0f}\n\nTop picks:\n"
        for x in top3:
            msg += f"• {x['symbol']}: score {x['score']} gap {x['gap_pct']:+.1f}% RVOL {x['rvol']:.1f}x\n  {', '.join(x['reasons'][:2])}\n"
        tg(msg)
    else:
        tg(f"🌅 AutoTrade Pro — {today_str()}\nCapital: ${STARTING_CAPITAL:.0f}\nNo strong pre-market movers today.")

def job_minute():
    """Every minute — monitor exits, attempt entries."""
    if now_et().weekday() >= 5: return
    state['date'] = today_str()
    monitor_trades()
    if market_open():
        auto_entry()

def job_prices():
    """Every 5s during market, 30s otherwise — refresh prices."""
    try: refresh_prices()
    except: pass

def job_eod():
    """3:55 PM — Close all day trades."""
    if now_et().weekday() >= 5: return
    for tid in list(state['trades'].keys()):
        close_trade(tid, 'eod')
    pnl = state['daily_pnl']
    tg(f"📊 EOD — {today_str()}\nP&L: ${pnl:+.2f} ({pnl/STARTING_CAPITAL*100:+.1f}%)\nCapital: ${state['capital']:.0f}")

def job_keepalive():
    try:
        url = APP_URL or f"http://localhost:{PORT}"
        urllib.request.urlopen(urllib.request.Request(f"{url}/ping",headers={'User-Agent':UA}),timeout=8)
    except: pass

# ── Price refresh thread (non-blocking) ───────────────────────────────
def price_thread():
    while True:
        try:
            refresh_prices()
            t  = now_et().hour*60+now_et().minute
            sleep = 5 if market_open() else 30
            time.sleep(sleep)
        except Exception as e:
            print(f"price_thread: {e}")
            time.sleep(30)

# ══════════════════════════════════════════════════════════════════════
# BOOT
# ══════════════════════════════════════════════════════════════════════
load_state()
log("AutoTrade Pro v4 starting...")
threading.Thread(target=price_thread, daemon=True).start()

if BackgroundScheduler:
    import atexit
    try:
        sched = BackgroundScheduler(timezone=ET_TZ)
        sched.add_job(job_morning,   'cron', day_of_week='mon-fri', hour=8,  minute=30)
        sched.add_job(job_minute,    'cron', day_of_week='mon-fri', hour='9-16', minute='*')
        sched.add_job(job_eod,       'cron', day_of_week='mon-fri', hour=15, minute=55)
        sched.add_job(job_keepalive, 'interval', minutes=8)
        sched.start()
        atexit.register(lambda: sched.shutdown(wait=False))
        print(f"Scheduler: {len(sched.get_jobs())} jobs ✅")
    except Exception as e:
        print(f"Scheduler: {e}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=PORT, debug=False)
