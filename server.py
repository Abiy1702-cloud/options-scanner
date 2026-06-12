#!/usr/bin/env python3
"""AutoTrade Pro — Alpaca + AI Chat + Custom Watchlist"""
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
        print(f"  Alpaca error: {e}")

app = Flask(__name__, static_folder='.')

# ── WATCHLISTS ─────────────────────────────────────────────────────────
# Base watchlist — includes leveraged ETFs (3x = best for day trading %)
BASE_WATCHLIST = [
    # Leveraged ETFs — 3x daily moves, best for hitting 10% target
    'SOXL','TQQQ','UPRO','TECL','LABU','FNGU','CURE',
    # High-beta semiconductors
    'NVDA','AMD','MU','SMCI','ARM','AVGO',
    # High-beta tech / momentum
    'META','TSLA','COIN','MSTR','PLTR','APP',
    # Large cap tech
    'AAPL','MSFT','GOOGL','AMZN','NFLX'
]
# User's custom watchlist (up to 5 stocks) — stored in memory
custom_watchlist = []
# Chat history
chat_history = []

# ── PRICE CACHE ───────────────────────────────────────────────────────
_prices   = {}
_prev     = {}
_price_lock = threading.Lock()
_cache_ts   = 0

def get_rt_prices(syms):
    out = {}
    if alpaca_data and syms:
        try:
            req    = StockLatestTradeRequest(symbol_or_symbols=syms)
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
            print(f"  Alpaca prices: {e}")
    try:
        df = yf.download(syms, period='2d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if not len(cl): continue
                p    = float(cl.iloc[-1])
                prev = float(cl.iloc[-2]) if len(cl)>=2 else p
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
            stock_syms = [s for s in syms if s != 'BTC-USD']
            result = get_rt_prices(stock_syms)
            # BTC via yfinance always
            try:
                df = yf.download(['BTC-USD'], period='2d', interval='1d', auto_adjust=True, progress=False)
                cl = df['Close'].dropna() if not isinstance(df.columns, pd.MultiIndex) else df['Close']['BTC-USD'].dropna()
                if len(cl):
                    p=float(cl.iloc[-1]); prev=float(cl.iloc[-2]) if len(cl)>=2 else p
                    result['BTC-USD']={'symbol':'BTC-USD','price':round(p,2),'pct':round((p-prev)/prev*100,3),'change':round(p-prev,4),'source':'yfinance','time':datetime.now(ET_TZ).strftime('%H:%M:%S')}
            except: pass
            with _price_lock:
                _prices.update(result)
                _cache_ts = time.time()
        except Exception as e:
            print(f"  Price thread: {e}")
        time.sleep(interval)

def cp(sym):
    with _price_lock: return _prices.get(sym)
def all_prices():
    with _price_lock: return dict(_prices)

def load_prev_closes():
    syms = list({'QQQ','SPY','NVDA','AMD','SOXL','TQQQ'} | set(custom_watchlist))
    try:
        df = yf.download(syms, period='5d', interval='1d', auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl)>=2: _prev[sym] = float(cl.iloc[-2])
            except: pass
    except: pass

# ── HELPERS ────────────────────────────────────────────────────────────
def is_market_open():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    return n.weekday()<5 and 570<=t<960
def is_trading_day(): return datetime.now(ET_TZ).weekday()<5
def now_et(): return datetime.now(ET_TZ).strftime('%H:%M ET')
def today_str(): return datetime.now(ET_TZ).strftime('%a %b %d, %Y')

# ── TELEGRAM ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body=json.dumps({'chat_id':str(TELEGRAM_CHAT).strip(),'text':str(msg),'disable_web_page_preview':True}).encode()
        req=urllib.request.Request(f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',data=body,headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req,timeout=12) as r:
            return json.loads(r.read()).get('ok',False)
    except Exception as e:
        print(f"  Telegram: {e}"); return False

# ── PROFESSIONAL DAY TRADE SCORING ────────────────────────────────────
def score_day_trade(q, spy_pct=0, qqq_pct=0):
    """
    Professional day-trade scoring optimized for 10%+ intraday gains.
    Weights: Pre-market gap (30%) > Volume surge (25%) > Today momentum (20%)
             > Volatility/leverage factor (15%) > Market alignment (10%)
    """
    p      = q.get('regularMarketPrice',0)
    chg    = q.get('regularMarketChangePercent',0)
    vr     = q.get('_vr',1)
    pre    = q.get('preMarketChangePercent',0)
    hi52   = q.get('fiftyTwoWeekHigh',p) or p
    lo52   = q.get('fiftyTwoWeekLow',p*0.5) or p*0.5
    mc     = q.get('marketCap',0)
    sym    = q.get('symbol','')
    if p < 1: return 0  # skip penny stocks

    score = 0

    # ── 1. PRE-MARKET GAP (30 pts) ─ strongest day-trade signal ────────
    # A stock gapping up 3%+ pre-market with volume = institutional conviction
    if   pre > 10: score += 30
    elif pre > 7:  score += 26
    elif pre > 5:  score += 22
    elif pre > 3:  score += 17
    elif pre > 1:  score += 10
    elif pre > 0:  score += 4
    elif pre < -3: score -= 15
    elif pre < -1: score -= 5

    # ── 2. VOLUME SURGE (25 pts) ─ confirms institutional money ────────
    # Volume 3x+ average = big money moving in, best trades happen here
    if   vr > 10: score += 25
    elif vr > 5:  score += 20
    elif vr > 3:  score += 15
    elif vr > 2:  score += 9
    elif vr > 1.5:score += 4
    else:         score -= 8   # low volume = no conviction, skip

    # ── 3. TODAY'S MOMENTUM (20 pts) ───────────────────────────────────
    if   chg > 8:  score += 20
    elif chg > 5:  score += 16
    elif chg > 3:  score += 11
    elif chg > 1:  score += 6
    elif chg > 0:  score += 2
    elif chg < -5: score -= 18
    elif chg < -2: score -= 8
    elif chg < 0:  score -= 2

    # ── 4. VOLATILITY / LEVERAGE FACTOR (15 pts) ───────────────────────
    # 3x ETFs (SOXL/TQQQ) have 200%+ annual range → best for big daily moves
    # High-beta stocks (NVDA/TSLA) have 100%+ range
    annual_range = ((hi52-lo52)/lo52*100) if lo52>0 else 0
    if   annual_range > 300: score += 15  # 3x leveraged ETF
    elif annual_range > 200: score += 12  # 2-3x ETF or very high beta
    elif annual_range > 100: score += 8   # high beta stock
    elif annual_range > 60:  score += 4   # moderate beta
    elif annual_range < 20:  score -= 5   # boring/low beta

    # ── 5. MARKET ALIGNMENT (10 pts) ───────────────────────────────────
    # Trade WITH the market tide — don't fight it
    # If SPY + QQQ both up, go long confidently
    market_bias = (spy_pct + qqq_pct) / 2
    if   market_bias > 1:  score += 10
    elif market_bias > 0.3:score += 6
    elif market_bias > 0:  score += 2
    elif market_bias < -1: score -= 12  # market falling = very risky
    elif market_bias < -0.3:score -= 5

    # ── BONUS: Leveraged ETF confirmation ──────────────────────────────
    # If it's a 3x ETF AND market is up, multiply potential
    is_3x = sym in ('SOXL','TQQQ','UPRO','TECL','LABU','FNGU','CURE','SPXL','UDOW')
    if is_3x and market_bias > 0 and chg > 2:
        score += 8   # leveraged ETF + up market + momentum = perfect combo

    return min(99, max(0, round(score)))

def week_sig(q):
    chg=q.get('regularMarketChangePercent',0); vr=q.get('_vr',1)
    p=q.get('regularMarketPrice',0); hi=q.get('fiftyTwoWeekHigh',p) or p
    lo=q.get('fiftyTwoWeekLow',p*0.7) or p*0.7
    rng=(p-lo)/((hi-lo) or 1); bull=bear=0
    if chg>4: bull+=4
    elif chg>2: bull+=3
    elif chg>0: bull+=1
    elif chg<-4: bear+=4
    elif chg<-2: bear+=3
    elif chg<0: bear+=1
    if vr>2 and chg>0: bull+=2
    elif vr>2 and chg<0: bear+=2
    if rng>0.8: bull+=3
    elif rng>0.6: bull+=2
    elif rng<0.2: bear+=3
    elif rng<0.4: bear+=2
    d=bull-bear
    return '📈 BULL WEEK' if d>=4 else '📉 BEAR WEEK' if d<=-4 else '➡ HOLD WEEK'

def get_full_quotes(symbols):
    results=[]
    try:
        df5 =yf.download(symbols,period='5d', interval='1d',auto_adjust=True,progress=False)
        df1y=yf.download(symbols,period='1y', interval='1d',auto_adjust=True,progress=False)
        def fi(s):
            try: return s, yf.Ticker(s).info or {}
            except: return s,{}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            info_map=dict(ex.map(fi,symbols))
        multi=isinstance(df5.columns,pd.MultiIndex)
        for sym in symbols:
            try:
                c5 =(df5['Close'][sym] if multi else df5['Close']).dropna()
                v5 =(df5['Volume'][sym] if multi else df5['Volume']).dropna()
                c1y=(df1y['Close'][sym] if multi else df1y['Close']).dropna()
                v1y=(df1y['Volume'][sym] if multi else df1y['Volume']).dropna()
                if not len(c5): continue
                price=float(c5.iloc[-1]); prev=float(c5.iloc[-2]) if len(c5)>=2 else price
                chg=((price-prev)/prev*100) if prev else 0
                info=info_map.get(sym,{}); avg=int(v1y.mean()) if len(v1y) else 1
                vr=round(int(v5.iloc[-1])/avg,1) if avg and len(v5) else 1
                pre_p=float(info.get('preMarketPrice') or 0)
                pre_chg=((pre_p-price)/price*100) if pre_p and price else 0
                results.append({
                    'symbol':sym,'shortName':info.get('shortName',sym),
                    'regularMarketPrice':round(price,2),
                    'regularMarketChangePercent':round(chg,4),
                    'marketCap':int(info.get('marketCap') or 0),
                    'fiftyTwoWeekHigh':round(float(c1y.max()),2) if len(c1y) else price,
                    'fiftyTwoWeekLow':round(float(c1y.min()),2) if len(c1y) else price,
                    'preMarketChangePercent':round(pre_chg,2),
                    'preMarketPrice':round(pre_p,2),
                    '_vr':vr,
                })
            except: pass
    except Exception as e:
        print(f"  Quotes error: {e}")
    return results

# ── PAPER TRADING STATE ───────────────────────────────────────────────
paper = {
    'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
    'targets':[0.10,0.05,0.05],'stop_pct':0.03,
    'active':{},'completed':[],'total_pnl':0.0,
    'status':'waiting','picked':None,'date':None,'log':[],
    'all_candidates':[],  # all stocks scored this morning
    'better_opportunity':None,  # flag for mid-trade better pick
}

def p_log(msg):
    ts=now_et(); paper['log'].insert(0,{'time':ts,'msg':msg})
    paper['log']=paper['log'][:40]; print(f"  [{ts}] {msg}")

def paper_reset():
    if alpaca_trading:
        try: alpaca_trading.cancel_orders()
        except: pass
    paper.update({
        'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
        'active':{},'completed':[],'total_pnl':0.0,'status':'waiting',
        'picked':None,'date':datetime.now(ET_TZ).date().isoformat(),
        'log':[],'all_candidates':[],'better_opportunity':None
    })

def pick_best(spy_pct=0, qqq_pct=0):
    already=[t['symbol'] for t in paper['completed']]
    # Combine base + custom watchlist
    watchlist=list(set(BASE_WATCHLIST + custom_watchlist))
    quotes=get_full_quotes(watchlist[:20])
    # Score all candidates
    scored=[]
    for q in quotes:
        if q.get('regularMarketPrice',0)<1: continue
        if q['symbol'] in already: continue
        s=score_day_trade(q, spy_pct, qqq_pct)
        q['_score']=s; scored.append(q)
    scored.sort(key=lambda x:x['_score'],reverse=True)
    paper['all_candidates']=scored[:10]  # store top 10 for display
    if not scored: return None
    best=scored[0]
    # Log why it was picked
    p_log(f"PICKED {best['symbol']} score={best['_score']}/99 pre={best.get('preMarketChangePercent',0):+.1f}% vol={best.get('_vr',0):.1f}x chg={best.get('regularMarketChangePercent',0):+.1f}%")
    return best

def enter_trade(q, price=None):
    tnum=paper['trade_seq']
    ep=price or q['regularMarketPrice']
    cached=cp(q['symbol'])
    if cached: ep=cached['price']
    cap=paper['capital']; shrs=int(cap/ep)
    if shrs<1: return
    tp=paper['targets'][tnum-1]
    trade={
        'symbol':q['symbol'],'name':q.get('shortName',q['symbol']),
        'trade_num':tnum,'shares':shrs,'entry':round(ep,2),
        'cost':round(shrs*ep,2),'target':round(ep*(1+tp),2),
        'stop':round(ep*(1-paper['stop_pct']),2),
        'target_pct':tp,'entry_time':now_et(),
        'current':round(ep,2),'peak':round(ep,2),
        'score':q.get('_score',0),
        'via_alpaca':alpaca_trading is not None,
    }
    if alpaca_trading:
        try:
            oid=str(alpaca_trading.submit_order(MarketOrderRequest(
                symbol=q['symbol'],qty=shrs,
                side=OrderSide.BUY,time_in_force=TimeInForce.DAY
            )).id)
            trade['order_id']=oid
        except Exception as e:
            p_log(f"Alpaca order error: {e}")
    paper['active'][q['symbol']]=trade
    paper['status']='entered'; paper['capital']=0; paper['better_opportunity']=None
    p_log(f"ENTERED {q['symbol']} {shrs}sh @ ${ep:.2f} target=${trade['target']:.2f}")
    tg(
        f"📈 TRADE {tnum} ENTERED\n{'━'*22}\n"
        f"Stock:   {q['symbol']} — {q.get('shortName','')}\n"
        f"Shares:  {shrs} @ ${ep:.2f}\n"
        f"Cost:    ${shrs*ep:,.2f}\n{'━'*22}\n"
        f"Target:  ${trade['target']:.2f} (+{tp*100:.0f}%)\n"
        f"Stop:    ${trade['stop']:.2f} (-{paper['stop_pct']*100:.0f}%)\n"
        f"Why:     Score {q.get('_score',0)}/99 | {week_sig(q)}\n{'━'*22}\n"
        f"Dashboard: {APP_URL}"
    )

def close_trade(sym, reason='target'):
    t=paper['active'].pop(sym,None)
    if not t: return
    cached=cp(sym)
    sell=round(cached['price'] if cached else t['current'],2)
    if alpaca_trading:
        try: alpaca_trading.close_position(sym)
        except: pass
    pnl=round((sell-t['entry'])*t['shares'],2)
    pnl_pct=round((sell-t['entry'])/t['entry']*100,2)
    proceeds=round(sell*t['shares'],2)
    paper['completed'].append({**t,'exit':sell,'exit_time':now_et(),'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    paper['total_pnl']+=pnl; paper['capital']=proceeds
    p_log(f"CLOSED {sym} {reason.upper()} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    tg(
        f"{'💰'if reason=='target'else'🛑'if reason=='stop'else'🔔'} TRADE {t['trade_num']} CLOSED — {reason.upper()}\n{'━'*22}\n"
        f"Stock:  {sym}\nSold:   {t['shares']}sh @ ${sell:.2f}\n"
        f"Entry:  ${t['entry']:.2f} → Exit: ${sell:.2f}\n"
        f"P&L:    {'✅ +' if pnl>=0 else '🔴 '}${abs(pnl):.2f} ({pnl_pct:+.2f}%)\n"
        f"Total:  {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:,.2f}"
    )
    nxt=paper['trade_seq']+1
    if nxt<=3 and is_market_open():
        paper['trade_seq']=nxt; paper['status']='waiting'
        tg(f"🔍 Looking for Trade {nxt}/3 (+{paper['targets'][nxt-1]*100:.0f}% target)...")
        threading.Thread(target=_pick_and_enter,daemon=True).start()
    else:
        paper['status']='done'
        if nxt>3: tg(f"✅ All 3 trades done!\nTotal: {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:,.2f}\n{APP_URL}")

def _pick_and_enter():
    time.sleep(5)
    spy=cp('SPY'); qqq=cp('QQQ')
    spy_pct=spy['pct'] if spy else 0; qqq_pct=qqq['pct'] if qqq else 0
    q=pick_best(spy_pct,qqq_pct)
    if not q: tg("⚠️ No suitable stock found."); return
    paper['picked']=q; paper['status']='picked'
    _notify_pick(q)
    time.sleep(3); enter_trade(q)

def _notify_pick(q):
    tnum=paper['trade_seq']; tp=paper['targets'][tnum-1]*100
    cached=cp(q['symbol']); price=cached['price'] if cached else q['regularMarketPrice']
    shrs=int(paper['capital']/price) if price else 0
    # Build candidates summary
    alts=paper['all_candidates'][:4]
    alt_lines='\n'.join(f"   #{i+1} {a['symbol']} score={a['_score']} chg={a.get('regularMarketChangePercent',0):+.1f}% vol={a.get('_vr',0):.1f}x" for i,a in enumerate(alts))
    tg(
        f"🔍 TRADE {tnum} PICKED\n{'━'*22}\n"
        f"Stock:  {q['symbol']} — {q.get('shortName','')}\n"
        f"Price:  ${price:.2f} | Pre-mkt: {q.get('preMarketChangePercent',0):+.1f}%\n"
        f"Signal: {week_sig(q)} | Score {q.get('_score',0)}/99\n"
        f"Vol:    {q.get('_vr',0):.1f}x average\n{'━'*22}\n"
        f"Capital: ${paper['capital']:,.2f} | ~{shrs} shares\n"
        f"Target: ${price*(1+paper['targets'][tnum-1]):.2f} (+{tp:.0f}%)\n"
        f"Stop:   ${price*(1-paper['stop_pct']):.2f} (-3%)\n{'━'*22}\n"
        f"Top candidates this morning:\n{alt_lines}"
    )

# ── MID-TRADE QUALITY MONITOR ─────────────────────────────────────────
def check_better_opportunity():
    """Check if a significantly better stock appeared during the trade"""
    if not paper['active']: return
    active_sym = list(paper['active'].keys())[0]
    t = paper['active'][active_sym]
    cur = cp(active_sym); price = cur['price'] if cur else t['current']
    pnl_pct = (price - t['entry']) / t['entry'] * 100
    if pnl_pct > 1: return
    spy = cp('SPY'); qqq = cp('QQQ')
    spy_pct = spy['pct'] if spy else 0; qqq_pct = qqq['pct'] if qqq else 0
    cands = paper.get('all_candidates', [])
    for c in cands:
        if c['symbol'] == active_sym: continue
        live = cp(c['symbol'])
        if live: c['regularMarketChangePercent'] = live['pct']
        c['_score'] = score_day_trade(c, spy_pct, qqq_pct)
    cands.sort(key=lambda x: x.get('_score', 0), reverse=True)
    if not cands: return
    best = cands[0]
    if best.get('_score', 0) > t.get('score', 0) + 20:
        better_sym = best['symbol']
        better_score = best['_score']
        better_chg = best.get('regularMarketChangePercent', 0)
        better_vr = best.get('_vr', 0)
        msg = (
            "!! BETTER OPPORTUNITY DETECTED !!\n"
            f"Current: {active_sym} P&L {pnl_pct:+.1f}%\n"
            f"Better:  {better_sym} score {better_score}/99 "
            f"chg {better_chg:+.1f}% vol {better_vr:.1f}x\n\n"
            f"Type switch to {better_sym} in AI chat to act."
        )

        paper['better_opportunity'] = {
            'symbol': better_sym, 'score': better_score,
            'msg': msg, 'current_pnl': round(pnl_pct, 2)
        }
        tg(msg)

# ── AI CHAT ───────────────────────────────────────────────────────────
def ai_chat(user_msg):
    """Process chat message — AI responds with market analysis or executes commands"""
    # Build context
    active=list(paper['active'].values())
    ctx={
        'date': today_str(),
        'market': {k:v for k,v in all_prices().items() if k in ('SPY','QQQ','BTC-USD')},
        'status': paper['status'],
        'trade_num': paper['trade_seq'],
        'capital': paper['capital'],
        'total_pnl': paper['total_pnl'],
        'active_trade': active[0] if active else None,
        'completed': paper['completed'],
        'top_candidates': paper.get('all_candidates',[])[:5],
        'better_opportunity': paper.get('better_opportunity'),
        'custom_watchlist': custom_watchlist,
    }
    if active:
        t=active[0]; live=cp(t['symbol'])
        if live: ctx['active_trade']['current']=live['price']
        ctx['active_trade']['live_pnl']=round((ctx['active_trade'].get('current',t['entry'])-t['entry'])*t['shares'],2)

    system_prompt = f"""You are AutoTrade Pro AI for Abiy Kassa — a professional day trading assistant.

CURRENT STATE:
- Date: {ctx['date']}
- Status: {ctx['status']} (Trade {ctx['trade_num']}/3)
- Capital: ${ctx['capital']:,.2f}
- Total P&L today: ${ctx['total_pnl']:+.2f}
- Active trade: {json.dumps(ctx['active_trade'], default=str) if ctx['active_trade'] else 'None'}
- Completed trades: {len(ctx['completed'])} trades
- Market: SPY={ctx['market'].get('SPY',{}).get('pct',0):+.2f}% QQQ={ctx['market'].get('QQQ',{}).get('pct',0):+.2f}%
- Better opportunity flag: {ctx['better_opportunity']['symbol'] if ctx['better_opportunity'] else 'None'}

TOP CANDIDATES TODAY:
{chr(10).join(f"  {c['symbol']}: score={c.get('_score',0)}/99, chg={c.get('regularMarketChangePercent',0):+.1f}%, vol={c.get('_vr',1):.1f}x" for c in ctx['top_candidates'])}

YOUR ROLE:
1. Analyze trades and market conditions professionally
2. Answer questions about the current trade and P&L
3. If user wants to execute a trade action, respond with a JSON command block
4. Be direct, concise, and professional like a trading desk analyst

COMMANDS YOU CAN SUGGEST (include in response as JSON if user confirms action):
- {{"action": "switch", "symbol": "SOXL"}} — close current trade and open new one
- {{"action": "close_all"}} — close all positions
- {{"action": "close", "symbol": "NVDA"}} — close specific position
- {{"action": "buy", "symbol": "TSLA"}} — enter a new trade
- {{"action": "set_watchlist", "symbols": ["SOXL","NVDA","AMD","TSLA","META"]}} — update custom watchlist

Only include a command JSON if user explicitly asks to execute something AND you agree it's a good decision.
IMPORTANT: Always explain your reasoning. If suggesting a trade action, explain why."""

    messages = [{'role':'user','content':m['user']} for m in chat_history[-6:]]
    # Add alternating assistant responses
    all_msgs = []
    for i, m in enumerate(chat_history[-6:]):
        all_msgs.append({'role':'user','content':m['user']})
        if m.get('ai'): all_msgs.append({'role':'assistant','content':m['ai']})
    all_msgs.append({'role':'user','content':user_msg})

    if not ANTHROPIC_KEY:
        # Rule-based fallback
        msg_lower = user_msg.lower()
        if 'pnl' in msg_lower or 'profit' in msg_lower or 'loss' in msg_lower:
            return f"Today's P&L: {'+'if paper['total_pnl']>=0 else''}${paper['total_pnl']:.2f} | Capital: ${paper['capital']:,.2f}", None
        if 'status' in msg_lower or 'trade' in msg_lower:
            if active:
                t=active[0]; live=cp(t['symbol'])
                p=live['price'] if live else t['current']
                pnl=(p-t['entry'])*t['shares']
                return f"Active trade: {t['symbol']} {t['shares']}sh @ ${t['entry']:.2f} | Now ${p:.2f} | P&L ${pnl:+.2f}", None
            return f"No active trade. Status: {paper['status']} (Trade {paper['trade_seq']}/3)", None
        if 'switch' in msg_lower:
            words=msg_lower.split(); sym=None
            for w in words:
                if w.upper() in BASE_WATCHLIST+custom_watchlist:
                    sym=w.upper(); break
            if sym:
                return f"You want to switch to {sym}. This will close the current trade and open {sym}. Please confirm: type 'confirm switch to {sym}'", {'action':'switch','symbol':sym,'pending_confirm':True}
        return "I can help with: P&L status, trade analysis, switching stocks, or setting your watchlist. What would you like to know?", None

    try:
        payload={"model":"claude-sonnet-4-20250514","max_tokens":400,
                 "system":system_prompt,"messages":all_msgs}
        req=urllib.request.Request("https://api.anthropic.com/v1/messages",
            data=json.dumps(payload).encode(),
            headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
        with urllib.request.urlopen(req,timeout=25) as r:
            text=json.loads(r.read())['content'][0]['text']
        # Extract command if present
        cmd=None
        import re
        m=re.search(r'\{[^}]*"action"[^}]*\}',text)
        if m:
            try: cmd=json.loads(m.group())
            except: pass
        return text, cmd
    except Exception as e:
        print(f"  AI chat error: {e}")
        return f"AI unavailable. Current trade: {list(paper['active'].keys())[0] if paper['active'] else 'None'} | P&L: ${paper['total_pnl']:+.2f}", None

def execute_command(cmd):
    """Execute a trade command from AI chat"""
    action=cmd.get('action','')
    if action=='switch':
        sym=cmd.get('symbol','')
        if sym and paper['active']:
            # Close current
            for s in list(paper['active'].keys()):
                close_trade(s,'manual')
            # Open new
            q=next((c for c in paper.get('all_candidates',[]) if c['symbol']==sym),None)
            if not q: q={'symbol':sym,'shortName':sym,'regularMarketPrice':(cp(sym) or {}).get('price',0),'_vr':1,'regularMarketChangePercent':0,'marketCap':10e9,'fiftyTwoWeekHigh':0,'fiftyTwoWeekLow':0,'preMarketChangePercent':0,'_score':70}
            if q.get('regularMarketPrice',0)>0:
                enter_trade(q)
                return f"✅ Switched to {sym}"
        return f"Could not switch to {sym}"
    elif action=='close_all':
        for s in list(paper['active'].keys()):
            close_trade(s,'manual')
        return "✅ All positions closed"
    elif action=='close':
        sym=cmd.get('symbol','')
        if sym and sym in paper['active']:
            close_trade(sym,'manual')
            return f"✅ Closed {sym}"
    elif action=='set_watchlist':
        global custom_watchlist
        syms=cmd.get('symbols',[])[:5]
        custom_watchlist=[s.upper() for s in syms if s]
        return f"✅ Watchlist updated: {', '.join(custom_watchlist)}"
    return "Command noted"

# ── SCHEDULER JOBS ─────────────────────────────────────────────────────
def job_morning():
    if not is_trading_day(): return
    paper_reset(); load_prev_closes()
    p_log("Good morning — AutoTrade starting")
    spy=cp('SPY'); qqq=cp('QQQ')
    spy_pct=spy['pct'] if spy else 0; qqq_pct=qqq['pct'] if qqq else 0
    market_mood="📈 BULLISH" if spy_pct>0.3 and qqq_pct>0.3 else "📉 BEARISH" if spy_pct<-0.3 else "➡ NEUTRAL"
    tg(f"🌅 AUTOTRADE STARTING — {today_str()}\n{'━'*22}\nCapital: $10,000\nMarket:  SPY {spy_pct:+.2f}% | QQQ {qqq_pct:+.2f}% {market_mood}\nPlan:    T1 +10% · T2 +5% · T3 +5%\nStop:    -3%\n{'━'*22}\nScanning {len(BASE_WATCHLIST+custom_watchlist)} stocks...\n{APP_URL}")
    if market_mood=="📉 BEARISH":
        tg("⚠️ Market is BEARISH pre-market. Being selective — only taking high-score setups.")
    q=pick_best(spy_pct,qqq_pct)
    if not q: tg("⚠️ No high-quality setup found. Will retry at 9:30 AM."); return
    paper['picked']=q; paper['status']='picked'
    _notify_pick(q)
    cached=cp(q['symbol'])
    if cached and cached['pct']>0.8:
        tg(f"⚡ Pre-market momentum {cached['pct']:+.1f}% — entering early")
        enter_trade(q)

def job_market_open():
    if not is_trading_day(): return
    spy=cp('SPY'); qqq=cp('QQQ')
    spy_pct=spy['pct'] if spy else 0; qqq_pct=qqq['pct'] if qqq else 0
    tg(f"🔔 MARKET OPEN — {today_str()}\nSPY {spy_pct:+.2f}% | QQQ {qqq_pct:+.2f}%\n{APP_URL}")
    if paper['status']=='picked' and not paper['active']:
        enter_trade(paper['picked'])
    elif paper['status']=='waiting' and paper['trade_seq']==1:
        q=pick_best(spy_pct,qqq_pct)
        if q:
            paper['picked']=q; paper['status']='picked'
            _notify_pick(q); time.sleep(5); enter_trade(q)

def job_monitor():
    if not paper['active']: return
    if alpaca_trading:
        try:
            pos={p.symbol:p for p in alpaca_trading.get_all_positions()}
            for sym,t in paper['active'].items():
                if sym in pos:
                    paper['active'][sym]['current']=float(pos[sym].current_price or t['current'])
        except: pass
    for sym,t in list(paper['active'].items()):
        cached=cp(sym)
        price=cached['price'] if cached else t['current']
        paper['active'][sym]['current']=price
        paper['active'][sym]['peak']=max(price,t.get('peak',price))
        if price>=t['target']: close_trade(sym,'target')
        elif price<=t['stop']: close_trade(sym,'stop')
        elif datetime.now(ET_TZ).minute%30==0:
            pnl=(price-t['entry'])*t['shares']
            pnl_pct=(price-t['entry'])/t['entry']*100
            tg(f"📊 T{t['trade_num']} Update — {sym}\nEntry ${t['entry']:.2f} → ${price:.2f} | P&L {'+'if pnl>=0 else''}${pnl:.2f} ({pnl_pct:+.1f}%)\nTarget ${t['target']:.2f} | Stop ${t['stop']:.2f}")
    # Check for better opportunity every 10 min
    if datetime.now(ET_TZ).minute%10==0:
        check_better_opportunity()

def job_eod():
    if not is_trading_day(): return
    for sym in list(paper['active'].keys()): close_trade(sym,'eod')
    pct=paper['total_pnl']/paper['starting_capital']*100 if paper['starting_capital'] else 0
    lines=[f"📊 EOD REPORT — {today_str()}",f"{'━'*22}",
           f"Start: ${paper['starting_capital']:,.2f}",
           f"End:   ${paper['capital']:,.2f}",
           f"P&L:   {'✅ +' if paper['total_pnl']>=0 else '🔴 '}${abs(paper['total_pnl']):.2f} ({pct:+.2f}%)",
           f"{'━'*22}"]
    for t in paper['completed']:
        lines.append(f"{'✅'if t['pnl']>=0 else'🔴'} T{t['trade_num']} {t['symbol']}: {'+'if t['pnl']>=0 else''}${t['pnl']:.2f} ({t['pnl_pct']:+.2f}%) [{t['reason'].upper()}]")
    lines+=[f"{'━'*22}",f"Dashboard: {APP_URL}"]
    tg('\n'.join(lines))

def job_keepalive():
    n=datetime.now(ET_TZ); t=n.hour*60+n.minute
    if n.weekday()>=5 or t<7*60 or t>17*60: return
    try:
        urllib.request.urlopen(urllib.request.Request(f"{APP_URL}/ping",headers={'User-Agent':UA}),timeout=8)
    except: pass

# ── NEWS ──────────────────────────────────────────────────────────────
_news={'items':[],'ts':0}
def get_news():
    if time.time()-_news['ts']<90 and _news['items']: return _news['items']
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ,SPY,NVDA,SOXL,TSLA&region=US&lang=en-US']:
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
    trade_s=(f"In {active[0]['symbol']} T{active[0]['trade_num']}/3 entry ${active[0]['entry']:.2f} now ${active[0].get('current',active[0]['entry']):.2f}" if active else f"No active trade T{paper['trade_seq']}/3 pending")
    mkt_s=', '.join(f"{k} ${v['price']} ({v['pct']:+.2f}%)" for k,v in mkt.items() if k in ('QQQ','SPY','BTC-USD'))
    news_s=' | '.join(n['title'] for n in news[:4])
    if ANTHROPIC_KEY:
        try:
            prompt=(f"Day trader. Market: {mkt_s}. Active: {trade_s}. News: {news_s}. "
                    f"3-4 sentences: market direction, key risk, hold/adjust recommendation. Direct.")
            payload={"model":"claude-sonnet-4-20250514","max_tokens":200,"messages":[{"role":"user","content":prompt}]}
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",data=json.dumps(payload).encode(),
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
            with urllib.request.urlopen(req,timeout=20) as r:
                return json.loads(r.read())['content'][0]['text'],True
        except: pass
    spy=mkt.get('SPY'); qqq=mkt.get('QQQ')
    if spy and qqq:
        both_up=spy['pct']>0 and qqq['pct']>0
        d="bullish" if both_up else "bearish" if spy['pct']<0 and qqq['pct']<0 else "mixed"
        return (f"Market is {d} — SPY {spy['pct']:+.2f}%, QQQ {qqq['pct']:+.2f}%. "
                +("Conditions favorable for longs." if both_up else "Exercise caution with longs.")
                +(f" {news[0]['title']}." if news else "")),False
    return "Market data loading.",False

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
    fresh=(time.time()-_cache_ts)<10
    return cors({'data':all_prices(),'source':src,'fresh':fresh,
                 'delay':'~0 sec' if alpaca_data else '~15 min',
                 'updated':datetime.fromtimestamp(_cache_ts,ET_TZ).strftime('%H:%M:%S') if _cache_ts else '—'})

@app.route('/paper-status')
def paper_status():
    active=list(paper['active'].values())
    for t in active:
        cached=cp(t['symbol'])
        if cached: t['current']=cached['price']
    return cors({
        'status':paper['status'],'trade_seq':paper['trade_seq'],
        'capital':round(paper['capital'],2),'starting_capital':paper['starting_capital'],
        'total_pnl':round(paper['total_pnl'],2),
        'total_pnl_pct':round(paper['total_pnl']/paper['starting_capital']*100,2) if paper['starting_capital'] else 0,
        'active':active[0] if active else None,'completed':paper['completed'],
        'date':paper['date'],'log':paper['log'][:10],
        'picked_symbol':paper['picked']['symbol'] if paper['picked'] else None,
        'all_candidates':paper.get('all_candidates',[])[:8],
        'better_opportunity':paper.get('better_opportunity'),
        'alpaca_connected':alpaca_trading is not None,
    })

@app.route('/market-summary')
def market_summary():
    brief,is_ai=get_brief()
    return cors({'brief':brief,'ai':is_ai})

@app.route('/news')
def news_route(): return cors({'items':get_news()})

@app.route('/chat',methods=['POST'])
def chat():
    data=request.get_json() or {}
    user_msg=data.get('message','').strip()
    if not user_msg: return cors({'ok':False,'msg':'No message'})
    response, cmd = ai_chat(user_msg)
    result = {'ok':True,'response':response,'command':cmd}
    # Execute confirmed commands
    if cmd and not cmd.get('pending_confirm'):
        confirm_words=['confirm','yes','do it','execute','go ahead']
        if any(w in user_msg.lower() for w in confirm_words):
            exec_result=execute_command(cmd)
            result['executed']=exec_result
    # Store in history
    chat_history.append({'user':user_msg,'ai':response,'time':now_et()})
    if len(chat_history)>50: chat_history.pop(0)
    return cors(result)

@app.route('/chat-history')
def chat_history_route():
    return cors({'history':chat_history[-20:]})

@app.route('/watchlist',methods=['GET','POST'])
def watchlist():
    global custom_watchlist
    if request.method=='POST':
        data=request.get_json() or {}
        syms=[s.strip().upper() for s in data.get('symbols',[]) if s.strip()][:5]
        custom_watchlist=syms
        p_log(f"Custom watchlist updated: {syms}")
        return cors({'ok':True,'symbols':custom_watchlist})
    return cors({'symbols':custom_watchlist,'base_count':len(BASE_WATCHLIST)})

@app.route('/account')
def account():
    if not alpaca_trading: return cors({'alpaca_connected':False,'positions':[]})
    try:
        acct=alpaca_trading.get_account()
        pos=alpaca_trading.get_all_positions()
        return cors({'alpaca_connected':True,
            'account':{'equity':float(acct.equity),'cash':float(acct.cash),'portfolio_value':float(acct.portfolio_value),'buying_power':float(acct.buying_power)},
            'positions':[{'symbol':p.symbol,'qty':int(p.qty),'avg_entry':float(p.avg_entry_price),'current':float(p.current_price or 0),'unrealized_pnl':float(p.unrealized_pl or 0),'unrealized_pnl_pct':float(p.unrealized_plpc or 0)*100} for p in pos]})
    except Exception as e:
        return cors({'alpaca_connected':True,'error':str(e),'positions':[]})

@app.route('/candidates')
def candidates():
    return cors({'candidates':paper.get('all_candidates',[])})

@app.route('/paper-reset')
def paper_reset_route():
    paper_reset(); return cors({'ok':True})

@app.route('/paper-force-pick')
def paper_force_pick():
    paper_reset()
    threading.Thread(target=job_morning,daemon=True).start()
    return cors({'ok':True,'msg':'Morning triggered'})

@app.route('/test-telegram')
def test_telegram():
    ok=tg(f"✅ AutoTrade Pro Connected!\nAlpaca: {'✅' if alpaca_trading else '❌'} | AI Chat: {'✅' if ANTHROPIC_KEY else '❌'}\nDashboard: {APP_URL}")
    return cors({'ok':ok,'msg':'Sent! Check Telegram.' if ok else 'Failed.'})


@app.route('/et-news')
def et_news():
    """Ethiopian Reporter news via Google News RSS"""
    items = []
    urls = [
        'https://news.google.com/rss/search?q=site:ethiopianreporter.com&hl=en&gl=ET&ceid=ET:en',
        'https://news.google.com/rss/search?q=ethiopianreporter.com&hl=en&gl=ET',
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=8) as r:
                tree = ET.fromstring(r.read())
                for item in tree.iter('item'):
                    title_el = item.find('title')
                    link_el  = item.find('link')
                    date_el  = item.find('pubDate')
                    if title_el is not None and title_el.text and len(title_el.text.strip()) > 10:
                        title = title_el.text.strip()
                        # Remove " - Ethiopian Reporter" suffix if present
                        if ' - ' in title:
                            title = title.rsplit(' - ', 1)[0].strip()
                        link = link_el.text.strip() if link_el is not None and link_el.text else ''
                        date = date_el.text.strip() if date_el is not None and date_el.text else ''
                        items.append({'title': title, 'link': link, 'date': date})
            if items: break
        except Exception as e:
            print(f"  ET news error: {e}")
    return cors({'items': items[:20], 'count': len(items)})

@app.route('/notify')
def notify():
    msg=request.args.get('msg','')
    return cors({'ok':tg(msg) if msg else False})

# ── START ─────────────────────────────────────────────────────────────
threading.Thread(target=price_thread,daemon=True).start()
print(f"  Price thread: {'Alpaca 2-sec' if alpaca_data else 'yfinance 30-sec'}")

import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,    'cron',day_of_week='mon-fri',hour=8, minute=0)
    sched.add_job(job_market_open,'cron',day_of_week='mon-fri',hour=9, minute=30)
    sched.add_job(job_monitor,    'cron',day_of_week='mon-fri',hour='8-16',minute='*/2')
    sched.add_job(job_eod,        'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,  'interval',minutes=10)
    sched.start(); atexit.register(lambda:sched.shutdown(wait=False))
    print(f"  Scheduler: {len(sched.get_jobs())} jobs")
except Exception as e:
    print(f"  Scheduler: {e}")

if __name__=='__main__':
    print(f"\n  AutoTrade Pro — http://localhost:{PORT}")
    app.run(host='0.0.0.0',port=PORT,debug=False)
