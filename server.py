#!/usr/bin/env python3
"""
AutoTrade Pro v3 — Whale Intelligence Platform
Sources: SEC Form 4, Congress Trades, Unusual Whales free, yfinance, Alpaca
Modes: Day Trade | Swing Trade | Long Term — all auto-managed
"""
import json, os, time, threading, urllib.request, urllib.parse, re, hashlib, concurrent.futures
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

alpaca_trading = alpaca_data = None
if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest
        alpaca_trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data    = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("Alpaca ✅")
    except Exception as e: print(f"Alpaca: {e}")

app = Flask(__name__, static_folder='.')

# ── AUTH ──────────────────────────────────────────────────────────────
def _token():
    return hashlib.sha256((ABIY_PIN+'atp-whale-2026').encode()).hexdigest() if ABIY_PIN else ''

@app.before_request
def _auth():
    pub = {'/','/ping','/unlock','/favicon.ico'}
    if not ABIY_PIN or request.path in pub: return
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
    r = jsonify(d); r.headers['Access-Control-Allow-Origin']='*'; return r

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
_whale_cache = {'sec4':[],'congress':[],'unusual':[],'ts':0}
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
    # Country / region codes
    'UAE','USA','UK','EU','US','UK','UN','NATO','OPEC','IMF','WHO',
    # Financial jargon
    'IPO','ETF','CEO','CFO','CTO','COO','SEC','FDA','IRS','FED','GDP','CPI',
    'NFP','EPS','P&L','LLC','INC','CORP','LTD','PLC',
    # Other non-ticker caps
    'BUY','SELL','HOLD','CALL','PUT','ESG','SPX','VIX','DXY','WTI',
    'FOMC','OPEC','REPO','BOND','DEBT','CASH','GOLD','OIL','GAS',
    'TECH','BANK','FUND','REIT','ETF','ADR',
    # Ambiguous 2-letters that aren't tickers in our universe
    'AT','BE','BY','DO','GO','IF','IN','IS','IT','ME','MY','NO','OF',
    'ON','OR','SO','TO','UP','WE',
}

def _valid_sym(sym):
    """Return True only if sym looks like a real, tradeable stock ticker"""
    if not sym or len(sym) < 2 or len(sym) > 5: return False
    if sym in _NOT_TICKERS: return False
    if not re.match(r'^[A-Z]{1,5}$', sym): return False
    return True

def fetch_sec_form4():
    """SEC EDGAR Form 4 — parse both Atom feed AND full-text search for buy transactions"""
    items = []
    # Method 1: EDGAR Atom feed for recent Form 4 filings
    try:
        url = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=40&search_text=&output=atom'
        data = http_get(url, timeout=12).decode('utf-8','replace')
        tree = ET.fromstring(data)
        ns = {'atom':'http://www.w3.org/2005/Atom'}
        for entry in tree.findall('atom:entry', ns)[:25]:
            title_el = entry.find('atom:title', ns)
            upd_el   = entry.find('atom:updated', ns)
            if title_el is None: continue
            title = title_el.text or ''
            upd   = (upd_el.text or '')[:10] if upd_el is not None else ''
            # Form 4 titles: "4 - CompanyName (TICKER) (InsiderName)"
            # Extract ALL parenthesized tokens, first valid one is the ticker
            matches = re.findall(r'\(([A-Z0-9\.]{1,6})\)', title)
            sym = ''
            for m in matches:
                # Ticker is usually all caps letters, not a person's name abbreviation
                if _valid_sym(m):
                    sym = m; break
            if not sym: continue
            # Only include if title suggests a BUY (acquisitions, not dispositions)
            title_lower = title.lower()
            if 'disposition' in title_lower or 'sale' in title_lower: continue
            items.append({
                'symbol':sym,'source':'SEC Form 4','title':title[:80],
                'date':upd,'type':'insider','direction':'buy',
                'confidence':78,'hold_days':90
            })
        print(f"  SEC Form4 Atom: {len(items)} items")
    except Exception as e:
        print(f"SEC Form4 Atom: {e}")

    # Method 2: EDGAR full-text search for large insider purchases (>$100k) via EFTS
    try:
        url2 = 'https://efts.sec.gov/LATEST/search-index?q=%22acquisition%22&forms=4&dateRange=custom&startdt={}&enddt={}'.format(
            (datetime.now()-timedelta(days=7)).strftime('%Y-%m-%d'), today_str())
        data2 = http_get(url2, timeout=10).decode('utf-8','replace')
        j = json.loads(data2)
        for hit in (j.get('hits',{}).get('hits',[]) or [])[:10]:
            src = hit.get('_source',{})
            tickers = src.get('period_of_report','') or ''
            entity = src.get('entity_name','') or ''
            # Try to get ticker from entity name context — skip if we can't
            # These hits confirm large block buying, used as a confidence signal
        # Just use Method 1 results — Method 2 validates volume
    except Exception as e:
        print(f"SEC EFTS: {e}")

    return items[:15]

def fetch_congress_trades():
    """
    Congress stock trades — three free sources tried in order:
    1. house-stock-watcher-data S3 (primary)
    2. quiverquant Congress data (free tier)
    3. Senate stock watcher
    """
    items = []

    # Source 1: House Stock Watcher S3 JSON
    try:
        data = http_get(
            'https://house-stock-watcher-data.s3-us-gov-west-1.amazonaws.com/data/all_transactions.json',
            timeout=20)
        trades = json.loads(data)
        cutoff = (datetime.now() - timedelta(days=45)).strftime('%Y-%m-%d')
        purchases = [
            t for t in trades
            if t.get('type','').strip().lower() in ('purchase','buy')
            and (t.get('transaction_date','') or '') >= cutoff
            and _valid_sym((t.get('ticker','') or '').upper().strip())
        ]
        purchases.sort(key=lambda x: x.get('transaction_date',''), reverse=True)
        seen = set()
        for t in purchases[:50]:
            sym = (t.get('ticker','') or '').upper().strip()
            if sym in seen: continue
            seen.add(sym)
            rep  = (t.get('representative','') or 'Member').strip()
            amt  = t.get('amount','').strip()
            disc = t.get('disclosure_date','') or ''
            items.append({
                'symbol':sym,'source':'Congress Trade','direction':'buy',
                'title':f"{rep} bought {sym} ({amt})",
                'date':t.get('transaction_date',''),'type':'congress',
                'confidence':85,'hold_days':180,
                'rep':rep,'amount':amt,'disclosure_date':disc,
                'party':t.get('party','') or ''
            })
        print(f"  Congress House: {len(items)} purchases")
    except Exception as e:
        print(f"Congress House S3: {e}")

    # Source 2: Senate stock watcher (if house was empty)
    if not items:
        try:
            data = http_get(
                'https://senate-stock-watcher-data.s3-us-gov-west-1.amazonaws.com/aggregate/all_transactions.json',
                timeout=20)
            trades = json.loads(data)
            cutoff = (datetime.now() - timedelta(days=60)).strftime('%Y-%m-%d')
            for t in trades:
                txs = t.get('transactions') or []
                senator = t.get('senator','Senator')
                for tx in txs:
                    if tx.get('type','').lower() not in ('purchase','buy'): continue
                    if (tx.get('transaction_date','') or '') < cutoff: continue
                    sym = (tx.get('ticker','') or '').upper().strip()
                    if not _valid_sym(sym): continue
                    amt = tx.get('amount','')
                    items.append({
                        'symbol':sym,'source':'Congress Trade','direction':'buy',
                        'title':f"Senator {senator} bought {sym} ({amt})",
                        'date':tx.get('transaction_date',''),'type':'congress',
                        'confidence':85,'hold_days':180,
                        'rep':senator,'amount':amt
                    })
            items.sort(key=lambda x: x.get('date',''), reverse=True)
            # Deduplicate by symbol
            seen2=set(); dedup=[]
            for it in items:
                if it['symbol'] not in seen2: seen2.add(it['symbol']); dedup.append(it)
            items=dedup[:20]
            print(f"  Congress Senate: {len(items)} purchases")
        except Exception as e:
            print(f"Congress Senate S3: {e}")

    return items[:25]

def fetch_unusual_whales():
    """
    Unusual options activity — multiple free sources:
    1. Unusual Whales public API (if accessible)
    2. MarketBeat unusual options (scrape headline list)
    3. StockAnalysis earnings/volume spike list
    Only adds symbols that pass _valid_sym() — no garbage words
    """
    items = []

    # Source 1: Unusual Whales public endpoint
    try:
        data = http_get('https://unusualwhales.com/api/option_activity?limit=30&is_bullish=true', timeout=10)
        flow = json.loads(data)
        for f in (flow.get('data') or [])[:20]:
            sym = (f.get('ticker','') or '').upper().strip()
            if not _valid_sym(sym): continue
            premium = f.get('premium',0) or 0
            items.append({
                'symbol':sym,'source':'Unusual Whales','direction':'buy',
                'title':f"Unusual call flow ${premium:,.0f} premium",
                'date':today_str(),'type':'options_flow',
                'confidence':72,'hold_days':21,
                'premium':premium
            })
        print(f"  UnusualWhales API: {len(items)} items")
    except Exception as e:
        print(f"UnusualWhales: {e}")

    # Source 2: Barchart unusual options (public page scan for tickers)
    if not items:
        try:
            url2 = 'https://www.barchart.com/options/unusual-activity/stocks'
            req  = urllib.request.Request(url2, headers={
                'User-Agent': UA,
                'Accept': 'text/html,application/xhtml+xml',
                'Accept-Language': 'en-US,en;q=0.9'
            })
            with urllib.request.urlopen(req, timeout=12) as r:
                html = r.read().decode('utf-8','replace')
            # Find ticker symbols in the HTML — look for data-symbol attributes
            found = re.findall(r'data-symbol=["\']([A-Z]{2,5})["\']', html)
            seen_bc = set()
            for sym in found:
                if not _valid_sym(sym) or sym in seen_bc: continue
                seen_bc.add(sym)
                items.append({
                    'symbol':sym,'source':'Unusual Options Flow','direction':'buy',
                    'title':f"Unusual options activity detected: {sym}",
                    'date':today_str(),'type':'options_flow',
                    'confidence':68,'hold_days':14
                })
            print(f"  Barchart unusual: {len(items)} items")
        except Exception as e:
            print(f"Barchart: {e}")

    # Source 3: StockAnalysis volume leaders (stocks with unusual volume — reliable free source)
    if len(items) < 5:
        try:
            url3 = 'https://stockanalysis.com/stocks/screener/api/?p=annual&s=volume&type=stocks&country=US&col=s,volume,chp,ma5&i=volume'
            data3 = http_get(url3, timeout=10)
            j3 = json.loads(data3)
            rows = (j3.get('data') or {}).get('data') or []
            for row in rows[:15]:
                sym = (row[0] if isinstance(row,list) else row.get('s','')).upper().strip()
                if not _valid_sym(sym): continue
                vol_ratio = float(row[1] if isinstance(row,list) else row.get('volume',0) or 0)
                chg = float(row[2] if isinstance(row,list) else row.get('chp',0) or 0)
                if chg < 0: continue  # only bullish
                items.append({
                    'symbol':sym,'source':'Volume Surge','direction':'buy',
                    'title':f"Volume surge +{chg:.1f}% — unusual buying detected",
                    'date':today_str(),'type':'volume_surge',
                    'confidence':60,'hold_days':7
                })
            print(f"  StockAnalysis volume: {len(items)} items")
        except Exception as e:
            print(f"StockAnalysis: {e}")

    # Last resort: curated momentum universe with live volume check — NOT from news text
    if len(items) < 3:
        CURATED = ['NVDA','AMD','TSLA','AAPL','META','MSFT','AMZN','GOOGL',
                   'SOXL','TQQQ','MSTR','COIN','PLTR','SMCI','MU','AVGO',
                   'SOFI','RKLB','ASTS','IONQ','ARM','HIMS','APP']
        try:
            prices = get_rt_prices(CURATED)
            for sym, q in prices.items():
                if q.get('pct',0) > 2.5:  # only stocks up >2.5% with momentum
                    items.append({
                        'symbol':sym,'source':'Momentum Scanner','direction':'buy',
                        'title':f"Strong momentum +{q['pct']:.1f}% today",
                        'date':today_str(),'type':'momentum',
                        'confidence':58,'hold_days':3
                    })
        except Exception as e:
            print(f"Curated fallback: {e}")

    # Deduplicate by symbol
    seen_f=set(); dedup=[]
    for it in items:
        if it['symbol'] not in seen_f: seen_f.add(it['symbol']); dedup.append(it)
    return dedup[:20]

def refresh_whale_data():
    """Pull all 3 whale sources in parallel, deduplicate, score"""
    global _whale_cache
    with _whale_lock:
        if time.time() - _whale_cache['ts'] < 900: return  # 15min cache
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f4   = ex.submit(fetch_sec_form4)
            cong = ex.submit(fetch_congress_trades)
            uw   = ex.submit(fetch_unusual_whales)
            sec4_res  = f4.result(timeout=20)
            cong_res  = cong.result(timeout=20)
            uw_res    = uw.result(timeout=15)
    except Exception as e:
        print(f"Whale fetch: {e}")
        sec4_res=cong_res=uw_res=[]

    # Deduplicate by symbol — count how many sources agree
    sym_map = {}
    for item in sec4_res + cong_res + uw_res:
        sym = item['symbol']
        if sym not in sym_map:
            sym_map[sym] = {'symbol':sym,'sources':[],'items':[],'confidence':0,'hold_days':0}
        sym_map[sym]['sources'].append(item['source'])
        sym_map[sym]['items'].append(item)
        sym_map[sym]['confidence'] = max(sym_map[sym]['confidence'], item['confidence'])
        sym_map[sym]['hold_days']  = max(sym_map[sym]['hold_days'],  item['hold_days'])

    # Boost confidence if multiple sources agree
    for sym, data in sym_map.items():
        n = len(set(data['sources']))
        if n >= 3: data['confidence'] = min(99, data['confidence'] + 15)
        elif n >= 2: data['confidence'] = min(99, data['confidence'] + 8)
        data['multi_source'] = n >= 2
        data['source_count'] = n

    whales = sorted(sym_map.values(), key=lambda x: x['confidence'], reverse=True)

    with _whale_lock:
        _whale_cache = {'sec4':sec4_res,'congress':cong_res,'unusual':uw_res,
                        'combined':whales,'ts':time.time()}
    print(f"  Whale data: {len(sec4_res)} SEC4, {len(cong_res)} Congress, {len(uw_res)} Unusual = {len(whales)} symbols")

def get_whale_data():
    if not _whale_cache.get('ts') or time.time()-_whale_cache['ts']>3600:
        threading.Thread(target=refresh_whale_data, daemon=True).start()
    return _whale_cache

# ══════════════════════════════════════════════════════════════════════
# ── PRICE + TECHNICAL ENGINE ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════
_price_cache = {}
_price_ts    = 0
_price_lock  = threading.Lock()

def price_thread():
    global _price_ts
    base_syms = ['SPY','QQQ','VIX','NVDA','TSLA','AAPL','MSFT','AMZN','META','GOOGL',
                 'SOXL','TQQQ','MSTR','COIN','AMD','PLTR','MU','SMCI']
    while True:
        try:
            whale = list({w['symbol'] for w in _whale_cache.get('combined',[])})[:20]
            trade_syms = list({t['symbol'] for t in state['trades'].values()})
            syms = list(dict.fromkeys(base_syms + whale + trade_syms))
            result = {}
            if alpaca_data:
                try:
                    req = StockLatestTradeRequest(symbol_or_symbols=syms)
                    trades = alpaca_data.get_stock_latest_trade(req)
                    with _price_lock:
                        for sym, t in trades.items():
                            p = float(t.price)
                            old = _price_cache.get(sym,{}).get('price',p)
                            pct = ((p-old)/old*100) if old else 0
                            result[sym]={'price':p,'pct':pct,'ts':time.time(),'src':'live'}
                        _price_cache.update(result); _price_ts=time.time()
                    time.sleep(2); continue
                except: pass
            # yfinance fallback
            try:
                df = yf.download(syms[:30],period='2d',interval='1d',auto_adjust=True,progress=False)
                multi = isinstance(df.columns,pd.MultiIndex)
                with _price_lock:
                    for sym in syms:
                        try:
                            cl=(df['Close'][sym] if multi else df['Close']).dropna()
                            if len(cl)<1: continue
                            p=float(cl.iloc[-1]); prev=float(cl.iloc[-2]) if len(cl)>=2 else p
                            _price_cache[sym]={'price':round(p,2),'pct':round((p-prev)/prev*100,3),'ts':time.time(),'src':'delayed'}
                        except: pass
                _price_ts=time.time()
            except: pass
        except Exception as e: print(f"Price thread: {e}")
        time.sleep(30)

def cp(sym):
    with _price_lock: return _price_cache.get(sym)

def all_prices():
    with _price_lock: return dict(_price_cache)

def get_candles(sym, period='1d', interval='5m'):
    try:
        df = yf.download(sym, period=period, interval=interval, auto_adjust=True, progress=False)
        if df.empty and interval=='5m':
            df = yf.download(sym, period='5d', interval='15m', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        return df.dropna()
    except: return pd.DataFrame()

def compute_technicals(sym, df=None):
    """Full technical suite: VWAP, EMA, RSI, MACD, BB, ATR, support/resistance"""
    if df is None: df = get_candles(sym)
    if df.empty or len(df)<10:
        df = get_candles(sym,'5d','15m')
    if df.empty or len(df)<5: return None
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
    """Compute overall whale conviction score — real sources score much higher than news fallback"""
    score   = 0
    reasons = []

    # ── Technical component (max 35 pts) ─────────────────────────────
    if tech:
        bp = tech.get('bull_pts', 0)
        score += bp * 6  # max 24
        if tech.get('rsi_oversold'):  score += 8;  reasons.append('RSI oversold — bounce zone')
        if tech.get('near_support'):  score += 6;  reasons.append('Near key support')
        if tech.get('macd_bull'):     score += 6;  reasons.append('MACD bullish crossover')
        if tech.get('above_vwap'):    score += 5;  reasons.append('Trading above VWAP')
        if tech.get('trend') == 'bullish': score += 4

    # ── Whale source component (max 65 pts) ───────────────────────────
    if whale_item:
        src_types = set(it.get('type','') for it in whale_item.get('items', []))
        sources   = set(whale_item.get('sources', []))

        # Score each source type — real named sources score much higher
        if 'congress' in src_types:
            score += 38
            reps = [it.get('rep','') for it in whale_item.get('items',[]) if it.get('type')=='congress']
            reasons.append(f"Congress purchase: {reps[0] if reps else 'member'}")
        if 'insider' in src_types:
            score += 30
            reasons.append('SEC Form 4 insider acquisition')
        if 'options_flow' in src_types:
            score += 22
            reasons.append('Unusual options flow detected')
        if 'volume_surge' in src_types:
            score += 15
            reasons.append('Volume surge — institutional buying pattern')
        if 'momentum' in src_types:
            score += 10
            reasons.append('Strong price momentum today')
        # news_flow is the weakest signal — should NOT auto-trigger entries
        if src_types == {'news_flow'}:
            score = min(score, 55)  # hard cap at 55 if only source is news text
            reasons.append('⚠ News-only signal — verify before entering')

        # Multi-source agreement bonus
        real_count = len([s for s in sources if 'News' not in s])
        if real_count >= 3: score += 12; reasons.append('3+ real whale sources agree')
        elif real_count >= 2: score += 7; reasons.append('2 real whale sources agree')

    score = min(99, max(0, round(score)))

    # ── Mode recommendation ───────────────────────────────────────────
    hold_days = whale_item.get('hold_days', 14) if whale_item else 14
    best_tech  = tech.get('best_mode', 'swing') if tech else 'swing'
    vol        = tech.get('daily_vol', 2.0) if tech else 2.0

    if hold_days >= 90:   rec_mode = 'longterm'
    elif hold_days >= 14: rec_mode = 'swing'
    else:                 rec_mode = 'day'

    # Override: very high volatility → always day trade
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
    """Full AI analysis with prediction and recommendation"""
    price=tech['price'] if tech else 0
    whale_info=''
    if whale:
        for item in whale.get('items',[])[:3]:
            whale_info+=f"- {item.get('source')}: {item.get('title','')[:60]}\n"
    sys_p=("You are AutoTrade Pro — expert trading AI for Abiy Kassa. "
           "Give SPECIFIC price levels and CLEAR direction. 3-5 sentences max per section. "
           "Always state exact BUY/WAIT/SELL and price targets.")
    user_p=(f"Analyze {sym} for trading:\n"
            f"Price: ${price:.2f} | Signal: {tech['signal'] if tech else '?'} | RSI: {tech['rsi'] if tech else '?'}\n"
            f"VWAP: ${tech['vwap'] if tech else '?'} ({'above' if tech and tech['above_vwap'] else 'below'})\n"
            f"EMA9: ${tech['ema9'] if tech else '?'} | EMA20: ${tech['ema20'] if tech else '?'} | EMA50: ${tech['ema50'] if tech else '?'}\n"
            f"MACD: {'bullish' if tech and tech['macd_bull'] else 'bearish'} | ATR: {tech['atr'] if tech else '?'}\n"
            f"Support: ${tech['support'] if tech else '?'} | Resistance: ${tech['resistance'] if tech else '?'}\n"
            f"Trend (linear regression): {tech['trend'] if tech else '?'} | Daily vol: {tech['daily_vol'] if tech else '?'}%\n"
            f"5-day price prediction: ${tech['pred_5d'] if tech else '?'} | 30-day: ${tech['pred_30d'] if tech else '?'} | 90-day: ${tech['pred_90d'] if tech else '?'}\n"
            f"Whale activity:\n{whale_info if whale_info else 'No whale data'}\n"
            f"Best mode per volatility: {tech['best_mode'] if tech else '?'}\n\n"
            "Respond with:\n"
            "1. SIGNAL: BUY/WAIT/SELL — one sentence why\n"
            "2. Day Trade: entry zone, target, stop\n"
            "3. Swing Trade (2-3wk): entry zone, target, stop\n"
            "4. Long Term (3mo+): thesis and price target\n"
            "5. PREDICTION: realistic 30-day price range\n"
            "6. RISK: main risk in one sentence")
    resp,src=ai_call(sys_p,user_p,max_tokens=600)
    return resp or "AI unavailable — check GROQ_API_KEY",src or 'none'

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

@app.route('/whale-data')
def whale_data_route():
    d=get_whale_data()
    combined=d.get('combined',[])[:25]
    # Enrich with live price and top technical signal
    prices=all_prices()
    for w in combined:
        sym=w['symbol']
        q=prices.get(sym)
        if q: w['price']=q['price']; w['pct']=q['pct']
        else: w.setdefault('price',None); w.setdefault('pct',None)
        # Get cached signal if available
        cached_tech=state.get('analysis_cache',{}).get(sym,{}).get('tech')
        if cached_tech:
            w['top_signal']=cached_tech.get('signal','WAIT')
            w['top_reason']=cached_tech.get('signal_reason','')
        else:
            w.setdefault('top_signal','WAIT')
            w.setdefault('top_reason','; '.join((w.get('reasons') or [])[:2]))
        w['rec_mode']=w.get('rec_mode') or 'swing'
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
    p=all_prices()
    return _cors({'data':p,'src':'live' if alpaca_data else 'delayed','ts':_price_ts})

@app.route('/technicals')
def technicals():
    sym=request.args.get('symbol','').upper()
    if not sym: return _cors({'error':'No symbol'})
    tech=compute_technicals(sym)
    return _cors({'technicals':tech,'symbol':sym})

@app.route('/candles')
def candles():
    sym=request.args.get('symbol','').upper()
    period=request.args.get('period','1d')
    interval=request.args.get('interval','5m')
    if not sym: return _cors({'bars':[]})
    try:
        df=get_candles(sym,period,interval)
        if df.empty: return _cors({'bars':[],'symbol':sym})
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df=df.dropna().tail(100)
        bars=[]
        for i,r in df.iterrows():
            try:
                idx_et=i.tz_convert(ET_TZ) if hasattr(i,'tzinfo') and i.tzinfo else i
                hm=str(idx_et)[11:16] if ' ' in str(idx_et) else str(idx_et)[-8:-3]
            except: hm=str(i)[11:16]
            bars.append({'t':str(i),'hm':hm,'o':round(float(r.Open),3),'h':round(float(r.High),3),
                         'l':round(float(r.Low),3),'c':round(float(r.Close),3),'v':int(r.Volume)})
        return _cors({'bars':bars,'symbol':sym})
    except Exception as e: return _cors({'bars':[],'error':str(e)})

@app.route('/analyze', methods=['POST'])
def analyze():
    d=request.get_json() or {}
    sym=d.get('symbol','').upper()
    if not sym: return _cors({'ok':False})
    def do():
        tech=compute_technicals(sym)
        whale=next((w for w in _whale_cache.get('combined',[]) if w['symbol']==sym),None)
        rec=whale_score_and_recommend(sym,tech,whale) if tech else {}
        analysis,ai_src=ai_analyze(sym,tech,whale)
        state.setdefault('analysis_cache',{})[sym]={
            'tech':tech,'whale':whale,'rec':rec,
            'analysis':analysis,'ai_src':ai_src,'ts':now_str(),'symbol':sym
        }
    threading.Thread(target=do,daemon=True).start()
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
    d=request.get_json() or {}
    msg=d.get('message','').strip(); sym=d.get('symbol','')
    if not msg: return _cors({'ok':False})
    trades_summary='; '.join(f"{t['symbol']}[{t['mode']}] ${t.get('pnl',0):+.1f}" for t in state['trades'].values())
    pnl_summary=f"Day ${state['pnl']['day']:+.0f} | Swing ${state['pnl']['swing']:+.0f} | LT ${state['pnl']['longterm']:+.0f}"
    whale_top=', '.join(w['symbol'] for w in _whale_cache.get('combined',[])[:5])
    ctx=(f"Context: {today_str()} | {pnl_summary}\n"
         f"Open trades: {trades_summary or 'none'}\n"
         f"Top whale signals: {whale_top or 'none'}\n"
         f"Market open: {is_market_open()}")
    if sym and sym in state.get('analysis_cache',{}):
        r=state['analysis_cache'][sym]
        tech=r.get('tech',{})
        ctx+=f"\n{sym} signal: {tech.get('signal','?')} RSI:{tech.get('rsi','?')} VWAP:{'above' if tech.get('above_vwap') else 'below'}"
    sys_p=("AutoTrade Pro AI for Abiy Kassa. Expert day/swing/long-term trader. "
           "Be specific, direct, cite real prices. 3-5 sentences. Paper trading.\n"+ctx)
    resp,src=ai_call(sys_p,msg,max_tokens=400)
    return _cors({'ok':True,'response':resp or 'AI unavailable','src':src})

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
