#!/usr/bin/env python3
"""AutoTrade Pro — Professional Day Trading Engine"""
import json, os, time, threading, urllib.request, concurrent.futures
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

try:
    import yfinance as yf, pandas as pd, pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"Missing: {e}"); raise SystemExit(1)

ALPACA_KEY    = os.environ.get('ALPACA_API_KEY','')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY','')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY','')
TELEGRAM_TOKEN= os.environ.get('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')
APP_URL       = os.environ.get('APP_URL','https://your-app.onrender.com')
_detected_app_url = None
def get_app_url():
    """Falls back to the actual request host if APP_URL was never set on
    Render (or still has the placeholder value) — fixes Telegram links
    pointing at 'your-app.onrender.com' literally."""
    if APP_URL and 'your-app' not in APP_URL:
        return APP_URL
    return _detected_app_url or APP_URL
PORT          = int(os.environ.get('PORT',8765))
ET_TZ         = pytz.timezone('America/New_York')
UA            = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

alpaca_trading = None
alpaca_data    = None
if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        alpaca_trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data    = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("  Alpaca connected ✅")
    except Exception as e:
        print(f"  Alpaca: {e}")

# Real options data (separate, optional) — requires a newer alpaca-py and
# an account with options market-data entitlement. If either is missing,
# we fall back to the simulated leverage-proxy tracker further down, and
# every UI/Telegram label makes clear which mode is actually active.
alpaca_options_data = None
ALPACA_OPTIONS_AVAILABLE = False
if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient
        from alpaca.data.requests import OptionChainRequest
        alpaca_options_data = OptionHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        ALPACA_OPTIONS_AVAILABLE = True
        print("  Alpaca options data ✅")
    except Exception as e:
        print(f"  Alpaca options data not available, using simulated estimate instead: {e}")

app = Flask(__name__, static_folder='.')

# ── ACCESS CODE (optional) ──────────────────────────────────────────
# Since this is ONE shared backend, all your devices already see the same
# live data automatically — there's no real "per-device" data loss, only
# the restart/persistence issue handled above. This just keeps strangers
# from opening your public Render URL and touching your trades. Set
# ABIY_PIN in Render's environment variables to turn it on; leave it
# unset and the app behaves exactly as before (no gate).
import hashlib
ABIY_PIN = os.environ.get('ABIY_PIN', '').strip()
def _expected_token():
    return hashlib.sha256((ABIY_PIN + 'autotrade-pro-salt-2026').encode()).hexdigest()
_PUBLIC_PATHS = {'/', '/ping', '/unlock'}

@app.before_request
def _detect_url():
    global _detected_app_url
    if _detected_app_url is None and request.host and ('onrender.com' in request.host or '.' in request.host) and 'localhost' not in request.host:
        _detected_app_url = f"https://{request.host}"

@app.before_request
def _check_auth():
    if not ABIY_PIN or request.path in _PUBLIC_PATHS:
        return None
    if request.headers.get('X-Auth','') != _expected_token():
        return jsonify({'ok':False,'locked':True,'msg':'Enter your code to unlock'}), 401

@app.route('/unlock', methods=['POST'])
def unlock():
    if not ABIY_PIN:
        return cors({'ok':True,'token':'','gated':False})
    data = request.get_json() or {}
    if str(data.get('pin','')) == ABIY_PIN:
        return cors({'ok':True,'token':_expected_token(),'gated':True})
    return cors({'ok':False,'msg':'Incorrect code'})

# ══════════════════════════════════════════════════════════════════════
# STOCK UNIVERSE — stocks that ACTUALLY move 10%+ in a day
# Big caps (AAPL/MSFT) almost never move 10% — wrong universe for day trading
# ══════════════════════════════════════════════════════════════════════
UNIVERSE = {
    # ── 3x Leveraged ETFs: BEST for hitting 10% target ──────────────
    # When sector moves 3-4%, these move 9-12%. That's your 10% right there.
    '3x_etf': ['SOXL','TQQQ','UPRO','TECL','LABU','CURE','SPXL','TNA','UDOW','FNGU'],

    # ── Crypto/Fintech: Move 10-20% on crypto news ──────────────────
    'crypto': ['MSTR','COIN','HOOD','MARA','RIOT','HUT','CLSK','IREN','CIFR'],

    # ── AI/Semiconductor mid-cap: 10%+ on earnings/news ────────────
    'ai_semi': ['NVDA','AMD','SMCI','ARM','PLTR','IONQ','RGTI','QUBT','BBAI','SOUN','OKLO'],

    # ── High-beta growth/momentum ────────────────────────────────────
    'momentum': ['SOFI','UPST','AFRM','APP','HIMS','RKLB','ACHR','JOBY','LUNR','SMR','ASTS'],

    # ── Mid-cap volatile (float <200M, moves fast) ───────────────────
    'mid_vol': ['CRWD','DDOG','NET','SNOW','MDB','GTLB','BILL','CELH','TSLA','META'],

    # ── 100%+ candidates: ETFs/stocks that can double ────────────────
    # DRAM, NVDL, SOXL, MSTR, SMCI have ALL doubled in recent runs
    '100x_candidates': ['DRAM','NVDL','FNGU','MSTR','SMCI','RGTI','IONQ','BBAI','SOUN','ASTS'],

    # ── Biotech: binary catalysts (FDA, trial data) cause 10-50% gaps ──
    'biotech_volatile': ['MRNA','NVAX','CRSP','NTLA','SAVA'],

    # ── China ADRs: notoriously volatile on regulatory/economic news ──
    'china_adr': ['BABA','NIO','XPEV','LI','PDD'],

    # ── EV/clean energy: high-beta, sentiment-driven ──────────────────
    'ev_clean': ['RIVN','LCID','CHPT','PLUG','FCEL'],

    # ── More semiconductors beyond the AI-flagship names ──────────────
    'more_semis': ['MU','LRCX','AMAT','MRVL'],

    # ── Meme/retail-driven volatility ──────────────────────────────────
    'meme_volatile': ['GME','AMC'],
}
ALL_UNIVERSE = [s for group in UNIVERSE.values() for s in group]

# User custom watchlist (up to 5 stocks)
custom_watchlist = []

# ── PROFESSIONAL TRADING RULES ────────────────────────────────────────
RULES = {
    'MIN_SCORE':          58,    # Minimum score/99 — reject weak setups
    'MIN_VOLUME_RATIO':   1.15,  # Need 1.15x avg volume (institutional buying)
    'TARGET_PCT':          0.08, # 8% profit ceiling — take it if you hit it
    'STOP_PCT':           0.04,  # 4% stop — 3% too tight for volatile names
    'TRAIL_TRIGGER_PCT':  0.03,  # Once up 3%+, start trailing the peak
    'TRAIL_GIVEBACK_PCT': 0.02,  # If price gives back 2pts from peak gain, exit
    'NO_ENTRY_BEFORE':    (9,45),# Never enter first 15min — pure chaos
    'NO_ENTRY_AFTER':     (14,30),# No new trades after 2:30PM
    'MIN_SPY_PCT':        -0.5,  # Don't fight a crashing market
    'MAX_TRADES_PER_DAY': 8,     # Rotate through many trades, don't cap at 3
    'UNDERPERFORM_MINUTES':   60,  # After 60 min with little movement...
    'UNDERPERFORM_MIN_PCT':   1.5, # ...and gain is under 1.5%...
    'UNDERPERFORM_SCORE_GAP': 15,  # ...rotate if a candidate scores 15+ higher
    'PREMARKET_ENTRY_ENABLED': True,  # watchlist-only, limit orders, riskier liquidity
    'PREMARKET_START': (8, 0),        # ET — start of eligible pre-market entry window
}
# Simulated options tracker config (educational estimate, NOT real option data)
OPTIONS_MAX_SLOTS   = 3
OPTIONS_LEVERAGE    = 5      # Approximate leverage of an ATM short-dated option
OPTIONS_TARGET_PCT  = 0.35   # Simulated option-level take-profit
OPTIONS_STOP_PCT    = 0.25   # Simulated option-level stop
# Bad-news keywords that trigger an immediate exit of an ACTIVE position
BAD_STOCK_KEYWORDS = ['downgrade','lawsuit','recall','fraud investigation',
    'plunges','sell-off','selloff','misses estimates','cuts guidance',
    'accounting fraud','bankruptcy','delisted','sec investigation',
    'short report','class action','subpoena','ceo resigns','ceo steps down',
    'restated earnings','going concern']

# ── PRICE CACHE ───────────────────────────────────────────────────────
_prices    = {}
_prev      = {}
_price_lock= threading.Lock()
_cache_ts  = 0

def get_rt_prices(syms):
    out = {}
    if alpaca_data and syms:
        try:
            req    = StockLatestTradeRequest(symbol_or_symbols=[s for s in syms if s != 'BTC-USD'])
            trades = alpaca_data.get_stock_latest_trade(req)
            for sym, t in trades.items():
                p    = float(t.price)
                prev = _prev.get(sym, p)
                pct  = ((p-prev)/prev*100) if prev else 0
                out[sym] = {'symbol':sym,'price':round(p,2),'pct':round(pct,3),
                            'change':round(p-prev,4),'source':'alpaca',
                            'time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            return out
        except Exception as e:
            print(f"  Alpaca rt: {e}")
    try:
        df = yf.download([s for s in syms if s != 'BTC-USD'],
                         period='2d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            if sym == 'BTC-USD': continue
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if not len(cl): continue
                p    = float(cl.iloc[-1]); prev = float(cl.iloc[-2]) if len(cl)>=2 else p
                pct  = ((p-prev)/prev*100) if prev else 0
                out[sym] = {'symbol':sym,'price':round(p,2),'pct':round(pct,3),
                            'change':round(p-prev,4),'source':'yfinance',
                            'time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            except: pass
    except: pass
    return out

def price_thread():
    global _cache_ts
    load_prev_closes()
    interval = 2 if alpaca_data else 30
    while True:
        try:
            syms = list({'QQQ','SPY','BTC-USD'}
                       | {t['symbol'] for t in paper['active'].values()}
                       | set(custom_watchlist))
            result = get_rt_prices(list(syms))
            try:
                df = yf.download(['BTC-USD'], period='2d', interval='1d',
                                  auto_adjust=True, progress=False)
                cl = df['Close'].dropna() if not isinstance(df.columns,pd.MultiIndex) else df['Close']['BTC-USD'].dropna()
                if len(cl):
                    p=float(cl.iloc[-1]); pv=float(cl.iloc[-2]) if len(cl)>=2 else p
                    result['BTC-USD']={'symbol':'BTC-USD','price':round(p,2),
                        'pct':round((p-pv)/pv*100,3),'change':round(p-pv,4),
                        'source':'yfinance','time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            except: pass
            with _price_lock:
                _prices.update(result); _cache_ts = time.time()
        except Exception as e:
            print(f"  Price thread: {e}")
        time.sleep(interval)

def cp(sym):
    with _price_lock: return _prices.get(sym)
def all_prices():
    with _price_lock: return dict(_prices)

def load_prev_closes():
    syms = list({'QQQ','SPY','SOXL','TQQQ','MSTR','COIN'} | set(custom_watchlist))
    try:
        df = yf.download(syms, period='5d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl)>=2: _prev[sym] = float(cl.iloc[-2])
            except: pass
    except: pass

# ── TELEGRAM ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body=json.dumps({'chat_id':str(TELEGRAM_CHAT).strip(),'text':str(msg),
                         'disable_web_page_preview':True}).encode()
        req=urllib.request.Request(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body,headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req,timeout=12) as r:
            return json.loads(r.read()).get('ok',False)
    except Exception as e:
        print(f"  Telegram: {e}"); return False

# ── HELPERS ────────────────────────────────────────────────────────────
def is_market_open():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    return n.weekday()<5 and 570<=t<960
def is_trading_day(): return datetime.now(ET_TZ).weekday()<5
def now_et(): return datetime.now(ET_TZ).strftime('%H:%M ET')
def today_str(): return datetime.now(ET_TZ).strftime('%a %b %d, %Y')

def market_ok_to_trade():
    spy=cp('SPY'); qqq=cp('QQQ')
    if not spy or not qqq: return True, 0, 0
    ok = not (spy['pct'] < RULES['MIN_SPY_PCT'] and qqq['pct'] < RULES['MIN_SPY_PCT'])
    return ok, spy['pct'], qqq['pct']

def can_enter():
    now=datetime.now(ET_TZ); t=now.hour*60+now.minute
    if not is_market_open(): return False,"Market closed"
    if t < 9*60+45: return False,f"Too early — entering at 9:45 AM"
    if t >= 14*60+30: return False,"Too late — no entries after 2:30 PM"
    ok,sp,qp = market_ok_to_trade()
    if not ok: return False,f"Market bearish (SPY {sp:+.2f}% QQQ {qp:+.2f}%) — protecting capital"
    return True,"OK"

# ── PRE-MARKET ENTRY (watchlist only) ──────────────────────────────────
# Liquidity and spreads pre-market are much worse for unknown universe
# stocks — only Abiy's own hand-picked watchlist is eligible here, and
# entries use a LIMIT order with extended_hours=True since Alpaca rejects
# market orders outside regular session hours.
def is_premarket_window():
    n = datetime.now(ET_TZ); t = n.hour*60 + n.minute
    start = RULES['PREMARKET_START'][0]*60 + RULES['PREMARKET_START'][1]
    return n.weekday() < 5 and start <= t < 570

def can_enter_premarket():
    if not RULES.get('PREMARKET_ENTRY_ENABLED'): return False, "Pre-market entry disabled"
    if not is_premarket_window(): return False, "Not in pre-market window"
    if paper['trade_seq'] > RULES['MAX_TRADES_PER_DAY']: return False, "Max trades reached"
    if not custom_watchlist: return False, "No watchlist symbols set"
    return True, "OK"

# ── PRO DAY-TRADE SCORING ─────────────────────────────────────────────
def score_day_trade(q, spy_pct=0, qqq_pct=0):
    """
    Score optimized for stocks that move 10%+ intraday.
    Key insight: 3x ETFs + pre-market gap + high volume = best setup.
    """
    sym  = q.get('symbol','')
    p    = q.get('regularMarketPrice',0)
    chg  = q.get('regularMarketChangePercent',0)
    vr   = q.get('_vr',0)
    pre  = q.get('preMarketChangePercent',0)
    hi52 = q.get('fiftyTwoWeekHigh',p) or p
    lo52 = q.get('fiftyTwoWeekLow',p*0.3) or p*0.3
    mc   = q.get('marketCap',0)
    if p < 1: return 0

    score = 0
    mkt_bias = (spy_pct + qqq_pct) / 2

    # ── 1. ANNUAL VOLATILITY RANGE (25pts) ────────────────────────────
    # The single best predictor: how much does this stock move in a year?
    # SOXL moves 300%+ per year — that's what we want for 10% daily
    # AAPL moves 30% per year — wrong stock for this strategy
    annual_range = ((hi52-lo52)/lo52*100) if lo52>0 else 0
    if   annual_range > 400: score += 25  # 3x ETF territory (SOXL, LABU)
    elif annual_range > 250: score += 21  # Very high beta
    elif annual_range > 150: score += 16  # High beta (NVDA, COIN)
    elif annual_range > 100: score += 11  # Mid beta
    elif annual_range > 60:  score += 6
    elif annual_range < 30:  score -= 15  # Too stable, wrong stock

    # ── 2. TODAY'S MOMENTUM (25pts) ───────────────────────────────────
    # Stock already moving = momentum continuation. Don't chase reversal.
    if   chg > 12: score += 25
    elif chg > 8:  score += 21
    elif chg > 5:  score += 16
    elif chg > 3:  score += 11
    elif chg > 1:  score += 6
    elif chg > 0:  score += 2
    elif chg < -5: score -= 20  # Strong downtrend, avoid
    elif chg < -2: score -= 10
    elif chg < 0:  score -= 3

    # ── 3. VOLUME SURGE (20pts) ───────────────────────────────────────
    # Volume is king — no volume = no conviction = no trade
    if   vr > 8:  score += 20
    elif vr > 5:  score += 17
    elif vr > 3:  score += 13
    elif vr > 2:  score += 8
    elif vr > 1.5:score += 4
    elif vr > 1:  score += 1
    else:         score -= 12  # Zero volume = never trade

    # ── 4. PRE-MARKET GAP (15pts) ─────────────────────────────────────
    # Gap up pre-market = institutions positioned overnight
    if   pre > 8:  score += 15
    elif pre > 5:  score += 13
    elif pre > 3:  score += 10
    elif pre > 1:  score += 6
    elif pre > 0:  score += 2
    elif pre < -2: score -= 8

    # ── 5. MARKET ALIGNMENT (10pts) ───────────────────────────────────
    # Trade WITH the tide. If market is up, go long.
    if   mkt_bias > 1.5: score += 10
    elif mkt_bias > 0.5: score += 7
    elif mkt_bias > 0:   score += 3
    elif mkt_bias < -1:  score -= 15
    elif mkt_bias < -0.3:score -= 5

    # ── BONUSES ───────────────────────────────────────────────────────
    # 3x ETF + up market = perfect combo (multiplies sector move 3x)
    if sym in UNIVERSE['3x_etf'] and mkt_bias > 0 and chg > 2:
        score += 10

    # Crypto play + BTC moving
    btc = cp('BTC-USD')
    if sym in UNIVERSE['crypto'] and btc and btc['pct'] > 2:
        score += 8

    # Small/mid market cap = more volatile = easier to move
    if   mc < 2e9:  score += 5   # Small cap
    elif mc < 10e9: score += 3   # Mid cap
    elif mc > 500e9:score -= 8   # Too big, moves too slow

    return min(99, max(0, round(score)))

def compute_atr_levels(symbol, entry_price):
    """Compute ATR-based stop and target.
    ATR (Average True Range) measures how much a stock actually moves per bar
    on average — a $75 stock with ATR of $1.50 needs a wider stop than a $200
    stock with ATR of $1.50. Using 1.5x ATR for the stop prevents the common
    problem of getting shaken out on normal volatility noise.

    Also finds the nearest recent support (for stop) and resistance (for target)
    from the last 20 bars of 5-min data, which gives better levels than a flat %.
    """
    try:
        df = fetch_intraday_bars(symbol)
        if df.empty or len(df) < 10:
            raise ValueError("not enough bars")
        high = df['High'].values
        low  = df['Low'].values
        close = df['Close'].values
        # True Range and ATR (14-period)
        tr = [max(high[i]-low[i], abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
              for i in range(1,len(close))]
        atr = sum(tr[-14:]) / min(14, len(tr))
        # Support = lowest low of last 20 bars; Resistance = highest high
        support    = float(min(low[-20:]))
        resistance = float(max(high[-20:]))
        # Stop: entry - 1.5x ATR, but not below recent support
        stop_atr  = entry_price - 1.5 * atr
        stop      = round(max(stop_atr, support * 0.995), 2)
        # Target: entry + 2x ATR minimum, aim for resistance if it's higher
        target_atr = entry_price + 2.0 * atr
        target = round(max(target_atr, resistance), 2)
        # Sanity bounds — stop never more than 8% below entry, target never more than 20% above
        stop   = max(stop,   round(entry_price * 0.92, 2))
        target = min(target, round(entry_price * 1.20, 2))
        stop_pct   = (entry_price - stop)   / entry_price
        target_pct = (target - entry_price) / entry_price
        return {'stop':stop,'target':target,'atr':round(atr,3),
                'stop_pct':round(stop_pct*100,1),'target_pct':round(target_pct*100,1),
                'support':round(support,2),'resistance':round(resistance,2)}
    except Exception:
        # Fallback to sensible defaults — wider than the old 4%/8% to avoid noise exits
        return {'stop':round(entry_price*0.94,2),'target':round(entry_price*1.10,2),
                'atr':None,'stop_pct':6.0,'target_pct':10.0,
                'support':round(entry_price*0.94,2),'resistance':round(entry_price*1.10,2)}
    """5-min bars for today's session, used by both the chart and the signal."""
    try:
        df = yf.download(symbol, period='1d', interval='5m', auto_adjust=True, progress=False)
        if df.empty:
            df = yf.download(symbol, period='5d', interval='15m', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except Exception:
        return pd.DataFrame()

def compute_intraday_signal(symbol):
    """VWAP + 9/20 EMA — research on what professional day traders actually
    watch consistently points to this exact 2-indicator combo over loading
    a chart with 8+ indicators (more indicators measured worse win rates in
    several studies). VWAP = institutional fair value for today; 9/20 EMA
    cross = short-term momentum direction. Buy when price is above VWAP,
    the fast EMA is above the slow EMA, and price is actually rising —
    sell/wait when that breaks down."""
    df = fetch_intraday_bars(symbol)
    if df.empty or len(df) < 6:
        return None
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    cum_vol = df['Volume'].cumsum().replace(0, 1)
    vwap = (typical * df['Volume']).cumsum() / cum_vol
    ema9 = df['Close'].ewm(span=9, adjust=False).mean()
    ema20 = df['Close'].ewm(span=20, adjust=False).mean()
    last_close = float(df['Close'].iloc[-1])
    last_vwap  = float(vwap.iloc[-1])
    last_ema9  = float(ema9.iloc[-1])
    last_ema20 = float(ema20.iloc[-1])
    rising = last_close > float(df['Close'].iloc[-3])
    above_vwap = last_close > last_vwap
    ema_bull = last_ema9 > last_ema20

    if above_vwap and ema_bull and rising:
        signal, reason = 'BUY', f"Above VWAP (${last_vwap:.2f}), 9EMA>20EMA, rising"
    elif (not above_vwap) and (not ema_bull):
        signal, reason = 'SELL', f"Below VWAP (${last_vwap:.2f}), 9EMA<20EMA — momentum bearish"
    else:
        signal, reason = 'WAIT', f"Mixed: {'above' if above_vwap else 'below'} VWAP, 9EMA {'>' if ema_bull else '<'} 20EMA"
    return {
        'signal': signal, 'reason': reason, 'price': round(last_close,2),
        'vwap': round(last_vwap,2), 'ema9': round(last_ema9,2), 'ema20': round(last_ema20,2),
        'vwap_series': [round(float(v),2) for v in vwap.tolist()],
        'ema9_series': [round(float(v),2) for v in ema9.tolist()],
        'ema20_series': [round(float(v),2) for v in ema20.tolist()],
    }

def get_hold_signal(t):
    """Real-time hold/sell/exit recommendation based on trade position"""
    entry  = t['entry']
    cur    = t.get('current', entry)
    target = t['target']
    stop   = t['stop']
    peak   = t.get('peak', cur)
    shares = t['shares']

    pnl_pct          = (cur - entry) / entry * 100
    pct_to_target    = (target - cur) / cur * 100
    drawdown_from_peak = (peak - cur) / peak * 100 if peak > 0 else 0
    pct_to_stop      = (cur - stop) / cur * 100

    if cur >= target:
        return {'signal':'SELL','color':'green','icon':'💰',
                'msg':f'TARGET REACHED! Sell now for {pnl_pct:.1f}% gain = ${(cur-entry)*shares:.0f}'}
    elif cur <= stop:
        return {'signal':'EXIT','color':'red','icon':'🛑',
                'msg':f'STOP HIT — Exit immediately. Loss: {pnl_pct:.1f}% = ${(cur-entry)*shares:.0f}'}
    elif pnl_pct > 7 and drawdown_from_peak > 2.5:
        return {'signal':'TAKE PROFIT','color':'amber','icon':'⚡',
                'msg':f'Pulling back from peak ${peak:.2f}. Consider taking profit at {pnl_pct:.1f}%'}
    elif pnl_pct > 5:
        return {'signal':'HOLD STRONG','color':'green','icon':'📈',
                'msg':f'Strong move. {pct_to_target:.1f}% to target. Trail your stop up to ${entry:.2f}'}
    elif pnl_pct > 2:
        return {'signal':'HOLD','color':'green','icon':'✅',
                'msg':f'Good progress. {pct_to_target:.1f}% to target. Stop cushion: {pct_to_stop:.1f}%'}
    elif pnl_pct > 0:
        return {'signal':'HOLD','color':'green','icon':'✅',
                'msg':f'Slightly up. Watching. {pct_to_target:.1f}% to target.'}
    elif pnl_pct > -1.5:
        return {'signal':'HOLD','color':'amber','icon':'⏳',
                'msg':f'Flat/small pullback — normal. Stop still {pct_to_stop:.1f}% away.'}
    elif pnl_pct > -3:
        return {'signal':'WATCH','color':'red','icon':'⚠️',
                'msg':f'Getting close to stop. {pct_to_stop:.1f}% cushion left. Ready to exit.'}
    else:
        return {'signal':'EXIT','color':'red','icon':'🛑',
                'msg':f'Near stop loss. Consider exiting now to preserve capital.'}

def get_full_quotes(symbols):
    results = []
    try:
        # No truncation — scan the FULL universe every time. This is what
        # was silently capping scans to 20 random stocks and missing movers.
        batch = list(dict.fromkeys([s for s in symbols if s]))
        df5  = yf.download(batch, period='5d', interval='1d', auto_adjust=True, progress=False)
        df1y = yf.download(batch, period='1y', interval='1d', auto_adjust=True, progress=False)
        def fi(s):
            try: return s, yf.Ticker(s).info or {}
            except: return s, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
            info_map = dict(ex.map(fi, batch))
        multi = isinstance(df5.columns, pd.MultiIndex)
        for sym in batch:
            try:
                c5  = (df5['Close'][sym] if multi else df5['Close']).dropna()
                v5  = (df5['Volume'][sym] if multi else df5['Volume']).dropna()
                c1y = (df1y['Close'][sym] if multi else df1y['Close']).dropna()
                v1y = (df1y['Volume'][sym] if multi else df1y['Volume']).dropna()
                if not len(c5): continue
                price=float(c5.iloc[-1]); prev=float(c5.iloc[-2]) if len(c5)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                info=info_map.get(sym,{}); avg=int(v1y.mean()) if len(v1y) else 1
                vr=round(int(v5.iloc[-1])/avg,1) if avg and len(v5) else 0
                pre_p=float(info.get('preMarketPrice') or 0)
                pre_chg=((pre_p-price)/price*100) if pre_p and price else 0
                hi52=round(float(c1y.max()),2) if len(c1y) else price
                lo52=round(float(c1y.min()),2) if len(c1y) else price
                results.append({
                    'symbol':sym,'shortName':info.get('shortName',sym),
                    'regularMarketPrice':round(price,2),
                    'regularMarketChangePercent':round(chg,4),
                    'marketCap':int(info.get('marketCap') or 0),
                    'fiftyTwoWeekHigh':hi52,'fiftyTwoWeekLow':lo52,
                    'preMarketChangePercent':round(pre_chg,2),
                    'preMarketPrice':round(pre_p,2),'_vr':vr,
                })
            except: pass
    except Exception as e:
        print(f"  Quotes: {e}")
    return results

_quotes_cache = {'data':{}, 'ts':0}
QUOTES_TTL = 45  # seconds — long enough to avoid hammering yfinance on rapid rotation

def get_full_quotes_cached(symbols):
    now = time.time()
    if now - _quotes_cache['ts'] < QUOTES_TTL and _quotes_cache['data']:
        return list(_quotes_cache['data'].values())
    fresh = get_full_quotes(symbols)
    _quotes_cache['data'] = {q['symbol']: q for q in fresh}
    _quotes_cache['ts'] = now
    return fresh

# ── DEDICATED WATCHLIST QUOTES (fast, separate cache — up to 5 stocks) ─
_watchlist_quotes_cache = {'data':{}, 'ts':0}
WATCHLIST_QUOTES_TTL = 20  # refresh much faster than the 82-stock universe
WATCHLIST_MIN_SCORE = 50   # more lenient than universe MIN_SCORE — Abiy chose these himself

def get_watchlist_quotes():
    if not custom_watchlist: return []
    now = time.time()
    if now - _watchlist_quotes_cache['ts'] < WATCHLIST_QUOTES_TTL and _watchlist_quotes_cache['data']:
        return list(_watchlist_quotes_cache['data'].values())
    fresh = get_full_quotes(custom_watchlist)
    _watchlist_quotes_cache['data'] = {q['symbol']: q for q in fresh}
    _watchlist_quotes_cache['ts'] = now
    return fresh

def score_watchlist(spy_pct=0, qqq_pct=0):
    out = []
    for q in get_watchlist_quotes():
        q['_score'] = score_day_trade(q, spy_pct, qqq_pct)
        q['_qualifies'] = q['_score'] >= WATCHLIST_MIN_SCORE and q.get('_vr',0) >= 1.0
        out.append(q)
    out.sort(key=lambda x: x['_score'], reverse=True)
    return out

_last_watchlist_notify = {}

# ── PAPER TRADING ─────────────────────────────────────────────────────
paper = {
    'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
    'stop_pct':RULES['STOP_PCT'],
    'active':{},'completed':[],'total_pnl':0.0,
    'status':'waiting','picked':None,'date':None,'log':[],
    'candidates':[],'better_opp':None,'skipped':[],'trade_alert':None,
    'pro_locked_symbol':None,  # Abiy PRO Live: once set, the bot ONLY trades
                               # this one symbol (buy signal -> auto re-buy
                               # cycle) instead of rotating the universe.
}

def p_log(msg):
    ts=now_et(); paper['log'].insert(0,{'time':ts,'msg':msg})
    paper['log']=paper['log'][:40]; print(f"  [{ts}] {msg}")

# ── SIMULATED OPTIONS CANDIDATES TRACKER ───────────────────────────────
# Educational estimate only — NOT real options data or a real options fill.
# Approximates an option's P&L as (underlying % move) x OPTIONS_LEVERAGE.
options_tracker = {'candidates': [], 'completed': []}

# ── PERSISTENCE ──────────────────────────────────────────────────────
# Render's free tier sleeps after ~15 min of no traffic, which kills this
# whole Python process — wiping every in-memory variable. That's why trade
# history was disappearing. STATE_FILE lets today's session survive a
# restart; HISTORY_FILE is a permanent, ever-growing log across all days
# for the performance analytics. Neither survives a brand-new code deploy
# on Render's free tier (ephemeral disk), so Telegram remains the ultimate
# backup record regardless.
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DATA_DIR, 'autotrade_state.json')
HISTORY_FILE = os.path.join(DATA_DIR, 'autotrade_history.json')

def save_state():
    try:
        snapshot = {
            'date': paper['date'], 'capital': paper['capital'],
            'starting_capital': paper['starting_capital'], 'trade_seq': paper['trade_seq'],
            'status': paper['status'], 'completed': paper['completed'],
            'total_pnl': paper['total_pnl'], 'skipped': paper.get('skipped', []),
            'custom_watchlist': custom_watchlist,
            'options_completed': options_tracker['completed'],
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(snapshot, f)
    except Exception as e:
        print(f"  Save state: {e}")

def load_state():
    global custom_watchlist
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE) as f:
            snap = json.load(f)
    except Exception as e:
        print(f"  Load state: {e}"); return
    custom_watchlist = snap.get('custom_watchlist', custom_watchlist)
    today = datetime.now(ET_TZ).date().isoformat()
    if snap.get('date') != today:
        return  # different day — only the watchlist carries forward
    paper.update({
        'date': snap['date'], 'capital': snap.get('capital', paper['capital']),
        'starting_capital': snap.get('starting_capital', paper['starting_capital']),
        'trade_seq': snap.get('trade_seq', 1), 'completed': snap.get('completed', []),
        'total_pnl': snap.get('total_pnl', 0.0), 'skipped': snap.get('skipped', []),
        'status': 'waiting', 'active': {},
    })
    options_tracker['completed'] = snap.get('options_completed', [])
    # Reconcile with Alpaca's REAL open positions — don't trust stale local
    # state about an active trade, since nothing was monitoring it for
    # target/stop while the process was asleep.
    if alpaca_trading:
        try:
            positions = alpaca_trading.get_all_positions()
            if positions:
                p = positions[0]
                sym = p.symbol; entry = float(p.avg_entry_price); shares = int(float(p.qty))
                paper['active'] = {sym: {
                    'symbol': sym, 'name': sym, 'trade_num': paper['trade_seq'], 'shares': shares,
                    'entry': round(entry,2), 'cost': round(entry*shares,2),
                    'target': round(entry*(1+RULES['TARGET_PCT']),2),
                    'stop': round(entry*(1-RULES['STOP_PCT']),2),
                    'entry_time': 'resumed after restart', 'current': round(entry,2),
                    'peak': round(entry,2), 'peak_pnl_pct': 0.0, 'entered_at': time.time(),
                    'score': 0, 'manual': True, 'via_alpaca': True,
                }}
                paper['status'] = 'entered'
                p_log(f"Reconciled with Alpaca: found live position {sym} {shares}sh @ ${entry:.2f} — resuming monitoring")
        except Exception as e:
            print(f"  Reconcile Alpaca positions: {e}")
    p_log(f"Resumed session from disk: {len(paper['completed'])} trades today, P&L ${paper['total_pnl']:+.2f}")

def append_history(record):
    """Permanent all-time trade log, separate from the per-day 'completed' list."""
    try:
        hist = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                hist = json.load(f)
        hist.append({**record, 'date': paper.get('date')})
        hist = hist[-3000:]
        with open(HISTORY_FILE, 'w') as f:
            json.dump(hist, f)
    except Exception as e:
        print(f"  Append history: {e}")

def paper_reset():
    if alpaca_trading:
        try: alpaca_trading.cancel_orders()
        except: pass
    paper.update({
        'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
        'active':{},'completed':[],'total_pnl':0.0,'status':'waiting',
        'picked':None,'date':datetime.now(ET_TZ).date().isoformat(),
        'log':[],'candidates':[],'better_opp':None,'skipped':[],'trade_alert':None,
    })
    options_tracker['candidates'] = []
    options_tracker['completed'] = []

def pick_best(spy_pct=0, qqq_pct=0, force=False, exclude=None):
    already = set([t['symbol'] for t in paper['completed']] + paper.get('skipped',[]))
    if exclude: already |= set(exclude)
    # Deterministic order (no random set sampling) — full universe every time
    watchlist = list(dict.fromkeys(ALL_UNIVERSE + custom_watchlist))
    quotes = get_full_quotes_cached(watchlist)
    scored = []
    for q in quotes:
        if q['symbol'] in already: continue
        if q.get('regularMarketPrice',0) < 1: continue
        s = score_day_trade(q, spy_pct, qqq_pct)
        q['_score'] = s; scored.append(q)
    scored.sort(key=lambda x: x['_score'], reverse=True)
    paper['candidates'] = scored[:10]
    if not scored: return None
    best = scored[0]
    if not force and best['_score'] < RULES['MIN_SCORE']:
        p_log(f"Best pick {best['symbol']} score {best['_score']}/99 < min {RULES['MIN_SCORE']} — no trade")
        return None
    if not force and best.get('_vr',0) < RULES['MIN_VOLUME_RATIO']:
        p_log(f"Best pick {best['symbol']} vol {best.get('_vr',0):.1f}x < min {RULES['MIN_VOLUME_RATIO']}x — no trade")
        return None
    return best

def enter_trade(q, manual=False, bypass_hours=False):
    if not manual and not bypass_hours:
        ok, reason = can_enter()
        if not ok:
            p_log(f"Entry blocked: {reason}")
            if reason != "Too early — entering at 9:45 AM":
                tg(f"⛔ Entry blocked: {reason}")
            return False

    tnum = paper['trade_seq']
    ep   = q.get('regularMarketPrice', 0)
    cached = cp(q['symbol'])
    if cached: ep = cached['price']
    cap  = paper['capital']
    shrs = int(cap / ep) if ep > 0 else 0
    if shrs < 1:
        p_log(f"Not enough capital for {q['symbol']} @ ${ep}"); return False

    # Compute ATR-based stop/target for this specific stock's volatility
    levels = compute_atr_levels(q['symbol'], ep)
    tp    = levels['target_pct'] / 100
    stop  = levels['stop_pct']   / 100
    trade = {
        'symbol':q['symbol'],'name':q.get('shortName',q['symbol']),
        'trade_num':tnum,'shares':shrs,'entry':round(ep,2),
        'cost':round(shrs*ep,2),
        'target':levels['target'],'stop':levels['stop'],
        'target_pct':tp,'atr':levels.get('atr'),
        'support':levels.get('support'),'resistance':levels.get('resistance'),
        'entry_time':now_et(),'current':round(ep,2),'peak':round(ep,2),
        'peak_pnl_pct':0.0,'entered_at':time.time(),
        'score':q.get('_score',0),'manual':manual,
        'via_alpaca':alpaca_trading is not None,
    }
    if alpaca_trading:
        try:
            if is_premarket_window():
                limit_px = round(ep * 1.003, 2)  # small buffer above last price to help fill
                oid=str(alpaca_trading.submit_order(LimitOrderRequest(
                    symbol=q['symbol'],qty=shrs,side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,limit_price=limit_px,extended_hours=True)).id)
                trade['order_id']=oid; trade['premarket']=True
            else:
                oid=str(alpaca_trading.submit_order(MarketOrderRequest(
                    symbol=q['symbol'],qty=shrs,side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY)).id)
                trade['order_id']=oid
        except Exception as e:
            p_log(f"Alpaca order: {e}")

    paper['active'][q['symbol']] = trade
    paper['status'] = 'entered'
    paper['capital'] = 0
    paper['better_opp'] = None
    save_state()
    p_log(f"ENTERED {q['symbol']} {shrs}sh @ ${ep:.2f} target=${trade['target']:.2f}")

    reason_str = "manual entry" if manual else f"Score {q.get('_score',0)}/99"
    tg(
        f"📈 TRADE {tnum} ENTERED {'(MANUAL)' if manual else ''}\n{'━'*22}\n"
        f"Stock:   {q['symbol']} — {q.get('shortName','')}\n"
        f"Shares:  {shrs} @ ${ep:.2f}\n"
        f"Cost:    ${shrs*ep:,.2f}\n{'━'*22}\n"
        f"Target:  ${trade['target']:.2f} (+{tp*100:.0f}%)\n"
        f"Stop:    ${trade['stop']:.2f} (-{stop*100:.0f}%)\n"
        f"Why:     {reason_str}\n{'━'*22}\n"
        f"Dashboard: {get_app_url()}"
    )
    return True

def close_trade(sym, reason='target'):
    t = paper['active'].pop(sym, None)
    if not t: return
    cached = cp(sym)
    sell   = round(cached['price'] if cached else t['current'], 2)
    if alpaca_trading:
        try: alpaca_trading.close_position(sym)
        except: pass
    pnl     = round((sell-t['entry'])*t['shares'],2)
    pnl_pct = round((sell-t['entry'])/t['entry']*100,2)
    proceeds= round(sell*t['shares'],2)
    paper['completed'].append({**t,'exit':sell,'exit_time':now_et(),
                                'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    paper['total_pnl'] += pnl; paper['capital'] = proceeds
    append_history({**t,'exit':sell,'exit_time':now_et(),'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    save_state()
    reason_label = {'target':'TARGET','stop':'STOP','momentum_stall':'MOMENTUM STALL',
                     'rotate_better':'ROTATING','news_exit':'NEWS EXIT','eod':'EOD',
                     'manual':'MANUAL'}.get(reason, reason.upper())
    p_log(f"CLOSED {sym} {reason_label} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    icon = '💰' if reason=='target' else '🛑' if reason in ('stop','news_exit') else '🔄' if reason in ('momentum_stall','rotate_better') else '🔔'
    tg(
        f"{icon} T{t['trade_num']} CLOSED — {reason_label}\n"
        f"{'━'*22}\n{sym}: {t['shares']}sh\n"
        f"Entry ${t['entry']:.2f} → Exit ${sell:.2f}\n"
        f"P&L: {'✅ +' if pnl>=0 else '🔴 '}${abs(pnl):.2f} ({pnl_pct:+.2f}%)\n"
        f"Capital: ${proceeds:,.2f}\n"
        f"Total: {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:,.2f}"
    )
    nxt = paper['trade_seq']+1
    locked = paper.get('pro_locked_symbol')
    multi_day_closed = t.get('multi_day', False)
    can_rotate, _ = can_enter() if not locked else (True, "OK")
    if nxt<=RULES['MAX_TRADES_PER_DAY'] and is_market_open() and can_rotate:
        paper['trade_seq']=nxt; paper['status']='waiting'
        if locked:
            tg(f"🎯 Watching {locked} for the next VWAP/EMA buy signal (Abiy PRO Live)...")
        else:
            tg(f"🔍 Trade {nxt}/{RULES['MAX_TRADES_PER_DAY']} — scanning for next setup...")
        threading.Thread(target=_auto_pick_enter, daemon=True).start()
    elif nxt<=RULES['MAX_TRADES_PER_DAY'] and is_market_open():
        paper['status']='waiting'
    else:
        paper['status']='done'
        tg(f"✅ Max trades ({RULES['MAX_TRADES_PER_DAY']}) reached for today!\nTotal: {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:,.2f}\n{get_app_url()}")

_pick_lock = threading.Lock()
_picking_now = False

def _auto_pick_enter():
    global _picking_now, _last_no_setup_notify
    with _pick_lock:
        if _picking_now:
            return  # already scanning in another thread — don't overlap
        _picking_now = True
    try:
        time.sleep(3)

        # -1. TOP PRIORITY: Abiy PRO Live locked symbol. Once he's manually
        # bought a stock from that tab, the bot stops rotating the universe
        # entirely and ONLY watches this one symbol for a fresh VWAP/EMA buy
        # signal, re-entering automatically — no more button clicks needed
        # until he releases the lock.
        locked = paper.get('pro_locked_symbol')
        if locked and is_market_open():
            sig = compute_intraday_signal(locked)
            if sig and sig['signal'] == 'BUY':
                cached = cp(locked)
                if cached:
                    q = {'symbol':locked,'shortName':locked,'regularMarketPrice':cached['price'],
                         '_vr':2,'regularMarketChangePercent':cached['pct'],'marketCap':5e9,
                         'fiftyTwoWeekHigh':0,'fiftyTwoWeekLow':0,'preMarketChangePercent':0,'_score':0}
                    tg(f"🎯 ABIY PRO LIVE — re-entering {locked}\n{'━'*22}\n{sig['reason']}\nAuto re-buy from your locked symbol.")
                    paper['picked']=q; paper['status']='picked'
                    enter_trade(q, manual=True)
            return
        elif locked:
            return  # locked but market closed — just wait, don't fall through to universe

        # 0. PRE-MARKET window (watchlist only, limit orders, extra caution)
        if is_premarket_window() and not is_market_open():
            ok, reason = can_enter_premarket()
            if not ok:
                return
            spy=cp('SPY'); qqq=cp('QQQ')
            best_wl = next((q for q in score_watchlist(spy['pct'] if spy else 0, qqq['pct'] if qqq else 0) if q['_qualifies']), None)
            if best_wl:
                last = _last_watchlist_notify.get(best_wl['symbol'], 0)
                if time.time() - last > 60:
                    tg(f"🌙 PRE-MARKET WATCHLIST OPPORTUNITY — {best_wl['symbol']}\n{'━'*22}\n"
                       f"Score {best_wl['_score']}/99 | Vol {best_wl.get('_vr',0):.1f}x | "
                       f"Chg {best_wl.get('regularMarketChangePercent',0):+.1f}%\n"
                       f"Entering with a LIMIT order (extended hours) — pre-market liquidity "
                       f"is thinner, so this may fill partially or not at all.\n{get_app_url()}")
                    _last_watchlist_notify[best_wl['symbol']] = time.time()
                paper['picked'] = best_wl; paper['status'] = 'picked'
                enter_trade(best_wl, bypass_hours=True)
            return

        ok, reason = can_enter()
        if not ok:
            p_log(f"Auto-pick skipped: {reason}"); return
        spy=cp('SPY'); qqq=cp('QQQ')
        spy_pct = spy['pct'] if spy else 0
        qqq_pct = qqq['pct'] if qqq else 0

        # 1. PRIORITY: Abiy's own watchlist (up to 5 stocks) gets checked first,
        # with a more lenient bar since these were hand-picked, not algorithm-found.
        if custom_watchlist:
            best_wl = next((q for q in score_watchlist(spy_pct, qqq_pct) if q['_qualifies']), None)
            if best_wl:
                last = _last_watchlist_notify.get(best_wl['symbol'], 0)
                if time.time() - last > 60:
                    tg(f"🌟 ABIY WATCHLIST OPPORTUNITY — {best_wl['symbol']}\n{'━'*22}\n"
                       f"Score {best_wl['_score']}/99 | Vol {best_wl.get('_vr',0):.1f}x | "
                       f"Chg {best_wl.get('regularMarketChangePercent',0):+.1f}%\n"
                       f"Entering now — picked from your personal watchlist.\n{get_app_url()}")
                    _last_watchlist_notify[best_wl['symbol']] = time.time()
                paper['picked'] = best_wl; paper['status'] = 'picked'
                enter_trade(best_wl)
                return

        # 2. Fall back to the full curated universe scan
        q=pick_best(spy_pct, qqq_pct)
        if not q:
            p_log(f"No qualifying stock yet (score≥{RULES['MIN_SCORE']}, vol≥{RULES['MIN_VOLUME_RATIO']}x) — will keep scanning every minute")
            if time.time() - _last_no_setup_notify > 600:  # at most once every 10 min
                tg(f"⚠️ Still scanning — no stock meets standards yet (score≥{RULES['MIN_SCORE']}, vol≥{RULES['MIN_VOLUME_RATIO']}x). Will keep checking every minute automatically — no need to Force Pick.")
                _last_no_setup_notify = time.time()
            paper['status'] = 'waiting'
            return
        paper['picked']=q; paper['status']='picked'
        _notify_pick(q); time.sleep(3); enter_trade(q)
    finally:
        _picking_now = False

def _notify_pick(q):
    tnum=paper['trade_seq']
    cached=cp(q['symbol']); price=cached['price'] if cached else q['regularMarketPrice']
    shrs=int(paper['capital']/price) if price else 0
    lvl=compute_atr_levels(q['symbol'], price)
    top3=paper.get('candidates',[])[:3]
    alts='\n'.join(f"  #{i+1} {c['symbol']} score={c['_score']}/99 chg={c.get('regularMarketChangePercent',0):+.1f}% vol={c.get('_vr',0):.1f}x" for i,c in enumerate(top3))
    atr_note=f" (ATR ${lvl['atr']:.2f})" if lvl.get('atr') else ""
    tg(
        f"🔍 Trade {tnum}/{RULES['MAX_TRADES_PER_DAY']} PICKED — {q['symbol']}\n{'━'*22}\n"
        f"Price:  ${price:.2f} | Pre-mkt {q.get('preMarketChangePercent',0):+.1f}%\n"
        f"Score:  {q.get('_score',0)}/99 | Vol {q.get('_vr',0):.1f}x\n"
        f"Range:  ${q.get('fiftyTwoWeekLow',0):.0f} - ${q.get('fiftyTwoWeekHigh',0):.0f}\n{'━'*22}\n"
        f"Capital ${paper['capital']:,.2f} | ~{shrs}sh\n"
        f"Target ${lvl['target']:.2f} (+{lvl['target_pct']:.1f}%){atr_note}\n"
        f"Stop   ${lvl['stop']:.2f} (-{lvl['stop_pct']:.1f}%) | Support ${lvl['support']:.2f}\n{'━'*22}\n"
        f"Top candidates:\n{alts}\n"
        f"Reply 'enter' or use dashboard to confirm\n{get_app_url()}"
    )

# ── SCHEDULER ─────────────────────────────────────────────────────────
def get_real_option_quote(symbol, underlying_price):
    """Tries to fetch a real near-the-money, near-term call contract's live
    quote from Alpaca. Returns None on ANY failure (no entitlement, no
    contracts, API mismatch) so callers cleanly fall back to the simulated
    estimate — this should never be able to crash the app."""
    if not ALPACA_OPTIONS_AVAILABLE or not alpaca_options_data:
        return None
    try:
        chain = alpaca_options_data.get_option_chain(OptionChainRequest(underlying_symbol=symbol))
        if not chain:
            return None
        best, best_diff = None, None
        for contract_sym, snap in chain.items():
            if not contract_sym[-9:-8] == 'C':  # Alpaca OCC symbol: ...C/P + 8-digit strike
                continue
            try:
                strike = float(contract_sym[-8:]) / 1000.0
            except Exception:
                continue
            quote = getattr(snap, 'latest_quote', None)
            if not quote: continue
            bid = float(getattr(quote, 'bid_price', 0) or 0)
            ask = float(getattr(quote, 'ask_price', 0) or 0)
            if bid <= 0 or ask <= 0: continue
            diff = abs(strike - underlying_price)
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best = {'contract': contract_sym, 'strike': strike, 'bid': bid,
                         'ask': ask, 'mid': round((bid+ask)/2, 2)}
        return best
    except Exception as e:
        print(f"  Real option quote {symbol}: {e}")
        return None

def refresh_options_candidates():
    """Keep up to OPTIONS_MAX_SLOTS option positions filled from the current
    scored candidate list. Tries a REAL Alpaca option quote first; if that's
    not available (no entitlement, or alpaca-py too old), falls back to the
    educational leverage-estimate. Every Telegram/UI label says which mode
    is actually active — never silently mislabeled."""
    if not is_market_open(): return
    active_syms = set(paper['active'].keys())
    tracked_syms = {c['symbol'] for c in options_tracker['candidates']}
    slots_open = OPTIONS_MAX_SLOTS - len(options_tracker['candidates'])
    if slots_open <= 0: return
    for c in paper.get('candidates', []):
        if slots_open <= 0: break
        sym = c['symbol']
        if sym in active_syms or sym in tracked_syms: continue
        if c.get('_score', 0) < RULES['MIN_SCORE']: continue
        cached = cp(sym)
        price = cached['price'] if cached else c.get('regularMarketPrice', 0)
        if not price: continue

        real = get_real_option_quote(sym, price)
        if real:
            pos = {'symbol': sym, 'is_real': True, 'contract': real['contract'],
                    'strike': real['strike'], 'entry_premium': real['mid'],
                    'current_premium': real['mid'], 'entry_underlying': price,
                    'current_underlying': price, 'entry_time': now_et(),
                    'entered_at': time.time(), 'score': c.get('_score', 0), 'peak_pnl_pct': 0.0}
            options_tracker['candidates'].append(pos)
            slots_open -= 1
            p_log(f"OPTIONS (REAL) entered {sym} {real['contract']} @ ${real['mid']:.2f} premium")
            tg(f"🎯 REAL Option Candidate — {sym}\n"
               f"Contract: {real['contract']} (strike ${real['strike']:.2f})\n"
               f"Premium: ${real['mid']:.2f} (bid ${real['bid']:.2f} / ask ${real['ask']:.2f})\n"
               f"Real listed Alpaca option quote, tracked as paper — not a simulated estimate.")
            continue

        pos = {'symbol': sym, 'is_real': False, 'entry_underlying': price,
                'current_underlying': price, 'entry_time': now_et(),
                'entered_at': time.time(), 'score': c.get('_score', 0), 'peak_pnl_pct': 0.0}
        options_tracker['candidates'].append(pos)
        slots_open -= 1
        p_log(f"OPTIONS (sim) entered {sym} @ ${price:.2f} underlying")
        tg(f"🎯 Simulated Option Candidate — {sym}\n"
           f"Underlying entry: ${price:.2f} (score {c.get('_score',0)}/99)\n"
           f"Sim target: +{OPTIONS_TARGET_PCT*100:.0f}% · Sim stop: -{OPTIONS_STOP_PCT*100:.0f}%\n"
           f"(Real options data unavailable right now — using leverage estimate instead)")

def monitor_options():
    if not options_tracker['candidates']: return
    for pos in list(options_tracker['candidates']):
        sym = pos['symbol']
        if pos.get('is_real'):
            cached = cp(sym)
            underlying_price = cached['price'] if cached else pos['current_underlying']
            pos['current_underlying'] = underlying_price
            real = get_real_option_quote(sym, underlying_price)
            current_premium = real['mid'] if (real and real.get('contract')==pos.get('contract')) else pos.get('current_premium', pos['entry_premium'])
            pos['current_premium'] = current_premium
            pnl_pct = (current_premium - pos['entry_premium']) / pos['entry_premium'] * 100 if pos['entry_premium'] else 0
        else:
            cached = cp(sym)
            price = cached['price'] if cached else pos['current_underlying']
            pos['current_underlying'] = price
            underlying_pct = (price - pos['entry_underlying']) / pos['entry_underlying'] * 100
            pnl_pct = underlying_pct * OPTIONS_LEVERAGE

        pos['peak_pnl_pct'] = max(pos.get('peak_pnl_pct', 0), pnl_pct)
        pos['sim_pnl_pct'] = pnl_pct
        closed_reason = None
        if pnl_pct >= OPTIONS_TARGET_PCT*100: closed_reason = 'target'
        elif pnl_pct <= -OPTIONS_STOP_PCT*100: closed_reason = 'stop'
        elif pos['peak_pnl_pct'] >= 15 and (pos['peak_pnl_pct']-pnl_pct) >= 10: closed_reason = 'momentum_stall'
        if closed_reason:
            options_tracker['candidates'].remove(pos)
            options_tracker['completed'].insert(0, {**pos, 'exit_time': now_et(),
                'reason': closed_reason, 'sim_pnl_pct': pnl_pct})
            options_tracker['completed'] = options_tracker['completed'][:20]
            icon = '💰' if closed_reason=='target' else '🛑' if closed_reason=='stop' else '🔄'
            mode = "REAL option" if pos.get('is_real') else "Simulated option"
            disclaimer = "(Real Alpaca options quote)" if pos.get('is_real') else "(Educational estimate — not a real options fill)"
            tg(f"{icon} {mode} CLOSED — {sym} ({closed_reason.upper()})\nP&L: {pnl_pct:+.1f}%\n{disclaimer}")
            p_log(f"OPTIONS ({'real' if pos.get('is_real') else 'sim'}) closed {sym} {closed_reason} P&L {pnl_pct:+.1f}%")

def job_morning():
    if not is_trading_day(): return
    paper_reset(); load_prev_closes()
    spy=cp('SPY'); qqq=cp('QQQ')
    sp=spy['pct'] if spy else 0; qp=qqq['pct'] if qqq else 0
    mood="📈 BULLISH" if sp>0.3 and qp>0.3 else "📉 BEARISH" if sp<-0.5 else "➡ MIXED"
    tg(
        f"🌅 AUTOTRADE — {today_str()}\n{'━'*22}\n"
        f"Capital: $10,000 | Market: {mood}\n"
        f"SPY {sp:+.2f}% | QQQ {qp:+.2f}%\n"
        f"Strategy: T1 +10% · T2/3 +5%\n"
        f"Rules: Enter 9:45AM · Stop 4% · No trades after 2:30PM\n{'━'*22}\n"
        f"Scanning {len(ALL_UNIVERSE)+len(custom_watchlist)} stocks...\n{get_app_url()}"
    )
    q=pick_best(sp,qp)
    if not q:
        tg("⚠️ No qualifying setup found at 8AM. Will scan fresh at 9:45 AM."); return
    paper['picked']=q; paper['status']='picked'
    tg(f"👀 EARLY PREVIEW — {q['symbol']} looks strongest right now (score {q.get('_score',0)}/99). "
       f"This is NOT final — we re-scan with live data at 9:45 AM before risking any capital.")

def job_enter_945():
    """9:45 AM — opening volatility has settled. Routes through the SAME
    locked entry point as everything else (_auto_pick_enter), so it can
    never race with job_monitor's per-minute checks and double-fire.
    That race was the cause of the confusing duplicate 'PICKED — Capital
    $0.00' message right after a watchlist entry already executed."""
    if not is_trading_day(): return
    if paper['active']: return  # Already in a trade
    spy=cp('SPY'); qqq=cp('QQQ')
    sp=spy['pct'] if spy else 0; qp=qqq['pct'] if qqq else 0
    mkt_ok,_,_ = market_ok_to_trade()
    if not mkt_ok:
        tg(f"🚫 9:45 AM — Market too weak (SPY {sp:+.2f}% QQQ {qp:+.2f}%). Protecting capital today."); return
    p_log("9:45 AM — running fresh scan (ignoring 8AM preview)")
    threading.Thread(target=_auto_pick_enter, daemon=True).start()

# ── PER-SYMBOL NEWS CHECK (for active position emergency exit) ───────
_symbol_news_cache = {}  # sym -> (ts, bool)
def check_symbol_news(symbol):
    now = time.time()
    cached = _symbol_news_cache.get(symbol)
    if cached and now - cached[0] < 90:
        return cached[1]
    hit = False
    try:
        url = f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            tree = ET.fromstring(r.read())
            for item in tree.iter('item'):
                t = item.find('title')
                if t is None or not t.text: continue
                # Skip stale articles — only react to news from the last 24h,
                # otherwise an old headline can trigger a false-positive exit
                # seconds after entry (this is what happened with FCEL).
                pub = item.find('pubDate')
                if pub is not None and pub.text:
                    try:
                        from email.utils import parsedate_to_datetime
                        pub_dt = parsedate_to_datetime(pub.text)
                        age_hrs = (datetime.now(pub_dt.tzinfo) - pub_dt).total_seconds() / 3600
                        if age_hrs > 24:
                            continue
                    except Exception:
                        pass
                title = t.text.lower()
                if any(kw in title for kw in BAD_STOCK_KEYWORDS):
                    hit = True; break
    except Exception:
        pass
    _symbol_news_cache[symbol] = (now, hit)
    return hit

_last_rotate_check = 0

_last_no_setup_notify = 0

def job_monitor():
    if not paper['active']:
        # No trade open — keep retrying instead of sitting idle. This was the bug:
        # if 9:45's scan came up empty, nothing ever tried again until you
        # manually clicked Force Pick.
        if paper['status'] in ('waiting', 'picked') and is_trading_day():
            if paper.get('pro_locked_symbol'):
                if is_market_open():
                    threading.Thread(target=_auto_pick_enter, daemon=True).start()
            elif is_premarket_window():
                ok, _ = can_enter_premarket()
                if ok:
                    threading.Thread(target=_auto_pick_enter, daemon=True).start()
            else:
                ok, reason = can_enter()
                if ok:
                    threading.Thread(target=_auto_pick_enter, daemon=True).start()
                elif 'Max trades' in reason or 'closed' in reason:
                    pass  # nothing to do, day is over or capped
        return
    if alpaca_trading:
        try:
            pos={p.symbol:p for p in alpaca_trading.get_all_positions()}
            for sym,t in paper['active'].items():
                if sym in pos:
                    paper['active'][sym]['current']=float(pos[sym].current_price or t['current'])
        except: pass

    # Market-wide danger alert applies to every open position
    mkt_alert = scan_market_alerts()
    market_danger = mkt_alert and mkt_alert.get('type') == 'danger'

    global _last_rotate_check
    check_rotation = (time.time() - _last_rotate_check) > 600  # throttle to every ~10 min

    for sym,t in list(paper['active'].items()):
        cached=cp(sym)
        price=cached['price'] if cached else t['current']
        paper['active'][sym]['current']=price
        peak=max(price,t.get('peak',price))
        paper['active'][sym]['peak']=peak
        pnl_pct = (price - t['entry']) / t['entry'] * 100
        peak_pct = (peak - t['entry']) / t['entry'] * 100
        paper['active'][sym]['peak_pnl_pct'] = max(peak_pct, t.get('peak_pnl_pct', 0))

        # 1. News exit — heavily tightened after the DRAM/QQQ losses (Jun 18).
        # The root cause: "Fed decision" triggered market_danger with zero grace
        # period, killing every trade within 60 seconds. New rules:
        #   a) Never exit in the first 10 min (news existed before we entered)
        #   b) Market-wide alerts only auto-exit on TRUE catastrophe (exchange
        #      halt, nuclear, flash crash). Fed meetings, rate decisions, bank
        #      news etc → show the banner, but DON'T force-close positions.
        #   c) Per-symbol news needs 5-min grace AND price must be -0.5%+
        #      below entry to confirm the headline is actually hurting the stock.
        held_sec = time.time() - t.get('entered_at', time.time())
        if held_sec > 600:  # 10-minute minimum hold before any news exit
            true_catastrophe = mkt_alert and mkt_alert.get('catastrophe') and mkt_alert.get('type')=='danger'
            price_confirms_damage = price < t['entry'] * 0.995
            symbol_news_hit = held_sec > 300 and check_symbol_news(sym) and price_confirms_damage
            if true_catastrophe or symbol_news_hit:
                label = mkt_alert['title'] if true_catastrophe else f"Negative headline + price decline on {sym}"
                paper['trade_alert'] = {'symbol': sym, 'msg': f"🚨 News exit: {label}", 'ts': now_et()}
                p_log(f"NEWS EXIT {sym}: {label} (held {held_sec:.0f}s, price ${price:.2f} vs entry ${t['entry']:.2f})")
                close_trade(sym, 'news_exit')
                continue

        multi_day = t.get('multi_day', False)

        # 2. Hit profit ceiling (skip for multi-day holds — they use the stop only)
        if not multi_day and price >= t['target']:
            close_trade(sym,'target'); continue
        # 3. Hit hard stop (always applies, even multi-day)
        if price <= t['stop']:
            close_trade(sym,'stop'); continue
        # 4. Trailing stop — skip for multi-day (designed to hold through pullbacks)
        if not multi_day and peak_pct >= RULES['TRAIL_TRIGGER_PCT']*100 and (peak_pct - pnl_pct) >= RULES['TRAIL_GIVEBACK_PCT']*100:
            p_log(f"{sym} momentum stalling — peak +{peak_pct:.1f}%, now +{pnl_pct:.1f}%. Locking in profit.")
            close_trade(sym,'momentum_stall'); continue
        # 5. Rotation: held a while with little gain AND a much better candidate exists (intraday only)
        held_min = (time.time() - t.get('entered_at', time.time())) / 60
        if not multi_day and check_rotation and held_min >= RULES['UNDERPERFORM_MINUTES'] and pnl_pct < RULES['UNDERPERFORM_MIN_PCT']:
            spy=cp('SPY'); qqq=cp('QQQ')
            better = pick_best(spy['pct'] if spy else 0, qqq['pct'] if qqq else 0, exclude=[sym])
            if better and better.get('_score',0) - t.get('score',0) >= RULES['UNDERPERFORM_SCORE_GAP']:
                p_log(f"{sym} underperforming ({pnl_pct:+.1f}% in {held_min:.0f}min). {better['symbol']} scores {better['_score']} vs {t.get('score',0)} — rotating.")
                tg(f"🔄 Rotating out of {sym} (+{pnl_pct:.1f}% in {held_min:.0f}min) into stronger setup {better['symbol']} (score {better['_score']}/99)")
                close_trade(sym,'rotate_better'); continue
            _last_rotate_check = time.time()

        # Periodic status ping every ~30 min
        if datetime.now(ET_TZ).minute%30==0:
            sig=get_hold_signal(paper['active'].get(sym,t))
            pnl=(price-t['entry'])*t['shares']
            tg(f"📊 T{t['trade_num']} {sym}\n${t['entry']:.2f}→${price:.2f} | {'+'if pnl>=0 else''}${pnl:.2f}\n{sig['icon']} {sig['signal']}: {sig['msg']}")

    # Keep the simulated options tracker fresh every monitor tick
    try:
        refresh_options_candidates(); monitor_options()
    except Exception as e:
        print(f"  Options tracker: {e}")

def job_eod():
    if not is_trading_day(): return
    for sym in list(paper['active'].keys()): close_trade(sym,'eod')
    pct=paper['total_pnl']/paper['starting_capital']*100 if paper['starting_capital'] else 0
    lines=[f"📊 EOD — {today_str()}",f"{'━'*22}",
           f"Start: ${paper['starting_capital']:,.2f} | End: ${paper['capital']:,.2f}",
           f"P&L: {'✅ +' if paper['total_pnl']>=0 else '🔴 '}${abs(paper['total_pnl']):.2f} ({pct:+.2f}%)",
           f"{'━'*22}"]
    for t in paper['completed']:
        lines.append(f"{'✅'if t['pnl']>=0 else'🔴'} T{t['trade_num']} {t['symbol']}: {'+'if t['pnl']>=0 else''}${t['pnl']:.2f} ({t['pnl_pct']:+.2f}%) [{t['reason'].upper()}]")
    lines+=[f"{'━'*22}",f"{get_app_url()}"]
    tg('\n'.join(lines))

def job_keepalive():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    if n.weekday()>=5 or t<7*60 or t>17*60: return
    try:
        urllib.request.urlopen(urllib.request.Request(f"{get_app_url()}/ping",headers={'User-Agent':UA}),timeout=8)
    except: pass

# ── NEWS & AI ─────────────────────────────────────────────────────────
_news={'items':[],'ts':0}
def get_news():
    if time.time()-_news['ts']<90 and _news['items']: return _news['items']
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ,SPY,NVDA,TSLA,COIN,SOXL&region=US&lang=en-US']:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=6) as r:
                tree=ET.fromstring(r.read())
                for item in tree.iter('item'):
                    t=item.find('title')
                    if t is not None and t.text and len(t.text.strip())>20:
                        items.append({'title':t.text.strip()})
        except: pass
    if not items: items=[{'title':'Market data loading'}]
    _news.update({'items':items[:12],'ts':time.time()})
    return _news['items']

def get_brief():
    mkt=all_prices(); news=get_news()
    active=list(paper['active'].values())
    trade_s=(f"In {active[0]['symbol']} T{active[0]['trade_num']}/3 entry ${active[0]['entry']:.2f}" if active else f"No active trade")
    mkt_s=', '.join(f"{k} ${v['price']} ({v['pct']:+.2f}%)" for k,v in mkt.items() if k in ('QQQ','SPY','BTC-USD'))
    if ANTHROPIC_KEY:
        try:
            prompt=(f"Professional day trader. Market: {mkt_s}. Active: {trade_s}. "
                    f"News: {' | '.join(n['title'] for n in news[:4])}. "
                    f"3 sentences max: market direction, trade recommendation, key risk.")
            payload={"model":"claude-sonnet-4-20250514","max_tokens":180,"messages":[{"role":"user","content":prompt}]}
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=json.dumps(payload).encode(),
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
            with urllib.request.urlopen(req,timeout=20) as r:
                return json.loads(r.read())['content'][0]['text'],True
        except: pass
    spy=mkt.get('SPY'); qqq=mkt.get('QQQ')
    if spy and qqq:
        both_up=spy['pct']>0 and qqq['pct']>0
        return (f"Market {'bullish' if both_up else 'bearish'} — SPY {spy['pct']:+.2f}%, QQQ {qqq['pct']:+.2f}%. "
                +("Favorable for momentum longs." if both_up else "Caution advised.")),False
    return "Market data loading.",False

# AI Chat
chat_history=[]
def ai_chat(user_msg, symbol=None):
    active=list(paper['active'].values())
    ctx_parts=[
        f"Trading state: {paper['status']}, Trade {paper['trade_seq']}/{RULES['MAX_TRADES_PER_DAY']}, P&L ${paper['total_pnl']:+.2f}",
        f"Capital: ${paper['capital']:,.2f}",
    ]
    if active:
        t=active[0]; cached=cp(t['symbol'])
        cur=cached['price'] if cached else t['current']
        pnl=(cur-t['entry'])*t['shares']
        sig=get_hold_signal({**t,'current':cur})
        ctx_parts.append(f"Active trade: {t['symbol']} {t['shares']}sh entry ${t['entry']:.2f} now ${cur:.2f} P&L ${pnl:+.2f} signal: {sig['signal']}")
    mkt=all_prices()
    ctx_parts.append(f"Market: SPY {mkt.get('SPY',{}).get('pct',0):+.2f}% QQQ {mkt.get('QQQ',{}).get('pct',0):+.2f}%")
    ctx_parts.append(f"Top candidates: {', '.join(c['symbol']+'('+str(c.get('_score',0))+')' for c in paper.get('candidates',[])[:5])}")

    # If a specific symbol is in context, add live data for it
    if symbol:
        try:
            cached_sym = cp(symbol)
            price = cached_sym['price'] if cached_sym else 0
            sig_sym = compute_intraday_signal(symbol)
            lvl = compute_atr_levels(symbol, price) if price else {}
            ctx_parts.append(f"Symbol in focus: {symbol} @ ${price:.2f}")
            if sig_sym:
                ctx_parts.append(f"VWAP/EMA signal: {sig_sym['signal']} — {sig_sym['reason']}")
            if lvl:
                ctx_parts.append(f"ATR levels: stop ${lvl.get('stop',0):.2f}, target ${lvl.get('target',0):.2f}, support ${lvl.get('support',0):.2f}, resistance ${lvl.get('resistance',0):.2f}")
        except Exception:
            pass

    sys_prompt = (
        "You are AutoTrade Pro AI for Abiy Kassa. You are a professional day trader assistant.\n"
        "Current state:\n" + '\n'.join(ctx_parts) + "\n\n"
        "You can suggest these actions (include JSON if user wants to execute):\n"
        '{"action":"enter_now","symbol":"SOXL"} — enter a trade immediately\n'
        '{"action":"close","symbol":"NVDA"} — close position\n'
        '{"action":"skip","symbol":"NVDA"} — skip this pick, find another\n'
        '{"action":"close_all"} — close everything\n'
        "Only include command JSON if user explicitly wants to execute. Be brief and direct."
    )
    if not ANTHROPIC_KEY:
        msg=user_msg.lower()
        if any(w in msg for w in ['pnl','profit','loss','how much']): return f"P&L: ${paper['total_pnl']:+.2f} | Capital: ${paper['capital']:,.2f}",None
        if 'signal' in msg or 'hold' in msg or 'sell' in msg:
            if active:
                t=active[0]; cached=cp(t['symbol']); cur=cached['price'] if cached else t['current']
                sig=get_hold_signal({**t,'current':cur})
                return f"{sig['icon']} {sig['signal']}: {sig['msg']}",None
        return "AI needs ANTHROPIC_API_KEY set in Render environment variables.",None
    try:
        msgs=[{'role':'user','content':m['user']} for m in chat_history[-4:]]
        all_m=[]
        for m in chat_history[-4:]:
            all_m.append({'role':'user','content':m['user']})
            if m.get('ai'): all_m.append({'role':'assistant','content':m['ai']})
        all_m.append({'role':'user','content':user_msg})
        payload={"model":"claude-sonnet-4-20250514","max_tokens":300,"system":sys_prompt,"messages":all_m}
        req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=json.dumps(payload).encode(),
            headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
        with urllib.request.urlopen(req,timeout=25) as r:
            text=json.loads(r.read())['content'][0]['text']
        import re; m=re.search(r'\{[^}]*"action"[^}]*\}',text)
        cmd=None
        if m:
            try: cmd=json.loads(m.group())
            except: pass
        return text, cmd
    except Exception as e:
        return f"AI error: {e}",None

def execute_cmd(cmd):
    a=cmd.get('action',''); sym=cmd.get('symbol','').upper()
    if a=='enter_now':
        q=next((c for c in paper.get('candidates',[]) if c['symbol']==sym),None)
        if not q:
            cached=cp(sym)
            q={'symbol':sym,'shortName':sym,'regularMarketPrice':cached['price'] if cached else 0,
               '_vr':2,'regularMarketChangePercent':0,'marketCap':5e9,
               'fiftyTwoWeekHigh':0,'fiftyTwoWeekLow':0,'preMarketChangePercent':0,'_score':65}
        if q.get('regularMarketPrice',0)>0:
            enter_trade(q,manual=True); return f"✅ Entered {sym}"
        return f"❌ No price for {sym}"
    elif a=='close':
        if sym in paper['active']: close_trade(sym,'manual'); return f"✅ Closed {sym}"
        return f"{sym} not in active trades"
    elif a=='skip':
        paper.setdefault('skipped',[]).append(sym)
        if paper['picked'] and paper['picked']['symbol']==sym:
            paper['picked']=None; paper['status']='waiting'
            threading.Thread(target=_auto_pick_enter,daemon=True).start()
        return f"✅ Skipped {sym}, looking for next..."
    elif a=='close_all':
        for s in list(paper['active'].keys()): close_trade(s,'manual')
        return "✅ All positions closed"
    return "Unknown command"

# ── FLASK ROUTES ───────────────────────────────────────────────────────
def cors(d):
    r=jsonify(d); r.headers['Access-Control-Allow-Origin']='*'; return r

@app.route('/') 
def index(): return send_from_directory('.','index.html')
@app.route('/ping') 
def ping(): return cors({'ok':True,'time':now_et()})

@app.route('/prices')
def prices():
    src='alpaca_realtime' if alpaca_data else 'yfinance_delayed'
    return cors({'data':all_prices(),'source':src,'fresh':(time.time()-_cache_ts)<10,
                 'delay':'~0 sec' if alpaca_data else '~15 min',
                 'updated':datetime.fromtimestamp(_cache_ts,ET_TZ).strftime('%H:%M:%S') if _cache_ts else '—'})

@app.route('/paper-status')
def paper_status():
    active=list(paper['active'].values())
    for t in active:
        cached=cp(t['symbol'])
        if cached: t['current']=cached['price']
        t['signal']=get_hold_signal(t)
    alert = paper.get('trade_alert')
    if alert:
        paper['trade_alert'] = None  # one-shot — UI shows it once, then it's cleared
    return cors({
        'status':paper['status'],'trade_seq':paper['trade_seq'],
        'max_trades':RULES['MAX_TRADES_PER_DAY'],
        'capital':round(paper['capital'],2),'starting_capital':paper['starting_capital'],
        'total_pnl':round(paper['total_pnl'],2),
        'total_pnl_pct':round(paper['total_pnl']/paper['starting_capital']*100,2) if paper['starting_capital'] else 0,
        'active':active[0] if active else None,'completed':paper['completed'],
        'date':paper['date'],'log':paper['log'][:10],
        'picked':paper['picked'],'candidates':paper.get('candidates',[])[:8],
        'better_opp':paper.get('better_opp'),'alpaca':alpaca_trading is not None,
        'trade_alert':alert,'pro_locked_symbol':paper.get('pro_locked_symbol'),
    })

@app.route('/close-trade',methods=['POST'])
def close_trade_route():
    """Direct close — fixes the Sell/Exit buttons that used to go through
    unreliable AI-chat text parsing."""
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    reason=data.get('reason','manual')
    if sym not in paper['active']:
        return cors({'ok':False,'msg':f'{sym} is not an active trade'})
    close_trade(sym, reason)
    return cors({'ok':True,'msg':f'Closed {sym}'})

@app.route('/candles')
def candles():
    sym = request.args.get('symbol','').upper()
    if not sym: return cors({'bars':[],'error':'no symbol'})
    try:
        df = yf.download(sym, period='5d', interval='5m', auto_adjust=True, progress=False)
        if df.empty:
            df = yf.download(sym, period='1mo', interval='15m', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna().tail(60)
        bars = []
        for i, r in df.iterrows():
            try:
                idx_et = i.tz_convert(ET_TZ) if i.tzinfo else i.tz_localize('UTC').tz_convert(ET_TZ)
                hm = idx_et.strftime('%H:%M')
            except Exception:
                hm = str(i)[11:16]
            bars.append({'t':str(i),'hm':hm,'o':round(float(r.Open),2),'h':round(float(r.High),2),
                         'l':round(float(r.Low),2),'c':round(float(r.Close),2)})
        return cors({'bars':bars,'symbol':sym})
    except Exception as e:
        return cors({'bars':[],'error':str(e)})

@app.route('/options-status')
def options_status():
    return cors({'candidates':options_tracker['candidates'],
                 'completed':options_tracker['completed'][:10],
                 'max_slots':OPTIONS_MAX_SLOTS,
                 'leverage':OPTIONS_LEVERAGE})

@app.route('/trade-markers')
def trade_markers():
    """Buy/sell markers for the live chart tab — today's entries/exits for a symbol."""
    sym = request.args.get('symbol','').upper()
    if not sym: return cors({'markers':[]})
    markers = []
    for t in paper['completed']:
        if t['symbol'] != sym: continue
        markers.append({'type':'buy','time':t['entry_time'],'price':t['entry']})
        markers.append({'type':'sell','time':t['exit_time'],'price':t['exit'],'reason':t['reason']})
    for t in paper['active'].values():
        if t['symbol'] == sym:
            markers.append({'type':'buy','time':t['entry_time'],'price':t['entry']})
    return cors({'markers':markers})

@app.route('/pro-watch', methods=['POST'])
def pro_watch():
    """Set the watched symbol WITHOUT entering a trade — Abiy decides when to buy."""
    data = request.get_json() or {}
    sym = data.get('symbol','').upper()
    if not sym: return cors({'ok':False,'msg':'No symbol given'})
    paper['pro_locked_symbol'] = sym
    save_state()
    p_log(f"PRO Live: now watching {sym} (manual buy required first)")
    return cors({'ok':True,'symbol':sym})

@app.route('/pro-signal')
def pro_signal():
    sym = request.args.get('symbol','').upper()
    if not sym: return cors({'signal':None})
    sig = compute_intraday_signal(sym)
    return cors({'signal': sig})

@app.route('/pro-buy', methods=['POST'])
def pro_buy():
    """Manual first entry for PRO Live — strictly market-hours only."""
    if not is_market_open():
        return cors({'ok':False,'msg':'Market is closed — entries only work 9:30 AM-4:00 PM ET'})
    data = request.get_json() or {}
    sym = data.get('symbol','').upper()
    multi_day = data.get('multi_day', False)  # #5: allow multi-day holds
    if not sym: return cors({'ok':False,'msg':'No symbol given'})
    if paper['active']:
        return cors({'ok':False,'msg':f"Already in a trade ({list(paper['active'].keys())[0]})"})
    q = fetch_symbol_on_demand(sym)
    if not q: return cors({'ok':False,'msg':f'Could not get market data for {sym} — check the ticker'})
    ok = enter_trade(q, manual=True)
    if ok:
        paper['pro_locked_symbol'] = sym
        if multi_day:
            paper['active'][sym]['multi_day'] = True
        save_state()
        p_log(f"PRO Live: manual buy {sym} {'(multi-day hold)' if multi_day else ''}")
    return cors({'ok':ok,'msg':f"Entered {sym}" if ok else "Entry failed"})

@app.route('/pro-release', methods=['POST'])
def pro_release():
    """Stop following the locked symbol — return to normal watchlist/universe rotation."""
    paper['pro_locked_symbol'] = None
    save_state()
    p_log("PRO Live: released the locked symbol — back to normal rotation")
    return cors({'ok':True})

@app.route('/watchlist-prices')
def watchlist_prices():
    if not custom_watchlist: return cors({'items':[]})
    items=[]
    for sym in custom_watchlist:
        cached=cp(sym)
        if cached:
            items.append({'symbol':sym,'price':cached['price'],'pct':cached['pct'],'source':cached['source']})
        else:
            items.append({'symbol':sym,'price':0,'pct':0,'source':'—'})
    return cors({'items':items})

@app.route('/watchlist-status')
def watchlist_status():
    """Live-scored view of Abiy's personal watchlist for the dedicated tab."""
    if not custom_watchlist:
        return cors({'stocks':[],'min_score':WATCHLIST_MIN_SCORE})
    spy=cp('SPY'); qqq=cp('QQQ')
    scored = score_watchlist(spy['pct'] if spy else 0, qqq['pct'] if qqq else 0)
    out = []
    for q in scored:
        why_not = []
        if q['_score'] < WATCHLIST_MIN_SCORE: why_not.append(f"score {q['_score']}<{WATCHLIST_MIN_SCORE}")
        if q.get('_vr',0) < 1.0: why_not.append(f"volume {q.get('_vr',0):.1f}x<1.0x")
        out.append({
            'symbol': q['symbol'], 'name': q.get('shortName', q['symbol']),
            'price': q.get('regularMarketPrice', 0), 'score': q['_score'],
            'vol_ratio': q.get('_vr', 0), 'change_pct': q.get('regularMarketChangePercent', 0),
            'qualifies': q['_qualifies'], 'why_not': ', '.join(why_not) if why_not else None,
        })
    return cors({'stocks': out, 'min_score': WATCHLIST_MIN_SCORE})

def fetch_symbol_on_demand(sym):
    """Fetch live data for any symbol, even if not in the scanned universe.
    This fixes the 'no market data' error when manually entering SOXL, QQQ, etc."""
    cached = cp(sym)
    if cached:
        return {'symbol':sym,'shortName':sym,'regularMarketPrice':cached['price'],
                '_vr':1,'regularMarketChangePercent':cached['pct'],'marketCap':5e9,
                'fiftyTwoWeekHigh':0,'fiftyTwoWeekLow':0,'preMarketChangePercent':0,'_score':0}
    # Not in cache — fetch directly from yfinance
    try:
        tk = yf.Ticker(sym)
        info = tk.fast_info
        price = float(getattr(info,'last_price',0) or getattr(info,'regularMarketPrice',0) or 0)
        if price <= 0:
            hist = tk.history(period='1d',interval='1m')
            if not hist.empty: price = float(hist['Close'].iloc[-1])
        if price <= 0: return None
        chg = float(getattr(info,'regularMarketChangePercent',0) or 0)
        q = {'symbol':sym,'shortName':sym,'regularMarketPrice':price,
             '_vr':1,'regularMarketChangePercent':chg,'marketCap':5e9,
             'fiftyTwoWeekHigh':float(getattr(info,'year_high',0) or 0),
             'fiftyTwoWeekLow':float(getattr(info,'year_low',0) or 0),
             'preMarketChangePercent':0,'_score':0}
        # Inject into the price cache so monitoring works normally
        _price_cache[sym] = {'price':price,'pct':chg,'source':'yf-live','ts':time.time()}
        return q
    except Exception as e:
        p_log(f"fetch_symbol_on_demand {sym}: {e}"); return None

@app.route('/enter-now',methods=['POST'])
def enter_now():
    if not is_market_open():
        return cors({'ok':False,'msg':'Market is closed — manual entries only work 9:30 AM-4:00 PM ET'})
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    q=next((c for c in paper.get('candidates',[]) if c['symbol']==sym),None)
    if not q and paper.get('picked') and paper['picked'] and paper['picked'].get('symbol')==sym:
        q=paper['picked']
    if not q:
        q = fetch_symbol_on_demand(sym)
        if not q: return cors({'ok':False,'msg':f'Could not get market data for {sym} — check the ticker symbol'})
    ok=enter_trade(q,manual=True)
    return cors({'ok':ok,'msg':f"Entered {sym}" if ok else "Entry failed"})

@app.route('/skip-stock',methods=['POST'])
def skip_stock():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    paper.setdefault('skipped',[]).append(sym)
    if paper.get('picked') and paper.get('picked',{}).get('symbol')==sym:
        paper['picked']=None; paper['status']='waiting'
        tg(f"⏭️ {sym} skipped. Scanning for next pick...")
        threading.Thread(target=_auto_pick_enter,daemon=True).start()
    return cors({'ok':True,'msg':f'Skipped {sym}'})

@app.route('/paper-reset')
def paper_reset_route(): paper_reset(); return cors({'ok':True})

@app.route('/paper-force-pick')
def paper_force_pick():
    # Don't wipe the day's trades/capital just because the button was
    # clicked mid-morning — that was producing confusing "at 8AM" messages
    # hours later and erasing real trade history. Just trigger an
    # immediate scan-and-enter attempt using whatever capital/state exists.
    if paper['active']:
        return cors({'ok':False,'msg':'Already in a trade — nothing to force.'})
    if paper['date'] != datetime.now(ET_TZ).date().isoformat():
        paper_reset()  # genuinely a new day, safe to reset
    paper['status'] = 'waiting'
    threading.Thread(target=_auto_pick_enter, daemon=True).start()
    return cors({'ok':True})

@app.route('/watchlist',methods=['GET','POST'])
def watchlist():
    global custom_watchlist
    if request.method=='POST':
        data=request.get_json() or {}
        syms=[s.strip().upper() for s in data.get('symbols',[]) if s.strip()][:5]
        custom_watchlist=syms; p_log(f"Watchlist: {syms}"); save_state()
        return cors({'ok':True,'symbols':custom_watchlist})
    return cors({'symbols':custom_watchlist})

@app.route('/chat',methods=['POST'])
def chat():
    data=request.get_json() or {}
    msg=data.get('message','').strip()
    symbol=data.get('symbol','').upper()  # optional: scope chat to a specific symbol
    if not msg: return cors({'ok':False})
    response,cmd=ai_chat(msg, symbol=symbol)
    result={'ok':True,'response':response,'command':cmd}
    if cmd:
        confirm=['confirm','yes','do it','go ahead','execute']
        if any(w in msg.lower() for w in confirm) or cmd.get('action')=='enter_now':
            result['executed']=execute_cmd(cmd)
    chat_history.append({'user':msg,'ai':response,'time':now_et(),'symbol':symbol})
    if len(chat_history)>50: chat_history.pop(0)
    return cors(result)

@app.route('/chat-history')
def chat_history_route(): return cors({'history':chat_history[-20:]})

@app.route('/news')
def news_route(): return cors({'items':get_news()})

@app.route('/stock-info')
def stock_info():
    """Context bundle for the AI chat — price, VWAP/EMA signal, ATR levels,
    and recent news headlines for a given symbol, all in one call."""
    sym = request.args.get('symbol','').upper()
    if not sym: return cors({'error':'no symbol'})
    cached = cp(sym)
    if not cached:
        q = fetch_symbol_on_demand(sym)
        if q: cached = {'price':q['regularMarketPrice'],'pct':q['regularMarketChangePercent']}
    price = cached['price'] if cached else 0
    signal = compute_intraday_signal(sym) if price else None
    levels = compute_atr_levels(sym, price) if price else {}
    # Grab recent news headlines for this symbol
    news_items = []
    try:
        url = f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US'
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            tree = ET.fromstring(r.read())
            for item in list(tree.iter('item'))[:5]:
                t_el = item.find('title')
                if t_el is not None and t_el.text:
                    news_items.append(t_el.text.strip())
    except Exception:
        pass
    # Active position context
    active_trade = paper['active'].get(sym)
    return cors({
        'symbol': sym, 'price': price,
        'change_pct': cached['pct'] if cached else 0,
        'signal': signal, 'levels': levels,
        'news': news_items,
        'active_trade': active_trade,
        'total_pnl': paper['total_pnl'],
        'trade_seq': paper['trade_seq'],
    })


# ── MARKET ALERT SCANNER ──────────────────────────────────────────────
# ── MARKET ALERT SCANNER ──────────────────────────────────────────────
# These are INFORMATIONAL alerts shown in the UI banner.
# Only TRUE_CATASTROPHE events trigger an auto-exit. Everything else
# is surfaced as a warning so YOU can decide. The FCEL/DRAM/QQQ
# losses this week all happened because "Fed decision" triggered
# market_danger, which had zero grace period and killed trades
# immediately. That's now fixed — the Fed making a routine decision
# is NOT a reason to close a trade in progress.
MAJOR_NEGATIVE = [
    'war declared','military invasion','nuclear strike','terror attack',
    'exchange suspended','trading halted market','market circuit breaker',
    'fed emergency','systemic bank failure','lehman','bear stearns',
    'market crash','stock market crash','flash crash',
]
TRUE_CATASTROPHE = [
    'nuclear','exchange suspended','trading halted market',
    'market circuit breaker','flash crash','stock market crash',
]
MAJOR_POSITIVE = ['rate cut','fed cut','pause rate','rate reduction',
                   'stimulus','deal signed','merger','acquisition',
                   'earnings beat','record high','bull market']
MAJOR_NEUTRAL  = ['fomc','fed meeting','cpi','jobs report','gdp',
                   'inflation data','federal reserve','rate decision',
                   'powell','yellen','treasury','kevin warsh']

_alert_cache = {'alert':None,'ts':0}

# ── WHALE ACTIVITY (heuristic) ─────────────────────────────────────────
# Real institutional order-flow / dark-pool data requires a paid Level 2
# or options-flow feed we don't have. This is a volume-surge proxy instead:
# unusually high relative volume + a strong directional move is what large
# buyers/sellers typically leave behind. Labeled as an estimate, not fact.
def detect_whale_activity(q):
    vr = q.get('_vr', 0); chg = q.get('regularMarketChangePercent', 0)
    if vr >= 3 and chg >= 4:
        return {'symbol':q['symbol'],'direction':'buying','strength':min(99,round(vr*8)),
                'vol_ratio':vr,'change_pct':chg}
    if vr >= 3 and chg <= -4:
        return {'symbol':q['symbol'],'direction':'selling','strength':min(99,round(vr*8)),
                'vol_ratio':vr,'change_pct':chg}
    return None

def scan_whale_activity():
    pool = paper.get('candidates', [])[:15]
    out = [w for w in (detect_whale_activity(q) for q in pool) if w]
    out.sort(key=lambda x: x['strength'], reverse=True)
    return out[:6]

def scan_market_alerts():
    if time.time()-_alert_cache['ts']<120 and _alert_cache['ts']>0:
        return _alert_cache['alert']
    news=get_news()
    whales = scan_whale_activity()
    whale_note = ''
    if whales:
        buying = [w for w in whales if w['direction']=='buying']
        selling = [w for w in whales if w['direction']=='selling']
        if selling: whale_note = f" Heavy volume selling also detected in {selling[0]['symbol']}."
        elif buying: whale_note = f" Note: heavy volume buying detected in {buying[0]['symbol']}."
    for n in news:
        title=n['title'].lower()
        for kw in MAJOR_NEGATIVE:
            if kw in title:
                # Distinguish true market-halting catastrophe from general negative news.
                # Only TRUE_CATASTROPHE events will auto-close open positions.
                is_catastrophe = any(tc in title for tc in TRUE_CATASTROPHE)
                alert={'type':'danger','catastrophe':is_catastrophe,'icon':'🚨','keyword':kw,
                       'title':n['title']+whale_note,'color':'red'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
        for kw in MAJOR_POSITIVE:
            if kw in title:
                alert={'type':'positive','catastrophe':False,'icon':'🚀','keyword':kw,
                       'title':n['title'],'color':'green'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
        for kw in MAJOR_NEUTRAL:
            if kw in title:
                alert={'type':'watch','catastrophe':False,'icon':'⚠️','keyword':kw,
                       'title':n['title'],'color':'amber'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
    _alert_cache.update({'alert':None,'ts':time.time()})
    return None

@app.route('/whale-activity')
def whale_activity_route():
    return cors({'whales': scan_whale_activity()})

@app.route('/market-summary')
def market_summary():
    brief,is_ai=get_brief(); return cors({'brief':brief,'ai':is_ai})

@app.route('/account')
def account():
    if not alpaca_trading: return cors({'alpaca_connected':False,'positions':[]})
    try:
        acct=alpaca_trading.get_account(); pos=alpaca_trading.get_all_positions()
        return cors({'alpaca_connected':True,
            'account':{'equity':float(acct.equity),'cash':float(acct.cash),
                       'portfolio_value':float(acct.portfolio_value),'buying_power':float(acct.buying_power)},
            'positions':[{'symbol':p.symbol,'qty':int(p.qty),'avg_entry':float(p.avg_entry_price),
                          'current':float(p.current_price or 0),'unrealized_pnl':float(p.unrealized_pl or 0),
                          'unrealized_pnl_pct':float(p.unrealized_plpc or 0)*100} for p in pos]})
    except Exception as e: return cors({'alpaca_connected':True,'error':str(e),'positions':[]})

@app.route('/test-telegram')
def test_telegram():
    ok=tg(f"✅ AutoTrade Pro\nAlpaca: {'✅' if alpaca_trading else '❌'} | AI: {'✅' if ANTHROPIC_KEY else '❌'}\nUniverse: {len(ALL_UNIVERSE)} stocks\n{get_app_url()}")
    return cors({'ok':ok,'msg':'Sent!' if ok else 'Failed.'})

@app.route('/market-alert')
def market_alert():
    alert = scan_market_alerts()
    return cors({'alert': alert})

@app.route('/performance')
def performance():
    """All-time stats from the permanent history file, not just today."""
    try:
        if not os.path.exists(HISTORY_FILE):
            return cors({'trades':0,'win_rate':0,'total_pnl':0,'avg_win':0,'avg_loss':0,
                         'best':None,'worst':None,'by_reason':{}})
        with open(HISTORY_FILE) as f:
            hist = json.load(f)
        if not hist:
            return cors({'trades':0,'win_rate':0,'total_pnl':0,'avg_win':0,'avg_loss':0,
                         'best':None,'worst':None,'by_reason':{}})
        wins = [h for h in hist if h.get('pnl',0) >= 0]
        losses = [h for h in hist if h.get('pnl',0) < 0]
        by_reason = {}
        for h in hist:
            r = h.get('reason','unknown')
            by_reason[r] = by_reason.get(r, 0) + 1
        best = max(hist, key=lambda h: h.get('pnl_pct',0))
        worst = min(hist, key=lambda h: h.get('pnl_pct',0))
        return cors({
            'trades': len(hist),
            'win_rate': round(len(wins)/len(hist)*100, 1),
            'total_pnl': round(sum(h.get('pnl',0) for h in hist), 2),
            'avg_win': round(sum(h.get('pnl',0) for h in wins)/len(wins), 2) if wins else 0,
            'avg_loss': round(sum(h.get('pnl',0) for h in losses)/len(losses), 2) if losses else 0,
            'best': {'symbol':best['symbol'],'pnl_pct':best.get('pnl_pct',0),'date':best.get('date','')},
            'worst': {'symbol':worst['symbol'],'pnl_pct':worst.get('pnl_pct',0),'date':worst.get('date','')},
            'by_reason': by_reason,
        })
    except Exception as e:
        return cors({'error': str(e)})

@app.route('/notify')
def notify(): return cors({'ok':tg(request.args.get('msg','')) if request.args.get('msg') else False})

# ── START ─────────────────────────────────────────────────────────────
load_state()
threading.Thread(target=price_thread,daemon=True).start()
import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,    'cron',day_of_week='mon-fri',hour=8, minute=0)
    sched.add_job(job_enter_945,  'cron',day_of_week='mon-fri',hour=9, minute=45)
    sched.add_job(job_monitor,    'cron',day_of_week='mon-fri',hour='8-16',minute='*')
    sched.add_job(job_eod,        'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,  'interval',minutes=10)
    sched.start(); atexit.register(lambda:sched.shutdown(wait=False))
    print(f"  Scheduler: {len(sched.get_jobs())} jobs | Universe: {len(ALL_UNIVERSE)} stocks")
except Exception as e: print(f"  Scheduler: {e}")

if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT,debug=False)
