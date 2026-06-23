#!/usr/bin/env python3
"""AutoTrade Pro — 3-Tab Rebuild: Day Trade Picks | Options | Custom Research"""
import json, os, time, threading, urllib.request, concurrent.futures
import xml.etree.ElementTree as ET
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

try:
    import yfinance as yf, pandas as pd, pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"Missing: {e}"); raise SystemExit(1)

# ── ENV ───────────────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY','')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY','')
GROQ_KEY      = os.environ.get('GROQ_API_KEY','')
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY','')
TELEGRAM_TOKEN= os.environ.get('TELEGRAM_BOT_TOKEN','')
TELEGRAM_CHAT = os.environ.get('TELEGRAM_CHAT_ID','')
APP_URL       = os.environ.get('APP_URL','https://your-app.onrender.com')
PORT          = int(os.environ.get('PORT', 8765))
ET_TZ         = pytz.timezone('America/New_York')
UA            = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'

_detected_app_url = None
def get_app_url():
    if APP_URL and 'your-app' not in APP_URL: return APP_URL
    return _detected_app_url or APP_URL

# ── ALPACA ────────────────────────────────────────────────────────────
alpaca_trading = alpaca_data = None
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

app = Flask(__name__, static_folder='.')

# ── PIN AUTH ──────────────────────────────────────────────────────────
import hashlib
ABIY_PIN = os.environ.get('ABIY_PIN','').strip()
def _expected_token():
    return hashlib.sha256((ABIY_PIN+'autotrade-pro-salt-2026').encode()).hexdigest()
_PUBLIC_PATHS = {'/','/ping','/unlock'}

@app.before_request
def _detect_url():
    global _detected_app_url
    if _detected_app_url is None and request.host and 'localhost' not in request.host:
        _detected_app_url = f"https://{request.host}"

@app.before_request
def _check_auth():
    if not ABIY_PIN or request.path in _PUBLIC_PATHS: return None
    if request.headers.get('X-Auth','') != _expected_token():
        return jsonify({'ok':False,'locked':True,'msg':'Enter your code to unlock'}), 401

@app.route('/unlock', methods=['POST'])
def unlock():
    if not ABIY_PIN: return cors({'ok':True,'token':'','gated':False})
    data = request.get_json() or {}
    if str(data.get('pin','')) == ABIY_PIN:
        return cors({'ok':True,'token':_expected_token(),'gated':True})
    return cors({'ok':False,'msg':'Incorrect code'})

# ── UNIVERSE ──────────────────────────────────────────────────────────
UNIVERSE = [
    # 3x ETFs — best for 10%+ daily
    'SOXL','TQQQ','UPRO','TECL','LABU','SPXL','TNA','FNGU',
    # Crypto / high beta
    'MSTR','COIN','MARA','RIOT','HUT','CLSK',
    # AI / Semi
    'NVDA','AMD','SMCI','PLTR','IONQ','RGTI','SOUN',
    # Momentum
    'SOFI','UPST','APP','HIMS','RKLB','ASTS','SMR',
    # More semis + growth
    'MU','AVGO','ARM','DRAM','NVDL',
    # Biotech volatile
    'MRNA','CRSP',
    # EV
    'TSLA','RIVN',
    # Meme
    'GME',
]

# ── PRICE CACHE ───────────────────────────────────────────────────────
_prices    = {}
_prev      = {}
_price_lock= threading.Lock()
_cache_ts  = 0

def get_rt_prices(syms):
    out = {}
    if alpaca_data and syms:
        try:
            req    = StockLatestTradeRequest(symbol_or_symbols=list(syms))
            trades = alpaca_data.get_stock_latest_trade(req)
            for sym, t in trades.items():
                p = float(t.price); prev = _prev.get(sym, p)
                pct = ((p-prev)/prev*100) if prev else 0
                out[sym] = {'symbol':sym,'price':round(p,2),'pct':round(pct,3),
                            'source':'alpaca','time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            return out
        except Exception as e:
            print(f"  Alpaca rt: {e}")
    try:
        df = yf.download(list(syms), period='2d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if not len(cl): continue
                p=float(cl.iloc[-1]); prev=float(cl.iloc[-2]) if len(cl)>=2 else p
                pct=((p-prev)/prev*100) if prev else 0
                out[sym]={'symbol':sym,'price':round(p,2),'pct':round(pct,3),
                          'source':'yfinance','time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            except: pass
    except: pass
    return out

def price_thread():
    global _cache_ts
    while True:
        try:
            syms = list(set(UNIVERSE+['SPY','QQQ','BTC-USD']) |
                        {t['symbol'] for t in state['active_trades'].values()} |
                        set(state['day_picks']) | set(state['custom_watchlist']))
            syms = [s for s in syms if s != 'BTC-USD']
            result = get_rt_prices(syms)
            with _price_lock:
                _prices.update(result); _cache_ts = time.time()
        except Exception as e:
            print(f"  Price thread: {e}")
        time.sleep(3 if alpaca_data else 30)

def cp(sym):
    with _price_lock: return _prices.get(sym)
def all_prices():
    with _price_lock: return dict(_prices)

def load_prev_closes():
    syms = UNIVERSE[:20]
    try:
        df = yf.download(syms, period='5d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl)>=2: _prev[sym]=float(cl.iloc[-2])
            except: pass
    except: pass

# ── TELEGRAM ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body = json.dumps({'chat_id':str(TELEGRAM_CHAT).strip(),'text':str(msg),
                           'disable_web_page_preview':True}).encode()
        req  = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get('ok',False)
    except Exception as e:
        print(f"  Telegram: {e}"); return False

# ── HELPERS ───────────────────────────────────────────────────────────
def is_market_open():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    return n.weekday()<5 and 570<=t<960
def is_trading_day(): return datetime.now(ET_TZ).weekday()<5
def now_et(): return datetime.now(ET_TZ).strftime('%H:%M ET')
def today_str(): return datetime.now(ET_TZ).strftime('%Y-%m-%d')

# ── SCORING ───────────────────────────────────────────────────────────
def score_stock(q, spy_pct=0, qqq_pct=0):
    p   = q.get('regularMarketPrice',0)
    chg = q.get('regularMarketChangePercent',0)
    vr  = q.get('_vr',0)
    pre = q.get('preMarketChangePercent',0)
    hi52= q.get('fiftyTwoWeekHigh',p) or p
    lo52= q.get('fiftyTwoWeekLow',p*0.3) or p*0.3
    if p < 1: return 0
    score = 0
    mkt = (spy_pct+qqq_pct)/2
    annual = ((hi52-lo52)/lo52*100) if lo52>0 else 0
    if   annual>400: score+=25
    elif annual>250: score+=21
    elif annual>150: score+=16
    elif annual>100: score+=11
    elif annual>60:  score+=6
    elif annual<30:  score-=15
    if   chg>12: score+=25
    elif chg>8:  score+=21
    elif chg>5:  score+=16
    elif chg>3:  score+=11
    elif chg>1:  score+=6
    elif chg>0:  score+=2
    elif chg<-5: score-=20
    elif chg<-2: score-=10
    elif chg<0:  score-=3
    if   vr>8:  score+=20
    elif vr>5:  score+=17
    elif vr>3:  score+=13
    elif vr>2:  score+=8
    elif vr>1.5:score+=4
    elif vr>1:  score+=1
    else:        score-=12
    if   pre>8:  score+=15
    elif pre>5:  score+=13
    elif pre>3:  score+=10
    elif pre>1:  score+=6
    elif pre>0:  score+=2
    elif pre<-2: score-=8
    if   mkt>1.5: score+=10
    elif mkt>0.5: score+=7
    elif mkt>0:   score+=3
    elif mkt<-1:  score-=15
    elif mkt<-0.3:score-=5
    return min(99, max(0, round(score)))

def get_full_quotes(symbols):
    results = []
    try:
        batch = list(dict.fromkeys([s for s in symbols if s]))
        df5   = yf.download(batch, period='5d', interval='1d', auto_adjust=True, progress=False)
        df1y  = yf.download(batch, period='1y', interval='1d', auto_adjust=True, progress=False)
        def fi(s):
            try: return s, yf.Ticker(s).info or {}
            except: return s, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
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
                results.append({'symbol':sym,'shortName':info.get('shortName',sym),
                    'regularMarketPrice':round(price,2),
                    'regularMarketChangePercent':round(chg,4),
                    'marketCap':int(info.get('marketCap') or 0),
                    'fiftyTwoWeekHigh':hi52,'fiftyTwoWeekLow':lo52,
                    'preMarketChangePercent':round(pre_chg,2),
                    'preMarketPrice':round(pre_p,2),'_vr':vr})
            except: pass
    except Exception as e:
        print(f"  Quotes: {e}")
    return results

def fetch_intraday_bars(symbol):
    try:
        df = yf.download(symbol, period='1d', interval='5m', auto_adjust=True, progress=False)
        if df.empty:
            df = yf.download(symbol, period='5d', interval='15m', auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df.dropna()
    except: return pd.DataFrame()

def compute_vwap_ema(symbol):
    df = fetch_intraday_bars(symbol)
    if df.empty or len(df)<6: return None
    typical=(df['High']+df['Low']+df['Close'])/3
    cum_vol=df['Volume'].cumsum().replace(0,1)
    vwap=(typical*df['Volume']).cumsum()/cum_vol
    ema9=df['Close'].ewm(span=9,adjust=False).mean()
    ema20=df['Close'].ewm(span=20,adjust=False).mean()
    ema50=df['Close'].ewm(span=50,adjust=False).mean()
    rsi_delta=df['Close'].diff()
    gain=rsi_delta.clip(lower=0).rolling(14).mean()
    loss=(-rsi_delta.clip(upper=0)).rolling(14).mean()
    rs=gain/loss.replace(0,1e-9)
    rsi=100-100/(1+rs)
    last=float(df['Close'].iloc[-1])
    lv=float(vwap.iloc[-1]); le9=float(ema9.iloc[-1]); le20=float(ema20.iloc[-1])
    le50=float(ema50.iloc[-1]); lr=float(rsi.iloc[-1])
    rising=last>float(df['Close'].iloc[-3]) if len(df)>=3 else False
    above_vwap=last>lv; bull_ema=le9>le20
    if above_vwap and bull_ema and rising:
        signal,color='BUY','green'
        reason=f"Above VWAP ${lv:.2f}, 9EMA>20EMA, price rising"
    elif not above_vwap and not bull_ema:
        signal,color='SELL','red'
        reason=f"Below VWAP ${lv:.2f}, 9EMA<20EMA — bearish"
    else:
        signal,color='WAIT','amber'
        reason=f"{'Above' if above_vwap else 'Below'} VWAP, mixed EMAs — wait for clarity"
    return {'signal':signal,'color':color,'reason':reason,'price':round(last,2),
            'vwap':round(lv,2),'ema9':round(le9,2),'ema20':round(le20,2),
            'ema50':round(le50,2),'rsi':round(lr,1),
            'above_vwap':above_vwap,'bull_ema':bull_ema,
            'vwap_series':[round(float(v),2) for v in vwap.tolist()],
            'ema9_series':[round(float(v),2) for v in ema9.tolist()],
            'ema20_series':[round(float(v),2) for v in ema20.tolist()]}

def compute_atr_levels(symbol, entry_price):
    try:
        df=fetch_intraday_bars(symbol)
        if df.empty or len(df)<10: raise ValueError
        high=df['High'].values; low=df['Low'].values; close=df['Close'].values
        tr=[max(high[i]-low[i],abs(high[i]-close[i-1]),abs(low[i]-close[i-1])) for i in range(1,len(close))]
        atr=sum(tr[-14:])/min(14,len(tr))
        support=float(min(low[-20:])); resistance=float(max(high[-20:]))
        stop=round(max(entry_price-1.5*atr, support*0.995),2)
        target=round(max(entry_price+2.0*atr, resistance),2)
        stop=max(stop,round(entry_price*0.92,2))
        target=min(target,round(entry_price*1.20,2))
        return {'stop':stop,'target':target,'atr':round(atr,3),
                'stop_pct':round((entry_price-stop)/entry_price*100,1),
                'target_pct':round((target-entry_price)/entry_price*100,1),
                'support':round(support,2),'resistance':round(resistance,2)}
    except:
        return {'stop':round(entry_price*0.94,2),'target':round(entry_price*1.10,2),
                'atr':None,'stop_pct':6.0,'target_pct':10.0,
                'support':round(entry_price*0.94,2),'resistance':round(entry_price*1.10,2)}

# ── AI BRAIN (Groq first, Anthropic fallback) ─────────────────────────
def ai_call(system_prompt, user_msg, max_tokens=400):
    """Try Groq first (free), fall back to Anthropic if available."""
    # ── Groq (free) ──────────────────────────────────────────────────
    if GROQ_KEY:
        try:
            payload = json.dumps({
                "model": "llama-3.3-70b-versatile",
                "max_tokens": max_tokens,
                "messages": [
                    {"role":"system","content":system_prompt},
                    {"role":"user","content":user_msg}
                ]
            }).encode()
            req = urllib.request.Request(
                "https://api.groq.com/openai/v1/chat/completions",
                data=payload,
                headers={"Authorization":f"Bearer {GROQ_KEY}",
                         "Content-Type":"application/json","User-Agent":UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())['choices'][0]['message']['content'], 'groq'
        except Exception as e:
            print(f"  Groq: {e}")
    # ── Anthropic fallback ────────────────────────────────────────────
    if ANTHROPIC_KEY:
        try:
            payload = json.dumps({
                "model":"claude-sonnet-4-6",
                "max_tokens":max_tokens,
                "system":system_prompt,
                "messages":[{"role":"user","content":user_msg}]
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=payload,
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json","User-Agent":UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())['content'][0]['text'], 'anthropic'
        except Exception as e:
            print(f"  Anthropic: {e}")
    return None, None

# ── NEWS ──────────────────────────────────────────────────────────────
_news_cache={'items':[],'ts':0}
def get_news(symbols=None):
    if time.time()-_news_cache['ts']<90 and _news_cache['items']:
        return _news_cache['items']
    items=[]
    syms_str=','.join((symbols or ['QQQ','SPY','NVDA','TSLA','SOXL'])[:6])
    try:
        url=f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={syms_str}&region=US&lang=en-US'
        req=urllib.request.Request(url,headers={'User-Agent':UA})
        with urllib.request.urlopen(req,timeout=6) as r:
            tree=ET.fromstring(r.read())
            for item in tree.iter('item'):
                t=item.find('title')
                if t is not None and t.text and len(t.text.strip())>15:
                    pub=item.find('pubDate')
                    items.append({'title':t.text.strip(),'pub':pub.text if pub is not None else ''})
    except: pass
    if not items: items=[{'title':'Market data loading','pub':''}]
    _news_cache.update({'items':items[:15],'ts':time.time()})
    return _news_cache['items']

def get_symbol_news(symbol):
    items=[]
    try:
        url=f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US'
        req=urllib.request.Request(url,headers={'User-Agent':UA})
        with urllib.request.urlopen(req,timeout=6) as r:
            tree=ET.fromstring(r.read())
            for item in list(tree.iter('item'))[:5]:
                t=item.find('title')
                if t is not None and t.text:
                    items.append(t.text.strip())
    except: pass
    return items

# ══════════════════════════════════════════════════════════════════════
# ── SHARED STATE ─────────────────────────────────────────────────────
# All 3 tabs read/write this single state object
# ══════════════════════════════════════════════════════════════════════
state = {
    # Tab 1: Day trade picks (up to 5)
    'day_picks': [],           # list of symbols (auto + manual)
    'pick_details': {},        # sym -> {score, signal, levels, last_updated}
    'active_trades': {},       # sym -> trade dict
    'completed_trades': [],    # all closed trades today
    'capital': 10000.0,
    'starting_capital': 10000.0,
    'total_pnl': 0.0,
    'date': None,

    # Tab 2: Options candidates (top 5)
    'option_candidates': [],   # [{sym, strategy, score, entry, pnl_pct, status}]
    'option_history': [],

    # Tab 3: Custom research
    'custom_watchlist': [],    # user-typed tickers for deep research
    'custom_research': {},     # sym -> {analysis, signal, entry_zone, last_updated}
    'custom_trades': {},       # sym -> active custom trade

    # Shared
    'log': [],
    'last_scan': 0,
}

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),'atp_state.json')

def save_state():
    try:
        snap={k:v for k,v in state.items() if k not in ('pick_details','custom_research')}
        snap['date']=state['date']
        with open(STATE_FILE,'w') as f: json.dump(snap,f)
    except Exception as e: print(f"  save: {e}")

def load_state():
    if not os.path.exists(STATE_FILE): return
    try:
        with open(STATE_FILE) as f: snap=json.load(f)
        today=today_str()
        state['custom_watchlist']=snap.get('custom_watchlist',[])
        state['day_picks']=snap.get('day_picks',[])
        if snap.get('date')==today:
            state.update({k:snap[k] for k in
                ['active_trades','completed_trades','capital','starting_capital',
                 'total_pnl','option_candidates','option_history','custom_trades'] if k in snap})
            state['date']=today
        add_log(f"Session resumed: {len(state['completed_trades'])} trades, P&L ${state['total_pnl']:+.2f}")
    except Exception as e: print(f"  load: {e}")

def add_log(msg):
    ts=now_et()
    state['log'].insert(0,{'time':ts,'msg':msg})
    state['log']=state['log'][:60]
    print(f"  [{ts}] {msg}")
    tg(f"[AutoTrade Pro] {msg}")

# ══════════════════════════════════════════════════════════════════════
# ── TAB 1: DAY TRADE ENGINE ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════
_scan_lock = threading.Lock()

def run_morning_scan():
    """Score the full universe, pick top 5, store with details."""
    state['date'] = today_str()
    add_log("🔍 Morning scan starting...")
    try:
        quotes = get_full_quotes(UNIVERSE)
        spy=cp('SPY'); qqq=cp('QQQ')
        sp=spy['pct'] if spy else 0; qp=qqq['pct'] if qqq else 0
        for q in quotes:
            q['_score'] = score_stock(q, sp, qp)
        quotes.sort(key=lambda x: x['_score'], reverse=True)
        top5 = [q['symbol'] for q in quotes[:5]]
        state['day_picks'] = top5
        state['last_scan'] = time.time()
        # Compute signals for each pick
        for q in quotes[:5]:
            sym = q['symbol']
            sig = compute_vwap_ema(sym)
            lvl = compute_atr_levels(sym, q['regularMarketPrice'])
            state['pick_details'][sym] = {
                'quote': q, 'signal': sig, 'levels': lvl,
                'score': q['_score'], 'last_updated': now_et()
            }
        mood="📈 BULL" if sp>0.3 and qp>0.3 else "📉 BEAR" if sp<-0.5 else "➡ MIXED"
        add_log(f"✅ Scan done. Top picks: {', '.join(top5)}. Market: {mood}")
        tg(f"🌅 AutoTrade Pro — {today_str()}\n{'━'*20}\n"
           f"Top 5 Picks: {', '.join(top5)}\nSPY {sp:+.2f}% | QQQ {qp:+.2f}%\n{get_app_url()}")
    except Exception as e:
        add_log(f"❌ Scan error: {e}")

def refresh_pick_signals():
    """Update VWAP/EMA signals for all day picks every minute."""
    for sym in state['day_picks']:
        try:
            cached = cp(sym)
            if not cached: continue
            price = cached['price']
            sig   = compute_vwap_ema(sym)
            lvl   = state['pick_details'].get(sym,{}).get('levels') or compute_atr_levels(sym, price)
            if sym not in state['pick_details']:
                state['pick_details'][sym] = {}
            state['pick_details'][sym].update({'signal':sig,'levels':lvl,'last_updated':now_et()})

            # Notify on fresh BUY signal if not already in a trade
            if sig and sig['signal']=='BUY' and sym not in state['active_trades']:
                key = f"notified_buy_{sym}_{today_str()}"
                if not state.get(key):
                    state[key] = True
                    tg(f"🟢 BUY SIGNAL — {sym}\n{'━'*20}\n"
                       f"${price:.2f} | Above VWAP ${sig['vwap']:.2f} | 9EMA>{sig['ema20']:.2f}\n"
                       f"Target ${lvl['target']:.2f} (+{lvl['target_pct']:.1f}%) | Stop ${lvl['stop']:.2f} (-{lvl['stop_pct']:.1f}%)\n"
                       f"{get_app_url()}")
        except Exception as e:
            print(f"  Signal refresh {sym}: {e}")

def enter_day_trade(sym, manual=False):
    if not is_market_open():
        return False, "Market closed"
    if sym in state['active_trades']:
        return False, f"Already in {sym}"
    cached=cp(sym)
    if not cached:
        # Try on-demand fetch
        try:
            tk=yf.Ticker(sym); info=tk.fast_info
            price=float(getattr(info,'last_price',0) or 0)
            if price<=0: return False, "No price data"
            cached={'price':price,'pct':0}
        except: return False, "No price data"
    price=cached['price']
    # Allocate capital per open slot (equal weight across 5 picks)
    n_active=len(state['active_trades'])
    if n_active>=5: return False, "Max 5 trades open"
    alloc=state['capital']/(5-n_active) if state['capital']>0 else 0
    if alloc<price: return False, f"Insufficient capital ${state['capital']:.2f}"
    shares=int(alloc/price)
    if shares<1: return False, "Not enough capital for 1 share"
    lvl=compute_atr_levels(sym, price)
    trade={
        'symbol':sym,'entry':round(price,2),'shares':shares,'cost':round(shares*price,2),
        'target':lvl['target'],'stop':lvl['stop'],'target_pct':lvl['target_pct'],
        'stop_pct':lvl['stop_pct'],'atr':lvl.get('atr'),
        'current':round(price,2),'peak':round(price,2),'peak_pnl_pct':0.0,
        'entry_time':now_et(),'entered_at':time.time(),'manual':manual,'tab':'day'
    }
    state['active_trades'][sym]=trade
    state['capital']-=trade['cost']
    save_state()
    add_log(f"{'MANUAL' if manual else 'AUTO'} BUY {sym} {shares}sh @${price:.2f} T=${lvl['target']:.2f} S=${lvl['stop']:.2f}")
    tg(f"📈 {'MANUAL ' if manual else ''}TRADE ENTERED — {sym}\n{'━'*20}\n"
       f"{shares}sh @ ${price:.2f} | Cost ${shares*price:,.2f}\n"
       f"Target ${lvl['target']:.2f} (+{lvl['target_pct']:.1f}%)\n"
       f"Stop   ${lvl['stop']:.2f} (-{lvl['stop_pct']:.1f}%)\n{get_app_url()}")
    # Execute on Alpaca
    if alpaca_trading:
        try:
            oid=str(alpaca_trading.submit_order(MarketOrderRequest(
                symbol=sym,qty=shares,side=OrderSide.BUY,time_in_force=TimeInForce.DAY)).id)
            state['active_trades'][sym]['order_id']=oid
        except Exception as e: print(f"  Alpaca order: {e}")
    return True, f"Entered {sym}"

def close_day_trade(sym, reason='manual'):
    t=state['active_trades'].pop(sym,None)
    if not t: return
    cached=cp(sym); sell=round(cached['price'] if cached else t['current'],2)
    pnl=round((sell-t['entry'])*t['shares'],2)
    pnl_pct=round((sell-t['entry'])/t['entry']*100,2)
    proceeds=round(sell*t['shares'],2)
    state['completed_trades'].append({**t,'exit':sell,'exit_time':now_et(),'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    state['total_pnl']+=pnl; state['capital']+=proceeds
    save_state()
    labels={'target':'TARGET HIT 🎯','stop':'STOP HIT 🛑','manual':'MANUAL EXIT','eod':'EOD CLOSE','momentum_stall':'TRAIL STOP'}
    add_log(f"CLOSED {sym} {labels.get(reason,reason)} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    if alpaca_trading:
        try: alpaca_trading.close_position(sym)
        except: pass

def monitor_day_trades():
    """Called every minute: update prices, check stops/targets."""
    if not is_trading_day(): return
    for sym, t in list(state['active_trades'].items()):
        if t.get('tab') not in ('day', None): continue
        cached=cp(sym)
        if not cached: continue
        price=cached['price']
        state['active_trades'][sym]['current']=price
        peak=max(price, t.get('peak',price))
        state['active_trades'][sym]['peak']=peak
        pnl_pct=(price-t['entry'])/t['entry']*100
        peak_pct=(peak-t['entry'])/t['entry']*100
        state['active_trades'][sym]['peak_pnl_pct']=max(peak_pct,t.get('peak_pnl_pct',0))
        if price>=t['target']:
            close_day_trade(sym,'target'); continue
        if price<=t['stop']:
            close_day_trade(sym,'stop'); continue
        # Trailing: peak up 3%+, gives back 2pts
        if peak_pct>=3 and (peak_pct-pnl_pct)>=2:
            close_day_trade(sym,'momentum_stall'); continue

# ══════════════════════════════════════════════════════════════════════
# ── TAB 2: OPTIONS ENGINE ────────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════
OPT_LEVERAGE   = 5
OPT_TARGET_PCT = 35
OPT_STOP_PCT   = 25

def determine_option_strategy(sym, score, chg, vr, sig):
    """Return CALL / PUT / STRADDLE + reasoning."""
    if sig and sig['signal']=='BUY' and score>=60:
        return 'CALL', f"BUY signal + score {score}/99 — bullish momentum play"
    if sig and sig['signal']=='SELL' and chg<-2:
        return 'PUT', f"SELL signal + bearish momentum — hedge or short play"
    if vr>5 and abs(chg)>3:
        return 'STRADDLE', f"Extreme volume {vr:.1f}x — big move expected either way"
    if score>=65:
        return 'CALL', f"Strong score {score}/99 — directional bullish"
    return 'CALL', f"Score {score}/99 — mild bullish setup"

def refresh_option_candidates():
    """Build top 5 option candidates from day picks + universe top scores."""
    if not is_trading_day(): return
    all_syms=list(dict.fromkeys(state['day_picks']+UNIVERSE[:15]))
    quotes=get_full_quotes(all_syms[:20])
    spy=cp('SPY'); qqq=cp('QQQ')
    sp=spy['pct'] if spy else 0; qp=qqq['pct'] if qqq else 0
    scored=[]
    for q in quotes:
        q['_score']=score_stock(q,sp,qp)
        scored.append(q)
    scored.sort(key=lambda x:x['_score'],reverse=True)
    candidates=[]
    for q in scored[:8]:
        sym=q['symbol']; price=q['regularMarketPrice']
        sig=compute_vwap_ema(sym)
        strategy,reason=determine_option_strategy(
            sym, q['_score'], q['regularMarketChangePercent'], q['_vr'], sig)
        # Check if already tracking this one
        existing=next((c for c in state['option_candidates'] if c['symbol']==sym),None)
        if existing:
            cached=cp(sym); cur=cached['price'] if cached else existing['entry_underlying']
            underlying_chg=(cur-existing['entry_underlying'])/existing['entry_underlying']*100
            pnl_pct=underlying_chg*OPT_LEVERAGE
            existing.update({'current_underlying':round(cur,2),'sim_pnl_pct':round(pnl_pct,2),
                             'signal':sig,'score':q['_score']})
            peak=max(existing.get('peak_pnl_pct',0),pnl_pct)
            existing['peak_pnl_pct']=peak
            # Check exits
            if pnl_pct>=OPT_TARGET_PCT:
                state['option_candidates'].remove(existing)
                state['option_history'].insert(0,{**existing,'reason':'target','exit_time':now_et(),'sim_pnl_pct':pnl_pct})
                add_log(f"OPTIONS TARGET {sym} +{pnl_pct:.0f}% 🎯")
            elif pnl_pct<=-OPT_STOP_PCT:
                state['option_candidates'].remove(existing)
                state['option_history'].insert(0,{**existing,'reason':'stop','exit_time':now_et(),'sim_pnl_pct':pnl_pct})
                add_log(f"OPTIONS STOP {sym} {pnl_pct:.0f}% 🛑")
        else:
            if len(candidates)+len([c for c in state['option_candidates'] if c not in candidates])>=5:
                break
            entry={'symbol':sym,'strategy':strategy,'reason':reason,
                   'score':q['_score'],'entry_underlying':round(price,2),
                   'current_underlying':round(price,2),'entry_time':now_et(),
                   'entered_at':time.time(),'sim_pnl_pct':0.0,'peak_pnl_pct':0.0,
                   'signal':sig}
            candidates.append(entry)
    # Add new candidates if slots open
    tracked=set(c['symbol'] for c in state['option_candidates'])
    for c in candidates:
        if len(state['option_candidates'])>=5: break
        if c['symbol'] not in tracked:
            state['option_candidates'].append(c)
            tracked.add(c['symbol'])
    state['option_history']=state['option_history'][:30]
    save_state()

# ══════════════════════════════════════════════════════════════════════
# ── TAB 3: CUSTOM DEEP RESEARCH ──────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

def deep_research(sym):
    """Full AI analysis of a ticker — returns structured research dict."""
    cached=cp(sym)
    if not cached:
        try:
            tk=yf.Ticker(sym); info=tk.fast_info
            price=float(getattr(info,'last_price',0) or 0)
            cached={'price':price,'pct':0} if price>0 else None
        except: pass
    if not cached or cached['price']<=0:
        return {'symbol':sym,'error':'No price data','signal':None,'analysis':None}

    price=cached['price']; chg=cached['pct']
    sig=compute_vwap_ema(sym)
    lvl=compute_atr_levels(sym, price)
    news=get_symbol_news(sym)

    # Get full quote data
    quotes=get_full_quotes([sym])
    q=quotes[0] if quotes else {}
    score=score_stock(q) if q else 0
    vr=q.get('_vr',0) if q else 0
    hi52=q.get('fiftyTwoWeekHigh',price) if q else price
    lo52=q.get('fiftyTwoWeekLow',price) if q else price
    annual_range=((hi52-lo52)/lo52*100) if lo52>0 else 0

    # Build context for AI
    sys_prompt = (
        "You are AutoTrade Pro AI — a professional day trading assistant for Abiy Kassa. "
        "You give SPECIFIC, ACTIONABLE direction. Be direct and concise. "
        "Always end with: SIGNAL: BUY / WAIT / SELL and WHY in one sentence."
    )
    user_msg = (
        f"Deep analysis for {sym}:\n"
        f"Price: ${price:.2f} | Change: {chg:+.2f}% | Score: {score}/99\n"
        f"Volume ratio: {vr:.1f}x | 52w range: ${lo52:.2f}–${hi52:.2f} (annual range {annual_range:.0f}%)\n"
        f"VWAP signal: {sig['signal'] if sig else 'N/A'} — {sig['reason'] if sig else ''}\n"
        f"RSI: {sig['rsi'] if sig else 'N/A'} | EMA9: {sig['ema9'] if sig else 'N/A':.2f} | EMA20: {sig['ema20'] if sig else 'N/A':.2f}\n"
        f"ATR Stop: ${lvl['stop']:.2f} (-{lvl['stop_pct']:.1f}%) | ATR Target: ${lvl['target']:.2f} (+{lvl['target_pct']:.1f}%)\n"
        f"Recent news: {' | '.join(news[:3]) if news else 'None'}\n\n"
        "Give: 1) Market context (1-2 sentences), 2) Technical read (2-3 sentences with RSI/VWAP/EMA analysis), "
        "3) Entry zone and exact price levels, 4) Risk/reward, 5) SIGNAL: BUY/WAIT/SELL + one-sentence reason."
    )
    analysis, ai_source = ai_call(sys_prompt, user_msg, max_tokens=500)

    # Determine entry zone
    if sig:
        if sig['signal']=='BUY':
            entry_zone=f"${sig['vwap']:.2f}–${price:.2f}"
            entry_note="Enter on VWAP hold or pullback to EMA9"
        elif sig['signal']=='SELL':
            entry_zone="Avoid long entry"
            entry_note="Wait for signal to turn BUY before entering"
        else:
            entry_zone=f"${sig['vwap']:.2f}–${sig['ema9']:.2f}"
            entry_note="Wait — price consolidating near VWAP"
    else:
        entry_zone=f"${lvl['support']:.2f}–${price:.2f}"
        entry_note="No intraday data yet — use ATR levels"

    result={
        'symbol':sym,'price':price,'change_pct':chg,'score':score,
        'signal':sig,'levels':lvl,'vr':vr,'rsi':sig['rsi'] if sig else None,
        'annual_range':round(annual_range,1),'news':news,
        'entry_zone':entry_zone,'entry_note':entry_note,
        'analysis':analysis,'ai_source':ai_source,
        'last_updated':now_et(),'hi52':hi52,'lo52':lo52
    }
    state['custom_research'][sym]=result
    return result

def monitor_custom_trades():
    """Monitor custom-tab trades — same stop/target logic."""
    for sym, t in list(state['custom_trades'].items()):
        cached=cp(sym)
        if not cached: continue
        price=cached['price']
        state['custom_trades'][sym]['current']=price
        pnl_pct=(price-t['entry'])/t['entry']*100
        peak_pct=max((price-t['entry'])/t['entry']*100, t.get('peak_pnl_pct',0))
        state['custom_trades'][sym]['peak_pnl_pct']=peak_pct
        if price>=t['target']:
            t_copy=state['custom_trades'].pop(sym)
            state['completed_trades'].append({**t_copy,'exit':price,'exit_time':now_et(),
                'pnl':round((price-t_copy['entry'])*t_copy['shares'],2),
                'pnl_pct':round(pnl_pct,2),'reason':'target','tab':'custom'})
            state['total_pnl']+=round((price-t_copy['entry'])*t_copy['shares'],2)
            add_log(f"CUSTOM TARGET {sym} +{pnl_pct:.1f}% 🎯")
            save_state()
        elif price<=t['stop']:
            t_copy=state['custom_trades'].pop(sym)
            pnl=round((price-t_copy['entry'])*t_copy['shares'],2)
            state['completed_trades'].append({**t_copy,'exit':price,'exit_time':now_et(),
                'pnl':pnl,'pnl_pct':round(pnl_pct,2),'reason':'stop','tab':'custom'})
            state['total_pnl']+=pnl
            add_log(f"CUSTOM STOP {sym} {pnl_pct:.1f}% 🛑")
            save_state()

# ── SCHEDULED JOBS ────────────────────────────────────────────────────
def job_morning():
    if not is_trading_day(): return
    state['date']=today_str()
    state['capital']=10000.0; state['starting_capital']=10000.0
    state['total_pnl']=0.0; state['completed_trades']=[]
    state['active_trades']={}; state['option_candidates']=[]
    run_morning_scan()

def job_945():
    if not is_trading_day() or state['active_trades']: return
    run_morning_scan()

def job_minute():
    if not is_trading_day(): return
    state['date']=today_str()
    refresh_pick_signals()
    monitor_day_trades()
    monitor_custom_trades()
    # Auto-enter on clean BUY signals for day picks
    t_now=datetime.now(ET_TZ)
    t_min=t_now.hour*60+t_now.minute
    if is_market_open() and t_min>=585:  # after 9:45
        for sym in state['day_picks']:
            if sym in state['active_trades']: continue
            detail=state['pick_details'].get(sym,{})
            sig=detail.get('signal')
            if sig and sig['signal']=='BUY' and sig['rsi'] and sig['rsi']<70:
                enter_day_trade(sym)

def job_eod():
    if not is_trading_day(): return
    for sym in list(state['active_trades'].keys()):
        close_day_trade(sym,'eod')
    for sym in list(state['custom_trades'].keys()):
        t=state['custom_trades'].pop(sym)
        cached=cp(sym); sell=cached['price'] if cached else t['current']
        pnl=round((sell-t['entry'])*t['shares'],2)
        pnl_pct=round((sell-t['entry'])/t['entry']*100,2)
        state['completed_trades'].append({**t,'exit':sell,'exit_time':now_et(),
            'pnl':pnl,'pnl_pct':pnl_pct,'reason':'eod','tab':'custom'})
        state['total_pnl']+=pnl
    pct=state['total_pnl']/state['starting_capital']*100 if state['starting_capital'] else 0
    add_log(f"EOD — P&L ${state['total_pnl']:+.2f} ({pct:+.2f}%)")
    save_state()

def job_options():
    if is_trading_day(): refresh_option_candidates()

def job_keepalive():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    if n.weekday()>=5 or t<7*60 or t>17*60: return
    try: urllib.request.urlopen(urllib.request.Request(f"{get_app_url()}/ping",headers={'User-Agent':UA}),timeout=8)
    except: pass

# ── FLASK ROUTES ──────────────────────────────────────────────────────
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
                 'delay':'~0sec' if alpaca_data else '~15min',
                 'updated':datetime.fromtimestamp(_cache_ts,ET_TZ).strftime('%H:%M:%S') if _cache_ts else '—'})

# ── Tab 1 routes ──────────────────────────────────────────────────────
@app.route('/day-status')
def day_status():
    picks_out=[]
    for sym in state['day_picks']:
        detail=state['pick_details'].get(sym,{})
        cached=cp(sym)
        price=cached['price'] if cached else 0
        active=state['active_trades'].get(sym)
        if active and cached: state['active_trades'][sym]['current']=cached['price']
        picks_out.append({'symbol':sym,'price':price,'pct':cached['pct'] if cached else 0,
            'score':detail.get('score',0),'signal':detail.get('signal'),
            'levels':detail.get('levels'),'active_trade':active,
            'last_updated':detail.get('last_updated','—')})
    completed_day=[t for t in state['completed_trades'] if t.get('tab') in ('day',None)]
    return cors({'picks':picks_out,'active_trades':state['active_trades'],
        'completed':completed_day,'capital':round(state['capital'],2),
        'starting_capital':state['starting_capital'],
        'total_pnl':round(state['total_pnl'],2),
        'date':state['date'],'log':state['log'][:15],
        'last_scan':state['last_scan']})

@app.route('/day-picks', methods=['POST'])
def set_day_picks():
    data=request.get_json() or {}
    syms=[s.strip().upper().replace(' ','') for s in data.get('symbols',[]) if s.strip()][:5]
    state['day_picks']=list(dict.fromkeys(syms))
    # Trigger signal computation in background
    def compute():
        for sym in state['day_picks']:
            try:
                quotes=get_full_quotes([sym])
                q=quotes[0] if quotes else None
                if q:
                    sig=compute_vwap_ema(sym); lvl=compute_atr_levels(sym,q['regularMarketPrice'])
                    state['pick_details'][sym]={'quote':q,'signal':sig,'levels':lvl,'score':q.get('_score',0),'last_updated':now_et()}
            except: pass
    threading.Thread(target=compute,daemon=True).start()
    save_state()
    return cors({'ok':True,'picks':state['day_picks']})

@app.route('/day-scan', methods=['POST'])
def day_scan():
    threading.Thread(target=run_morning_scan,daemon=True).start()
    return cors({'ok':True,'msg':'Scan started — takes ~60s'})

@app.route('/day-enter', methods=['POST'])
def day_enter():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    ok,msg=enter_day_trade(sym, manual=True)
    return cors({'ok':ok,'msg':msg})

@app.route('/day-close', methods=['POST'])
def day_close():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    reason=data.get('reason','manual')
    if sym not in state['active_trades']:
        return cors({'ok':False,'msg':f'{sym} not active'})
    close_day_trade(sym, reason)
    return cors({'ok':True,'msg':f'Closed {sym}'})

@app.route('/day-signal')
def day_signal():
    sym=request.args.get('symbol','').upper()
    if not sym: return cors({'signal':None})
    return cors({'signal':compute_vwap_ema(sym)})

@app.route('/candles')
def candles():
    sym=request.args.get('symbol','').upper()
    if not sym: return cors({'bars':[]})
    try:
        df=yf.download(sym,period='5d',interval='5m',auto_adjust=True,progress=False)
        if df.empty: df=yf.download(sym,period='1mo',interval='15m',auto_adjust=True,progress=False)
        if isinstance(df.columns,pd.MultiIndex): df.columns=df.columns.get_level_values(0)
        df=df.dropna().tail(78)
        bars=[]
        for i,r in df.iterrows():
            try:
                idx_et=i.tz_convert(ET_TZ) if i.tzinfo else i.tz_localize('UTC').tz_convert(ET_TZ)
                hm=idx_et.strftime('%H:%M')
            except: hm=str(i)[11:16]
            bars.append({'t':str(i),'hm':hm,'o':round(float(r.Open),2),'h':round(float(r.High),2),
                         'l':round(float(r.Low),2),'c':round(float(r.Close),2),'v':int(r.Volume)})
        return cors({'bars':bars,'symbol':sym})
    except Exception as e: return cors({'bars':[],'error':str(e)})

# ── Tab 2 routes ──────────────────────────────────────────────────────
@app.route('/options-status')
def options_status():
    # Update current prices/pnl inline
    out=[]
    for c in state['option_candidates']:
        cached=cp(c['symbol'])
        if cached:
            cur=cached['price']
            underlying_chg=(cur-c['entry_underlying'])/c['entry_underlying']*100
            pnl=underlying_chg*OPT_LEVERAGE
            c.update({'current_underlying':round(cur,2),'sim_pnl_pct':round(pnl,2)})
        out.append(c)
    return cors({'candidates':out,'history':state['option_history'][:20],
                 'max_slots':5,'leverage':OPT_LEVERAGE,
                 'target_pct':OPT_TARGET_PCT,'stop_pct':OPT_STOP_PCT})

@app.route('/options-refresh', methods=['POST'])
def options_refresh():
    threading.Thread(target=refresh_option_candidates,daemon=True).start()
    return cors({'ok':True,'msg':'Refreshing...'})

# ── Tab 3 routes ──────────────────────────────────────────────────────
@app.route('/research', methods=['POST'])
def research():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    if not sym: return cors({'ok':False,'msg':'No symbol'})
    if sym not in state['custom_watchlist']:
        state['custom_watchlist'].append(sym)
        if len(state['custom_watchlist'])>10: state['custom_watchlist']=state['custom_watchlist'][-10:]
        save_state()
    def do_research():
        result=deep_research(sym)
        state['custom_research'][sym]=result
    threading.Thread(target=do_research,daemon=True).start()
    return cors({'ok':True,'msg':f'Research started for {sym}','symbol':sym})

@app.route('/research-result')
def research_result():
    sym=request.args.get('symbol','').upper()
    result=state['custom_research'].get(sym)
    return cors({'result':result,'ready':result is not None})

@app.route('/custom-enter', methods=['POST'])
def custom_enter():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    if not is_market_open(): return cors({'ok':False,'msg':'Market closed'})
    if sym in state['custom_trades']: return cors({'ok':False,'msg':f'Already tracking {sym}'})
    cached=cp(sym)
    if not cached: return cors({'ok':False,'msg':'No price data'})
    price=cached['price']
    alloc=min(state['capital'], 5000)  # max $5k per custom trade
    if alloc<price: return cors({'ok':False,'msg':f'Need ${price:.2f}, have ${state["capital"]:.2f}'})
    shares=int(alloc/price)
    lvl=compute_atr_levels(sym,price)
    trade={'symbol':sym,'entry':round(price,2),'shares':shares,'cost':round(shares*price,2),
           'target':lvl['target'],'stop':lvl['stop'],'current':round(price,2),
           'peak':round(price,2),'peak_pnl_pct':0.0,'entry_time':now_et(),
           'entered_at':time.time(),'tab':'custom'}
    state['custom_trades'][sym]=trade
    state['capital']-=trade['cost']
    save_state()
    add_log(f"CUSTOM ENTER {sym} {shares}sh @${price:.2f}")
    if alpaca_trading:
        try:
            alpaca_trading.submit_order(MarketOrderRequest(
                symbol=sym,qty=shares,side=OrderSide.BUY,time_in_force=TimeInForce.DAY))
        except Exception as e: print(f"  Alpaca custom: {e}")
    return cors({'ok':True,'msg':f'Entered {sym}','trade':trade})

@app.route('/custom-close', methods=['POST'])
def custom_close():
    data=request.get_json() or {}
    sym=data.get('symbol','').upper()
    t=state['custom_trades'].pop(sym,None)
    if not t: return cors({'ok':False,'msg':f'{sym} not tracked'})
    cached=cp(sym); sell=cached['price'] if cached else t['current']
    pnl=round((sell-t['entry'])*t['shares'],2)
    pnl_pct=round((sell-t['entry'])/t['entry']*100,2)
    proceeds=round(sell*t['shares'],2)
    state['completed_trades'].append({**t,'exit':sell,'exit_time':now_et(),'pnl':pnl,'pnl_pct':pnl_pct,'reason':'manual','tab':'custom'})
    state['total_pnl']+=pnl; state['capital']+=proceeds
    save_state()
    add_log(f"CUSTOM CLOSE {sym} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    if alpaca_trading:
        try: alpaca_trading.close_position(sym)
        except: pass
    return cors({'ok':True,'msg':f'Closed {sym}','pnl':pnl,'pnl_pct':pnl_pct})

@app.route('/custom-status')
def custom_status():
    out={}
    for sym,t in state['custom_trades'].items():
        cached=cp(sym)
        if cached: state['custom_trades'][sym]['current']=cached['price']
        out[sym]=state['custom_trades'][sym]
    completed_custom=[t for t in state['completed_trades'] if t.get('tab')=='custom']
    return cors({'trades':out,'research':state['custom_research'],
                 'watchlist':state['custom_watchlist'],
                 'completed':completed_custom[-20:]})

# ── Shared routes ─────────────────────────────────────────────────────
@app.route('/chat', methods=['POST'])
def chat():
    data=request.get_json() or {}
    msg=data.get('message','').strip()
    sym=data.get('symbol','').upper()
    tab=data.get('tab','day')
    if not msg: return cors({'ok':False})
    # Build rich context
    ctx=[]
    ctx.append(f"Today: {today_str()} | Capital: ${state['capital']:.2f} | P&L: ${state['total_pnl']:+.2f}")
    ctx.append(f"Day picks: {', '.join(state['day_picks']) or 'none'}")
    ctx.append(f"Active trades: {', '.join(state['active_trades'].keys()) or 'none'}")
    if sym and sym in state['custom_research']:
        r=state['custom_research'][sym]
        ctx.append(f"Research on {sym}: score {r.get('score',0)}/99, signal {r.get('signal',{}).get('signal','?')}, RSI {r.get('rsi','?')}")
    if sym and state['pick_details'].get(sym):
        d=state['pick_details'][sym]
        sig=d.get('signal',{})
        ctx.append(f"{sym} live signal: {sig.get('signal','?')} — {sig.get('reason','')}")
    mkt=all_prices()
    ctx.append(f"SPY {mkt.get('SPY',{}).get('pct',0):+.2f}% | QQQ {mkt.get('QQQ',{}).get('pct',0):+.2f}%")
    sys_prompt=(
        "You are AutoTrade Pro AI for Abiy Kassa — professional day trading assistant. "
        "Give specific, actionable advice. Be concise (3-5 sentences max). "
        "Reference real prices and levels when available. Never give generic disclaimers — this is paper trading.\n"
        "Context:\n" + "\n".join(ctx)
    )
    response, ai_source = ai_call(sys_prompt, msg, max_tokens=350)
    if not response:
        response="No AI key configured. Add GROQ_API_KEY to Render environment variables."
        ai_source='none'
    return cors({'ok':True,'response':response,'ai_source':ai_source,'symbol':sym})

@app.route('/news')
def news_route():
    sym=request.args.get('symbol','')
    if sym: return cors({'items':get_symbol_news(sym.upper())})
    return cors({'items':get_news()})

@app.route('/performance')
def performance():
    trades=state['completed_trades']
    if not trades: return cors({'trades':0,'win_rate':0,'total_pnl':0,'avg_win':0,'avg_loss':0})
    wins=[t for t in trades if t.get('pnl',0)>=0]
    losses=[t for t in trades if t.get('pnl',0)<0]
    return cors({'trades':len(trades),'win_rate':round(len(wins)/len(trades)*100,1),
                 'total_pnl':round(sum(t.get('pnl',0) for t in trades),2),
                 'avg_win':round(sum(t.get('pnl',0) for t in wins)/len(wins),2) if wins else 0,
                 'avg_loss':round(sum(t.get('pnl',0) for t in losses)/len(losses),2) if losses else 0})

@app.route('/test-telegram')
def test_telegram():
    ok=tg(f"✅ AutoTrade Pro alive | Alpaca: {'✅' if alpaca_trading else '❌'} | AI: {'Groq ✅' if GROQ_KEY else ('Anthropic ✅' if ANTHROPIC_KEY else '❌')}\n{get_app_url()}")
    return cors({'ok':ok})

@app.route('/notify')
def notify():
    return cors({'ok':tg(request.args.get('msg','')) if request.args.get('msg') else False})

@app.route('/watchlist', methods=['GET','POST'])
def watchlist():
    if request.method=='POST':
        data=request.get_json() or {}
        syms=[s.strip().upper() for s in data.get('symbols',[]) if s.strip()]
        state['custom_watchlist']=syms[:10]; save_state()
        return cors({'ok':True,'symbols':state['custom_watchlist']})
    return cors({'symbols':state['custom_watchlist']})

# ── START ─────────────────────────────────────────────────────────────
load_state()
load_prev_closes()
threading.Thread(target=price_thread,daemon=True).start()
import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning, 'cron',day_of_week='mon-fri',hour=8,minute=0)
    sched.add_job(job_945,     'cron',day_of_week='mon-fri',hour=9,minute=45)
    sched.add_job(job_minute,  'cron',day_of_week='mon-fri',hour='8-16',minute='*')
    sched.add_job(job_options, 'cron',day_of_week='mon-fri',hour='9-16',minute='*/15')
    sched.add_job(job_eod,     'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,'interval',minutes=10)
    sched.start(); atexit.register(lambda:sched.shutdown(wait=False))
    print(f"  Scheduler: {len(sched.get_jobs())} jobs")
except Exception as e: print(f"  Scheduler: {e}")

if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT,debug=False)
