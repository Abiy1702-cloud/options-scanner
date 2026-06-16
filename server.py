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
PORT          = int(os.environ.get('PORT',8765))
ET_TZ         = pytz.timezone('America/New_York')
UA            = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

alpaca_trading = None
alpaca_data    = None
if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        alpaca_trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data    = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("  Alpaca connected ✅")
    except Exception as e:
        print(f"  Alpaca: {e}")

app = Flask(__name__, static_folder='.')

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
}
# Simulated options tracker config (educational estimate, NOT real option data)
OPTIONS_MAX_SLOTS   = 3
OPTIONS_LEVERAGE    = 5      # Approximate leverage of an ATM short-dated option
OPTIONS_TARGET_PCT  = 0.35   # Simulated option-level take-profit
OPTIONS_STOP_PCT    = 0.25   # Simulated option-level stop
# Bad-news keywords that trigger an immediate exit of an ACTIVE position
BAD_STOCK_KEYWORDS = ['downgrade','lawsuit','recall','investigation','plunge',
    'sell-off','selloff','miss estimates','misses estimates','cuts guidance',
    'fraud','bankruptcy','delisted','halted','sec probe','short report',
    'accounting','class action','subpoena','resigns','ceo steps down']

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

# ── PAPER TRADING ─────────────────────────────────────────────────────
paper = {
    'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
    'stop_pct':RULES['STOP_PCT'],
    'active':{},'completed':[],'total_pnl':0.0,
    'status':'waiting','picked':None,'date':None,'log':[],
    'candidates':[],'better_opp':None,'skipped':[],'trade_alert':None,
}

def p_log(msg):
    ts=now_et(); paper['log'].insert(0,{'time':ts,'msg':msg})
    paper['log']=paper['log'][:40]; print(f"  [{ts}] {msg}")

# ── SIMULATED OPTIONS CANDIDATES TRACKER ───────────────────────────────
# Educational estimate only — NOT real options data or a real options fill.
# Approximates an option's P&L as (underlying % move) x OPTIONS_LEVERAGE.
options_tracker = {'candidates': [], 'completed': []}

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

def enter_trade(q, manual=False):
    if not manual:
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

    tp    = RULES['TARGET_PCT']
    stop  = RULES['STOP_PCT']
    trade = {
        'symbol':q['symbol'],'name':q.get('shortName',q['symbol']),
        'trade_num':tnum,'shares':shrs,'entry':round(ep,2),
        'cost':round(shrs*ep,2),'target':round(ep*(1+tp),2),
        'stop':round(ep*(1-stop),2),'target_pct':tp,
        'entry_time':now_et(),'current':round(ep,2),'peak':round(ep,2),
        'peak_pnl_pct':0.0,'entered_at':time.time(),
        'score':q.get('_score',0),'manual':manual,
        'via_alpaca':alpaca_trading is not None,
    }
    if alpaca_trading:
        try:
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
        f"Dashboard: {APP_URL}"
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
    can_rotate, _ = can_enter()
    if nxt<=RULES['MAX_TRADES_PER_DAY'] and is_market_open() and can_rotate:
        paper['trade_seq']=nxt; paper['status']='waiting'
        tg(f"🔍 Trade {nxt}/{RULES['MAX_TRADES_PER_DAY']} — scanning for next setup...")
        threading.Thread(target=_auto_pick_enter, daemon=True).start()
    elif nxt<=RULES['MAX_TRADES_PER_DAY'] and is_market_open():
        paper['status']='waiting'  # market open but past cutoff time — will retry via job_monitor
    else:
        paper['status']='done'
        if nxt>RULES['MAX_TRADES_PER_DAY']:
            tg(f"✅ Max trades ({RULES['MAX_TRADES_PER_DAY']}) reached for today!\nTotal: {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:,.2f}\n{APP_URL}")

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
        ok, reason = can_enter()
        if not ok:
            p_log(f"Auto-pick skipped: {reason}"); return
        spy=cp('SPY'); qqq=cp('QQQ')
        q=pick_best(spy['pct'] if spy else 0, qqq['pct'] if qqq else 0)
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
    tnum=paper['trade_seq']; tp=RULES['TARGET_PCT']*100
    cached=cp(q['symbol']); price=cached['price'] if cached else q['regularMarketPrice']
    shrs=int(paper['capital']/price) if price else 0
    top3=paper.get('candidates',[])[:3]
    alts='\n'.join(f"  #{i+1} {c['symbol']} score={c['_score']}/99 chg={c.get('regularMarketChangePercent',0):+.1f}% vol={c.get('_vr',0):.1f}x" for i,c in enumerate(top3))
    tg(
        f"🔍 Trade {tnum}/{RULES['MAX_TRADES_PER_DAY']} PICKED — {q['symbol']}\n{'━'*22}\n"
        f"Price:  ${price:.2f} | Pre-mkt {q.get('preMarketChangePercent',0):+.1f}%\n"
        f"Score:  {q.get('_score',0)}/99 | Vol {q.get('_vr',0):.1f}x\n"
        f"Range:  ${q.get('fiftyTwoWeekLow',0):.0f} - ${q.get('fiftyTwoWeekHigh',0):.0f}\n{'━'*22}\n"
        f"Capital ${paper['capital']:,.2f} | ~{shrs}sh\n"
        f"Target ${price*(1+RULES['TARGET_PCT']):.2f} (+{tp:.0f}%)\n"
        f"Stop   ${price*(1-RULES['STOP_PCT']):.2f} (-4%)\n{'━'*22}\n"
        f"Top candidates:\n{alts}\n"
        f"Reply 'enter' or use dashboard to confirm\n{APP_URL}"
    )

# ── SCHEDULER ─────────────────────────────────────────────────────────
def refresh_options_candidates():
    """Keep up to OPTIONS_MAX_SLOTS simulated option positions filled from
    the current scored candidate list, skipping whatever's already the active
    equity trade or already tracked. This is an educational P&L estimate —
    NOT real options data and NOT a real fill."""
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
        pos = {
            'symbol': sym, 'entry_underlying': price, 'current_underlying': price,
            'entry_time': now_et(), 'entered_at': time.time(),
            'score': c.get('_score', 0), 'peak_pnl_pct': 0.0,
        }
        options_tracker['candidates'].append(pos)
        slots_open -= 1
        p_log(f"OPTIONS (sim) entered {sym} @ ${price:.2f} underlying")
        tg(f"🎯 Simulated Option Candidate — {sym}\n"
           f"Underlying entry: ${price:.2f} (score {c.get('_score',0)}/99)\n"
           f"Sim target: +{OPTIONS_TARGET_PCT*100:.0f}% · Sim stop: -{OPTIONS_STOP_PCT*100:.0f}%\n"
           f"(Educational estimate — not a real options fill)")

def monitor_options():
    if not options_tracker['candidates']: return
    for pos in list(options_tracker['candidates']):
        sym = pos['symbol']; cached = cp(sym)
        price = cached['price'] if cached else pos['current_underlying']
        pos['current_underlying'] = price
        underlying_pct = (price - pos['entry_underlying']) / pos['entry_underlying'] * 100
        sim_option_pct = underlying_pct * OPTIONS_LEVERAGE
        pos['peak_pnl_pct'] = max(pos.get('peak_pnl_pct', 0), sim_option_pct)
        pos['sim_pnl_pct'] = sim_option_pct
        closed_reason = None
        if sim_option_pct >= OPTIONS_TARGET_PCT*100: closed_reason = 'target'
        elif sim_option_pct <= -OPTIONS_STOP_PCT*100: closed_reason = 'stop'
        elif pos['peak_pnl_pct'] >= 15 and (pos['peak_pnl_pct']-sim_option_pct) >= 10: closed_reason = 'momentum_stall'
        if closed_reason:
            options_tracker['candidates'].remove(pos)
            options_tracker['completed'].insert(0, {**pos, 'exit_underlying': price,
                'exit_time': now_et(), 'reason': closed_reason, 'sim_pnl_pct': sim_option_pct})
            options_tracker['completed'] = options_tracker['completed'][:20]
            icon = '💰' if closed_reason=='target' else '🛑' if closed_reason=='stop' else '🔄'
            tg(f"{icon} Simulated Option CLOSED — {sym} ({closed_reason.upper()})\n"
               f"Underlying ${pos['entry_underlying']:.2f}→${price:.2f} ({underlying_pct:+.1f}%)\n"
               f"Sim option P&L: {sim_option_pct:+.1f}% (5x leverage estimate)\n"
               f"(Educational estimate — not a real options fill)")
            p_log(f"OPTIONS (sim) closed {sym} {closed_reason} sim P&L {sim_option_pct:+.1f}%")

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
        f"Scanning {len(ALL_UNIVERSE)+len(custom_watchlist)} stocks...\n{APP_URL}"
    )
    q=pick_best(sp,qp)
    if not q:
        tg("⚠️ No qualifying setup found at 8AM. Will scan fresh at 9:45 AM."); return
    paper['picked']=q; paper['status']='picked'
    tg(f"👀 EARLY PREVIEW — {q['symbol']} looks strongest right now (score {q.get('_score',0)}/99). "
       f"This is NOT final — we re-scan with live data at 9:45 AM before risking any capital.")

def job_enter_945():
    """9:45 AM — opening volatility has settled. ALWAYS re-scan fresh here;
    never blindly enter the stale 8AM pick (that was the root cause of
    missing better setups like SOXL on big move days)."""
    if not is_trading_day(): return
    if paper['active']: return  # Already in a trade
    spy=cp('SPY'); qqq=cp('QQQ')
    sp=spy['pct'] if spy else 0; qp=qqq['pct'] if qqq else 0
    mkt_ok,_,_ = market_ok_to_trade()
    if not mkt_ok:
        tg(f"🚫 9:45 AM — Market too weak (SPY {sp:+.2f}% QQQ {qp:+.2f}%). Protecting capital today."); return
    p_log("9:45 AM — running fresh scan (ignoring 8AM preview)")
    q=pick_best(sp,qp)
    if q:
        paper['picked']=q; paper['status']='picked'
        _notify_pick(q); time.sleep(5); enter_trade(q)
    else:
        tg("⚠️ 9:45 AM — No stock meets standards yet. Watching for a setup..."); paper['status']='waiting'

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
                if t is not None and t.text:
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

        # 1. Bad news on THIS symbol, or a market-wide danger alert — exit now
        if market_danger or check_symbol_news(sym):
            label = mkt_alert['title'] if market_danger else f"Negative headline detected for {sym}"
            paper['trade_alert'] = {'symbol': sym, 'msg': f"🚨 News exit: {label}", 'ts': now_et()}
            p_log(f"NEWS EXIT triggered for {sym}: {label}")
            close_trade(sym, 'news_exit')
            continue

        # 2. Hit profit ceiling
        if price >= t['target']:
            close_trade(sym,'target'); continue
        # 3. Hit hard stop
        if price <= t['stop']:
            close_trade(sym,'stop'); continue
        # 4. Trailing stop: once up TRAIL_TRIGGER_PCT+, exit if it gives back TRAIL_GIVEBACK_PCT from the peak
        if peak_pct >= RULES['TRAIL_TRIGGER_PCT']*100 and (peak_pct - pnl_pct) >= RULES['TRAIL_GIVEBACK_PCT']*100:
            p_log(f"{sym} momentum stalling — peak +{peak_pct:.1f}%, now +{pnl_pct:.1f}%. Locking in profit and rotating.")
            close_trade(sym,'momentum_stall'); continue
        # 5. Rotation: held a while with little gain AND a much better candidate exists
        held_min = (time.time() - t.get('entered_at', time.time())) / 60
        if check_rotation and held_min >= RULES['UNDERPERFORM_MINUTES'] and pnl_pct < RULES['UNDERPERFORM_MIN_PCT']:
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
    lines+=[f"{'━'*22}",f"{APP_URL}"]
    tg('\n'.join(lines))

def job_keepalive():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    if n.weekday()>=5 or t<7*60 or t>17*60: return
    try:
        urllib.request.urlopen(urllib.request.Request(f"{APP_URL}/ping",headers={'User-Agent':UA}),timeout=8)
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
def ai_chat(user_msg):
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
        'trade_alert':alert,
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
        bars = [{'t':str(i),'o':round(float(r.Open),2),'h':round(float(r.High),2),
                 'l':round(float(r.Low),2),'c':round(float(r.Close),2)} for i,r in df.iterrows()]
        return cors({'bars':bars,'symbol':sym})
    except Exception as e:
        return cors({'bars':[],'error':str(e)})

@app.route('/options-status')
def options_status():
    return cors({'candidates':options_tracker['candidates'],
                 'completed':options_tracker['completed'][:10],
                 'max_slots':OPTIONS_MAX_SLOTS,
                 'leverage':OPTIONS_LEVERAGE})

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

@app.route('/enter-now',methods=['POST'])
def enter_now():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    q=next((c for c in paper.get('candidates',[]) if c['symbol']==sym),None)
    if not q and paper.get('picked') and paper['picked']['symbol']==sym:
        q=paper['picked']
    if not q:
        cached=cp(sym)
        if not cached: return cors({'ok':False,'msg':f'No data for {sym}'})
        q={'symbol':sym,'shortName':sym,'regularMarketPrice':cached['price'],
           '_vr':2,'regularMarketChangePercent':cached['pct'],'marketCap':5e9,
           'fiftyTwoWeekHigh':0,'fiftyTwoWeekLow':0,'preMarketChangePercent':0,'_score':70}
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
    paper_reset(); threading.Thread(target=job_morning,daemon=True).start()
    return cors({'ok':True})

@app.route('/watchlist',methods=['GET','POST'])
def watchlist():
    global custom_watchlist
    if request.method=='POST':
        data=request.get_json() or {}
        syms=[s.strip().upper() for s in data.get('symbols',[]) if s.strip()][:5]
        custom_watchlist=syms; p_log(f"Watchlist: {syms}")
        return cors({'ok':True,'symbols':custom_watchlist})
    return cors({'symbols':custom_watchlist})

@app.route('/chat',methods=['POST'])
def chat():
    data=request.get_json() or {}
    msg=data.get('message','').strip()
    if not msg: return cors({'ok':False})
    response,cmd=ai_chat(msg)
    result={'ok':True,'response':response,'command':cmd}
    if cmd:
        confirm=['confirm','yes','do it','go ahead','execute']
        if any(w in msg.lower() for w in confirm) or cmd.get('action')=='enter_now':
            result['executed']=execute_cmd(cmd)
    chat_history.append({'user':msg,'ai':response,'time':now_et()})
    if len(chat_history)>50: chat_history.pop(0)
    return cors(result)

@app.route('/chat-history')
def chat_history_route(): return cors({'history':chat_history[-20:]})

@app.route('/news')
def news_route(): return cors({'items':get_news()})


# ── MARKET ALERT SCANNER ──────────────────────────────────────────────
MAJOR_NEGATIVE = ['war','attack','invasion','sanctions','crash','recession',
                   'bank failure','emergency','crisis','missile','nuclear',
                   'default','collapse','surge inflation']
MAJOR_POSITIVE = ['rate cut','fed cut','pause rate','rate reduction',
                   'stimulus','deal signed','merger','acquisition',
                   'earnings beat','record high','bull market']
MAJOR_NEUTRAL  = ['fomc','fed meeting','cpi','jobs report','gdp',
                   'inflation data','federal reserve','rate decision',
                   'powell','yellen','treasury']

_alert_cache = {'alert':None,'ts':0}

def scan_market_alerts():
    if time.time()-_alert_cache['ts']<120 and _alert_cache['ts']>0:
        return _alert_cache['alert']
    news=get_news()
    for n in news:
        title=n['title'].lower()
        for kw in MAJOR_NEGATIVE:
            if kw in title:
                alert={'type':'danger','icon':'🚨','keyword':kw,
                       'title':n['title'],'color':'red'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
        for kw in MAJOR_POSITIVE:
            if kw in title:
                alert={'type':'positive','icon':'🚀','keyword':kw,
                       'title':n['title'],'color':'green'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
        for kw in MAJOR_NEUTRAL:
            if kw in title:
                alert={'type':'watch','icon':'⚠️','keyword':kw,
                       'title':n['title'],'color':'amber'}
                _alert_cache.update({'alert':alert,'ts':time.time()})
                return alert
    _alert_cache.update({'alert':None,'ts':time.time()})
    return None

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
    ok=tg(f"✅ AutoTrade Pro\nAlpaca: {'✅' if alpaca_trading else '❌'} | AI: {'✅' if ANTHROPIC_KEY else '❌'}\nUniverse: {len(ALL_UNIVERSE)} stocks\n{APP_URL}")
    return cors({'ok':ok,'msg':'Sent!' if ok else 'Failed.'})

@app.route('/market-alert')
def market_alert():
    alert = scan_market_alerts()
    return cors({'alert': alert})

@app.route('/notify')
def notify(): return cors({'ok':tg(request.args.get('msg','')) if request.args.get('msg') else False})

# ── START ─────────────────────────────────────────────────────────────
threading.Thread(target=price_thread,daemon=True).start()
import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,    'cron',day_of_week='mon-fri',hour=8, minute=0)
    sched.add_job(job_enter_945,  'cron',day_of_week='mon-fri',hour=9, minute=45)
    sched.add_job(job_monitor,    'cron',day_of_week='mon-fri',hour='9-16',minute='*')
    sched.add_job(job_eod,        'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,  'interval',minutes=10)
    sched.start(); atexit.register(lambda:sched.shutdown(wait=False))
    print(f"  Scheduler: {len(sched.get_jobs())} jobs | Universe: {len(ALL_UNIVERSE)} stocks")
except Exception as e: print(f"  Scheduler: {e}")

if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT,debug=False)
