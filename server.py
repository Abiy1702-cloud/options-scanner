#!/usr/bin/env python3
"""
AutoTrade Pro — Alpaca Paper Trading Server
Real-time prices + real paper order execution via Alpaca
"""
import json, os, time, threading, urllib.request
import xml.etree.ElementTree as ET, concurrent.futures
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

try:
    import yfinance as yf
    import pandas as pd
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"Missing: {e}"); raise SystemExit(1)

# ── Alpaca imports ─────────────────────────────────────────────────────
ALPACA_KEY    = os.environ.get('ALPACA_API_KEY', '')
ALPACA_SECRET = os.environ.get('ALPACA_SECRET_KEY', '')
alpaca_trading = None
alpaca_data    = None

if ALPACA_KEY and ALPACA_SECRET:
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest
        alpaca_trading = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=True)
        alpaca_data    = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
        print("  Alpaca connected (paper trading mode)")
    except Exception as e:
        print(f"  Alpaca init error: {e}")
else:
    print("  Alpaca not configured — set ALPACA_API_KEY and ALPACA_SECRET_KEY")

app    = Flask(__name__, static_folder='.')
PORT   = int(os.environ.get('PORT', 8765))
ET_TZ  = pytz.timezone('America/New_York')
UA     = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
APP_URL        = os.environ.get('APP_URL', 'https://your-app.onrender.com')

TRADE_WATCHLIST = [
    'NVDA','MU','AMD','TSLA','META','AAPL','MSFT','AMZN',
    'AVGO','COIN','PLTR','ARM','GOOGL','SMCI','NFLX',
    'MSTR','CRWD','PANW','NOW','CRM'
]

# ── Price Cache (2-sec updates via Alpaca, fallback yfinance) ──────────
_price_cache   = {}
_price_lock    = threading.Lock()
_cache_ts      = 0
# Store previous closes for % change calculation
_prev_closes   = {}

def get_realtime_prices(symbols):
    """Get real-time prices via Alpaca data API"""
    prices = {}
    if alpaca_data:
        try:
            from alpaca.data.requests import StockLatestTradeRequest
            req    = StockLatestTradeRequest(symbol_or_symbols=symbols)
            trades = alpaca_data.get_stock_latest_trade(req)
            for sym, trade in trades.items():
                price = float(trade.price)
                prev  = _prev_closes.get(sym, price)
                pct   = ((price - prev) / prev * 100) if prev else 0
                prices[sym] = {
                    'symbol': sym, 'price': round(price, 2),
                    'pct':   round(pct, 3),
                    'change':round(price - prev, 4),
                    'source':'alpaca',
                    'time':  datetime.now(ET_TZ).strftime('%H:%M:%S')
                }
            return prices
        except Exception as e:
            print(f"  Alpaca price error: {e}")
    # Fallback: yfinance
    try:
        df = yf.download(symbols, period='2d', interval='1d',
                         auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in symbols:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl) < 1: continue
                price = float(cl.iloc[-1])
                prev  = float(cl.iloc[-2]) if len(cl)>=2 else price
                pct   = ((price-prev)/prev*100) if prev else 0
                prices[sym] = {
                    'symbol': sym, 'price': round(price,2),
                    'pct':   round(pct,3), 'change': round(price-prev,4),
                    'source':'yfinance_delayed',
                    'time':  datetime.now(ET_TZ).strftime('%H:%M:%S')
                }
            except: pass
    except Exception as e:
        print(f"  yfinance fallback error: {e}")
    return prices

def load_prev_closes():
    """Load previous closes for % change calculation"""
    global _prev_closes
    syms = list({'QQQ','SPY','BTC-USD'}
               | {t['symbol'] for t in paper.get('active_trades',{}).values()})
    try:
        df = yf.download(syms, period='5d', interval='1d',
                         auto_adjust=True, progress=False)
        multi = isinstance(df.columns, pd.MultiIndex)
        for sym in syms:
            try:
                cl = (df['Close'][sym] if multi else df['Close']).dropna()
                if len(cl) >= 2:
                    _prev_closes[sym] = float(cl.iloc[-2])
            except: pass
        print(f"  Previous closes loaded for {len(_prev_closes)} symbols")
    except Exception as e:
        print(f"  Prev close error: {e}")

def update_prices_bg():
    """Background thread — updates every 2 sec via Alpaca, 30 sec via yfinance"""
    global _cache_ts
    load_prev_closes()
    interval = 2 if alpaca_data else 30
    while True:
        try:
            syms = list({'QQQ','SPY','BTC-USD'}
                       | {t['symbol'] for t in paper.get('active_trades',{}).values()}
                       | {paper.get('picked_symbol') or ''})
            syms = [s for s in syms if s and s != 'BTC-USD'] # Alpaca no crypto
            btc_syms = ['BTC-USD']

            # Real-time stock prices via Alpaca
            stock_prices = get_realtime_prices(syms) if syms else {}

            # BTC always via yfinance (Alpaca doesn't support crypto in basic tier)
            btc_prices = {}
            try:
                df = yf.download(['BTC-USD'], period='2d', interval='1d',
                                  auto_adjust=True, progress=False)
                cl = (df['Close']['BTC-USD'] if isinstance(df.columns, pd.MultiIndex)
                      else df['Close']).dropna()
                if len(cl) >= 1:
                    price = float(cl.iloc[-1])
                    prev  = float(cl.iloc[-2]) if len(cl)>=2 else price
                    pct   = ((price-prev)/prev*100) if prev else 0
                    btc_prices['BTC-USD'] = {
                        'symbol':'BTC-USD','price':round(price,2),
                        'pct':round(pct,3),'change':round(price-prev,4),
                        'source':'yfinance','time':datetime.now(ET_TZ).strftime('%H:%M:%S')
                    }
            except: pass

            with _price_lock:
                _price_cache.update(stock_prices)
                _price_cache.update(btc_prices)
                _cache_ts = time.time()
        except Exception as e:
            print(f"  Price thread error: {e}")
        time.sleep(interval)

def get_cached(sym):
    with _price_lock:
        return _price_cache.get(sym)

def get_all_cached():
    with _price_lock:
        return dict(_price_cache)

# ── Alpaca Account Info ────────────────────────────────────────────────
def get_alpaca_account():
    if not alpaca_trading: return None
    try:
        acct = alpaca_trading.get_account()
        return {
            'equity':          float(acct.equity),
            'cash':            float(acct.cash),
            'buying_power':    float(acct.buying_power),
            'portfolio_value': float(acct.portfolio_value),
        }
    except Exception as e:
        print(f"  Alpaca account error: {e}")
        return None

def get_alpaca_positions():
    if not alpaca_trading: return []
    try:
        pos = alpaca_trading.get_all_positions()
        return [{
            'symbol':     p.symbol,
            'qty':        int(p.qty),
            'avg_entry':  float(p.avg_entry_price),
            'current':    float(p.current_price) if p.current_price else 0,
            'market_val': float(p.market_value) if p.market_value else 0,
            'unrealized_pnl':     float(p.unrealized_pl) if p.unrealized_pl else 0,
            'unrealized_pnl_pct': float(p.unrealized_plpc)*100 if p.unrealized_plpc else 0,
        } for p in pos]
    except Exception as e:
        print(f"  Alpaca positions error: {e}")
        return []

# ── Telegram ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body = json.dumps({'chat_id': str(TELEGRAM_CHAT).strip(),
                           'text': str(msg), 'disable_web_page_preview': True}).encode()
        req  = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type':'application/json','User-Agent':UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get('ok', False)
    except Exception as e:
        print(f"  Telegram: {e}"); return False

# ── Market helpers ─────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 570 <= t < 960

def is_trading_day():
    return datetime.now(ET_TZ).weekday() < 5

def now_et(): return datetime.now(ET_TZ).strftime('%H:%M ET')
def today_str(): return datetime.now(ET_TZ).strftime('%a %b %d, %Y')

# ── Scoring (uses yfinance for fundamentals — not prices) ──────────────
def score_stock(q):
    p=q.get('regularMarketPrice',0); hi=q.get('fiftyTwoWeekHigh',p) or p
    lo=q.get('fiftyTwoWeekLow',p*0.7) or p*0.7
    chg=q.get('regularMarketChangePercent',0); vr=q.get('_vr',1)
    mc=q.get('marketCap',0); rng=(p-lo)/((hi-lo) or 1); s=50
    if   chg>5:  s+=25
    elif chg>3:  s+=19
    elif chg>2:  s+=14
    elif chg>1:  s+=9
    elif chg>0:  s+=4
    elif chg<-3: s-=18
    elif chg<-1: s-=8
    else:        s-=3
    if   vr>5:   s+=25
    elif vr>3:   s+=18
    elif vr>2:   s+=12
    elif vr>1.5: s+=7
    elif vr>1:   s+=3
    else:        s-=6
    if   rng>0.88: s+=20
    elif rng>0.72: s+=13
    elif rng>0.55: s+=7
    elif rng>0.35: s+=3
    else:          s-=5
    if   mc>200e9: s+=10
    elif mc>50e9:  s+=7
    elif mc>10e9:  s+=4
    elif mc>1e9:   s+=2
    return min(99, max(30, round(s)))

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

def get_quotes_for_scan(symbols):
    results=[]
    try:
        df5  = yf.download(symbols, period='5d', interval='1d', auto_adjust=True, progress=False)
        df1y = yf.download(symbols, period='1y', interval='1d', auto_adjust=True, progress=False)
        def fi(s):
            try: return s, yf.Ticker(s).info or {}
            except: return s, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            info_map = dict(ex.map(fi, symbols))
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
                info=info_map.get(sym,{}); avg_vol=int(v1y.mean()) if len(v1y) else 1
                vr=round(int(v5.iloc[-1])/avg_vol,1) if avg_vol and len(v5) else 1
                results.append({
                    'symbol':sym,'shortName':info.get('shortName',sym),
                    'regularMarketPrice':round(price,2),
                    'regularMarketChangePercent':round(chg,4),
                    'marketCap':int(info.get('marketCap') or 0),
                    'fiftyTwoWeekHigh':round(float(c1y.max()),2) if len(c1y) else price,
                    'fiftyTwoWeekLow':round(float(c1y.min()),2) if len(c1y) else price,
                    '_vr':vr,
                })
            except: pass
    except Exception as e:
        print(f"  Scan error: {e}")
    return results

# ── Paper Trading State ────────────────────────────────────────────────
paper = {
    'capital':          10000.0,
    'starting_capital': 10000.0,
    'trade_seq':        1,
    'targets':          [0.10, 0.05, 0.05],
    'stop_pct':         0.03,
    'active_trades':    {},
    'completed':        [],
    'total_pnl':        0.0,
    'status':           'waiting',
    'picked_symbol':    None,
    'picked_data':      None,
    'date':             None,
    'log':              [],
    'alpaca_orders':    {},   # {symbol: order_id}
}

def p_log(msg):
    ts = now_et()
    paper['log'].insert(0, {'time': ts, 'msg': msg})
    paper['log'] = paper['log'][:30]
    print(f"  [{ts}] {msg}")

def paper_reset():
    # Cancel any open Alpaca orders
    if alpaca_trading:
        try: alpaca_trading.cancel_orders()
        except: pass
    paper.update({
        'capital':10000.0,'starting_capital':10000.0,'trade_seq':1,
        'active_trades':{},'completed':[],'total_pnl':0.0,
        'status':'waiting','picked_symbol':None,'picked_data':None,
        'date':datetime.now(ET_TZ).date().isoformat(),'log':[],
        'alpaca_orders':{}
    })
    p_log("Paper trading reset for new day")

def pick_best_stock():
    already = [t['symbol'] for t in paper['completed']]
    quotes   = get_quotes_for_scan(TRADE_WATCHLIST[:15])
    eligible = [q for q in quotes
                if q.get('regularMarketPrice',0)>5
                and q.get('marketCap',0)>5e9
                and q['symbol'] not in already]
    if not eligible: return None
    return max(eligible, key=lambda q: score_stock(q))

def place_alpaca_order(symbol, qty, side='buy'):
    """Place real paper order via Alpaca"""
    if not alpaca_trading:
        p_log(f"Alpaca not configured — simulating {side} {qty} {symbol}")
        return None
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        order = alpaca_trading.submit_order(MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side=='buy' else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        ))
        p_log(f"Alpaca {side.upper()} order submitted: {qty} {symbol} | ID {str(order.id)[:8]}")
        return str(order.id)
    except Exception as e:
        p_log(f"Alpaca order error: {e}")
        return None

def close_alpaca_position(symbol):
    """Close position via Alpaca"""
    if not alpaca_trading: return False
    try:
        alpaca_trading.close_position(symbol)
        p_log(f"Alpaca position closed: {symbol}")
        return True
    except Exception as e:
        p_log(f"Alpaca close error {symbol}: {e}")
        return False

def enter_trade(q, price=None):
    tnum    = paper['trade_seq']
    ep      = price or q['regularMarketPrice']
    # If Alpaca available, use real-time price
    if alpaca_data:
        cached = get_cached(q['symbol'])
        if cached: ep = cached['price']
    cap  = paper['capital']
    shrs = int(cap / ep)
    if shrs < 1: return
    tgt_pct = paper['targets'][tnum-1]
    trade = {
        'symbol':     q['symbol'],
        'name':       q.get('shortName', q['symbol']),
        'trade_num':  tnum,
        'shares':     shrs,
        'entry':      round(ep, 2),
        'cost':       round(shrs * ep, 2),
        'target':     round(ep * (1+tgt_pct), 2),
        'stop':       round(ep * (1-paper['stop_pct']), 2),
        'target_pct': tgt_pct,
        'entry_time': now_et(),
        'current':    round(ep, 2),
        'peak':       round(ep, 2),
        'order_id':   None,
        'via_alpaca': alpaca_trading is not None,
    }
    # Place real Alpaca order
    order_id = place_alpaca_order(q['symbol'], shrs, 'buy')
    trade['order_id'] = order_id
    paper['active_trades'][q['symbol']] = trade
    paper['status']  = 'entered'
    paper['capital'] = 0
    mode = "📡 REAL Alpaca paper order" if order_id else "📋 Simulated"
    p_log(f"ENTERED {q['symbol']} {shrs}sh @ ${ep:.2f} [{mode}]")
    tg(
        f"📈 TRADE {tnum} ENTERED\n"
        f"{'━'*22}\n"
        f"Stock:   {q['symbol']} — {q.get('shortName','')}\n"
        f"Shares:  {shrs} @ ${ep:.2f}\n"
        f"Cost:    ${shrs*ep:,.2f}\n"
        f"{'━'*22}\n"
        f"Target:  ${trade['target']:.2f} (+{tgt_pct*100:.0f}%)\n"
        f"Stop:    ${trade['stop']:.2f} (-{paper['stop_pct']*100:.0f}%)\n"
        f"Mode:    {mode}\n"
        f"Time:    {trade['entry_time']}\n"
        f"{'━'*22}\n"
        f"Dashboard: {APP_URL}"
    )
    # Update prev close for accurate % calc
    _prev_closes[q['symbol']] = ep

def close_trade(sym, reason='target'):
    t = paper['active_trades'].pop(sym, None)
    if not t: return
    # Get real exit price
    cached = get_cached(sym)
    sell   = round(cached['price'] if cached else t['current'], 2)
    # Close via Alpaca
    if t.get('via_alpaca'):
        close_alpaca_position(sym)
    pnl     = round((sell - t['entry']) * t['shares'], 2)
    pnl_pct = round((sell - t['entry']) / t['entry'] * 100, 2)
    proceeds= round(sell * t['shares'], 2)
    paper['completed'].append({**t,'exit':sell,'exit_time':now_et(),
                                'pnl':pnl,'pnl_pct':pnl_pct,'reason':reason})
    paper['total_pnl'] += pnl
    paper['capital']    = proceeds
    icons  = {'target':'💰','stop':'🛑','eod':'🔔'}
    labels = {'target':'TARGET HIT','stop':'STOP LOSS','eod':'EOD CLOSE'}
    p_log(f"CLOSED {sym} {reason.upper()} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    tg(
        f"{icons.get(reason,'📊')} TRADE {t['trade_num']} CLOSED — {labels.get(reason,'')}\n"
        f"{'━'*22}\n"
        f"Stock:   {sym}\n"
        f"Sold:    {t['shares']} shares @ ${sell:.2f}\n"
        f"Entry:   ${t['entry']:.2f}  →  Exit: ${sell:.2f}\n"
        f"P&L:     {'✅ +$' if pnl>=0 else '🔴 -$'}{abs(pnl):.2f} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)\n"
        f"Capital: ${proceeds:,.2f}\n"
        f"{'━'*22}\n"
        f"Total P&L: {'+'if paper['total_pnl']>=0 else ''}${paper['total_pnl']:,.2f}"
    )
    nxt = paper['trade_seq'] + 1
    if nxt <= 3 and is_market_open():
        paper['trade_seq'] = nxt
        paper['status']    = 'waiting'
        tg(f"🔍 Looking for Trade {nxt}/3 (+{paper['targets'][nxt-1]*100:.0f}% target)...")
        threading.Thread(target=_pick_and_enter, daemon=True).start()
    else:
        paper['status'] = 'done'
        if nxt > 3:
            tg(f"✅ All 3 trades done!\nTotal: {'+'if paper['total_pnl']>=0 else ''}${paper['total_pnl']:,.2f}\n{APP_URL}")

def _pick_and_enter():
    time.sleep(5)
    q = pick_best_stock()
    if not q:
        tg("⚠️ No suitable stock found."); return
    paper['picked_symbol'] = q['symbol']
    paper['picked_data']   = q
    paper['status']        = 'picked'
    _notify_pick(q)
    time.sleep(3)
    enter_trade(q)

def _notify_pick(q):
    tnum = paper['trade_seq']
    tp   = paper['targets'][tnum-1]*100
    p    = get_cached(q['symbol'])
    price= p['price'] if p else q['regularMarketPrice']
    shrs = int(paper['capital'] / price) if price else 0
    tg(
        f"🔍 TRADE {tnum} PICKED\n"
        f"{'━'*22}\n"
        f"Stock:   {q['symbol']} — {q.get('shortName','')}\n"
        f"Price:   ${price:.2f} ({q['regularMarketChangePercent']:+.2f}% today)\n"
        f"Signal:  {week_sig(q)} | Score {score_stock(q)}/100\n"
        f"{'━'*22}\n"
        f"Capital: ${paper['capital']:,.2f}\n"
        f"Shares:  ~{shrs} @ ${price:.2f}\n"
        f"Target:  ${price*(1+paper['targets'][tnum-1]):.2f} (+{tp:.0f}%)\n"
        f"Stop:    ${price*(1-paper['stop_pct']):.2f} (-{paper['stop_pct']*100:.0f}%)\n"
        f"Entering now..."
    )

# ── Scheduler Jobs ─────────────────────────────────────────────────────
def job_morning():
    if not is_trading_day(): return
    paper_reset()
    load_prev_closes()
    p_log("Good morning — starting AutoTrade day")
    source = "Alpaca real-time" if alpaca_trading else "yfinance (15-min delayed)"
    tg(
        f"🌅 AUTOTRADE STARTING\n"
        f"{'━'*22}\n"
        f"Date:    {today_str()}\n"
        f"Capital: $10,000.00\n"
        f"Plan:    T1: +10% · T2: +5% · T3: +5%\n"
        f"Stop:    -3% on all trades\n"
        f"Data:    {source}\n"
        f"{'━'*22}\n"
        f"Scanning for best stock...\n"
        f"Dashboard: {APP_URL}"
    )
    q = pick_best_stock()
    if not q:
        tg("⚠️ No stock found. Will retry at 9:30 AM."); return
    paper['picked_symbol'] = q['symbol']
    paper['picked_data']   = q
    paper['status']        = 'picked'
    _notify_pick(q)
    # Pre-market entry if strong gap
    cached = get_cached(q['symbol'])
    if cached and cached['pct'] > 0.8:
        tg(f"⚡ {q['symbol']} up {cached['pct']:+.2f}% pre-market — entering early")
        enter_trade(q)

def job_market_open():
    if not is_trading_day(): return
    tg(f"🔔 MARKET OPEN — {today_str()}\nDashboard: {APP_URL}")
    if paper['status']=='picked' and not paper['active_trades']:
        q = paper.get('picked_data')
        if q: enter_trade(q)
    elif paper['status']=='waiting' and paper['trade_seq']==1:
        q = pick_best_stock()
        if q:
            paper['picked_symbol']=q['symbol']
            paper['picked_data']=q
            paper['status']='picked'
            _notify_pick(q)
            time.sleep(5)
            enter_trade(q)

def job_monitor():
    if not paper['active_trades']: return
    # If Alpaca connected, sync real positions
    if alpaca_trading:
        try:
            positions = {p.symbol: p for p in alpaca_trading.get_all_positions()}
            for sym, t in list(paper['active_trades'].items()):
                if sym in positions:
                    pos = positions[sym]
                    real_price = float(pos.current_price) if pos.current_price else t['current']
                    paper['active_trades'][sym]['current'] = real_price
                    paper['active_trades'][sym]['peak']    = max(real_price, t.get('peak',real_price))
        except Exception as e:
            print(f"  Position sync: {e}")
    # Check cached prices
    for sym, t in list(paper['active_trades'].items()):
        cached = get_cached(sym)
        price  = cached['price'] if cached else t['current']
        paper['active_trades'][sym]['current'] = price
        if price >= t['target']:
            close_trade(sym, 'target')
        elif price <= t['stop']:
            close_trade(sym, 'stop')
        elif datetime.now(ET_TZ).minute % 30 == 0:
            pnl = (price - t['entry']) * t['shares']
            pnl_pct = (price - t['entry']) / t['entry'] * 100
            tg(f"📊 Trade {t['trade_num']} Update — {sym}\nEntry: ${t['entry']:.2f} | Now: ${price:.2f}\nP&L: {'+'if pnl>=0 else''}${pnl:.2f} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)\nTarget: ${t['target']:.2f} | Stop: ${t['stop']:.2f}")

def job_eod():
    if not is_trading_day(): return
    for sym in list(paper['active_trades'].keys()):
        close_trade(sym, 'eod')
    pct = paper['total_pnl']/paper['starting_capital']*100 if paper['starting_capital'] else 0
    lines=[
        f"📊 END OF DAY — {today_str()}",f"{'━'*22}",
        f"Start:  ${paper['starting_capital']:,.2f}",
        f"End:    ${paper['capital']:,.2f}",
        f"P&L:    {'✅ +' if paper['total_pnl']>=0 else '🔴 '}${abs(paper['total_pnl']):,.2f} ({'+' if pct>=0 else ''}{pct:.2f}%)",
        f"{'━'*22}"
    ]
    for t in paper['completed']:
        lines.append(f"{'✅'if t['pnl']>=0 else'🔴'} T{t['trade_num']} {t['symbol']}: {'+'if t['pnl']>=0 else''}${t['pnl']:.2f} ({'+' if t['pnl_pct']>=0 else''}{t['pnl_pct']:.2f}%) [{t['reason'].upper()}]")
    if not paper['completed']:
        lines.append("No trades completed today.")
    lines += [f"{'━'*22}", f"Dashboard: {APP_URL}"]
    tg('\n'.join(lines))

def job_keepalive():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return  # skip weekends
    t = now.hour * 60 + now.minute
    if t < 7*60 or t > 17*60: return  # only 7 AM - 5 PM ET
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{APP_URL}/ping", headers={'User-Agent':UA}), timeout=8)
        print(f"  Keepalive ping OK at {now_et()}")
    except: pass

# ── News ──────────────────────────────────────────────────────────────
_news = {'items':[],'ts':0}

def get_news():
    if time.time()-_news['ts']<90 and _news['items']: return _news['items']
    items=[]
    for url in ['https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ,SPY,NVDA,TSLA,META&region=US&lang=en-US',
                'https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,AMZN,MSFT,COIN&region=US&lang=en-US']:
        try:
            req=urllib.request.Request(url,headers={'User-Agent':UA})
            with urllib.request.urlopen(req,timeout=6) as r:
                tree=ET.fromstring(r.read())
                for item in tree.iter('item'):
                    t=item.find('title')
                    if t is not None and t.text and len(t.text.strip())>20:
                        items.append({'title':t.text.strip()})
        except: pass
    if not items: items=[{'title':'Market data loading — check back shortly'}]
    _news.update({'items':items[:15],'ts':time.time()})
    return _news['items']

# ── AI Brief ──────────────────────────────────────────────────────────
def get_brief():
    mkt     = get_all_cached()
    news    = get_news()
    active  = list(paper['active_trades'].values())
    trade_s = (f"In {active[0]['symbol']} T{active[0]['trade_num']}/3, "
               f"entry ${active[0]['entry']:.2f}, now ${active[0].get('current',active[0]['entry']):.2f}"
               if active else f"No active trade, T{paper['trade_seq']}/3 pending")
    mkt_s   = ', '.join(f"{k} ${v['price']} ({v['pct']:+.2f}%)" for k,v in mkt.items() if k in ('QQQ','SPY','BTC-USD'))
    news_s  = ' | '.join(n['title'] for n in news[:5])
    if ANTHROPIC_KEY:
        try:
            prompt=(f"Day trader context. Market: {mkt_s}. Trade: {trade_s}. News: {news_s}. "
                    f"Give 3-4 sentences: market direction, key risk, hold/adjust recommendation. Direct and specific.")
            payload={"model":"claude-sonnet-4-20250514","max_tokens":200,
                     "messages":[{"role":"user","content":prompt}]}
            req=urllib.request.Request("https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"})
            with urllib.request.urlopen(req,timeout=25) as r:
                return json.loads(r.read())['content'][0]['text'], True
        except Exception as e:
            print(f"  AI brief: {e}")
    # Rule-based
    qqq=mkt.get('QQQ'); spy=mkt.get('SPY')
    if qqq and spy:
        both_up=qqq['pct']>0 and spy['pct']>0
        both_dn=qqq['pct']<0 and spy['pct']<0
        d="bullish" if both_up else "bearish" if both_dn else "mixed"
        brief=(f"Market is {d} today — QQQ {qqq['pct']:+.2f}% and SPY {spy['pct']:+.2f}%. "
               +("Tech and broad market aligned — favorable for longs. " if both_up
                 else "Broad selling pressure — watch stops carefully. " if both_dn
                 else "Diverging signals — stay disciplined with risk. ")
               +(f"Key news: {news[0]['title']}" if news else ""))
        return brief, False
    return "Market data loading.", False

# ── Flask Routes ───────────────────────────────────────────────────────
def cors(data):
    r=jsonify(data); r.headers['Access-Control-Allow-Origin']='*'; return r

@app.route('/')
def index(): return send_from_directory('.','index.html')

@app.route('/ping')
def ping(): return cors({'ok':True,'time':now_et()})

@app.route('/prices')
def prices():
    source = 'alpaca_realtime' if alpaca_data else 'yfinance_delayed'
    delay  = '~0 sec (real-time)' if alpaca_data else '~15 min delayed'
    fresh  = (time.time()-_cache_ts) < 10
    return cors({'data':get_all_cached(),'source':source,'delay':delay,
                 'fresh':fresh,'updated':datetime.fromtimestamp(_cache_ts,ET_TZ).strftime('%H:%M:%S') if _cache_ts else '—'})

@app.route('/account')
def account():
    acct = get_alpaca_account()
    pos  = get_alpaca_positions()
    return cors({'account':acct,'positions':pos,'alpaca_connected': alpaca_trading is not None})

@app.route('/market-summary')
def market_summary():
    brief, is_ai = get_brief()
    return cors({'brief':brief,'ai':is_ai})

@app.route('/news')
def news_route(): return cors({'items':get_news()})

@app.route('/paper-status')
def paper_status():
    active=list(paper['active_trades'].values())
    for t in active:
        cached=get_cached(t['symbol'])
        if cached: t['current']=cached['price']
    return cors({
        'status':paper['status'],'trade_seq':paper['trade_seq'],
        'capital':round(paper['capital'],2),
        'starting_capital':paper['starting_capital'],
        'total_pnl':round(paper['total_pnl'],2),
        'total_pnl_pct':round(paper['total_pnl']/paper['starting_capital']*100,2) if paper['starting_capital'] else 0,
        'active':active[0] if active else None,
        'completed':paper['completed'],
        'date':paper['date'],'log':paper['log'][:10],
        'picked_symbol':paper['picked_symbol'],
        'alpaca_connected':alpaca_trading is not None,
    })

@app.route('/paper-reset')
def paper_reset_route():
    paper_reset(); return cors({'ok':True})

@app.route('/paper-force-pick')
def paper_force_pick():
    paper_reset()
    threading.Thread(target=job_morning, daemon=True).start()
    return cors({'ok':True,'msg':'Morning job triggered'})

@app.route('/test-telegram')
def test_telegram():
    src = "Alpaca real-time ✅" if alpaca_trading else "yfinance delayed ⚠️"
    ok  = tg(f"✅ AutoTrade Pro — Connected!\nAbiy Kassa · St Louis MO\n\nData source: {src}\n\nAlerts: Pick · Entry · Target · Stop · EOD Report\n\nDashboard: {APP_URL}")
    return cors({'ok':ok,'msg':'Sent! Check Telegram.' if ok else 'Failed — check tokens.'})

@app.route('/notify')
def notify():
    msg=request.args.get('msg','')
    return cors({'ok':tg(msg) if msg else False})

# ── Start background threads + scheduler ──────────────────────────────
threading.Thread(target=update_prices_bg, daemon=True).start()
print(f"  Price thread started ({'Alpaca 2-sec' if alpaca_data else 'yfinance 30-sec'})")

import atexit
try:
    sched=BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,     'cron',day_of_week='mon-fri',hour=8, minute=0)
    sched.add_job(job_market_open, 'cron',day_of_week='mon-fri',hour=9, minute=30)
    sched.add_job(job_monitor,     'cron',day_of_week='mon-fri',hour='8-16',minute='*/2')
    sched.add_job(job_eod,         'cron',day_of_week='mon-fri',hour=15,minute=55)
    sched.add_job(job_keepalive,   'interval',minutes=10)
    sched.start()
    atexit.register(lambda: sched.shutdown(wait=False))
    print(f"  Scheduler: {len(sched.get_jobs())} jobs")
except Exception as e:
    print(f"  Scheduler error: {e}")

if __name__=='__main__':
    print(f"\n  AutoTrade Pro — http://localhost:{PORT}")
    print(f"  Alpaca: {'✅ connected' if alpaca_trading else '❌ not configured'}")
    app.run(host='0.0.0.0',port=PORT,debug=False)
