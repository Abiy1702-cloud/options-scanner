#!/usr/bin/env python3
"""AutoTrade Pro — Standalone Paper Trading Server"""
import json, os, time, threading, urllib.request, urllib.parse
import xml.etree.ElementTree as ET, concurrent.futures
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

try:
    import yfinance as yf, pandas as pd, pytz
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError as e:
    print(f"Missing: {e}\npip install flask gunicorn yfinance pandas APScheduler pytz")
    raise SystemExit(1)

app    = Flask(__name__, static_folder='.')
PORT   = int(os.environ.get('PORT', 8765))
ET_TZ  = pytz.timezone('America/New_York')
UA     = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'
ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT  = os.environ.get('TELEGRAM_CHAT_ID', '')
APP_URL        = os.environ.get('APP_URL', 'https://your-app.onrender.com')

# ── Watchlist for auto-trading ─────────────────────────────────────────
TRADE_WATCHLIST = [
    'NVDA','MU','AMD','TSLA','META','AAPL','MSFT','AMZN',
    'AVGO','COIN','PLTR','ARM','GOOGL','SMCI','NFLX',
    'MSTR','CRWD','PANW','NOW','CRM'
]

# ── Price cache (updated every 30 sec by background thread) ───────────
_price_cache   = {}   # {symbol: {price, change, pct, time}}
_price_lock    = threading.Lock()
_cache_updated = 0

def update_prices_bg():
    """Background thread — refreshes price cache every 30 sec"""
    global _cache_updated
    while True:
        try:
            syms = list({'QQQ','SPY','BTC-USD'}
                       | {t['symbol'] for t in paper['active_trades'].values()}
                       | {paper.get('picked_symbol','') or ''})
            syms = [s for s in syms if s]
            df = yf.download(syms, period='2d', interval='1d',
                             auto_adjust=True, progress=False, threads=True)
            multi = isinstance(df.columns, pd.MultiIndex)
            with _price_lock:
                for sym in syms:
                    try:
                        cl = (df['Close'][sym] if multi else df['Close']).dropna()
                        if len(cl) < 1: continue
                        price = float(cl.iloc[-1])
                        prev  = float(cl.iloc[-2]) if len(cl) >= 2 else price
                        pct   = ((price - prev) / prev * 100) if prev else 0
                        _price_cache[sym] = {
                            'symbol': sym, 'price': round(price, 2),
                            'change': round(price - prev, 4),
                            'pct':   round(pct, 3),
                            'time':  datetime.now(ET_TZ).strftime('%H:%M:%S')
                        }
                    except: pass
                _cache_updated = time.time()
        except Exception as e:
            print(f"  Price cache error: {e}")
        time.sleep(28)

def get_cached_price(sym):
    with _price_lock:
        return _price_cache.get(sym)

def get_all_cached():
    with _price_lock:
        return dict(_price_cache)

# ── Telegram ──────────────────────────────────────────────────────────
def tg(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        body = json.dumps({'chat_id': str(TELEGRAM_CHAT).strip(), 'text': str(msg),
                           'disable_web_page_preview': True}).encode()
        req  = urllib.request.Request(
            f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage',
            data=body, headers={'Content-Type': 'application/json', 'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=12) as r:
            return json.loads(r.read()).get('ok', False)
    except Exception as e:
        print(f"  Telegram error: {e}"); return False

# ── Market helpers ─────────────────────────────────────────────────────
def is_market_open():
    now = datetime.now(ET_TZ)
    if now.weekday() >= 5: return False
    t = now.hour * 60 + now.minute
    return 570 <= t < 960

def is_trading_day():
    return datetime.now(ET_TZ).weekday() < 5

def now_et():
    return datetime.now(ET_TZ).strftime('%H:%M ET')

def today_str():
    return datetime.now(ET_TZ).strftime('%a %b %d, %Y')

# ── Scoring ────────────────────────────────────────────────────────────
def score_stock(q):
    p   = q.get('regularMarketPrice', 0)
    hi  = q.get('fiftyTwoWeekHigh', p) or p
    lo  = q.get('fiftyTwoWeekLow', p * 0.7) or p * 0.7
    chg = q.get('regularMarketChangePercent', 0)
    vr  = q.get('_vr', 1)
    mc  = q.get('marketCap', 0)
    rng = (p - lo) / ((hi - lo) or 1)
    s   = 50
    if   chg > 5: s += 25
    elif chg > 3: s += 19
    elif chg > 2: s += 14
    elif chg > 1: s += 9
    elif chg > 0: s += 4
    elif chg < -3: s -= 18
    elif chg < -1: s -= 8
    else: s -= 3
    if   vr > 5: s += 25
    elif vr > 3: s += 18
    elif vr > 2: s += 12
    elif vr > 1.5: s += 7
    elif vr > 1:  s += 3
    else: s -= 6
    if   rng > 0.88: s += 20
    elif rng > 0.72: s += 13
    elif rng > 0.55: s += 7
    elif rng > 0.35: s += 3
    else: s -= 5
    if   mc > 200e9: s += 10
    elif mc > 50e9:  s += 7
    elif mc > 10e9:  s += 4
    elif mc > 1e9:   s += 2
    return min(99, max(30, round(s)))

def week_signal_str(q):
    chg = q.get('regularMarketChangePercent', 0)
    vr  = q.get('_vr', 1)
    p   = q.get('regularMarketPrice', 0)
    hi  = q.get('fiftyTwoWeekHigh', p) or p
    lo  = q.get('fiftyTwoWeekLow', p * 0.7) or p * 0.7
    rng = (p - lo) / ((hi - lo) or 1)
    bull, bear = 0, 0
    if   chg > 4:  bull += 4
    elif chg > 2:  bull += 3
    elif chg > 0:  bull += 1
    elif chg < -4: bear += 4
    elif chg < -2: bear += 3
    elif chg < 0:  bear += 1
    if vr > 2 and chg > 0: bull += 2
    elif vr > 2 and chg < 0: bear += 2
    if   rng > 0.80: bull += 3
    elif rng > 0.60: bull += 2
    elif rng < 0.20: bear += 3
    elif rng < 0.40: bear += 2
    d = bull - bear
    if d >= 4:  return '📈 BULL WEEK'
    if d <= -4: return '📉 BEAR WEEK'
    return '➡ HOLD WEEK'

# ── Paper Trading State ────────────────────────────────────────────────
paper = {
    'capital':          10000.0,
    'starting_capital': 10000.0,
    'trade_seq':        1,          # 1, 2, 3
    'targets':          [0.10, 0.05, 0.05],
    'stop_pct':         0.03,
    'slippage':         0.001,
    'active_trades':    {},         # {symbol: trade_dict}
    'completed':        [],
    'total_pnl':        0.0,
    'status':           'waiting',  # waiting | picked | entered | done
    'picked_symbol':    None,
    'picked_data':      None,
    'date':             None,
    'log':              [],         # activity log
}

def p_log(msg):
    ts = now_et()
    paper['log'].insert(0, {'time': ts, 'msg': msg})
    paper['log'] = paper['log'][:30]
    print(f"  [{ts}] {msg}")

def paper_reset():
    paper.update({
        'capital': 10000.0, 'starting_capital': 10000.0,
        'trade_seq': 1, 'active_trades': {}, 'completed': [],
        'total_pnl': 0.0, 'status': 'waiting',
        'picked_symbol': None, 'picked_data': None,
        'date': datetime.now(ET_TZ).date().isoformat(), 'log': []
    })
    p_log("Paper trading reset for new day")

def get_quotes_for_trading(symbols):
    results = []
    try:
        df5  = yf.download(symbols, period='5d', interval='1d', auto_adjust=True, progress=False)
        df1y = yf.download(symbols, period='1y', interval='1d', auto_adjust=True, progress=False)
        def fetch_info(s):
            try: return s, yf.Ticker(s).info or {}
            except: return s, {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            info_map = dict(ex.map(fetch_info, symbols))
        multi = isinstance(df5.columns, pd.MultiIndex)
        for sym in symbols:
            try:
                c5  = (df5['Close'][sym] if multi else df5['Close']).dropna()
                v5  = (df5['Volume'][sym] if multi else df5['Volume']).dropna()
                c1y = (df1y['Close'][sym] if multi else df1y['Close']).dropna()
                v1y = (df1y['Volume'][sym] if multi else df1y['Volume']).dropna()
                if len(c5) < 1: continue
                price = float(c5.iloc[-1]); prev = float(c5.iloc[-2]) if len(c5)>=2 else price
                chg = ((price-prev)/prev*100) if prev else 0
                info = info_map.get(sym, {})
                avg_vol = int(v1y.mean()) if len(v1y) else 1
                vr = round(int(v5.iloc[-1])/avg_vol, 1) if avg_vol and len(v5) else 1
                results.append({
                    'symbol': sym, 'shortName': info.get('shortName', sym),
                    'regularMarketPrice': round(price, 2),
                    'regularMarketChangePercent': round(chg, 4),
                    'marketCap': int(info.get('marketCap') or 0),
                    'fiftyTwoWeekHigh': round(float(c1y.max()), 2) if len(c1y) else price,
                    'fiftyTwoWeekLow':  round(float(c1y.min()), 2) if len(c1y) else price,
                    '_vr': vr,
                })
            except: pass
    except Exception as e:
        print(f"  Quotes error: {e}")
    return results

def pick_best_stock():
    already = [t['symbol'] for t in paper['completed']]
    quotes = get_quotes_for_trading(TRADE_WATCHLIST[:15])
    eligible = [q for q in quotes
                if q.get('regularMarketPrice', 0) > 5
                and q.get('marketCap', 0) > 5e9
                and q['symbol'] not in already]
    if not eligible: return None
    return max(eligible, key=lambda q: score_stock(q))

def enter_trade(q, price=None):
    tnum = paper['trade_seq']
    ep   = round((price or q['regularMarketPrice']) * (1 + paper['slippage']), 3)
    cap  = paper['capital']
    shrs = int(cap / ep)
    if shrs < 1: return
    tgt_pct = paper['targets'][tnum - 1]
    trade = {
        'symbol':     q['symbol'],
        'name':       q.get('shortName', q['symbol']),
        'trade_num':  tnum,
        'shares':     shrs,
        'entry':      ep,
        'cost':       round(shrs * ep, 2),
        'target':     round(ep * (1 + tgt_pct), 2),
        'stop':       round(ep * (1 - paper['stop_pct']), 2),
        'target_pct': tgt_pct,
        'entry_time': now_et(),
        'current':    ep,
        'peak':       ep,
    }
    paper['active_trades'][q['symbol']] = trade
    paper['status'] = 'entered'
    paper['capital'] = 0  # capital is locked in the trade
    p_log(f"ENTERED {q['symbol']} {shrs} shares @ ${ep:.2f}")
    tg(
        f"📈 TRADE {tnum} ENTERED\n"
        f"{'━'*22}\n"
        f"Stock:  {q['symbol']} — {q.get('shortName','')}\n"
        f"Bought: {shrs} shares @ ${ep:.2f}\n"
        f"Cost:   ${shrs*ep:,.2f}\n"
        f"{'━'*22}\n"
        f"Target: ${trade['target']:.2f} (+{tgt_pct*100:.0f}%)\n"
        f"Stop:   ${trade['stop']:.2f} (-{paper['stop_pct']*100:.0f}%)\n"
        f"Time:   {trade['entry_time']}\n"
        f"{'━'*22}\n"
        f"Dashboard: {APP_URL}"
    )

def close_trade(sym, reason='target'):
    t = paper['active_trades'].pop(sym, None)
    if not t: return
    cached = get_cached_price(sym)
    sell   = round((cached['price'] if cached else t['current']) * (1 - paper['slippage']), 3)
    pnl    = round((sell - t['entry']) * t['shares'], 2)
    pnl_pct= round((sell - t['entry']) / t['entry'] * 100, 2)
    proceeds = round(sell * t['shares'], 2)
    paper['completed'].append({**t, 'exit': sell, 'exit_time': now_et(),
                                'pnl': pnl, 'pnl_pct': pnl_pct, 'reason': reason})
    paper['total_pnl'] += pnl
    paper['capital']    = proceeds
    icons = {'target': '💰', 'stop': '🛑', 'eod': '🔔'}
    labels= {'target': 'TARGET HIT', 'stop': 'STOP LOSS', 'eod': 'EOD CLOSE'}
    p_log(f"CLOSED {sym} {reason.upper()} P&L ${pnl:+.2f} ({pnl_pct:+.2f}%)")
    tg(
        f"{icons.get(reason,'📊')} TRADE {t['trade_num']} CLOSED — {labels.get(reason,'')}\n"
        f"{'━'*22}\n"
        f"Stock:   {sym}\n"
        f"Sold:    {t['shares']} shares @ ${sell:.2f}\n"
        f"Entry:   ${t['entry']:.2f}  →  Exit: ${sell:.2f}\n"
        f"P&L:     {'✅ +' if pnl>=0 else '🔴 '}${abs(pnl):,.2f} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)\n"
        f"Capital: ${proceeds:,.2f}\n"
        f"{'━'*22}\n"
        f"Total today: {'✅ +' if paper['total_pnl']>=0 else '🔴 '}${abs(paper['total_pnl']):,.2f}"
    )
    nxt = paper['trade_seq'] + 1
    if nxt <= 3 and is_market_open():
        paper['trade_seq'] = nxt
        paper['status']    = 'waiting'
        p_log(f"Looking for Trade {nxt} (target +{paper['targets'][nxt-1]*100:.0f}%)...")
        tg(f"🔍 Looking for Trade {nxt}/3 (target +{paper['targets'][nxt-1]*100:.0f}%)...")
        # Pick and enter immediately
        import threading as _th
        _th.Thread(target=_pick_and_enter, daemon=True).start()
    else:
        paper['status'] = 'done'
        if nxt > 3:
            tg(f"✅ All 3 trades complete!\nTotal P&L: {'+'if paper['total_pnl']>=0 else ''}${paper['total_pnl']:,.2f}\n{APP_URL}")

def _pick_and_enter():
    import time as _t; _t.sleep(5)
    q = pick_best_stock()
    if not q:
        tg("⚠️ No suitable stock for next trade."); return
    paper['picked_symbol'] = q['symbol']
    paper['picked_data']   = q
    paper['status']        = 'picked'
    _notify_pick(q)
    _t.sleep(3)
    enter_trade(q)

def _notify_pick(q):
    tnum = paper['trade_seq']
    tp   = paper['targets'][tnum - 1] * 100
    p    = q['regularMarketPrice']
    shrs = int(paper['capital'] / (p * (1 + paper['slippage'])))
    tg(
        f"🔍 TRADE {tnum} PICKED\n"
        f"{'━'*22}\n"
        f"Stock:    {q['symbol']} — {q.get('shortName','')}\n"
        f"Price:    ${p:.2f} ({q['regularMarketChangePercent']:+.2f}% today)\n"
        f"Signal:   {week_signal_str(q)} | Score {score_stock(q)}/100\n"
        f"{'━'*22}\n"
        f"Capital:  ${paper['capital']:,.2f}\n"
        f"Shares:   ~{shrs} @ ${p:.2f}\n"
        f"Target:   ${p*(1+paper['targets'][tnum-1]):.2f} (+{tp:.0f}%)\n"
        f"Stop:     ${p*(1-paper['stop_pct']):.2f} (-{paper['stop_pct']*100:.0f}%)\n"
        f"Entering now..."
    )

# ── Scheduler Jobs ─────────────────────────────────────────────────────
def job_morning():
    if not is_trading_day(): return
    paper_reset()
    p_log("Good morning — starting day")
    tg(
        f"🌅 AUTOTRADE STARTING\n"
        f"{'━'*22}\n"
        f"Date:     {today_str()}\n"
        f"Capital:  $10,000.00\n"
        f"Plan:     Trade 1: +10% | Trade 2&3: +5%\n"
        f"Stop:     -3% on all trades\n"
        f"{'━'*22}\n"
        f"Scanning for best stock...\n"
        f"Dashboard: {APP_URL}"
    )
    q = pick_best_stock()
    if not q:
        tg("⚠️ No pick found. Will retry at 9:30 AM."); return
    paper['picked_symbol'] = q['symbol']
    paper['picked_data']   = q
    paper['status']        = 'picked'
    _notify_pick(q)
    # Check pre-market gap
    cached = get_cached_price(q['symbol'])
    if cached and cached['pct'] > 0.5:
        tg(f"⚡ Pre-market momentum detected. Entering early.")
        enter_trade(q, cached['price'])

def job_market_open():
    if not is_trading_day(): return
    tg(f"🔔 MARKET OPEN — {today_str()}\nWatch for volume in first 10 min.\nDashboard: {APP_URL}")
    if paper['status'] == 'picked' and not paper['active_trades']:
        q = paper.get('picked_data')
        if q:
            cached = get_cached_price(q['symbol'])
            enter_trade(q, cached['price'] if cached else None)
    elif paper['status'] == 'waiting' and paper['trade_seq'] == 1:
        q = pick_best_stock()
        if q:
            paper['picked_symbol'] = q['symbol']
            paper['picked_data']   = q
            paper['status']        = 'picked'
            _notify_pick(q)
            import time as _t; _t.sleep(5)
            enter_trade(q)

def job_monitor():
    """Every 2 min — check active trades"""
    if not paper['active_trades']: return
    for sym, t in list(paper['active_trades'].items()):
        cached = get_cached_price(sym)
        if not cached: continue
        price = cached['price']
        paper['active_trades'][sym]['current'] = price
        paper['active_trades'][sym]['peak']    = max(price, t.get('peak', price))
        pnl_pct = (price - t['entry']) / t['entry'] * 100
        if price >= t['target']:
            close_trade(sym, 'target')
        elif price <= t['stop']:
            close_trade(sym, 'stop')
        # 30-min progress update
        elif datetime.now(ET_TZ).minute % 30 == 0:
            pnl = (price - t['entry']) * t['shares']
            tg(
                f"📊 TRADE {t['trade_num']} UPDATE — {sym}\n"
                f"Entry: ${t['entry']:.2f} | Now: ${price:.2f}\n"
                f"P&L: {'+'if pnl>=0 else ''}${pnl:.2f} ({'+' if pnl_pct>=0 else ''}{pnl_pct:.2f}%)\n"
                f"Target: ${t['target']:.2f} | Stop: ${t['stop']:.2f}"
            )

def job_eod():
    if not is_trading_day(): return
    for sym in list(paper['active_trades'].keys()):
        close_trade(sym, 'eod')
    lines = [
        f"📊 END OF DAY REPORT", f"{'━'*22}",
        f"Date:  {today_str()}",
        f"Start: ${paper['starting_capital']:,.2f}",
        f"End:   ${paper['capital']:,.2f}",
        f"P&L:   {'✅ +' if paper['total_pnl']>=0 else '🔴 '}${abs(paper['total_pnl']):,.2f} ({'+' if paper['total_pnl']>=0 else ''}{paper['total_pnl']/paper['starting_capital']*100:.2f}%)",
        f"{'━'*22}"
    ]
    for t in paper['completed']:
        lines.append(f"{'✅'if t['pnl']>=0 else'🔴'} T{t['trade_num']} {t['symbol']}: {'+'if t['pnl']>=0 else''}${t['pnl']:.2f} ({'+' if t['pnl_pct']>=0 else''}{t['pnl_pct']:.2f}%) [{t['reason'].upper()}]")
    if not paper['completed']:
        lines.append("No trades completed today.")
    lines += [f"{'━'*22}", f"Dashboard: {APP_URL}"]
    tg('\n'.join(lines))
    p_log("EOD report sent")

def job_keepalive():
    if not is_market_open(): return
    try:
        urllib.request.urlopen(urllib.request.Request(f"{APP_URL}/ping",
            headers={'User-Agent':UA}), timeout=8)
    except: pass

# ── News ──────────────────────────────────────────────────────────────
_news_cache = {'items': [], 'ts': 0}

def get_news():
    if time.time() - _news_cache['ts'] < 90 and _news_cache['items']:
        return _news_cache['items']
    items = []
    for url in [
        'https://feeds.finance.yahoo.com/rss/2.0/headline?s=QQQ,SPY,NVDA,TSLA,META&region=US&lang=en-US',
        'https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,AMZN,MSFT,COIN&region=US&lang=en-US'
    ]:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=6) as r:
                tree = ET.fromstring(r.read())
                for item in tree.iter('item'):
                    t = item.find('title')
                    if t is not None and t.text and len(t.text.strip()) > 20:
                        items.append({'title': t.text.strip(), 'time': ''})
        except: pass
    if not items:
        items = [{'title': 'Market data loading — check back shortly', 'time': ''}]
    _news_cache.update({'items': items[:15], 'ts': time.time()})
    return _news_cache['items']

# ── AI Brief ──────────────────────────────────────────────────────────
def get_ai_brief(mkt_data, news_items, trade_info):
    mkt_str   = ', '.join(f"{q['symbol']} ${q['price']} ({q['pct']:+.2f}%)" for q in mkt_data)
    news_str  = ' | '.join(n['title'] for n in news_items[:5])
    trade_str = trade_info
    prompt = (
        f"You are a professional day trader. Market right now: {mkt_str}. "
        f"Active trade: {trade_str}. News: {news_str}. "
        f"Give a 3-4 sentence market brief: overall direction, key risk, "
        f"and whether to hold or adjust the active trade. Be direct and specific."
    )
    if ANTHROPIC_KEY:
        try:
            payload = {"model":"claude-sonnet-4-20250514","max_tokens":200,
                       "messages":[{"role":"user","content":prompt}]}
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={"x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01",
                         "content-type":"application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())['content'][0]['text'], True
        except Exception as e:
            print(f"  AI brief error: {e}")
    # Rule-based fallback
    qqq = next((q for q in mkt_data if q['symbol']=='QQQ'), None)
    spy = next((q for q in mkt_data if q['symbol']=='SPY'), None)
    if qqq and spy:
        both_up = qqq['pct'] > 0 and spy['pct'] > 0
        both_dn = qqq['pct'] < 0 and spy['pct'] < 0
        direction = "bullish" if both_up else "bearish" if both_dn else "mixed"
        brief = f"Market is {direction} today — QQQ {qqq['pct']:+.2f}% and SPY {spy['pct']:+.2f}%. "
        if both_up:
            brief += "Tech and broad market aligned higher — favorable for long positions. "
        elif both_dn:
            brief += "Broad selling pressure — watch your stops carefully. "
        else:
            brief += "Mixed signals — QQQ and SPY diverging, stay disciplined with risk. "
        brief += f"Key news: {news_items[0]['title'] if news_items else 'No major headlines'}."
        return brief, False
    return "Market data loading. Check back in a moment.", False

# ── Flask Routes ───────────────────────────────────────────────────────
def cors(data):
    r = jsonify(data)
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r

@app.route('/')
def index(): return send_from_directory('.', 'index.html')

@app.route('/ping')
def ping(): return cors({'ok': True, 'time': now_et()})

@app.route('/prices')
def prices():
    """Fast — returns cached prices instantly (2-sec polling safe)"""
    return cors({
        'data':    get_all_cached(),
        'updated': datetime.fromtimestamp(_cache_updated, ET_TZ).strftime('%H:%M:%S') if _cache_updated else '—',
        'fresh':   (time.time() - _cache_updated) < 60
    })

@app.route('/market-summary')
def market_summary():
    data    = get_all_cached()
    news    = get_news()
    active  = list(paper['active_trades'].values())
    trade_str = (
        f"In {active[0]['symbol']} Trade {active[0]['trade_num']}/3, "
        f"entry ${active[0]['entry']:.2f}, current ${active[0].get('current',active[0]['entry']):.2f}"
        if active else f"No active trade (Trade {paper['trade_seq']}/3 pending)"
    )
    mkt_list = [v for k, v in data.items() if k in ('QQQ','SPY','BTC-USD')]
    brief, is_ai = get_ai_brief(mkt_list, news, trade_str)
    return cors({'brief': brief, 'ai': is_ai})

@app.route('/news')
def news_route(): return cors({'items': get_news()})

@app.route('/paper-status')
def paper_status():
    active = list(paper['active_trades'].values())
    # Inject live prices
    for t in active:
        cached = get_cached_price(t['symbol'])
        if cached:
            t['current'] = cached['price']
    return cors({
        'status':           paper['status'],
        'trade_seq':        paper['trade_seq'],
        'capital':          round(paper['capital'], 2),
        'starting_capital': paper['starting_capital'],
        'total_pnl':        round(paper['total_pnl'], 2),
        'total_pnl_pct':    round(paper['total_pnl'] / paper['starting_capital'] * 100, 2) if paper['starting_capital'] else 0,
        'active':           active[0] if active else None,
        'completed':        paper['completed'],
        'date':             paper['date'],
        'log':              paper['log'][:10],
        'picked_symbol':    paper['picked_symbol'],
    })

@app.route('/paper-reset')
def paper_reset_route():
    paper_reset()
    return cors({'ok': True})

@app.route('/paper-force-pick')
def paper_force_pick():
    paper_reset()
    import threading as _th
    _th.Thread(target=job_morning, daemon=True).start()
    return cors({'ok': True, 'msg': 'Morning job triggered'})

@app.route('/test-telegram')
def test_telegram():
    ok = tg(
        f"✅ AutoTrade Pro — Telegram Working!\n"
        f"Abiy Kassa · St Louis MO\n\n"
        f"You will receive:\n"
        f"  🔍 Stock pick notifications\n"
        f"  📈 Trade entry alerts\n"
        f"  💰 Target hit alerts\n"
        f"  🛑 Stop loss alerts\n"
        f"  📊 End of day P&L report\n\n"
        f"Dashboard: {APP_URL}"
    )
    if ok: return cors({'ok': True,  'msg': 'Sent! Check Telegram.'})
    return cors({'ok': False, 'msg': 'Failed — check bot token and chat ID on Render.'})

@app.route('/notify')
def notify():
    msg = request.args.get('msg', '')
    return cors({'ok': tg(msg) if msg else False})

# ── Start background price updater + scheduler ─────────────────────────
threading.Thread(target=update_prices_bg, daemon=True).start()
print("  Price cache thread started")

import atexit
try:
    sched = BackgroundScheduler(timezone=ET_TZ)
    sched.add_job(job_morning,      'cron', day_of_week='mon-fri', hour=8,  minute=0)
    sched.add_job(job_market_open,  'cron', day_of_week='mon-fri', hour=9,  minute=30)
    sched.add_job(job_monitor,      'cron', day_of_week='mon-fri', hour='8-16', minute='*/2')
    sched.add_job(job_eod,          'cron', day_of_week='mon-fri', hour=15, minute=55)
    sched.add_job(job_keepalive,    'interval', minutes=10)
    sched.start()
    atexit.register(lambda: sched.shutdown(wait=False))
    print(f"  Scheduler started — {len(sched.get_jobs())} jobs")
except Exception as e:
    print(f"  Scheduler error: {e}")

if __name__ == '__main__':
    print(f"\n  AutoTrade Pro — http://localhost:{PORT}\n")
    app.run(host='0.0.0.0', port=PORT, debug=False)
