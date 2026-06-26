#!/usr/bin/env python3
"""
AutoTrade Pro v3 — Whale Intelligence Platform
Sources: SEC Form 4, Congress Trades, Unusual Whales free, yfinance, Alpaca
Modes: Day Trade | Swing Trade | Long Term — all auto-managed
"""
import json, os, time, threading, urllib.request, urllib.parse, re, hashlib, concurrent.futures
import math

def _sanitize(obj, depth=0):
    """Recursively replace NaN/Inf with None so JSON stays valid"""
    if depth > 20: return obj
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else round(obj, 6)
    if isinstance(obj, dict):
        return {k: _sanitize(v, depth+1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, depth+1) for v in obj]
    return obj
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, send_from_directory

try:
    import yfinance as yf, pandas as pd, numpy as np, pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"Missing dep: {e}"); raise SystemExit(1)

# ── ENV ───────────────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY','')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY','')
GROQ_KEY      = os.environ.get('GROQ_API_KEY','')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY','')
TELEGRAM_TOKEN= os.environ.get('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')
APP_URL       = os.environ.get('APP_URL','')
ABIY_PIN      = os.environ.get('ABIY_PIN','').strip()
PORT          = int(os.environ.get('PORT', 8765))
ET_TZ         = pytz.timezone('America/New_York')
UA            = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

alpaca_trading = alpaca_data = alpaca_stream = None
_alpaca_live_prices = {}  # populated by websocket stream during market hours

if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import (
            StockLatestTradeRequest, StockLatestQuoteRequest,
            StockSnapshotRequest, StockBarsRequest
        )
        from alpaca.data.timeframe import TimeFrame
        alpaca_trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data    = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("Alpaca ✅")
    except Exception as e:
        print(f"Alpaca init: {e}")

def alpaca_snapshot_prices(syms):
    """
    Use Alpaca snapshot API — works during AND after market hours.
    Returns latest trade + quote + daily bar for each symbol.
    Much more reliable than StockLatestTrade which only works during IEX hours.
    """
    if not alpaca_data or not syms:
        return {}
    result = {}
    # Process in batches of 20 (API limit)
    batch = [s for s in syms if re.match(r'^[A-Z]{1,5}$', s)][:40]
    try:
        req  = StockSnapshotRequest(symbol_or_symbols=batch)
        snaps = alpaca_data.get_stock_snapshot(req)
        for sym, snap in snaps.items():
            try:
                # latest_trade is most current price (during hours)
                # daily_bar.close is last close (always available)
                price = None
                pct   = 0.0
                if snap.latest_trade and snap.latest_trade.price:
                    price = float(snap.latest_trade.price)
                if price is None and snap.daily_bar:
                    price = float(snap.daily_bar.close)
                if price is None:
                    continue
                # % change vs previous close
                if snap.previous_daily_bar and snap.previous_daily_bar.close:
                    prev  = float(snap.previous_daily_bar.close)
                    pct   = round((price - prev) / prev * 100, 3) if prev else 0
                elif snap.daily_bar and snap.daily_bar.open:
                    prev  = float(snap.daily_bar.open)
                    pct   = round((price - prev) / prev * 100, 3) if prev else 0
                # Bid/ask spread for quality check
                bid = ask = None
                if snap.latest_quote:
                    bid = snap.latest_quote.bid_price
                    ask = snap.latest_quote.ask_price
                result[sym] = {
                    'price': round(price, 2),
                    'pct':   pct,
                    'bid':   round(float(bid), 2) if bid else None,
                    'ask':   round(float(ask), 2) if ask else None,
                    'src':   'alpaca_snapshot',
                    'ts':    time.time()
                }
            except Exception as e:
                print(f"  snap {sym}: {e}")
    except Exception as e:
        print(f"Alpaca snapshot: {e}")
    return result

app = Flask(__name__, static_folder='.')

# ── AUTH ──────────────────────────────────────────────────────────────
def _token():
    return hashlib.sha256((ABIY_PIN+'atp-whale-2026').encode()).hexdigest() if ABIY_PIN else ''

@app.before_request
def _auth():
    # These endpoints are public — no PIN needed
    # analysis-result, candles, prices, technicals are read-only data
    # that the frontend polls without being able to send auth headers reliably
    pub = {
        '/','/ping','/unlock','/favicon.ico',
        '/analysis-result','/candles','/technicals',
        '/prices','/news','/data-status','/performance',
        '/whale-data','/trades',
    }
    if not ABIY_PIN or request.path in pub: return
    # Also allow paths that START with /analysis-result (query strings)
    if any(request.path.startswith(p) for p in ('/analysis-result','/candles','/technicals','/news')):
        return
    if request.headers.get('X-Auth','') != _token():
        return jsonify({'ok':False,'locked':True}), 401

@app.route('/unlock', methods=['POST'])
def unlock():
    if not ABIY_PIN: return _cors({'ok':True,'token':'','gated':False})
    d = request.get_json() or {}
    if str(d.get('pin','')) == ABIY_PIN:
        return _cors({'ok':True,'token':_token(),'gated':True})
    return _cors({'ok':False,'msg':'Wrong code'})

# ── HELPERS ───────────────────────────────────────────────────────────
def _cors(d):
    r = jsonify(_sanitize(d)); r.headers['Access-Control-Allow-Origin']='*'; return r

def now_et(): return datetime.now(ET_TZ)
def now_str(): return now_et().strftime('%H:%M ET')
def today_str(): return now_et().strftime('%Y-%m-%d')
def is_market_open():
    n=now_et(); t=n.hour*60+n.minute
    return n.weekday()<5 and 570<=t<960
def is_trading_day(): return now_et().weekday()<5

def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body=json.dumps({'chat_id':str(TELEGRAM_CHAT).strip(),'text':str(msg)[:4000],
                         'disable_web_page_preview':True}).encode()
        req=urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body,headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req,timeout=12) as r:
            return json.loads(r.read()).get('ok',False)
    except: return False

def http_get(url, timeout=10):
    req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
    with urllib.request.urlopen(req,timeout=timeout) as r: return r.read()

def ai_call(system_p, user_p, max_tokens=500):
    if GROQ_KEY:
        try:
            payload=json.dumps({"model":"llama-3.3-70b-versatile","max_tokens":max_tokens,
                "messages":[{"role":"system","content":system_p},{"role":"user","content":user_p}]}).encode()
            req=urllib.request.Request("https://api.groq.com/openai/v1/chat/completions",
                data=payload,headers={"Authorization":f"Bearer {GROQ_KEY}",
                "Content-Type":"application/json","User-Agent":UA})
            with urllib.request.urlopen(req,timeout=20) as r:
                return json.loads(r.read())['choices'][0]['message']['content'],'groq'
        except Exception as e: print(f"Groq: {e}")
    if ANTHROPIC_KEY:
        try:
            payload=json.dumps({"model":"claude-sonnet-4-6","max_tokens":max_tokens,
                "system":system_p,"messages":[{"role":"user","content":user_p}]}).encode()
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=payload,
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                "content-type":"application/json","User-Agent":UA})
            with urllib.request.urlopen(req,timeout=25) as r:
                return json.loads(r.read())['content'][0]['text'],'anthropic'
        except Exception as e: print(f"Anthropic: {e}")
    return None,'none'

# ══════════════════════════════════════════════════════════════════════
# ── WHALE DATA SOURCES ────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

# Cache
_whale_cache = {'sec4':[],'congress':[],'unusual':[],'combined':[],'ts':0}
_whale_lock  = threading.Lock()
_whale_lock  = threading.Lock()

def get_rt_prices(syms):
    """Quick price fetch for a list of symbols — used by whale curated fallback"""
    result = {}
    if alpaca_data:
        try:
            req    = StockLatestTradeRequest(symbol_or_symbols=list(syms))
            trades = alpaca_data.get_stock_latest_trade(req)
            with _price_lock:
                for sym, t in trades.items():
                    p   = float(t.price)
                    old = _price_cache.get(sym, {}).get('price', p)
                    pct = ((p - old) / old * 100) if old else 0
                    result[sym] = {'price': round(p, 2), 'pct': round(pct, 3)}
            return result
        except: pass
    try:
        df   = yf.download(list(syms), period='2d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl  = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl) < 1: continue
                p   = float(cl.iloc[-1])
                old = float(cl.iloc[-2]) if len(cl) >= 2 else p
                result[sym] = {'price': round(p, 2), 'pct': round((p - old) / old * 100, 3)}
            except: pass
    except: pass
    return result

# Comprehensive garbage-word blocklist — words that appear in headlines but are NOT tickers
_NOT_TICKERS = {
    # Common English words that look like tickers
    'AI','THE','AND','FOR','BUT','NOT','ALL','ARE','CAN','HAS','HAD','ITS','NEW',
    'NOW','OLD','ONE','OUR','OUT','OWN','SAY','SHE','WHO','WHY','WIN','YET',
    'DAY','GET','GOT','HER','HIM','HOW','LET','LOW','MAY','MET','OFF','PUT',
    'RUN','SET','TOP','TRY','TWO','USE','WAY','WAS','HIT','BIG','BAD','FIT',
    # Country / region codes
    'UAE','USA','UK','EU','US','UN','NATO','OPEC','IMF','IRS','ISO',
    # Financial jargon abbreviations
    'IPO','ETF','CEO','CFO','CTO','COO','SEC','FDA','FED','GDP','CPI',
    'NFP','EPS','LLC','INC','CORP','LTD','PLC','ESG','SPX','DXY','WTI',
    'FOMC','REPO','BOND','DEBT','CASH','REIT','ADR','OTC','ATS',
    # News words that appear in headlines as caps
    'TECH','BANK','FUND','GOLD','RATE','WEEK','YEAR','RISE','FALL','GAIN',
    'LOSS','DEAL','SALE','PLAN','DATA','NEWS','MOVE','NEXT','LAST','HIGH','LOW',
    'OPEN','SHUT','FAST','SLOW','LONG','NEAR','WIDE','FREE','LIVE','REAL',
    # 2-letter non-tickers
    'AT','BE','BY','DO','GO','IF','IN','IS','IT','ME','MY','NO','OF',
    'ON','OR','SO','TO','UP','WE',
    # Specific known garbage that appeared in our logs
    'NEXR','ILLR','DRAM_TECH',  # NEXR was a hallucination, ILLR unknown
    # Drug/pharma trial abbreviations
    'FDA','EMA','NDA','BLA',
}

# Price validation — if a ticker's price seems unreasonable, skip it
def _price_sanity_check(sym, price):
    """Return False if price looks like a hallucination or delisted stock"""
    if price is None or price <= 0: return False
    if price > 100000: return False  # unreasonably high
    return True

def _valid_sym(sym):
    """Return True only if sym looks like a real, tradeable stock ticker"""
    if not sym or len(sym) < 2 or len(sym) > 5: return False
    if sym in _NOT_TICKERS: return False
    if not re.match(r'^[A-Z]{1,5}$', sym): return False
    return True

# ════════════════════════════════════════════════════════════════════
# WHALE DATA ENGINE — Alpaca Screener + News + yfinance Momentum
# All sources confirmed to work on Render with Alpaca credentials
# ════════════════════════════════════════════════════════════════════

# Curated universe — always scanned as baseline
WATCHLIST = [
    # High-conviction momentum names
    'NVDA','AMD','TSLA','AAPL','MSFT','META','AMZN','GOOGL','AVGO',
    # Semi + AI
    'MU','SMCI','PLTR','ARM','IONQ','RGTI','QUBT','SOUN','BBAI','OKLO',
    # 3x ETFs
    'SOXL','TQQQ','UPRO','TECL','LABU','SPXL','FNGU','UDOW',
    # Crypto proxy
    'MSTR','COIN','MARA','RIOT','HUT','CLSK','HOOD',
    # High-beta momentum
    'SOFI','UPST','HIMS','RKLB','ASTS','SMR','APP','ACHR','JOBY',
    # Semis
    'DRAM','NVDL','INTC','QCOM','MRVL',
    # EV
    'RIVN','NIO','LCID',
]

def alpaca_get_screener(endpoint, top=25):
    """Call Alpaca screener API — most actives, gainers."""
    if not alpaca_data: return []
    items = []
    try:
        # most-actives requires ?by=volume, gainers/losers don't accept it
        if endpoint == 'most-actives':
            url = f'https://data.alpaca.markets/v1beta1/screener/stocks/most-actives?by=volume&top={top}'
        else:
            url = f'https://data.alpaca.markets/v1beta1/screener/stocks/{endpoint}?top={top}'
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        stocks = data.get('most_actives') or data.get('gainers') or data.get('losers') or []
        for s in stocks:
            sym = (s.get('symbol') or '').upper()
            if not _valid_sym(sym): continue
            chg = float(s.get('percent_change') or 0)
            vol = int(s.get('volume') or 0)
            price = float(s.get('trade') or s.get('price') or 0)
            label = 'Most Active' if endpoint=='most-actives' else 'Top Gainer'
            confidence = 72 if endpoint=='most-actives' else 68
            hold = 3 if endpoint=='most-actives' else 7
            if endpoint=='losers': continue
            items.append({
                'symbol':sym,'source':f'Alpaca {label}','direction':'buy',
                'title':f'{label}: {sym} {chg:+.1f}% vol {vol:,}',
                'date':today_str(),'type':'volume_surge',
                'confidence':confidence,'hold_days':hold,
                'price':price,'chg_pct':chg,'volume':vol
            })
        print(f"  Alpaca {endpoint}: {len(items)} symbols")
    except Exception as e:
        print(f"  Alpaca screener {endpoint}: {e}")
    return items

def alpaca_get_news_tickers():
    """Pull Alpaca news feed, extract tickers mentioned in headlines."""
    if not alpaca_data: return []
    items = []
    try:
        url = 'https://data.alpaca.markets/v1beta1/news?limit=50&sort=desc'
        req = urllib.request.Request(url, headers={
            'APCA-API-KEY-ID': ALPACA_KEY,
            'APCA-API-SECRET-KEY': ALPACA_SECRET,
            'Accept': 'application/json'
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        news_items = data.get('news') or []
        seen = set()
        for n in news_items:
            syms = n.get('symbols') or []
            headline = n.get('headline','')
            summary  = n.get('summary','')
            # Use symbols Alpaca already extracted — much more reliable than regex
            for sym in syms:
                sym = sym.upper()
                if not _valid_sym(sym) or sym in seen: continue
                seen.add(sym)
                # Score the news sentiment
                bull_words = ['surge','soar','rally','beat','record','buy','upgrade','breakout','jump']
                bear_words = ['crash','plunge','miss','downgrade','cut','fall','drop','loss']
                text = (headline+' '+summary).lower()
                bull_count = sum(1 for w in bull_words if w in text)
                bear_count = sum(1 for w in bear_words if w in text)
                if bear_count > bull_count: continue  # skip negative news
                conf = min(70, 50 + bull_count*5)
                items.append({
                    'symbol':sym,'source':'Alpaca News','direction':'buy',
                    'title':headline[:80],
                    'date':today_str(),'type':'news_catalyst',
                    'confidence':conf,'hold_days':5
                })
        print(f"  Alpaca news: {len(items)} symbols")
    except Exception as e:
        print(f"  Alpaca news: {e}")
    return items

def yfinance_momentum_scan():
    """
    Score the full watchlist using yfinance — price change + volume ratio.
    This ALWAYS works on Render. It's the backbone of the whale engine.
    """
    items = []
    try:
        df = yf.download(
            WATCHLIST, period='5d', interval='1d',
            auto_adjust=True, progress=False, group_by='ticker'
        )
        multi = isinstance(df.columns, pd.MultiIndex)

        for sym in WATCHLIST:
            try:
                if multi:
                    cl = df['Close'][sym].dropna()
                    vl = df['Volume'][sym].dropna()
                else:
                    cl = df['Close'].dropna()
                    vl = df['Volume'].dropna()
                if len(cl) < 2: continue
                price = float(cl.iloc[-1])
                prev  = float(cl.iloc[-2])
                chg   = (price - prev) / prev * 100
                avg_vol = float(vl.iloc[:-1].mean()) if len(vl)>1 else float(vl.iloc[-1])
                vol_ratio = float(vl.iloc[-1]) / avg_vol if avg_vol > 0 else 1.0
                # Score: momentum * volume
                score = abs(chg) * min(vol_ratio, 5)
                if chg <= 0 and vol_ratio < 2: continue  # skip weak bearish
                # Confidence based on strength
                if chg > 5 and vol_ratio > 3:     conf, hold = 78, 14
                elif chg > 3 and vol_ratio > 2:   conf, hold = 70, 7
                elif chg > 1 and vol_ratio > 1.5: conf, hold = 62, 3
                elif vol_ratio > 3:               conf, hold = 65, 3  # volume spike even if flat
                else: continue
                source = 'High Momentum' if chg > 3 else ('Volume Surge' if vol_ratio > 3 else 'Momentum')
                items.append({
                    'symbol':sym,'source':source,'direction':'buy',
                    'title':f'{sym} {chg:+.1f}% with {vol_ratio:.1f}x volume — momentum signal',
                    'date':today_str(),'type':'momentum',
                    'confidence':conf,'hold_days':hold,
                    'price':round(price,2),'chg_pct':round(chg,2),'vol_ratio':round(vol_ratio,1)
                })
            except: pass
        items.sort(key=lambda x: x['confidence'], reverse=True)
        print(f"  yfinance momentum scan: {len(items)} signals from {len(WATCHLIST)} stocks")
    except Exception as e:
        print(f"  yfinance momentum: {e}")
    return items[:20]

def refresh_whale_data():
    """
    Pull all whale sources — Alpaca screener, news, yfinance momentum.
    Runs every 20 minutes during market hours. Always produces results.
    """
    global _whale_cache
    with _whale_lock:
        if time.time() - _whale_cache['ts'] < 900: return  # 15min cache

    print("  Scanning whale sources...")
    all_items = []

    # 1. Alpaca most-actives (best signal during market hours)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_active  = ex.submit(alpaca_get_screener, 'most-actives', 25)
        f_gainers = ex.submit(alpaca_get_screener, 'gainers', 15)
        f_news    = ex.submit(alpaca_get_news_tickers)
        try: all_items += f_active.result(timeout=15)
        except Exception as e: print(f"  most-actives timeout: {e}")
        try: all_items += f_gainers.result(timeout=15)
        except Exception as e: print(f"  gainers timeout: {e}")
        try: all_items += f_news.result(timeout=15)
        except Exception as e: print(f"  news timeout: {e}")

    # 2. yfinance momentum — always runs, always has data
    try: all_items += yfinance_momentum_scan()
    except Exception as e: print(f"  momentum error: {e}")

    # 3. Deduplicate by symbol, boost confidence when multiple sources agree
    sym_map = {}
    for item in all_items:
        sym = item['symbol']
        if sym not in sym_map:
            sym_map[sym] = {
                'symbol':sym,'sources':[],'items':[],
                'confidence':0,'hold_days':0,'source_count':0
            }
        sym_map[sym]['sources'].append(item['source'])
        sym_map[sym]['items'].append(item)
        sym_map[sym]['confidence'] = max(sym_map[sym]['confidence'], item['confidence'])
        sym_map[sym]['hold_days']  = max(sym_map[sym]['hold_days'],  item['hold_days'])
        if item.get('price'): sym_map[sym]['price'] = item['price']
        if item.get('chg_pct') is not None: sym_map[sym]['pct'] = item['chg_pct']

    # Multi-source confidence boost
    for sym, d in sym_map.items():
        n = len(set(d['sources']))
        d['source_count'] = n
        d['multi_source']  = n >= 2
        if n >= 3:   d['confidence'] = min(99, d['confidence'] + 12)
        elif n >= 2: d['confidence'] = min(99, d['confidence'] + 6)
        # Pick best hold recommendation
        d['rec_mode'] = 'day' if d['hold_days']<=1 else ('swing' if d['hold_days']<=30 else 'longterm')
        # Best reason from items
        best = max(d['items'], key=lambda x: x['confidence'])
        d['top_reason'] = best.get('title','')

    combined = sorted(sym_map.values(), key=lambda x: x['confidence'], reverse=True)

    # 4. Ensure price is populated from cache for all symbols
    prices = all_prices()
    for w in combined:
        if not w.get('price') and w['symbol'] in prices:
            w['price'] = prices[w['symbol']]['price']
            w['pct']   = prices[w['symbol']]['pct']

    with _whale_lock:
        _whale_cache = {
            'combined': combined,
            'sec4':     [i for i in all_items if 'Alpaca' in i.get('source','')],
            'congress': [],
            'unusual':  [i for i in all_items if 'momentum' in i.get('type','').lower()],
            'ts':       time.time()
        }
    print(f"  Whale engine: {len(combined)} symbols total (Alpaca + yfinance momentum)")

def get_whale_data():
    is_empty = len(_whale_cache.get('combined', [])) == 0
    is_stale = time.time() - _whale_cache.get('ts', 0) > 1800
    if is_empty:
        refresh_whale_data()  # synchronous on first call — must populate before returning
    elif is_stale:
        threading.Thread(target=refresh_whale_data, daemon=True).start()
    return _whale_cache


# ══════════════════════════════════════════════════════════════════════
# ── PRICE + TECHNICAL ENGINE ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════
_price_cache = {}
_price_ts    = 0
_price_lock  = threading.Lock()

def price_thread():
    """
    Price update loop.
    Primary:  Alpaca snapshot API (works 24/7, includes pre/after-hours close)
    Fallback: yfinance with prepost=True (catches after-hours moves)
    VIX/BTC:  always yfinance (Alpaca doesn't carry indices/crypto)
    Sleep:    5s market hours | 30s pre/after hours | 120s overnight/weekend
    """
    global _price_ts
    base_stocks = [
        'SPY','QQQ','NVDA','TSLA','AAPL','MSFT','AMZN','META','GOOGL',
        'SOXL','TQQQ','MSTR','COIN','AMD','PLTR','MU','SMCI','DRAM',
        'SOFI','RKLB','ASTS','IONQ','ARM','APP','AVGO','SMR'
    ]
    yf_special = {'^VIX':'VIX', 'BTC-USD':'BTC', 'GLD':'GLD'}

    while True:
        try:
            whale_syms = [w['symbol'] for w in _whale_cache.get('combined',[])
                          if re.match(r'^[A-Z]{1,5}$', w.get('symbol',''))][:20]
            trade_syms = [t['symbol'] for t in state['trades'].values()
                          if re.match(r'^[A-Z]{1,5}$', t.get('symbol',''))]
            stock_syms = list(dict.fromkeys(base_stocks + whale_syms + trade_syms))

            # ── PRIMARY: Alpaca Snapshot (24/7, pre+after hours included) ─────
            alpaca_ok = False
            if alpaca_data:
                snaps = alpaca_snapshot_prices(stock_syms)
                if snaps:
                    with _price_lock:
                        _price_cache.update(snaps)
                        _price_ts = time.time()
                    alpaca_ok = True

            # ── FALLBACK: yfinance with prepost=True ──────────────────────────
            if not alpaca_ok:
                try:
                    df = yf.download(
                        stock_syms[:25], period='5d', interval='1d',
                        prepost=True, auto_adjust=True, progress=False
                    )
                    multi = isinstance(df.columns, pd.MultiIndex)
                    with _price_lock:
                        for sym in stock_syms:
                            try:
                                cl  = (df['Close'][sym] if multi else df['Close']).dropna()
                                if not len(cl): continue
                                p   = float(cl.iloc[-1])
                                prv = float(cl.iloc[-2]) if len(cl)>=2 else p
                                _price_cache[sym] = {
                                    'price': round(p,2),
                                    'pct':   round((p-prv)/prv*100,3) if prv else 0,
                                    'src':   'yfinance_prepost', 'ts': time.time()
                                }
                            except: pass
                        _price_ts = time.time()
                except Exception as e:
                    print(f"yf fallback: {e}")

            # ── ALWAYS: VIX + BTC via yfinance ───────────────────────────────
            try:
                yf_df  = yf.download(
                    list(yf_special.keys()), period='5d', interval='1d',
                    prepost=True, auto_adjust=True, progress=False
                )
                yf_multi = isinstance(yf_df.columns, pd.MultiIndex)
                with _price_lock:
                    for raw, disp in yf_special.items():
                        try:
                            cl  = (yf_df['Close'][raw] if yf_multi else yf_df['Close']).dropna()
                            if not len(cl): continue
                            p   = float(cl.iloc[-1])
                            prv = float(cl.iloc[-2]) if len(cl)>=2 else p
                            entry = {
                                'price': round(p,2),
                                'pct':   round((p-prv)/prv*100,3) if prv else 0,
                                'src':   'yfinance', 'ts': time.time(), 'display': disp
                            }
                            _price_cache[raw]  = entry
                            _price_cache[disp] = entry
                        except: pass
            except Exception as e:
                print(f"yf VIX/BTC: {e}")

        except Exception as e:
            print(f"Price thread: {e}")

        # Adaptive sleep
        n = now_et(); t = n.hour*60 + n.minute
        mkt_open  = n.weekday()<5 and 570<=t<960
        pre_post  = n.weekday()<5 and (480<=t<570 or 960<=t<1200)
        sleep_sec = 5 if mkt_open else (30 if pre_post else 120)
        time.sleep(sleep_sec)


def cp(sym):
    with _price_lock: return _price_cache.get(sym)

def all_prices():
    with _price_lock: return dict(_price_cache)

def get_candles(sym, period='1d', interval='5m'):
    """
    Smart candle fetch with full after-hours fallback.
    After market close, intraday data is stale — automatically widens timeframe.
    """
    # Map display names back to yfinance format
    sym_map = {'VIX':'^VIX', 'BTC':'BTC-USD'}
    yf_sym  = sym_map.get(sym, sym)

    # Fallback chain: try requested, then progressively wider intervals
    attempts = [(period, interval)]
    if interval in ('1m','2m','5m'):
        attempts += [('5d','15m'), ('1mo','1h'), ('3mo','1d')]
    elif interval in ('15m','30m','60m','1h'):
        attempts += [('1mo','1h'), ('3mo','1d'), ('1y','1wk')]
    elif interval == '1d':
        attempts += [('3mo','1d'), ('1y','1wk')]

    for p, iv in attempts:
        try:
            df = yf.download(yf_sym, period=p, interval=iv,
                             auto_adjust=True, progress=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna()
            if not df.empty and len(df) >= 5:
                return df
        except Exception as e:
            print(f"  candles {yf_sym} {p}/{iv}: {e}")
            continue
    return pd.DataFrame()


def compute_technicals(sym, df=None):
    """Full technical suite with after-hours safe fallback"""
    if df is None:
        # Try intraday first, fall back to daily if empty
        df = get_candles(sym, '1d', '5m')
        if df.empty or len(df) < 10:
            df = get_candles(sym, '5d', '15m')
        if df.empty or len(df) < 10:
            df = get_candles(sym, '3mo', '1d')   # always works — daily historical
    if df.empty or len(df) < 5:
        return None
    c=df['Close']; h=df['High']; l=df['Low']; v=df['Volume']
    # VWAP
    typical=(h+l+c)/3; cum_vol=v.cumsum().replace(0,1)
    vwap=(typical*v).cumsum()/cum_vol
    # EMAs
    ema9=c.ewm(span=9,adjust=False).mean()
    ema20=c.ewm(span=20,adjust=False).mean()
    ema50=c.ewm(span=50,adjust=False).mean()
    ema200=c.ewm(span=200,adjust=False).mean()
    # RSI
    delta=c.diff(); gain=delta.clip(lower=0).rolling(14).mean()
    loss=(-delta.clip(upper=0)).rolling(14).mean()
    rs=gain/loss.replace(0,1e-9); rsi=100-100/(1+rs)
    # MACD
    ema12=c.ewm(span=12,adjust=False).mean(); ema26=c.ewm(span=26,adjust=False).mean()
    macd=ema12-ema26; signal=macd.ewm(span=9,adjust=False).mean(); hist=macd-signal
    # Bollinger Bands
    sma20=c.rolling(20).mean(); std20=c.rolling(20).std()
    bb_upper=sma20+2*std20; bb_lower=sma20-2*std20
    # ATR
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr=tr.rolling(14).mean()
    # Support/Resistance from recent pivots
    recent=df.tail(40)
    support=float(recent['Low'].quantile(.15))
    resistance=float(recent['High'].quantile(.85))
    # Last values
    last=float(c.iloc[-1]); lvwap=float(vwap.iloc[-1])
    le9=float(ema9.iloc[-1]); le20=float(ema20.iloc[-1])
    le50=float(ema50.iloc[-1]); le200=float(ema200.iloc[-1])
    lr=float(rsi.iloc[-1]); lm=float(macd.iloc[-1])
    ls=float(signal.iloc[-1]); lh=float(hist.iloc[-1])
    latr=float(atr.iloc[-1])
    lbb_u=float(bb_upper.iloc[-1]); lbb_l=float(bb_lower.iloc[-1])
    lbb_mid=float(sma20.iloc[-1])
    # Signals
    above_vwap=last>lvwap; bull_ema=le9>le20
    macd_bull=lh>0 and lm>ls
    rsi_ok=30<lr<70; rsi_os=lr<35; rsi_ob=lr>70
    near_support=last<support*1.03
    near_resistance=last>resistance*.97
    # Composite signal
    bull_pts=(1 if above_vwap else 0)+(1 if bull_ema else 0)+(1 if macd_bull else 0)+(1 if rsi_ok else 0)
    if bull_pts>=3 and not rsi_ob: sig,sig_col='BUY','green'
    elif bull_pts<=1 or rsi_ob: sig,sig_col='SELL','red'
    else: sig,sig_col='WAIT','amber'
    sig_reason=(f"{'Above' if above_vwap else 'Below'} VWAP ${lvwap:.2f} | "
                f"RSI {lr:.0f} | MACD {'▲' if macd_bull else '▼'} | "
                f"EMA9 {'>' if bull_ema else '<'} EMA20")
    # ATR-based levels
    entry=last
    stop_day   = round(max(entry - 1.5*latr,   support * 0.995), 2)
    target_day = round(min(entry + 2.5*latr,   resistance),       2)
    # Swing: minimum 7% stop, minimum 15% target
    stop_swing   = round(min(entry * 0.93, max(entry - 3.5*latr, le50 * 0.96)), 2)
    target_swing = round(max(entry * 1.15, min(entry + 6.0*latr, resistance * 1.08)), 2)
    # Long term: 12% stop, 35%+ target
    stop_lt   = round(entry * 0.88, 2)
    target_lt = round(entry * 1.40, 2)
    # Prediction: simple linear regression on close
    x=np.arange(len(c)); coeffs=np.polyfit(x,c.values,1)
    slope=coeffs[0]; pred_5d=round(float(np.polyval(coeffs,len(c)+1)),2)
    pred_30d=round(float(np.polyval(coeffs,len(c)+6)),2)
    pred_90d=round(float(np.polyval(coeffs,len(c)+18)),2)
    trend='bullish' if slope>0 else 'bearish'
    # Best mode
    daily_vol=float(c.pct_change().std()*100) if len(c)>5 else 2.0
    if daily_vol>4: best_mode='day'
    elif daily_vol>1.5: best_mode='swing'
    else: best_mode='longterm'
    # Store change_pct from price cache
    cached_p = _price_cache.get(sym)
    chg_pct = cached_p['pct'] if cached_p else round(((last-float(c.iloc[-2]))/float(c.iloc[-2])*100) if len(c)>=2 else 0, 3)
    return {
        'signal':sig,'signal_color':sig_col,'signal_reason':sig_reason,
        'price':round(last,2),'vwap':round(lvwap,2),'above_vwap':above_vwap,
        'ema9':round(le9,2),'ema20':round(le20,2),'ema50':round(le50,2),'ema200':round(le200,2),
        'rsi':round(lr,1),'rsi_oversold':rsi_os,'rsi_overbought':rsi_ob,
        'macd':round(lm,4),'macd_signal':round(ls,4),'macd_hist':round(lh,4),'macd_bull':macd_bull,
        'bb_upper':round(lbb_u,2),'bb_lower':round(lbb_l,2),'bb_mid':round(lbb_mid,2),
        'atr':round(latr,3),'support':round(support,2),'resistance':round(resistance,2),
        'near_support':near_support,'near_resistance':near_resistance,
        'stop_day':stop_day,'target_day':target_day,
        'stop_swing':stop_swing,'target_swing':target_swing,
        'stop_lt':stop_lt,'target_lt':target_lt,
        'pred_5d':pred_5d,'pred_30d':pred_30d,'pred_90d':pred_90d,'trend':trend,
        'best_mode':best_mode,'daily_vol':round(daily_vol,2),'change_pct':round(chg_pct,3),
        'vwap_series':vwap.tolist()[-80:],'ema9_series':ema9.tolist()[-80:],
        'ema20_series':ema20.tolist()[-80:],'ema50_series':ema50.tolist()[-80:],
        'bb_upper_series':bb_upper.tolist()[-80:],'bb_lower_series':bb_lower.tolist()[-80:],
        'close_series':c.tolist()[-80:],'times':[str(i)[-8:-3] if ' ' in str(i) else str(i)[-5:] for i in df.index[-80:]],
        'slope':round(float(slope),4),'pred_series':[round(float(np.polyval(coeffs,xi)),2) for xi in range(max(0,len(c)-80),len(c)+20)],
        'bull_ema':bull_ema,'bull_pts':bull_pts
    }

def whale_score_and_recommend(sym, tech, whale_item=None):
    """
    Differentiating score 0-99:
    AAPL down 6% SELL = ~15-25 | NVDA BUY above VWAP = ~50-65 | Congress+BUY = ~85-99
    """
    score   = 0
    reasons = []

    # ── Technical: PRIMARY differentiator (max 45 pts) ───────────────
    if tech:
        sig = tech.get('signal', 'WAIT')
        rsi = float(tech.get('rsi', 50) or 50)
        chg = float(tech.get('change_pct', 0) or 0)
        bp  = tech.get('bull_pts', 0)

        # Signal is the main driver
        if sig == 'BUY':
            score += 15 + bp * 5          # 15-35 for strong BUY
            if tech.get('above_vwap'):    score += 5;  reasons.append('Above VWAP')
            if tech.get('macd_bull'):     score += 4;  reasons.append('MACD bullish')
            if tech.get('rsi_oversold'):  score += 5;  reasons.append('RSI oversold bounce')
            if tech.get('near_support'):  score += 4;  reasons.append('Near support')
            if tech.get('trend') == 'bullish': score += 3
        elif sig == 'WAIT':
            score += 8
            if tech.get('macd_bull'):     score += 3
            if tech.get('above_vwap'):    score += 3
        else:  # SELL
            score -= 5

        # Price change today differentiates same-signal stocks
        if   chg > 8:  score += 12; reasons.append(f'+{chg:.1f}% strong momentum')
        elif chg > 5:  score += 8;  reasons.append(f'+{chg:.1f}% momentum')
        elif chg > 2:  score += 4
        elif chg > 0:  score += 1
        elif chg < -8: score -= 15; reasons.append(f'{chg:.1f}% heavy selling')
        elif chg < -4: score -= 10; reasons.append(f'{chg:.1f}% declining')
        elif chg < -1: score -= 4

        # RSI extremes
        if   rsi < 30: score += 6; reasons.append(f'RSI {rsi:.0f} oversold')
        elif rsi > 75: score -= 8; reasons.append(f'RSI {rsi:.0f} overbought — caution')

    # ── Whale source component (max 55 pts) ──────────────────────────
    if whale_item:
        src_types = set(it.get('type','') for it in whale_item.get('items', []))
        sources   = set(whale_item.get('sources', []))

        if 'congress'     in src_types: score += 35; reps=[it.get('rep','') for it in whale_item.get('items',[]) if it.get('type')=='congress']; reasons.append(f"Congress buy: {reps[0] if reps else 'member'}")
        if 'insider'      in src_types: score += 28; reasons.append('SEC Form 4 insider buy')
        if 'options_flow' in src_types: score += 20; reasons.append('Unusual options flow')
        if 'volume_surge' in src_types: score += 12; reasons.append('Volume surge')
        if 'momentum'     in src_types: score += 8;  reasons.append('Price momentum')
        if 'news_catalyst'in src_types: score += 5
        if src_types == {'news_flow'}:  score = min(score, 42)  # hard cap on news-only

        real_count = len([s for s in sources if 'News' not in s and 'Momentum' not in s])
        if real_count >= 3: score += 10; reasons.append('3+ sources agree')
        elif real_count >= 2: score += 5

    score = min(99, max(0, round(score)))

    # ── Mode recommendation ───────────────────────────────────────────
    hold_days = whale_item.get('hold_days', 14) if whale_item else 14
    vol       = float(tech.get('daily_vol', 2.0) or 2.0) if tech else 2.0

    if   hold_days >= 90: rec_mode = 'longterm'
    elif hold_days >= 14: rec_mode = 'swing'
    else:                 rec_mode = 'day'
    if vol > 6: rec_mode = 'day'

    return {
        'score': score, 'reasons': reasons[:5],
        'rec_mode': rec_mode, 'hold_days': hold_days
    }

# ══════════════════════════════════════════════════════════════════════
# ── TRADE ENGINE ──────────────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

state = {
    'trades':{},         # id -> trade
    'completed':[],
    'capital':{'day':5000.0,'swing':10000.0,'longterm':15000.0},
    'starting':{'day':5000.0,'swing':10000.0,'longterm':15000.0},
    'pnl':{'day':0.0,'swing':0.0,'longterm':0.0},
    'whale_cache':[],
    'log':[],
    'date':None,
    'daily_report':None,
    'ai_alerts':[],
}

STATE_FILE='atp_v3_state.json'

def save_state():
    try:
        snap={k:v for k,v in state.items() if k not in ('whale_cache',)}
        with open(STATE_FILE,'w') as f: json.dump(snap,f)
    except Exception as e: print(f"Save: {e}")

def load_state():
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE) as f: snap=json.load(f)
        today=today_str()
        if snap.get('date')==today:
            for k in ('trades','completed','capital','starting','pnl','log','ai_alerts'):
                if k in snap: state[k]=snap[k]
            state['date']=today
        else:
            state['capital']={'day':5000.0,'swing':10000.0,'longterm':15000.0}
            state['starting']={'day':5000.0,'swing':10000.0,'longterm':15000.0}
        # Always restore completed for history
        if 'completed' in snap: state['completed']=snap['completed'][-100:]
    except Exception as e: print(f"Load: {e}")

def add_log(msg, alert=False):
    ts=now_str()
    state['log'].insert(0,{'time':ts,'msg':msg,'alert':alert})
    state['log']=state['log'][:100]
    print(f"[{ts}] {msg}")
    if alert:
        state['ai_alerts'].insert(0,{'time':ts,'msg':msg})
        state['ai_alerts']=state['ai_alerts'][:30]
        tg(f"🚨 AutoTrade Pro\n{msg}")
    elif msg.startswith(('ENTER','CLOSE','TARGET','STOP','EOD')):
        tg(f"📊 {msg}")

import uuid

def enter_trade(sym, mode, manual=False, whale_data=None):
    """Enter day/swing/long-term trade"""
    if mode not in ('day','swing','longterm'):
        return False,'Invalid mode'
    # Check existing
    for t in state['trades'].values():
        if t['symbol']==sym and t['mode']==mode:
            return False,f"Already in {sym} ({mode})"
    # Price
    cached=cp(sym)
    if not cached:
        try:
            tk=yf.Ticker(sym); fi=tk.fast_info
            p=float(getattr(fi,'last_price',0) or 0)
            if p>0: cached={'price':p,'pct':0}
            else: return False,'No price'
        except: return False,'No price'
    price=cached['price']
    # Capital allocation
    cap=state['capital'][mode]
    n_open=sum(1 for t in state['trades'].values() if t['mode']==mode)
    max_positions={'day':5,'swing':8,'longterm':12}
    if n_open>=max_positions[mode]: return False,f"Max {max_positions[mode]} {mode} positions"
    alloc_pct={'day':0.25,'swing':0.20,'longterm':0.15}
    alloc=min(cap*alloc_pct[mode], cap/(max_positions[mode]-n_open+1))
    if alloc<price: return False,f"Insufficient capital: ${cap:.0f}"
    shares=int(alloc/price)
    if shares<1: return False,'Need at least 1 share'
    # Get technicals for stops/targets
    tech=compute_technicals(sym)
    tid=str(uuid.uuid4())[:8]
    stop_key={'day':'stop_day','swing':'stop_swing','longterm':'stop_lt'}[mode]
    target_key={'day':'target_day','swing':'target_swing','longterm':'target_lt'}[mode]
    stop=tech[stop_key] if tech and tech.get(stop_key) else round(price*(1-{'day':.06,'swing':.10,'longterm':.12}[mode]),2)
    target=tech[target_key] if tech and tech.get(target_key) else round(price*(1+{'day':.10,'swing':.20,'longterm':.35}[mode]),2)
    # Hold duration
    hold_map={'day':1,'swing':14,'longterm':90}
    hold_days=hold_map[mode]
    if whale_data: hold_days=max(hold_days,whale_data.get('hold_days',hold_days))
    trade={
        'id':tid,'symbol':sym,'mode':mode,'entry':round(price,2),'shares':shares,
        'cost':round(shares*price,2),'stop':stop,'target':target,
        'current':round(price,2),'peak':round(price,2),'peak_pnl_pct':0.0,
        'entry_time':now_str(),'entered_at':time.time(),'manual':manual,
        'hold_days':hold_days,'exit_after':time.time()+hold_days*86400,
        'whale_confidence':whale_data.get('confidence',0) if whale_data else 0,
        'whale_sources':whale_data.get('sources',[]) if whale_data else [],
    }
    state['trades'][tid]=trade
    state['capital'][mode]=round(cap-trade['cost'],2)
    save_state()
    stop_pct=round((price-stop)/price*100,1)
    tgt_pct=round((target-price)/price*100,1)
    add_log(f"{'MANUAL' if manual else 'AUTO'} ENTER {sym} [{mode.upper()}] {shares}sh @${price:.2f} | T=${target:.2f}(+{tgt_pct}%) S=${stop:.2f}(-{stop_pct}%)", alert=True)
    if alpaca_trading:
        try: alpaca_trading.submit_order(MarketOrderRequest(symbol=sym,qty=shares,side=OrderSide.BUY,time_in_force=TimeInForce.DAY))
        except Exception as e: print(f"Alpaca enter: {e}")
    return True,f"Entered {sym} [{mode}]"

def close_trade(tid, reason='manual'):
    t=state['trades'].pop(tid,None)
    if not t: return
    sym=t['symbol']; mode=t['mode']
    cached=cp(sym); sell=round(cached['price'] if cached else t['current'],2)
    pnl=round((sell-t['entry'])*t['shares'],2)
    pnl_pct=round((sell-t['entry'])/t['entry']*100,2)
    proceeds=round(sell*t['shares'],2)
    state['completed'].append({**t,'exit':sell,'exit_time':now_str(),'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    state['completed']=state['completed'][-200:]
    state['pnl'][mode]=round(state['pnl'][mode]+pnl,2)
    state['capital'][mode]=round(state['capital'][mode]+proceeds,2)
    save_state()
    label={'target':'🎯 TARGET','stop':'🛑 STOP','manual':'✋ MANUAL','eod':'🌙 EOD','swing_exit':'📅 SWING EXIT','lt_exit':'📅 LT EXIT'}
    add_log(f"CLOSE {sym} [{mode.upper()}] {label.get(reason,reason)} ${pnl:+.2f} ({pnl_pct:+.1f}%)", alert=pnl>200 or reason=='target')
    if alpaca_trading:
        try: alpaca_trading.close_position(sym)
        except: pass

def monitor_trades():
    """Called every minute — update prices, check exits"""
    if not is_trading_day(): return
    for tid, t in list(state['trades'].items()):
        cached=cp(t['symbol'])
        if not cached: continue
        price=cached['price']
        state['trades'][tid]['current']=price
        peak=max(price,t.get('peak',price))
        state['trades'][tid]['peak']=peak
        pnl_pct=(price-t['entry'])/t['entry']*100
        peak_pct=(peak-t['entry'])/t['entry']*100
        state['trades'][tid]['peak_pnl_pct']=max(peak_pct,t.get('peak_pnl_pct',0))
        mode=t['mode']
        # Target hit
        if price>=t['target']:
            close_trade(tid,'target'); continue
        # Stop hit
        if price<=t['stop']:
            close_trade(tid,'stop'); continue
        # Day trade: trailing stop if peak>=3%
        if mode=='day' and peak_pct>=3 and (peak_pct-pnl_pct)>=2:
            close_trade(tid,'momentum_stall'); continue
        # Swing: time exit
        if mode=='swing' and time.time()>t.get('exit_after',0) and not is_market_open():
            close_trade(tid,'swing_exit'); continue
        # Long term: only exit on big loss or huge gain
        if mode=='longterm' and pnl_pct<=-15:
            close_trade(tid,'stop'); continue

def whale_auto_enter():
    """Auto-enter ONLY high-conviction whale signals — real sources only, no news garbage"""
    if not is_market_open(): return
    whale_data = get_whale_data()
    combined   = whale_data.get('combined', [])

    for w in combined[:15]:
        sym = w['symbol']
        # Skip if already in any active trade
        if any(t['symbol'] == sym for t in state['trades'].values()): continue

        score      = w.get('confidence', 0)
        sources    = set(w.get('sources', []))
        src_types  = set(it.get('type','') for it in w.get('items', []))

        # ── STRICT ENTRY GATE ─────────────────────────────────────────
        # Must have at least one REAL whale source — not just news/RSS headlines
        real_sources = sources - {'Options News','News Flow','Momentum Scanner'}
        has_real = bool(real_sources)  # Congress, SEC Form 4, Unusual Whales, Volume Surge, Barchart

        # Require score >= 80 AND a real source
        # OR score >= 85 for momentum/volume surge (higher bar without named insider)
        if not has_real and score < 85: continue
        if has_real and score < 80:     continue

        # Must have a BUY technical signal
        tech = compute_technicals(sym)
        if not tech or tech['signal'] != 'BUY': continue

        # RSI must not be overbought
        if tech.get('rsi', 50) > 72: continue

        # Determine mode from whale data
        rec  = whale_score_and_recommend(sym, tech, w)
        mode = rec['rec_mode']

        ok, msg = enter_trade(sym, mode, manual=False, whale_data=w)
        if ok:
            src_str = ', '.join(sorted(real_sources or sources))
            add_log(
                f"🐋 WHALE AUTO ENTRY {sym} [{mode.upper()}] "
                f"Score:{score}/99 Sources:{src_str}",
                alert=True
            )

# ── AI ANALYSIS ───────────────────────────────────────────────────────
def ai_analyze(sym, tech, whale=None, mode=None):
    """Full AI analysis — grounded in real numbers, no hallucination"""
    if not tech:
        return f"No technical data available for {sym}.", 'none'

    price = tech.get('price', 0)
    if price <= 0:
        return f"No price data for {sym}. It may be after hours or an unsupported ticker.", 'none'

    whale_info = ''
    if whale:
        for item in (whale.get('items') or [])[:2]:
            whale_info += f"- {item.get('source','')}: {item.get('title','')[:70]}\n"

    # Known ETF / ticker descriptions to prevent hallucination
    ticker_notes = {
        'DRAM': 'DRAM is the Roundhill Memory & Storage Technology ETF — it tracks memory chip companies (Micron, SK Hynix, Samsung). It is NOT the DRAM memory technology itself.',
        'SOXL': 'SOXL is the 3x leveraged semiconductor ETF. It amplifies semiconductor index moves by 3x.',
        'TQQQ': 'TQQQ is the 3x leveraged Nasdaq-100 ETF.',
        'UPRO': 'UPRO is the 3x leveraged S&P 500 ETF.',
        'MSTR': 'MSTR is MicroStrategy — a Bitcoin proxy stock.',
        'FNGU': 'FNGU is the 3x leveraged FAANG+ ETF.',
    }
    ticker_context = ticker_notes.get(sym, '')

    sys_p = (
        f"You are AutoTrade Pro, a professional trading AI for Abiy Kassa. "
        f"You are analyzing the ticker {sym}. "
        f"{ticker_context} "
        f"CRITICAL RULES: "
        f"1. ONLY use the exact prices provided — NEVER invent or guess prices. "
        f"2. The current price of {sym} is EXACTLY ${price:.2f}. Do not use any other price. "
        f"3. Do not mention other stocks unless they are directly relevant to {sym}. "
        f"4. Do not suggest entering trades unless explicitly asked. "
        f"5. Be concise — max 5 sentences per section."
    )

    user_p = (
        f"Analyze {sym} (current price: ${price:.2f}):\n"
        f"Signal: {tech.get('signal','WAIT')} | RSI: {tech.get('rsi',50):.0f} | "
        f"VWAP: ${tech.get('vwap',price):.2f} ({'ABOVE' if tech.get('above_vwap') else 'BELOW'})\n"
        f"EMA9: ${tech.get('ema9',price):.2f} | EMA20: ${tech.get('ema20',price):.2f} | EMA50: ${tech.get('ema50',price):.2f}\n"
        f"MACD: {'BULLISH ▲' if tech.get('macd_bull') else 'BEARISH ▼'} | ATR: {tech.get('atr',0):.3f}\n"
        f"Support: ${tech.get('support',price*.94):.2f} | Resistance: ${tech.get('resistance',price*1.06):.2f}\n"
        f"Trend: {tech.get('trend','unknown')} | Daily volatility: {tech.get('daily_vol',2):.1f}%/day\n"
        f"Regression price targets — 5D: ${tech.get('pred_5d',price):.2f} | 30D: ${tech.get('pred_30d',price):.2f} | 90D: ${tech.get('pred_90d',price):.2f}\n"
        f"{'Whale signals: ' + whale_info if whale_info else 'No whale signals for this ticker.'}\n\n"
        f"Provide analysis in this exact format:\n"
        f"SIGNAL: [BUY/WAIT/AVOID] — [one sentence reason using real prices above]\n"
        f"DAY TRADE: Entry ${price:.2f}±X | Target $X | Stop $X\n"
        f"SWING (2-3wk): Entry zone $X-$X | Target $X (+X%) | Stop $X (-X%)\n"
        f"LONG TERM: [one sentence thesis] | 90-day target: $X\n"
        f"RISK: [main risk in one sentence]\n"
        f"NOTE: [one sentence about {sym} specifically — what drives it, any upcoming catalyst]"
    )

    resp, src = ai_call(sys_p, user_p, max_tokens=500)
    return resp or f"Analysis unavailable for {sym}. Check GROQ_API_KEY.", src or 'none'

# ── DAILY REPORT ──────────────────────────────────────────────────────
def generate_daily_report():
    all_trades=state['completed']
    today_trades=[t for t in all_trades if t.get('exit_time','').endswith('ET') and
                  datetime.now(ET_TZ).strftime('%Y-%m-%d') in (t.get('exit_time') or '')]
    total_pnl=sum(v for v in state['pnl'].values())
    wins=[t for t in today_trades if t.get('pnl',0)>=0]
    losses=[t for t in today_trades if t.get('pnl',0)<0]
    open_t=list(state['trades'].values())
    sys_p="You are AutoTrade Pro. Write a concise daily trading report. Professional but clear."
    user_p=(f"Daily Report — {today_str()}\n"
            f"Total P&L: ${total_pnl:+.2f}\n"
            f"Day P&L: ${state['pnl']['day']:+.2f} | Swing: ${state['pnl']['swing']:+.2f} | LT: ${state['pnl']['longterm']:+.2f}\n"
            f"Closed trades: {len(today_trades)} ({len(wins)} wins, {len(losses)} losses)\n"
            f"Open positions: {len(open_t)}: {', '.join(t['symbol'] for t in open_t)}\n"
            f"Whale signals today: {len(_whale_cache.get('combined',[]))}\n"
            f"Best trade: {max(today_trades,key=lambda x:x.get('pnl',0),default={}).get('symbol','none')} | "
            f"Worst: {min(today_trades,key=lambda x:x.get('pnl',0),default={}).get('symbol','none')}\n\n"
            "Write: Performance summary (2 sentences), Key insights (3 bullets), Tomorrow's watchlist from open positions.")
    report,_=ai_call(sys_p,user_p,max_tokens=400)
    state['daily_report']={'text':report or 'Report unavailable','date':today_str(),'ts':now_str()}
    msg=(f"📊 AutoTrade Pro Daily Report — {today_str()}\n{'━'*25}\n"
         f"P&L: ${total_pnl:+.2f} | {len(wins)}W/{len(losses)}L\n"
         f"Open: {len(open_t)} positions\n\n{report or ''}")
    tg(msg)
    add_log(f"Daily report generated: P&L ${total_pnl:+.2f} {len(wins)}W/{len(losses)}L")

# ── SCHEDULER ─────────────────────────────────────────────────────────
def job_morning():
    if not is_trading_day(): return
    state['date']=today_str()
    state['pnl']={'day':0.0,'swing':0.0,'longterm':0.0}
    state['capital']={'day':5000.0,'swing':10000.0,'longterm':15000.0}
    state['starting']={'day':5000.0,'swing':10000.0,'longterm':15000.0}
    add_log("🌅 New trading day started")
    refresh_whale_data()
    tg(f"🌅 AutoTrade Pro — {today_str()} open\nWhale scan running...")

def job_minute():
    if not is_trading_day(): return
    state['date']=today_str()
    monitor_trades()
    if is_market_open():
        t=now_et().hour*60+now_et().minute
        if t>=585: whale_auto_enter()  # after 9:45

def job_whale():
    refresh_whale_data()

def job_eod():
    if not is_trading_day(): return
    for tid in [t for t,v in state['trades'].items() if v['mode']=='day']:
        close_trade(tid,'eod')
    generate_daily_report()
    save_state()

def job_keepalive():
    url=APP_URL or f"http://localhost:{PORT}"
    try: urllib.request.urlopen(urllib.request.Request(f"{url}/ping",headers={'User-Agent':UA}),timeout=8)
    except: pass

# ══════════════════════════════════════════════════════════════════════
# ── ROUTES ────────────────────────════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
@app.route('/')
def index(): return send_from_directory('.','index.html')
@app.route('/ping')
def ping(): return _cors({'ok':True,'time':now_str(),'market':is_market_open()})

@app.route('/data-status')
def data_status():
    """Shows exactly where prices are coming from right now"""
    p = all_prices()
    src_counts = {}
    for sym, q in p.items():
        s = q.get('src','unknown')
        src_counts[s] = src_counts.get(s,0) + 1
    n = now_et(); t = n.hour*60 + n.minute
    mkt_open = n.weekday()<5 and 570<=t<960
    pre_post = n.weekday()<5 and (480<=t<570 or 960<=t<1200)
    session  = 'MARKET OPEN' if mkt_open else ('PRE/AFTER HOURS' if pre_post else 'CLOSED/WEEKEND')
    # Check Alpaca account
    acct_info = {}
    if alpaca_trading:
        try:
            acct = alpaca_trading.get_account()
            acct_info = {
                'status': str(acct.status),
                'buying_power': str(acct.buying_power),
                'data_permissions': 'iex' # paper = IEX feed
            }
        except Exception as e:
            acct_info = {'error': str(e)}
    return _cors({
        'session': session,
        'alpaca_connected': alpaca_data is not None,
        'alpaca_account': acct_info,
        'price_sources': src_counts,
        'prices_cached': len(p),
        'cache_age_sec': round(time.time() - _price_ts, 0) if _price_ts else None,
        'data_flow': {
            'market_hours':  'Alpaca IEX snapshot (real-time ~0s delay)',
            'pre_afterhours':'Alpaca snapshot last trade + yfinance prepost',
            'closed_weekend':'yfinance last daily close (no intraday)',
            'vix_btc':       'yfinance always (Alpaca does not carry indices/crypto)'
        },
        'note': 'Alpaca paper accounts use IEX feed — real-time during market hours only'
    })

@app.route('/whale-data')
def whale_data_route():
    d=get_whale_data()
    combined=d.get('combined',[])[:25]
    prices=all_prices()
    for w in combined:
        sym=w['symbol']
        q=prices.get(sym)
        if q:
            w['price'] = round(q['price'],2)
            # Only show pct if it's from a live source or genuinely non-zero
            pct = q.get('pct',0) or 0
            src = q.get('src','')
            # Hide stale 0.0% — show dash instead of misleading green +0.0%
            if pct == 0 and src not in ('alpaca_snapshot','live'):
                w['pct'] = None
            else:
                w['pct'] = round(pct,2)
        else:
            w.setdefault('price',None)
            w.setdefault('pct',None)
        if q and q.get('pct') is not None:
            w['live_pct'] = q.get('pct',0)
        cached_tech = state.get('analysis_cache',{}).get(sym,{}).get('tech')
        if cached_tech:
            w['top_signal']  = cached_tech.get('signal','WAIT')
            w['top_reason']  = cached_tech.get('signal_reason','')
            tech_for_score = dict(cached_tech)
            if q: tech_for_score['change_pct'] = q.get('pct',0)
            rec = whale_score_and_recommend(sym, tech_for_score, w)
            w['display_score'] = rec['score']
            w['rec_mode']      = rec['rec_mode']
        else:
            w.setdefault('top_signal','WAIT')
            w.setdefault('top_reason','; '.join((w.get('reasons') or [])[:2]))
            base = w.get('confidence', 60)
            pct  = q.get('pct',0) if q else 0
            adj  = 12 if pct>8 else 8 if pct>5 else 4 if pct>2 else 1 if pct>0 \
                   else -15 if pct<-5 else -8 if pct<-2 else -3 if pct<0 else 0
            w['display_score'] = max(5, min(99, base+adj))
            w.setdefault('rec_mode','swing')
    combined.sort(key=lambda x:x.get('display_score',x.get('confidence',0)),reverse=True)
    return _cors({'combined':combined,
                  'sec4':d.get('sec4',[]),'congress':d.get('congress',[]),
                  'unusual':d.get('unusual',[]),'ts':d.get('ts',0),
                  'age_min':round((time.time()-d.get('ts',time.time()))/60,1)})

@app.route('/whale-refresh', methods=['POST'])
def whale_refresh():
    threading.Thread(target=refresh_whale_data,daemon=True).start()
    return _cors({'ok':True,'msg':'Refreshing whale data...'})

@app.route('/prices')
def prices_route():
    p = all_prices()
    # Ensure VIX and BTC always present under display names
    for raw, disp in [('^VIX','VIX'),('BTC-USD','BTC')]:
        if disp not in p and raw in p:
            p[disp] = dict(p[raw])
    return _cors({'data':p,'src':'live' if alpaca_data else 'delayed','ts':_price_ts})

@app.route('/technicals')
def technicals():
    sym=request.args.get('symbol','').upper()
    if not sym: return _cors({'error':'No symbol'})
    tech=compute_technicals(sym)
    return _cors({'technicals':tech,'symbol':sym})

@app.route('/candles')
def candles():
    sym      = request.args.get('symbol','').upper()
    period   = request.args.get('period','1d')
    interval = request.args.get('interval','5m')
    if not sym: return _cors({'bars':[]})
    try:
        df = get_candles(sym, period, interval)
        if df.empty:
            return _cors({'bars':[],'symbol':sym,'note':'No data — try 5D or 1M timeframe'})
        if isinstance(df.columns, pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df = df.dropna().tail(120)
        bars = []
        for i, r in df.iterrows():
            try:
                idx_et = i.tz_convert(ET_TZ) if hasattr(i,'tzinfo') and i.tzinfo else i
                ts = str(idx_et)
                hm = ts[11:16] if len(ts)>11 else ts[:10]
            except:
                hm = str(i)[:16]
            bars.append({'t':str(i),'hm':hm,
                         'o':round(float(r['Open']),3),'h':round(float(r['High']),3),
                         'l':round(float(r['Low']),3),'c':round(float(r['Close']),3),
                         'v':int(r['Volume']) if 'Volume' in r.index else 0})
        return _cors({'bars':bars,'symbol':sym,'period':period,'interval':interval,'count':len(bars)})
    except Exception as e:
        return _cors({'bars':[],'error':str(e),'symbol':sym})

@app.route('/analyze', methods=['POST'])
def analyze():
    d=request.get_json() or {}
    sym=d.get('symbol','').upper()
    if not sym: return _cors({'ok':False})

    # Clear any stale result immediately so the poll gets fresh data
    state.setdefault('analysis_cache',{}).pop(sym, None)

    def do():
        try:
            tech = None
            # Try full technicals first (may fall back to daily data)
            try:
                tech = compute_technicals(sym)
            except Exception as te:
                print(f"  compute_technicals {sym}: {te}")

            # If still None, build from price cache or yfinance fast_info
            if tech is None:
                price = None
                cached = cp(sym)
                if cached: price = cached['price']
                if price is None:
                    try:
                        tk = yf.Ticker(sym)
                        fi = tk.fast_info
                        price = float(getattr(fi,'last_price',None) or
                                     getattr(fi,'previous_close',None) or 0)
                    except: pass
                if price and price > 0:
                    p = price
                    tech = {
                        'signal':'WAIT',
                        'signal_reason':f'Limited intraday data for {sym} — using last close ${p:.2f}. Full analysis requires market hours data.',
                        'price':p,'change_pct':cached.get('pct',0) if cached else 0,
                        'rsi':50,'vwap':p,'above_vwap':True,'macd_bull':False,
                        'ema9':round(p,2),'ema20':round(p,2),'ema50':round(p,2),
                        'ema200':round(p,2),
                        'bb_upper':round(p*1.05,2),'bb_lower':round(p*.95,2),'bb_mid':round(p,2),
                        'atr':round(p*.015,3),
                        'support':round(p*.93,2),'resistance':round(p*1.07,2),
                        'near_support':False,'near_resistance':False,
                        'stop_day':round(p*.94,2),'target_day':round(p*1.08,2),
                        'stop_swing':round(p*.92,2),'target_swing':round(p*1.18,2),
                        'stop_lt':round(p*.88,2),'target_lt':round(p*1.40,2),
                        'pred_5d':round(p*1.02,2),'pred_30d':round(p*1.06,2),'pred_90d':round(p*1.15,2),
                        'trend':'unknown','best_mode':'swing','daily_vol':2.0,
                        'bull_ema':False,'bull_pts':0,'slope':0,
                        'rsi_oversold':False,'rsi_overbought':False,
                        'macd':0,'macd_signal':0,'macd_hist':0,
                        'vwap_series':[],'ema9_series':[],'ema20_series':[],'ema50_series':[],
                        'bb_upper_series':[],'bb_lower_series':[],'close_series':[],'pred_series':[]
                    }
                else:
                    # Absolute last resort — write error result so spinner stops
                    state.setdefault('analysis_cache',{})[sym] = {
                        'tech':None,'whale':None,'rec':{},'symbol':sym,
                        'analysis':f'{sym} — no price data available. This ticker may be invalid, delisted, or not supported.',
                        'ai_src':'error','ts':now_str()
                    }
                    return

            # Get whale context
            whale = next((w for w in _whale_cache.get('combined',[]) if w['symbol']==sym), None)
            rec   = whale_score_and_recommend(sym, tech, whale) if tech else {}

            # ★ Write partial result immediately — stops the spinner within ~2s ★
            state.setdefault('analysis_cache',{})[sym] = {
                'tech':tech,'whale':whale,'rec':rec,
                'analysis':None,'ai_src':'pending','ts':now_str(),'symbol':sym
            }

            # Run AI analysis (takes 3-8s) — updates the stored result when done
            analysis, ai_src = ai_analyze(sym, tech, whale)
            state['analysis_cache'][sym].update({
                'analysis': analysis or f'{sym} analysis complete. RSI: {tech.get("rsi",50):.0f}, Signal: {tech.get("signal","WAIT")}',
                'ai_src': ai_src or 'groq'
            })

        except Exception as e:
            print(f"Analyze {sym}: {e}")
            import traceback; traceback.print_exc()
            state.setdefault('analysis_cache',{})[sym] = {
                'tech':None,'whale':None,'rec':{},'symbol':sym,
                'analysis':f'Analysis error for {sym}: {str(e)[:120]}',
                'ai_src':'error','ts':now_str()
            }

    threading.Thread(target=do, daemon=True).start()
    return _cors({'ok':True,'msg':f'Analyzing {sym}...'})

@app.route('/analysis-result')
def analysis_result():
    sym=request.args.get('symbol','').upper()
    r=state.get('analysis_cache',{}).get(sym)
    return _cors({'result':r,'ready':r is not None})

@app.route('/trades')
def trades_route():
    trades_out=[]
    for tid,t in state['trades'].items():
        cached=cp(t['symbol'])
        if cached: state['trades'][tid]['current']=cached['price']
        ct=state['trades'][tid]
        pnl=(ct['current']-ct['entry'])*ct['shares']
        pnl_pct=(ct['current']-ct['entry'])/ct['entry']*100
        trades_out.append({**ct,'pnl':round(pnl,2),'pnl_pct':round(pnl_pct,2),'id':tid})
    return _cors({
        'trades':trades_out,'completed':state['completed'][-30:],
        'capital':state['capital'],'pnl':state['pnl'],'starting':state['starting'],
        'log':state['log'][:20],'ai_alerts':state['ai_alerts'][:10]
    })

@app.route('/enter', methods=['POST'])
def enter_route():
    d=request.get_json() or {}
    sym=d.get('symbol','').upper(); mode=d.get('mode','day')
    # Find whale data
    whale=next((w for w in _whale_cache.get('combined',[]) if w['symbol']==sym),None)
    ok,msg=enter_trade(sym,mode,manual=True,whale_data=whale)
    return _cors({'ok':ok,'msg':msg})

@app.route('/close', methods=['POST'])
def close_route():
    d=request.get_json() or {}
    tid=d.get('id','')
    if tid not in state['trades']: return _cors({'ok':False,'msg':'Trade not found'})
    close_trade(tid, d.get('reason','manual'))
    return _cors({'ok':True})

@app.route('/chat', methods=['POST'])
def chat():
    d   = request.get_json() or {}
    msg = d.get('message','').strip()
    sym = (d.get('symbol','') or '').upper()
    if not msg: return _cors({'ok':False})

    # Known ticker descriptions (prevents "DRAM = memory technology" confusion)
    TICKER_NOTES = {
        'DRAM': 'DRAM is the Roundhill Memory & Storage Technology ETF (ticker: DRAM). It tracks memory chip stocks like Micron, SK Hynix, Samsung. It is NOT the DRAM memory technology.',
        'SOXL': 'SOXL is the Direxion 3x Semiconductor ETF.',
        'TQQQ': 'TQQQ is the ProShares 3x Nasdaq-100 ETF.',
        'MSTR': 'MSTR is MicroStrategy, primarily a Bitcoin proxy.',
        'FNGU': 'FNGU is the MicroSectors 3x FAANG+ ETF.',
        'UPRO': 'UPRO is the ProShares 3x S&P500 ETF.',
    }

    # Build scoped context — prioritize the ticker being discussed
    ctx_lines = [
        f"Date: {today_str()} | Market: {'OPEN' if is_market_open() else 'CLOSED'}",
        f"P&L — Day: ${state['pnl']['day']:+.0f} | Swing: ${state['pnl']['swing']:+.0f} | LT: ${state['pnl']['longterm']:+.0f}",
    ]

    # Active trades summary
    if state['trades']:
        ctx_lines.append("Open trades: " + ", ".join(
            f"{t['symbol']} [{t['mode']}] ${t.get('current',t['entry']):.2f} P&L ${(t.get('current',t['entry'])-t['entry'])*t['shares']:+.0f}"
            for t in list(state['trades'].values())[:5]
        ))
    else:
        ctx_lines.append("Open trades: none")

    # If discussing a specific ticker, inject its analysis with real prices
    if sym:
        ticker_note = TICKER_NOTES.get(sym,'')
        if ticker_note:
            ctx_lines.append(f"IMPORTANT — {sym}: {ticker_note}")

        cached = cp(sym)
        if cached:
            ctx_lines.append(f"{sym} live price: ${cached['price']:.2f} ({cached['pct']:+.2f}% today)")

        analysis_data = state.get('analysis_cache',{}).get(sym)
        if analysis_data and analysis_data.get('tech'):
            t = analysis_data['tech']
            ctx_lines.append(
                f"{sym} technicals: Signal={t.get('signal','?')} | RSI={t.get('rsi',50):.0f} | "
                f"VWAP=${t.get('vwap',0):.2f} ({'above' if t.get('above_vwap') else 'below'}) | "
                f"EMA9=${t.get('ema9',0):.2f} | Support=${t.get('support',0):.2f} | Resistance=${t.get('resistance',0):.2f}"
            )
            ctx_lines.append(
                f"{sym} price targets: 5D=${t.get('pred_5d',0):.2f} | 30D=${t.get('pred_30d',0):.2f} | 90D=${t.get('pred_90d',0):.2f}"
            )
        elif sym:
            ctx_lines.append(f"{sym}: No analysis cached yet. Prices from live feed only.")
    else:
        # No specific ticker — give whale summary
        whale_syms = [w['symbol'] for w in _whale_cache.get('combined',[])[:5]]
        if whale_syms:
            ctx_lines.append(f"Top whale signals today: {', '.join(whale_syms)}")

    sys_p = (
        "You are AutoTrade Pro AI — a professional trading discussion partner for Abiy Kassa. "
        "RULES:\n"
        "1. ONLY use exact prices from the context below — NEVER invent prices.\n"
        "2. If a price is not in context, say 'I don't have the current price for X'.\n"
        "3. DISCUSS, don't automatically suggest trades. Abiy will decide what to trade.\n"
        "4. Keep answers to 3-4 sentences maximum.\n"
        "5. If the user asks about a ticker, stay focused on THAT ticker only.\n"
        "6. Never recommend entering a trade unless explicitly asked.\n\n"
        "Current data:\n" + "\n".join(ctx_lines)
    )

    resp, src = ai_call(sys_p, msg, max_tokens=300)
    return _cors({'ok':True,'response':resp or 'AI unavailable — check GROQ_API_KEY','src':src})

@app.route('/daily-report')
def daily_report():
    if not state.get('daily_report'):
        threading.Thread(target=generate_daily_report,daemon=True).start()
        return _cors({'report':None,'generating':True})
    return _cors({'report':state['daily_report']})

@app.route('/test-telegram')
def test_tg():
    ok=tg(f"✅ AutoTrade Pro v3 ALIVE\nAI: {'Groq✅' if GROQ_KEY else '❌'} | Alpaca: {'✅' if alpaca_trading else '❌'}\nOpen trades: {len(state['trades'])}")
    return _cors({'ok':ok})

@app.route('/news')
def news():
    sym=request.args.get('symbol','NVDA')
    items=[]
    try:
        url=f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US'
        data=http_get(url,timeout=7).decode('utf-8','replace')
        tree=ET.fromstring(data)
        for it in list(tree.iter('item'))[:8]:
            t=it.find('title')
            if t is not None and t.text:
                items.append({'title':t.text.strip(),'pub':''})
    except: pass
    return _cors({'items':items})

@app.route('/performance')
def performance():
    c=state['completed']
    if not c: return _cors({'trades':0,'win_rate':0,'best':0,'worst':0})
    wins=[t for t in c if t.get('pnl',0)>=0]
    return _cors({'trades':len(c),'wins':len(wins),'losses':len(c)-len(wins),
                  'win_rate':round(len(wins)/len(c)*100,1),
                  'total_pnl':round(sum(t.get('pnl',0) for t in c),2),
                  'best':max(t.get('pnl',0) for t in c),
                  'worst':min(t.get('pnl',0) for t in c),
                  'by_mode':{m:round(state['pnl'][m],2) for m in ('day','swing','longterm')}})

# ── BOOT ──────────────────────────────────────────────────────────────
load_state()
threading.Thread(target=price_thread,daemon=True).start()
threading.Thread(target=refresh_whale_data,daemon=True).start()

import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,'cron',day_of_week='mon-fri',hour=8,minute=0)
    sched.add_job(job_minute,'cron',day_of_week='mon-fri',hour='8-16',minute='*')
    sched.add_job(job_whale,'cron',day_of_week='mon-fri',hour='8-16',minute='*/20')
    sched.add_job(job_eod,'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,'interval',minutes=8)
    sched.start(); atexit.register(lambda:sched.shutdown(wait=False))
    print(f"Scheduler: {len(sched.get_jobs())} jobs ✅")
except Exception as e: print(f"Scheduler: {e}")

if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT,debug=False)
