#!/usr/bin/env python3
"""Options Scanner Pro — Flask Server"""
import json, os, urllib.request, urllib.parse, concurrent.futures, time, xml.etree.ElementTree as ET
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("\n  ERROR: pip install yfinance flask gunicorn\n"); raise SystemExit(1)

app = Flask(__name__, static_folder='.')
PORT = int(os.environ.get('PORT', 8765))
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

VALID_PERIODS   = {'1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'}
VALID_INTERVALS = {'1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'}

_news_cache=[]; _news_ts=0
_market_cache=[]; _market_ts=0
_stock_news_cache={}; _stock_news_ts={}

def cors(data):
    r = jsonify(data)
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

# ── FULL QUOTES (scan) ─────────────────────────────────────────────────
def get_quotes(symbols):
    results = []
    try:
        df5  = yf.download(symbols, period="5d", interval="1d", auto_adjust=True, progress=False, threads=True)
        df1y = yf.download(symbols, period="1y", interval="1d", auto_adjust=True, progress=False, threads=True)
    except Exception as e:
        print(f"  ERROR download: {e}"); return []

    def fetch_info(sym):
        try: return sym, yf.Ticker(sym).info or {}
        except: return sym, {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        info_map = dict(ex.map(fetch_info, symbols))

    multi = isinstance(df5.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            c5  = (df5['Close'][sym]  if multi else df5['Close']).dropna()
            v5  = (df5['Volume'][sym] if multi else df5['Volume']).dropna()
            c1y = (df1y['Close'][sym] if multi else df1y['Close']).dropna()
            v1y = (df1y['Volume'][sym] if multi else df1y['Volume']).dropna()
            if len(c5) < 1: continue
            price = float(c5.iloc[-1]); prev = float(c5.iloc[-2]) if len(c5)>=2 else price
            chg = ((price-prev)/prev*100) if prev else 0
            info = info_map.get(sym, {})
            # pre/post market
            pre_price  = info.get('preMarketPrice') or 0
            pre_chg    = info.get('preMarketChangePercent') or 0
            post_price = info.get('postMarketPrice') or 0
            post_chg   = info.get('postMarketChangePercent') or 0
            mkt_state  = info.get('marketState', 'CLOSED')
            results.append({
                "symbol": sym, "shortName": info.get("shortName", sym),
                "regularMarketPrice": round(price,2),
                "regularMarketChangePercent": round(chg,4),
                "regularMarketVolume": int(v5.iloc[-1]) if len(v5) else 0,
                "averageDailyVolume3Month": int(v1y.mean()) if len(v1y) else 1,
                "marketCap": int(info.get("marketCap") or 0),
                "fiftyTwoWeekHigh": round(float(c1y.max()),2) if len(c1y) else round(price,2),
                "fiftyTwoWeekLow":  round(float(c1y.min()),2) if len(c1y) else round(price,2),
                "preMarketPrice": round(pre_price,2) if pre_price else 0,
                "preMarketChangePercent": round(float(pre_chg)*100,2) if pre_chg and abs(float(pre_chg)) < 1 else round(float(pre_chg),2) if pre_chg else 0,
                "postMarketPrice": round(post_price,2) if post_price else 0,
                "postMarketChangePercent": round(float(post_chg)*100,2) if post_chg and abs(float(post_chg)) < 1 else round(float(post_chg),2) if post_chg else 0,
                "marketState": mkt_state,
            })
            print(f"  OK  {sym:6}  ${round(price,2)}")
        except Exception as e:
            print(f"  skip {sym}: {e}")
    return results

# ── FAST PRICES (30-sec refresh) ──────────────────────────────────────
def get_fast_prices(symbols):
    try:
        df = yf.download(symbols, period="2d", interval="1d", auto_adjust=True, progress=False, threads=True)
        if df.empty: return []
        multi = isinstance(df.columns, pd.MultiIndex)
        results = []
        for sym in symbols:
            try:
                closes = (df['Close'][sym] if multi else df['Close']).dropna()
                vols   = (df['Volume'][sym] if multi else df['Volume']).dropna()
                if len(closes)<1: continue
                price=float(closes.iloc[-1]); prev=float(closes.iloc[-2]) if len(closes)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                results.append({"symbol":sym,"regularMarketPrice":round(price,2),
                    "regularMarketChangePercent":round(chg,3),"regularMarketVolume":int(vols.iloc[-1]) if len(vols) else 0})
            except: pass
        return results
    except Exception as e:
        print(f"  fast error: {e}"); return []

# ── HISTORY ───────────────────────────────────────────────────────────
def get_history(symbol, period="1mo", interval="1d"):
    try:
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
        if hist.empty: return None
        is_intra = interval in ('1m','2m','5m','15m','30m','60m','90m','1h')
        timestamps=[int(ts.timestamp()) for ts in hist.index]
        closes=[round(float(c),2) if c==c else None for c in hist["Close"]]
        labels=[]
        for ts in hist.index:
            if is_intra:
                h=ts.hour; m=ts.minute; h12=h%12 or 12
                labels.append(f"{h12}:{m:02d}")
            else:
                labels.append(ts.strftime('%b')+' '+str(ts.day))
        return {"chart":{"result":[{"timestamp":timestamps,"labels":labels,
                "indicators":{"quote":[{"close":closes}]}}]}}
    except Exception as e:
        print(f"  history skip {symbol}: {e}"); return None

# ── MARKET BAR (SPY, QQQ, BTC) ────────────────────────────────────────
def get_market():
    global _market_cache, _market_ts
    if time.time()-_market_ts < 12 and _market_cache: return _market_cache
    try:
        syms = ['SPY','QQQ','BTC-USD']
        df = yf.download(syms, period="2d", interval="1d", auto_adjust=True, progress=False)
        results=[]
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                closes=(df['Close'][sym] if multi else df['Close']).dropna()
                price=float(closes.iloc[-1]); prev=float(closes.iloc[-2]) if len(closes)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                results.append({"symbol":sym,"price":round(price,2),"change":round(chg,3)})
            except: pass
        _market_cache=results; _market_ts=time.time(); return results
    except Exception as e:
        print(f"  market error: {e}"); return _market_cache or []

# ── NEWS ──────────────────────────────────────────────────────────────
def get_news():
    global _news_cache, _news_ts
    if time.time()-_news_ts < 90 and _news_cache: return _news_cache
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,TSLA,AAPL,META,MSFT,AMZN,COIN,PLTR&region=US&lang=en-US',
                'https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ&region=US&lang=en-US']:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=6) as resp:
                tree=ET.fromstring(resp.read())
                for item in tree.iter('item'):
                    t_el=item.find('title')
                    if t_el is not None and t_el.text and len(t_el.text.strip())>20:
                        pub=item.find('pubDate')
                        items.append({"title":t_el.text.strip(),"symbol":"","time":pub.text if pub else ""})
        except Exception as e: print(f"  RSS: {e}")
    if not items:
        for sym in ['NVDA','TSLA','AAPL','META','MSFT']:
            try:
                raw=yf.Ticker(sym).get_news() or yf.Ticker(sym).news or []
                for n in raw[:2]:
                    title=(n.get('title') or '').strip()
                    if title and len(title)>20:
                        items.append({"title":title,"symbol":sym,"time":str(n.get('providerPublishTime',''))})
            except: pass
    if not items:
        items=[{"title":"AI chip demand drives semiconductor sector — NVDA AMD AVGO lead gains","symbol":"NVDA","time":""},
               {"title":"Federal Reserve signals data-dependent approach to interest rate decisions","symbol":"SPY","time":""},
               {"title":"Tech mega-caps outperform on strong earnings and AI monetization progress","symbol":"QQQ","time":""}]
    _news_cache=items[:25]; _news_ts=time.time(); return _news_cache

def get_stock_news(symbol):
    global _stock_news_cache, _stock_news_ts
    now=time.time()
    if symbol in _stock_news_cache and now-_stock_news_ts.get(symbol,0)<120:
        return _stock_news_cache[symbol]
    items=[]
    try:
        url=f'https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US'
        req=urllib.request.Request(url,headers={'User-Agent':UA})
        with urllib.request.urlopen(req,timeout=5) as resp:
            tree=ET.fromstring(resp.read())
            for item in list(tree.iter('item'))[:5]:
                t_el=item.find('title')
                if t_el is not None and t_el.text and len(t_el.text.strip())>15:
                    pub=item.find('pubDate')
                    items.append({"title":t_el.text.strip(),"source":"Yahoo Finance","time":pub.text if pub else ""})
    except: pass
    if not items:
        try:
            raw=yf.Ticker(symbol).get_news() or yf.Ticker(symbol).news or []
            for n in raw[:5]:
                title=(n.get('title') or '').strip()
                if title and len(title)>15:
                    items.append({"title":title,"source":n.get('publisher','Yahoo Finance'),"time":str(n.get('providerPublishTime',''))})
        except: pass
    _stock_news_cache[symbol]=items[:5]; _stock_news_ts[symbol]=now
    return items[:5]

# ── AI / RULE-BASED PREDICTIONS ────────────────────────────────────────
def get_rule_prediction(symbol, d, timeframe):
    price  = d.get('regularMarketPrice',0)
    chg    = d.get('regularMarketChangePercent',0)
    vr     = d.get('volumeRatio',1)
    hi52   = d.get('fiftyTwoWeekHigh',price) or price
    lo52   = d.get('fiftyTwoWeekLow',price*0.7) or price*0.7
    mc     = d.get('marketCap',0)
    rng    = (price-lo52)/(hi52-lo52) if (hi52-lo52)>0 else 0.5

    if timeframe=='today':
        t_up = round(price*1.018,2); t_dn = round(price*0.988,2)
        if chg>3 and vr>1.5:
            return f"📈 STRONG BULL: {chg:+.2f}% gain with {vr:.1f}x volume = institutional accumulation confirmed. Momentum continuation likely. Intraday target ${t_up}. Hold above entry; trail stop up. Avoid chasing — wait for any dip to entry zone."
        elif chg>1:
            return f"📈 MILD BULL: Positive {chg:+.2f}% with {vr:.1f}x volume. Price action constructive. Intraday target ${t_up} if volume holds. Risk: reversal to ${t_dn} if broad market weakens. Best entry on pullback to entry zone."
        elif chg<-3 and vr>1.5:
            return f"📉 BEARISH: {chg:.2f}% decline on {vr:.1f}x volume — distribution pattern. Avoid longs. Put play at ${t_dn} support break. Watch for bounce at entry zone before any call entry. This is a day to sit out or hedge."
        elif chg<-1:
            return f"⚠️ WEAK: {chg:.2f}% pullback on {vr:.1f}x volume. Wait for stabilization before entering calls. Support at ${t_dn}. Entry valid only if price reclaims today's open with volume. Otherwise stay flat."
        else:
            return f"➡️ NEUTRAL: Tight {chg:+.2f}% range on {vr:.1f}x volume — low conviction. Market makers in control. Wait for clean breakout above ${t_up} or breakdown below ${t_dn} with 2x volume before committing capital."

    elif timeframe=='week':
        t_up=round(price*1.055,2); t_dn=round(price*0.965,2)
        if rng>0.75 and chg>=0:
            return f"📈 BULLISH WEEK: In top {rng*100:.0f}% of 52-week range with positive momentum. Weekly target ${t_up} (+5.5%). Key catalyst: Friday options expiry could accelerate move. Risk: profit-taking near 52-week high ${hi52:.2f}. Hold calls through Wednesday — reassess Thursday."
        elif rng<0.4 or chg<-2:
            return f"📉 BEARISH WEEK: Technical weakness in lower {rng*100:.0f}% of range. Watch ${t_dn} as critical support. If breaks on volume, puts target ${round(price*0.94,2)}. Recovery needs strong volume above current level. Avoid calls until price stabilizes for 2+ days."
        else:
            return f"↔️ SIDEWAYS WEEK: Mixed signals in mid-range ({rng*100:.0f}% of 52-week range). Expected range ${t_dn}–${t_up}. Options decay works against you here — avoid buying premium. Best play: wait for directional catalyst or earnings event. Friday expiry adds pin risk."

    else:  # year
        bull=round(hi52*1.18,2); base=round(price*1.12,2); bear=round(lo52*1.15,2)
        return f"🎯 YEAR-END OUTLOOK: Bull case ${bull} (+18% from 52-wk high ${hi52:.2f}) if AI/sector tailwinds sustain and earnings beat consensus. Base case ${base} (+12% from current). Bear case ${bear} on macro deterioration. Current position {((price-lo52)/(hi52-lo52)*100):.0f}% through 52-week range. {'Mega-cap with $'+str(round(mc/1e9,0))+'B market cap = institutional support floor.' if mc>50e9 else 'Mid-cap volatility means wider outcome range.'} Key risk: Fed rate path and Q3/Q4 earnings season."

def get_ai_prediction(symbol, d, news, timeframe):
    price = d.get('regularMarketPrice',0)
    chg   = d.get('regularMarketChangePercent',0)
    hi52  = d.get('fiftyTwoWeekHigh',price)
    lo52  = d.get('fiftyTwoWeekLow',price*0.7)
    mc    = d.get('marketCap',0)
    news_text = ' | '.join([n['title'] for n in news[:4]])
    tf_prompts = {
        'today': f"You are a professional day trader. {symbol} is trading at ${price} ({chg:+.2f}% today). 52-week range: ${lo52:.2f}–${hi52:.2f}. Market cap: ${mc/1e9:.0f}B. Latest news: {news_text}. Give a sharp 2-3 sentence TODAY intraday prediction: direction, specific price target, key level to watch. Be direct like a trader, not a disclaimer-heavy analyst.",
        'week':  f"You are a swing trader analyst. {symbol} at ${price} ({chg:+.2f}% today). 52-week range: ${lo52:.2f}–${hi52:.2f}. News: {news_text}. Give a 2-3 sentence END-OF-WEEK prediction: expected close price range, key catalyst, and whether to hold calls/puts through Friday. Be specific and actionable.",
        'year':  f"You are a senior equity analyst. {symbol} at ${price}. 52-week range: ${lo52:.2f}–${hi52:.2f}. Market cap: ${mc/1e9:.0f}B. News context: {news_text}. Give a 2-3 sentence YEAR-END 2025 price target with bull/bear/base case prices. Include the single biggest risk to the thesis. Be specific."
    }
    payload = {"model":"claude-sonnet-4-20250514","max_tokens":220,
               "messages":[{"role":"user","content":tf_prompts.get(timeframe,tf_prompts['today'])}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=20) as resp:
        return json.loads(resp.read())['content'][0]['text']

# ── FLASK ROUTES ───────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory('.','scanner.html')

@app.route('/scanner.html')
def scanner(): return send_from_directory('.','scanner.html')

@app.route('/quotes')
def quotes():
    syms=[s.strip().upper() for s in request.args.get('symbols','').split(',') if s.strip()]
    if not syms: return cors({"result":[]})
    print(f"\n=== SCAN: {len(syms)} symbols ===")
    return cors({"result":get_quotes(syms)})

@app.route('/prices')
def prices():
    syms=[s.strip().upper() for s in request.args.get('symbols','').split(',') if s.strip()]
    if not syms: return cors({"result":[]})
    return cors({"result":get_fast_prices(syms)})

@app.route('/history')
def history():
    sym=request.args.get('symbol','').strip().upper()
    period=request.args.get('period','1mo'); interval=request.args.get('interval','1d')
    if period not in VALID_PERIODS: period='1mo'
    if interval not in VALID_INTERVALS: interval='1d'
    return cors(get_history(sym,period,interval) or {})

@app.route('/market')
def market(): return cors({"result":get_market()})

@app.route('/news')
def news(): return cors({"items":get_news()})

@app.route('/stock-news')
def stock_news():
    sym=request.args.get('symbol','').strip().upper()
    return cors({"items":get_stock_news(sym) if sym else []})

@app.route('/predict')
def predict():
    sym=request.args.get('symbol','').strip().upper()
    tf=request.args.get('timeframe','today')
    if not sym: return cors({"prediction":"No symbol provided","ai":False})
    # Get stock data
    d={}
    try:
        info=yf.Ticker(sym).info or {}
        d={"regularMarketPrice":info.get("currentPrice") or info.get("regularMarketPrice",0),
           "regularMarketChangePercent":info.get("regularMarketChangePercent",0),
           "fiftyTwoWeekHigh":info.get("fiftyTwoWeekHigh",0),
           "fiftyTwoWeekLow":info.get("fiftyTwoWeekLow",0),
           "marketCap":info.get("marketCap",0),
           "volumeRatio":(info.get("regularMarketVolume",1))/(info.get("averageVolume",1) or 1)}
    except: pass
    news=get_stock_news(sym)
    if ANTHROPIC_KEY:
        try:
            pred=get_ai_prediction(sym,d,news,tf)
            return cors({"prediction":pred,"ai":True})
        except Exception as e:
            print(f"  AI predict error: {e}")
    return cors({"prediction":get_rule_prediction(sym,d,tf),"ai":False})

if __name__=='__main__':
    print(f"\n  Options Scanner Pro — http://localhost:{PORT}\n")
    app.run(host='0.0.0.0',port=PORT,debug=False)
