#!/usr/bin/env python3
"""
Options Scanner Pro — Flask Server
Local:  python server.py  →  http://localhost:8765
Deploy: Render.com (see README)
"""
import json, urllib.request, concurrent.futures, time, xml.etree.ElementTree as ET
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, Response

try:
    import yfinance as yf
    import pandas as pd
except ImportError:
    print("\n  ERROR: pip install yfinance flask\n"); raise SystemExit(1)

app = Flask(__name__, static_folder='.')
PORT = 8765

VALID_PERIODS   = {'1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'}
VALID_INTERVALS = {'1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'}
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

_news_cache=[]; _news_ts=0
_market_cache=[]; _market_ts=0
_stock_news_cache={}; _stock_news_ts={}

def cors(data):
    r = jsonify(data)
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

# ── Data functions ──────────────────────────────────────────────────────
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
            if len(c5)<1: continue
            price=float(c5.iloc[-1]); prev=float(c5.iloc[-2]) if len(c5)>=2 else price
            chg=((price-prev)/prev*100) if prev else 0
            info=info_map.get(sym,{})
            results.append({"symbol":sym,"shortName":info.get("shortName",sym),
                "regularMarketPrice":round(price,2),"regularMarketChangePercent":round(chg,4),
                "regularMarketVolume":int(v5.iloc[-1]) if len(v5) else 0,
                "averageDailyVolume3Month":int(v1y.mean()) if len(v1y) else 1,
                "marketCap":int(info.get("marketCap") or 0),
                "fiftyTwoWeekHigh":round(float(c1y.max()),2) if len(c1y) else round(price,2)})
            print(f"  OK  {sym:6}  ${round(price,2)}")
        except Exception as e:
            print(f"  skip {sym}: {e}")
    return results

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
        print(f"  fast-prices error: {e}"); return []

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

def get_market():
    global _market_cache, _market_ts
    if time.time()-_market_ts < 12 and _market_cache: return _market_cache
    try:
        df = yf.download(['SPY','QQQ'], period="2d", interval="1d", auto_adjust=True, progress=False)
        results=[]
        for sym in ['SPY','QQQ']:
            try:
                closes=df['Close'][sym].dropna()
                price=float(closes.iloc[-1]); prev=float(closes.iloc[-2]) if len(closes)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                results.append({"symbol":sym,"price":round(price,2),"change":round(chg,3)})
            except: pass
        _market_cache=results; _market_ts=time.time(); return results
    except Exception as e:
        print(f"  market error: {e}"); return _market_cache or []

def get_news():
    global _news_cache, _news_ts
    if time.time()-_news_ts < 90 and _news_cache: return _news_cache
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,TSLA,AAPL,META,MSFT,AMZN,COIN,PLTR&region=US&lang=en-US',
                'https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ&region=US&lang=en-US']:
        try:
            req=urllib.request.Request(url, headers={'User-Agent':UA})
            with urllib.request.urlopen(req, timeout=6) as resp:
                tree=ET.fromstring(resp.read())
                for item in tree.iter('item'):
                    t_el=item.find('title')
                    if t_el is not None and t_el.text and len(t_el.text.strip())>20:
                        pub=item.find('pubDate')
                        items.append({"title":t_el.text.strip(),"symbol":"","time":pub.text if pub else ""})
        except Exception as e:
            print(f"  RSS: {e}")
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
               {"title":"Federal Reserve officials signal data-dependent approach to rate decisions","symbol":"SPY","time":""},
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
        req=urllib.request.Request(url, headers={'User-Agent':UA})
        with urllib.request.urlopen(req, timeout=5) as resp:
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

# ── Flask Routes ────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_from_directory('.', 'scanner.html')

@app.route('/scanner.html')
def scanner():
    return send_from_directory('.', 'scanner.html')

@app.route('/quotes')
def quotes():
    syms=[s.strip().upper() for s in request.args.get('symbols','').split(',') if s.strip()]
    if not syms: return cors({"result":[]})
    print(f"\n=== SCAN: {len(syms)} symbols ===")
    return cors({"result": get_quotes(syms)})

@app.route('/prices')
def prices():
    syms=[s.strip().upper() for s in request.args.get('symbols','').split(',') if s.strip()]
    if not syms: return cors({"result":[]})
    return cors({"result": get_fast_prices(syms)})

@app.route('/history')
def history():
    sym=request.args.get('symbol','').strip().upper()
    period=request.args.get('period','1mo')
    interval=request.args.get('interval','1d')
    if period not in VALID_PERIODS: period='1mo'
    if interval not in VALID_INTERVALS: interval='1d'
    return cors(get_history(sym, period, interval) or {})

@app.route('/market')
def market():
    return cors({"result": get_market()})

@app.route('/news')
def news():
    return cors({"items": get_news()})

@app.route('/stock-news')
def stock_news():
    sym=request.args.get('symbol','').strip().upper()
    return cors({"items": get_stock_news(sym) if sym else []})

if __name__ == '__main__':
    print("\n==========================================")
    print("  Options Scanner Pro  --  Flask Server")
    print(f"  Local:   http://localhost:{PORT}")
    print("  Stop:    Ctrl+C")
    print("==========================================\n")
    app.run(host='0.0.0.0', port=PORT, debug=False)
