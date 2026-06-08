#!/usr/bin/env python3
"""Options Scanner Pro — Flask Server with Auto-Scheduler"""
import json, os, urllib.request, urllib.parse, concurrent.futures
import time, xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

try:
    import yfinance as yf
    import pandas as pd
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"\n  ERROR: pip install yfinance flask gunicorn APScheduler pytz\n  Missing: {e}\n")
    raise SystemExit(1)

app = Flask(__name__, static_folder='.')
PORT        = int(os.environ.get('PORT', 8765))
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
APP_URL        = os.environ.get('APP_URL', 'https://options-scanner-59jt.onrender.com')

ET_TZ = pytz.timezone('America/New_York')
UA    = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'

VALID_PERIODS   = {'1d','5d','1mo','3mo','6mo','1y','2y','5y','10y','ytd','max'}
VALID_INTERVALS = {'1m','2m','5m','15m','30m','60m','90m','1h','1d','5d','1wk','1mo','3mo'}

# ── Watchlists ─────────────────────────────────────────────────────────
SP500_LIST  = ['MU','NVDA','AMD','SMCI','TSLA','META','GOOGL','MSFT','AAPL','AMZN','AVGO','QCOM','CRM','NOW','ADBE','PANW','CRWD','TXN','ORCL','NFLX','UBER','COIN','PLTR','ARM','SNOW','ZS','DDOG','MDB','SHOP']
UNUSUAL_LIST= ['SPY','QQQ','COIN','MSTR','SOFI','HOOD','IONQ','RGTI','QUBT','ACHR','JOBY','RKLB','LUNR','SOUN','UPST','AFRM','APP','HIMS','GME','AMC']
SQUEEZE_LIST= ['DEVS','SDOT','LASE','CMND','SPCE','HUT','MARA','RIOT','CIFR','LCID','RIVN','OCGN','VXRT','TLRY','CGC','ITRM','GME','AMC','SOUN','BBAI','BKKT','LUNR','RKLB','ACHR','JOBY','HOOD','SOFI','HIMS','IONQ','MSTR']

# ── In-memory state ────────────────────────────────────────────────────
server_trades  = {}   # {symbol: {entry,target,stop,mode,time,symbol,name}}
cached_picks   = {}   # {mode: [{symbol,price,score,...}]}
alert_sent     = {}   # {symbol: {entry:bool,target:bool,stop:bool}}
last_scan_time = None

# ── Caches ─────────────────────────────────────────────────────────────
_news_cache={}; _market_cache=[]; _market_ts=0
_stock_news_cache={}; _stock_news_ts={}

def cors(data):
    r = jsonify(data)
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

def is_market_hours():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 570 <= t < 960   # 9:30 AM – 4:00 PM ET

def is_trading_day():
    return datetime.now(ET_TZ).weekday() < 5

# ── TELEGRAM ──────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("  Telegram: env vars not set")
        return False
    try:
        url  = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        body = json.dumps({
            'chat_id': str(TELEGRAM_CHAT).strip(),
            'text':    str(msg),
            'disable_web_page_preview': True
        }).encode()
        req = urllib.request.Request(url, data=body,
            headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
            if result.get('ok'):
                print(f"  Telegram OK: {str(msg)[:50]}")
                return True
            print(f"  Telegram API: {result.get('description','unknown')}")
            return False
    except urllib.error.HTTPError as e:
        print(f"  Telegram HTTP {e.code}: {e.read().decode()[:100]}")
        return False
    except Exception as e:
        print(f"  Telegram {type(e).__name__}: {e}")
        return False

# ── SCORING ────────────────────────────────────────────────────────────
def score_bull(q):
    p=q.get('regularMarketPrice',0); hi=q.get('fiftyTwoWeekHigh',p) or p
    lo=q.get('fiftyTwoWeekLow',p*0.7) or p*0.7
    chg=q.get('regularMarketChangePercent',0); vr=q.get('_vr',1)
    mc=q.get('marketCap',0); rng=(p-lo)/((hi-lo) or 1)
    s=50
    if chg>5:s+=25 elif chg>3:s+=19 elif chg>2:s+=14 elif chg>1:s+=9 elif chg>0:s+=4 elif chg<-3:s-=18 elif chg<-1:s-=8 else:s-=3
    if vr>5:s+=25 elif vr>3:s+=18 elif vr>2:s+=12 elif vr>1.5:s+=7 elif vr>1:s+=3 else:s-=6
    if rng>0.88:s+=20 elif rng>0.72:s+=13 elif rng>0.55:s+=7 elif rng>0.35:s+=3 else:s-=5
    if mc>200e9:s+=10 elif mc>50e9:s+=7 elif mc>10e9:s+=4 elif mc>1e9:s+=2
    return min(99,max(30,round(s)))

def score_squeeze(q):
    p=q.get('regularMarketPrice',0); mc=q.get('marketCap',0)
    if p<0.30 or mc>1e9: return 0
    chg=q.get('regularMarketChangePercent',0); vr=q.get('_vr',1)
    short_pct=q.get('shortPercentOfFloat',0); float_sh=q.get('floatShares',0)
    short_ratio=q.get('shortRatio',0); s=50
    if short_pct>40:s+=30 elif short_pct>25:s+=23 elif short_pct>15:s+=16 elif short_pct>8:s+=9 elif short_pct>3:s+=4
    if float_sh>0:
        if float_sh<2e6:s+=25 elif float_sh<5e6:s+=20 elif float_sh<10e6:s+=14 elif float_sh<20e6:s+=8 elif float_sh<50e6:s+=3 else:s-=8
    if vr>15:s+=20 elif vr>8:s+=15 elif vr>5:s+=10 elif vr>3:s+=6 elif vr>2:s+=3 else:s-=8
    if chg>15:s+=15 elif chg>8:s+=11 elif chg>4:s+=7 elif chg>1:s+=3 elif chg<-5:s-=10
    if short_ratio>5:s+=8 elif short_ratio>3:s+=4
    if 1<=p<=15:s+=5
    return min(99,max(0,round(s)))

def week_signal(q):
    chg=q.get('regularMarketChangePercent',0); vr=q.get('_vr',1)
    p=q.get('regularMarketPrice',0); hi=q.get('fiftyTwoWeekHigh',p) or p
    lo=q.get('fiftyTwoWeekLow',p*0.7) or p*0.7
    rng=(p-lo)/((hi-lo) or 1); bull=0; bear=0
    if chg>4:bull+=4 elif chg>2:bull+=3 elif chg>1:bull+=2 elif chg>0:bull+=1 elif chg<-4:bear+=4 elif chg<-2:bear+=3 elif chg<-1:bear+=2 elif chg<0:bear+=1
    if vr>2 and chg>0:bull+=2 elif vr>2 and chg<0:bear+=2 elif vr>1.5:bull+=1
    if rng>0.80:bull+=3 elif rng>0.60:bull+=2 elif rng>0.40:bull+=1 elif rng<0.20:bear+=3 elif rng<0.40:bear+=2
    d=bull-bear
    if d>=4: return '📈 BULL WEEK'
    if d<=-4: return '📉 BEAR WEEK'
    return '➡ HOLD WEEK'

# ── DATA FUNCTIONS ─────────────────────────────────────────────────────
def get_quotes(symbols):
    results=[]
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
            c5  = (df5['Close'][sym] if multi else df5['Close']).dropna()
            v5  = (df5['Volume'][sym] if multi else df5['Volume']).dropna()
            c1y = (df1y['Close'][sym] if multi else df1y['Close']).dropna()
            v1y = (df1y['Volume'][sym] if multi else df1y['Volume']).dropna()
            if len(c5)<1: continue
            price=float(c5.iloc[-1]); prev=float(c5.iloc[-2]) if len(c5)>=2 else price
            chg=((price-prev)/prev*100) if prev else 0
            info=info_map.get(sym,{})
            pre_price=float(info.get('preMarketPrice') or 0)
            post_price=float(info.get('postMarketPrice') or 0)
            pre_chg=((pre_price-price)/price*100) if pre_price and price else 0
            post_chg=((post_price-price)/price*100) if post_price and price else 0
            vol_today=int(v5.iloc[-1]) if len(v5) else 0
            avg_vol=int(v1y.mean()) if len(v1y) else 1
            vr=round(vol_today/avg_vol,1) if avg_vol else 1
            float_sh=int(info.get("floatShares") or 0)
            short_pct=float(info.get("shortPercentOfFloat") or 0)
            if 0<short_pct<1: short_pct=round(short_pct*100,2)
            results.append({
                "symbol":sym,"shortName":info.get("shortName",sym),
                "regularMarketPrice":round(price,2),
                "regularMarketChangePercent":round(chg,4),
                "regularMarketVolume":vol_today,
                "averageDailyVolume3Month":avg_vol,
                "marketCap":int(info.get("marketCap") or 0),
                "fiftyTwoWeekHigh":round(float(c1y.max()),2) if len(c1y) else round(price,2),
                "fiftyTwoWeekLow":round(float(c1y.min()),2) if len(c1y) else round(price,2),
                "preMarketPrice":round(pre_price,2),
                "preMarketChangePercent":round(pre_chg,2),
                "postMarketPrice":round(post_price,2),
                "postMarketChangePercent":round(post_chg,2),
                "marketState":info.get("marketState","CLOSED"),
                "floatShares":float_sh,
                "shortPercentOfFloat":short_pct,
                "shortRatio":float(info.get("shortRatio") or 0),
                "_vr":vr,
            })
        except Exception as e:
            print(f"  skip {sym}: {e}")
    return results

def get_fast_prices(symbols):
    try:
        df=yf.download(symbols,period="2d",interval="1d",auto_adjust=True,progress=False,threads=True)
        if df.empty: return []
        multi=isinstance(df.columns,pd.MultiIndex)
        results=[]
        for sym in symbols:
            try:
                closes=(df['Close'][sym] if multi else df['Close']).dropna()
                vols=(df['Volume'][sym] if multi else df['Volume']).dropna()
                if len(closes)<1: continue
                price=float(closes.iloc[-1]); prev=float(closes.iloc[-2]) if len(closes)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                results.append({"symbol":sym,"regularMarketPrice":round(price,2),
                    "regularMarketChangePercent":round(chg,3),"regularMarketVolume":int(vols.iloc[-1]) if len(vols) else 0})
            except: pass
        return results
    except Exception as e:
        print(f"  fast error: {e}"); return []

def get_history(symbol, period="1mo", interval="1d"):
    try:
        hist=yf.Ticker(symbol).history(period=period,interval=interval)
        if hist.empty: return None
        is_intra=interval in ('1m','2m','5m','15m','30m','60m','90m','1h')
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
        print(f"  history {symbol}: {e}"); return None

def get_market():
    global _market_cache,_market_ts
    if time.time()-_market_ts<12 and _market_cache: return _market_cache
    try:
        df=yf.download(['SPY','QQQ','BTC-USD'],period="2d",interval="1d",auto_adjust=True,progress=False)
        results=[]; multi=isinstance(df.columns,pd.MultiIndex)
        for sym in ['SPY','QQQ','BTC-USD']:
            try:
                closes=(df['Close'][sym] if multi else df['Close']).dropna()
                price=float(closes.iloc[-1]); prev=float(closes.iloc[-2]) if len(closes)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                results.append({"symbol":sym,"price":round(price,2),"change":round(chg,3)})
            except: pass
        _market_cache=results; _market_ts=time.time(); return results
    except Exception as e:
        print(f"  market: {e}"); return _market_cache or []

def get_news():
    global _news_cache
    if time.time()-_news_cache.get('ts',0)<90 and _news_cache.get('items'):
        return _news_cache['items']
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=NVDA,TSLA,AAPL,META,MSFT&region=US&lang=en-US',
                'https://feeds.finance.yahoo.com/rss/2.0/headline?s=SPY,QQQ&region=US&lang=en-US']:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=6) as resp:
                tree=ET.fromstring(resp.read())
                for item in tree.iter('item'):
                    t_el=item.find('title')
                    if t_el is not None and t_el.text and len(t_el.text.strip())>20:
                        items.append({"title":t_el.text.strip(),"symbol":"","time":""})
        except: pass
    if not items:
        items=[{"title":"Markets monitoring major tech and AI sector movements","symbol":"","time":""}]
    _news_cache={'items':items[:25],'ts':time.time()}
    return items

def get_stock_news(symbol):
    global _stock_news_cache,_stock_news_ts
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
                    items.append({"title":t_el.text.strip(),"source":"Yahoo Finance","time":""})
    except:
        try:
            raw=yf.Ticker(symbol).get_news() or yf.Ticker(symbol).news or []
            for n in raw[:5]:
                title=(n.get('title') or '').strip()
                if title and len(title)>15:
                    items.append({"title":title,"source":n.get('publisher','Yahoo Finance'),"time":""})
        except: pass
    _stock_news_cache[symbol]=items[:5]; _stock_news_ts[symbol]=now
    return items[:5]

# ── AI / RULE PREDICTIONS ──────────────────────────────────────────────
def get_rule_prediction(symbol,d,timeframe):
    price=d.get('regularMarketPrice',0); chg=d.get('regularMarketChangePercent',0)
    vr=d.get('volumeRatio',1); hi52=d.get('fiftyTwoWeekHigh',price) or price
    lo52=d.get('fiftyTwoWeekLow',price*0.7) or price*0.7; mc=d.get('marketCap',0)
    rng=((price-lo52)/(hi52-lo52)) if (hi52-lo52)>0 else 0.5
    if timeframe=='today':
        t_up=round(price*1.018,2); t_dn=round(price*0.988,2)
        if chg>3 and vr>1.5:
            return f"PRICE TARGET: ${t_up}\n\n📈 STRONG BULL: {chg:+.2f}% gain with {vr:.1f}x volume = institutional accumulation. Momentum continuation likely. Hold above entry, trail stop up."
        elif chg>1:
            return f"PRICE TARGET: ${t_up}\n\n📈 MILD BULL: +{chg:.2f}% with {vr:.1f}x volume. Target ${t_up} if volume holds. Risk: reversal to ${t_dn} if market weakens."
        elif chg<-3 and vr>1.5:
            return f"PRICE TARGET: ${t_dn}\n\n📉 BEARISH: {chg:.2f}% on {vr:.1f}x volume — distribution. Avoid longs today. Watch ${t_dn} for support."
        elif chg<-1:
            return f"PRICE TARGET: ${t_dn}\n\n⚠️ WEAK: {chg:.2f}% pullback. Wait for stabilization at ${t_dn} before entering."
        else:
            return f"PRICE TARGET: ${t_up}\n\n➡️ NEUTRAL: Tight {chg:+.2f}% range. Wait for breakout above ${t_up} or breakdown below ${t_dn} with 2x volume."
    elif timeframe=='week':
        t_up=round(price*1.055,2); t_dn=round(price*0.965,2)
        if rng>0.75 and chg>=0:
            return f"PRICE TARGET: ${t_up}\n\n📈 BULLISH WEEK: Top {rng*100:.0f}% of 52-week range. Weekly target ${t_up} (+5.5%). Risk: profit-taking near 52-wk high ${hi52:.2f}."
        elif rng<0.4 or chg<-2:
            return f"PRICE TARGET: ${t_dn}\n\n📉 BEARISH WEEK: Technical weakness. Watch ${t_dn} support. Recovery needs strong volume."
        else:
            return f"PRICE TARGET: ${t_up}\n\n↔️ SIDEWAYS WEEK: Range ${t_dn}–${t_up}. Wait for directional break with volume."
    else:
        bull=round(hi52*1.18,2); base=round(price*1.12,2); bear=round(lo52*1.15,2)
        pos=round(((price-lo52)/(hi52-lo52)*100),0) if (hi52-lo52)>0 else 50
        cap=f"${round(mc/1e9,1)}B cap" if mc>1e9 else "small-cap"
        return f"PRICE TARGET: ${base} base · ${bull} bull · ${bear} bear\n\n🎯 YEAR-END: {pos:.0f}% of 52-week range. {cap}. Key risk: macro conditions and earnings revisions."

def get_ai_prediction(symbol,d,news,timeframe):
    price=d.get('regularMarketPrice',0); chg=d.get('regularMarketChangePercent',0)
    hi52=d.get('fiftyTwoWeekHigh',price); lo52=d.get('fiftyTwoWeekLow',price*0.7); mc=d.get('marketCap',0)
    news_text=' | '.join([n['title'] for n in news[:4]])
    tf_prompts={
        'today':f"You are a professional day trader. {symbol} at ${price} ({chg:+.2f}% today). 52-week range: ${lo52:.2f}-${hi52:.2f}. Market cap: ${mc/1e9:.0f}B. News: {news_text}. Start with exactly: PRICE TARGET: $[number]\n\nThen 2-3 sharp sentences on direction, key level, hold or exit. Be direct.",
        'week': f"You are a swing trader. {symbol} at ${price} ({chg:+.2f}% today). Range: ${lo52:.2f}-${hi52:.2f}. News: {news_text}. Start with: PRICE TARGET: $[number]\n\nThen 2-3 sentences on end-of-week price and hold/exit recommendation.",
        'year': f"You are a senior analyst. {symbol} at ${price}. Range: ${lo52:.2f}-${hi52:.2f}. Cap: ${mc/1e9:.0f}B. News: {news_text}. Start with: PRICE TARGET: $[base] base · $[bull] bull · $[bear] bear\n\nThen 2 sentences on year-end thesis and biggest risk."
    }
    payload={"model":"claude-sonnet-4-20250514","max_tokens":220,
              "messages":[{"role":"user","content":tf_prompts.get(timeframe,tf_prompts['today'])}]}
    req=urllib.request.Request("https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
    with urllib.request.urlopen(req,timeout=30) as resp:
        return json.loads(resp.read())['content'][0]['text']

# ── SCHEDULER JOBS ─────────────────────────────────────────────────────
def job_morning_scan():
    """8:00 AM ET Mon-Fri — Auto-scan and send top picks to Telegram"""
    global cached_picks, last_scan_time
    print("  SCHEDULER: Morning scan starting...")
    send_telegram(
        "🌅 Good morning! Options Scanner starting morning scan...\n"
        f"⏰ {datetime.now(ET_TZ).strftime('%A, %b %d %Y')}\n"
        "Results will follow in ~3 minutes"
    )
    try:
        quotes = get_quotes(SP500_LIST[:20])  # Top 20 for speed
        picks = sorted([q for q in quotes if q.get('regularMarketPrice',0)>0],
                      key=lambda q: score_bull(q), reverse=True)[:3]
        cached_picks['sp'] = picks
        last_scan_time = time.time()

        lines = [f"📊 MORNING TOP PICKS — {datetime.now(ET_TZ).strftime('%b %d')}",
                 f"Open scanner: {APP_URL}\n"]
        for i,q in enumerate(picks):
            sym=q['symbol']; p=q['regularMarketPrice']
            entry=round(p*0.995,2); target=round(p*1.072,2); stop=round(p*0.962,2)
            chg=q.get('regularMarketChangePercent',0)
            ws=week_signal(q)
            lines.append(f"{'🥇' if i==0 else '🥈' if i==1 else '🥉'} {sym} ${p:.2f} ({chg:+.2f}%)")
            lines.append(f"   {ws} | Score {score_bull(q)}/100")
            lines.append(f"   Entry ${entry} | Target ${target} | Stop ${stop}")
        send_telegram('\n'.join(lines))
    except Exception as e:
        send_telegram(f"⚠️ Morning scan error: {str(e)[:100]}")
        print(f"  SCHEDULER morning scan error: {e}")

def job_market_open():
    """9:30 AM ET Mon-Fri"""
    send_telegram(
        "🔔 MARKET OPEN\n\n"
        "✅ Confirm your picks have VOLUME in first 10 min\n"
        "⚠️ No volume = no trade, wait\n"
        f"Open scanner: {APP_URL}"
    )

def job_market_close():
    """4:01 PM ET Mon-Fri — Send P&L summary"""
    lines=["📊 MARKET CLOSED\n"]
    if server_trades:
        lines.append("Your tracked trades:")
        for sym,t in server_trades.items():
            try:
                quotes=get_fast_prices([sym])
                if quotes:
                    price=quotes[0]['regularMarketPrice']
                    pnl=((price-t['entry'])/t['entry']*100)
                    icon='✅' if pnl>0 else '🔴'
                    lines.append(f"{icon} {sym}: ${price:.2f} | P&L: {pnl:+.1f}%")
            except: pass
    else:
        lines.append("No trades tracked today.\nCheck I'm In tomorrow to track your trades.")
    lines.append(f"\nSee you tomorrow at 8 AM! {APP_URL}")
    send_telegram('\n'.join(lines))

def job_evening_squeeze():
    """8:00 PM ET Mon-Fri — Run squeeze scan and send watchlist"""
    global cached_picks
    print("  SCHEDULER: Evening squeeze scan...")
    send_telegram("🎯 Running evening squeeze scan for tomorrow's watchlist...")
    try:
        quotes=get_quotes(SQUEEZE_LIST[:25])
        picks=sorted([q for q in quotes if score_squeeze(q)>0 and q.get('regularMarketPrice',0)>=0.30],
                    key=lambda q: score_squeeze(q), reverse=True)[:4]
        cached_picks['squeeze']=picks

        lines=[f"🎯 TONIGHT'S SQUEEZE WATCHLIST — {datetime.now(ET_TZ).strftime('%b %d')}",
               "Set alerts for 9:35 AM open\n"]
        for i,q in enumerate(picks):
            sym=q['symbol']; p=q['regularMarketPrice']
            chg=q.get('regularMarketChangePercent',0)
            vr=q.get('_vr',1); short=q.get('shortPercentOfFloat',0)
            fl=q.get('floatShares',0)
            float_str=f"{fl/1e6:.1f}M" if fl>1e6 else f"{fl/1e3:.0f}K" if fl else '?'
            lines.append(f"#{i+1} {sym} ${p:.2f} ({chg:+.1f}%)")
            lines.append(f"   Vol {vr:.1f}x | Short {short:.0f}% | Float {float_str}")
            lines.append(f"   Target ${p*1.25:.2f} (+25%) | Stop ${p*0.92:.2f} (-8%)")
        lines.append(f"\nOpen scanner: {APP_URL}")
        send_telegram('\n'.join(lines))
    except Exception as e:
        send_telegram(f"⚠️ Squeeze scan error: {str(e)[:100]}")

def job_monitor_prices():
    """Every 2 min during market hours — check server-tracked trades"""
    if not server_trades: return
    syms=list(server_trades.keys())
    try:
        quotes=get_fast_prices(syms)
        for q in quotes:
            sym=q['symbol']; t=server_trades.get(sym)
            if not t: continue
            price=q['regularMarketPrice']
            pnl=((price-t['entry'])/t['entry']*100)
            prev_alert=alert_sent.get(sym,{})
            # Target hit
            if price>=t['target'] and not prev_alert.get('target'):
                send_telegram(
                    f"💰 TARGET HIT — {sym}\n"
                    f"Price: ${price:.2f} | Entry: ${t['entry']:.2f}\n"
                    f"P&L: +{pnl:.1f}% ✅\n"
                    f"SELL NOW — take profit!"
                )
                alert_sent.setdefault(sym,{})['target']=True
            # Stop hit
            elif price<=t['stop'] and not prev_alert.get('stop'):
                send_telegram(
                    f"🛑 STOP HIT — {sym}\n"
                    f"Price: ${price:.2f} | Entry: ${t['entry']:.2f}\n"
                    f"P&L: {pnl:.1f}% ⚠️\n"
                    f"EXIT NOW — stop loss triggered!"
                )
                alert_sent.setdefault(sym,{})['stop']=True
            # Entry zone alert
            elif abs(price-t['entry'])/t['entry']<0.005 and not prev_alert.get('entry'):
                send_telegram(
                    f"🎯 ENTRY ZONE — {sym}\n"
                    f"Price: ${price:.2f} (near entry ${t['entry']:.2f})\n"
                    f"Target: ${t['target']:.2f} | Stop: ${t['stop']:.2f}"
                )
                alert_sent.setdefault(sym,{})['entry']=True
            # Reset alerts if price moves away from triggered zones
            if price<t['target']*0.98:
                alert_sent.get(sym,{}).pop('target',None)
            if price>t['stop']*1.02:
                alert_sent.get(sym,{}).pop('stop',None)
    except Exception as e:
        print(f"  Monitor error: {e}")

def job_keepalive():
    """Every 10 min — self-ping to prevent Render sleep during market hours"""
    if not is_market_hours(): return
    try:
        req=urllib.request.Request(f"{APP_URL}/ping",headers={'User-Agent':UA})
        urllib.request.urlopen(req,timeout=8)
        print("  Keepalive ping OK")
    except Exception as e:
        print(f"  Keepalive: {e}")

# ── FLASK ROUTES ───────────────────────────────────────────────────────
@app.route('/')
def index(): return send_from_directory('.','scanner.html')
@app.route('/scanner.html')
def scanner_html(): return send_from_directory('.','scanner.html')
@app.route('/ping')
def ping(): return cors({"ok":True,"time":datetime.now(ET_TZ).strftime('%H:%M:%S ET')})
@app.route('/status')
def status():
    return cors({
        "ok":True,"market_hours":is_market_hours(),"trading_day":is_trading_day(),
        "server_trades":len(server_trades),"cached_picks":{k:len(v) for k,v in cached_picks.items()},
        "last_scan":datetime.fromtimestamp(last_scan_time,ET_TZ).strftime('%H:%M ET') if last_scan_time else "Never",
        "time":datetime.now(ET_TZ).strftime('%H:%M:%S ET')
    })

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

@app.route('/track', methods=['POST','GET','DELETE'])
def track():
    """Register/remove server-side trade tracking"""
    sym=request.args.get('symbol','').strip().upper()
    if not sym: return cors({"ok":False,"msg":"No symbol"})
    if request.method=='DELETE':
        server_trades.pop(sym,None)
        alert_sent.pop(sym,None)
        print(f"  Untracked {sym}")
        return cors({"ok":True,"msg":f"Untracked {sym}"})
    # POST or GET = register trade
    entry=float(request.args.get('entry',0) or 0)
    target=float(request.args.get('target',0) or 0)
    stop=float(request.args.get('stop',0) or 0)
    mode=request.args.get('mode','sp')
    name=request.args.get('name',sym)
    if not entry: return cors({"ok":False,"msg":"No entry price"})
    server_trades[sym]={'entry':entry,'target':target,'stop':stop,'mode':mode,'name':name,'time':datetime.now(ET_TZ).strftime('%H:%M ET')}
    alert_sent[sym]={}
    print(f"  Tracking {sym} entry=${entry} target=${target} stop=${stop}")
    send_telegram(
        f"✅ Now tracking {sym}\n"
        f"Entry: ${entry} | Target: ${target} | Stop: ${stop}\n"
        f"Mode: {mode.upper()} | Added at {server_trades[sym]['time']}\n"
        f"You'll get alerts even if browser is closed!"
    )
    return cors({"ok":True,"msg":f"Tracking {sym}"})

@app.route('/predict')
def predict():
    sym=request.args.get('symbol','').strip().upper()
    tf=request.args.get('timeframe','today')
    if not sym: return cors({"prediction":"No symbol","ai":False})
    price=float(request.args.get('price',0) or 0)
    chg=float(request.args.get('chg',0) or 0)
    hi52=float(request.args.get('hi52',0) or 0)
    lo52=float(request.args.get('lo52',0) or 0)
    vr=float(request.args.get('vr',1) or 1)
    mc=int(float(request.args.get('mc',0) or 0))
    if not price:
        try:
            info=yf.Ticker(sym).info or {}
            price=float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
            chg=float(info.get("regularMarketChangePercent",0) or 0)
            if abs(chg)<1: chg*=100
            hi52=float(info.get("fiftyTwoWeekHigh") or 0)
            lo52=float(info.get("fiftyTwoWeekLow") or 0)
            mc=int(info.get("marketCap") or 0)
            vol=int(info.get("regularMarketVolume") or 1); avg=int(info.get("averageVolume") or 1)
            vr=round(vol/avg,1) if avg else 1
        except: pass
    d={"regularMarketPrice":price,"regularMarketChangePercent":chg,
       "fiftyTwoWeekHigh":hi52,"fiftyTwoWeekLow":lo52,"marketCap":mc,"volumeRatio":vr}
    news=get_stock_news(sym)
    if ANTHROPIC_KEY:
        try:
            pred=get_ai_prediction(sym,d,news,tf)
            return cors({"prediction":pred,"ai":True})
        except Exception as e:
            print(f"  AI: {e}")
    return cors({"prediction":get_rule_prediction(sym,d,tf),"ai":False})

@app.route('/notify')
def notify():
    msg=request.args.get('msg','')
    ok=send_telegram(msg) if msg else False
    return cors({"ok":ok})

@app.route('/test-telegram')
def test_telegram():
    msg=(
        "Options Scanner Pro - Telegram Working!\n\n"
        "Abiy Kassa · St Louis MO\n\n"
        "Auto-alerts active for:\n"
        "  Entry zone reached\n"
        "  Target hit - take profit\n"
        "  Stop hit - exit now\n"
        "  8:00 AM morning scan picks\n"
        "  8:00 PM squeeze watchlist\n\n"
        f"App: {APP_URL}"
    )
    ok=send_telegram(msg)
    if ok: return cors({"ok":True,"msg":"Sent! Check your Telegram"})
    ok2=send_telegram("Options Scanner Pro - test message")
    if ok2: return cors({"ok":True,"msg":"Sent (plain)! Check Telegram"})
    tp=TELEGRAM_TOKEN[:10]+"..." if TELEGRAM_TOKEN else "NOT SET"
    cp=str(TELEGRAM_CHAT)[:6]+"..." if TELEGRAM_CHAT else "NOT SET"
    return cors({"ok":False,"msg":f"Failed. Token:{tp} Chat:{cp} — press START on your bot"})

# ── START SCHEDULER ────────────────────────────────────────────────────
def start_scheduler():
    sched=BackgroundScheduler(timezone=ET_TZ)
    # Morning scan 8:00 AM ET Mon-Fri
    sched.add_job(job_morning_scan,'cron',day_of_week='mon-fri',hour=8,minute=0)
    # Market open alert 9:30 AM ET
    sched.add_job(job_market_open,'cron',day_of_week='mon-fri',hour=9,minute=30)
    # Market close summary 4:01 PM ET
    sched.add_job(job_market_close,'cron',day_of_week='mon-fri',hour=16,minute=1)
    # Evening squeeze scan 8:00 PM ET
    sched.add_job(job_evening_squeeze,'cron',day_of_week='mon-fri',hour=20,minute=0)
    # Price monitoring every 2 min during market hours
    sched.add_job(job_monitor_prices,'cron',day_of_week='mon-fri',
                  hour='9-15',minute='*/2')
    # Self-keepalive every 10 min (prevents Render sleep during market)
    sched.add_job(job_keepalive,'interval',minutes=10)
    sched.start()
    print(f"  Scheduler started — {len(sched.get_jobs())} jobs")
    return sched

# Start scheduler when module loads (works with gunicorn AND direct python)
import atexit
try:
    _scheduler = start_scheduler()
    atexit.register(lambda: _scheduler.shutdown(wait=False))
except Exception as _se:
    print(f"  Scheduler start error: {_se}")

if __name__=='__main__':
    print("\n==========================================")
    print("  Options Scanner Pro — Auto-Scheduler")
    print(f"  Local:   http://localhost:{PORT}")
    print("==========================================")
    print(f"  Telegram: {'✅ configured' if TELEGRAM_TOKEN else '❌ not set'}")
    app.run(host='0.0.0.0',port=PORT,debug=False)
