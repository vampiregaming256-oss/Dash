"""Live options dashboard server (Flattrade). Run: python server.py"""
import json, math, time, threading, datetime, socket, re, os, hashlib, requests, csv, io
from flask import Flask, jsonify, request, Response, redirect
from api_helper import NorenApiPy

cfg = json.load(open('config.json')) if os.path.exists('config.json') else {}
try:                                           # reuse api_key / api_secret / token url from Flattrade's own gettoken.py
    _g = open('token_generator/gettoken.py').read()
    _m = re.search(r'https://[^"\']*apitoken[^"\']*', _g)
    if _m and not cfg.get('token_url'): cfg['token_url'] = _m.group(0)
except OSError:
    pass
TOKEN_FILE = 'token.json'
S = {'ok': False, 'msg': 'Not logged in', 'ws': False, 'sampler': False, 'day': str(datetime.date.today())}
# name: (spot exchange, token, option exchange, strike step). Verify SENSEX / MIDCPNIFTY / VIX tokens with searchscrip.
IDX = {'NIFTY': ('NSE', '26000', 'NFO', 50), 'BANKNIFTY': ('NSE', '26009', 'NFO', 100),
       'SENSEX': ('BSE', '1', 'BFO', 100), 'MIDCPNIFTY': ('NSE', '26074', 'NFO', 25)}
VIX = ('NSE', '26017')
SYMN = {'SENSEX': ('BSXOPT', 'BSXFUT')}      # BSE uses different internal names: (options, futures)
f = lambda x: float(x) if x not in (None, '') else 0.0
T, CH, SER, SMP, DEPTH, CC, ERR = {}, {}, {}, {}, {}, {}, {}
BAR, BRG, BC, STK, BRS = {}, {}, {}, {}, {'msg': 'not started', 'done': 0, 'total': 0}
OIH, SIG, LAST = {}, [], {}                  # per-contract OI by minute, signal log, last signal per key
SIGF = 'signals.json'
TN = {'5': '5m', '15': '15m', '60': '1h', 'D': '1d'}
HF, STKF = 'hist.json', 'stocks.json'
GN = {'ALL': 'Nifty 500', 'NIFTY': 'Nifty 50', 'BANKNIFTY': 'Bank Nifty', 'SENSEX': 'Sensex 30'}
FB = {  # fallback stock lists if the official NSE lists cannot be downloaded (add "sensex_stocks": [...] to config.json to override Sensex)
 'NIFTY': 'RELIANCE TCS HDFCBANK ICICIBANK INFY BHARTIARTL ITC SBIN LT HINDUNILVR KOTAKBANK AXISBANK BAJFINANCE MARUTI SUNPHARMA NTPC ONGC M&M HCLTECH TITAN POWERGRID ULTRACEMCO ASIANPAINT BAJAJFINSV TATASTEEL WIPRO JSWSTEEL ADANIENT ADANIPORTS COALINDIA NESTLEIND TECHM GRASIM HINDALCO CIPLA DRREDDY EICHERMOT APOLLOHOSP BPCL BRITANNIA HEROMOTOCO INDUSINDBK SBILIFE HDFCLIFE TATACONSUM SHRIRAMFIN TRENT BEL ETERNAL JIOFIN TATAMOTORS',
 'BANKNIFTY': 'HDFCBANK ICICIBANK SBIN KOTAKBANK AXISBANK INDUSINDBK BANKBARODA PNB FEDERALBNK IDFCFIRSTB AUBANK CANBK',
 'SENSEX': 'RELIANCE HDFCBANK ICICIBANK INFY BHARTIARTL ITC TCS LT SBIN AXISBANK KOTAKBANK HINDUNILVR BAJFINANCE M&M MARUTI SUNPHARMA NTPC TITAN HCLTECH POWERGRID ULTRACEMCO ASIANPAINT BAJAJFINSV TATASTEEL TECHM NESTLEIND ADANIPORTS TATAMOTORS INDUSINDBK ETERNAL'}
lock = threading.Lock()
api = NorenApiPy()
opened = threading.Event()

def on_tick(m):
    if 'e' in m and 'tk' in m:
        with lock:
            T.setdefault(m['e'] + '|' + m['tk'], {}).update(m)

# ---------- expiries (discovered from Flattrade, so holiday-shifted expiry days are handled) ----------
EXP = {}
def pdate(x):
    return datetime.datetime.strptime(x, '%d-%b-%Y').date()

def discover(sym):
    c = EXP.get(sym)
    if c and time.time() - c[0] < 300:
        return c[1]
    ex, tok, oex, step = IDX[sym]
    try:
        spot = f(api.get_quotes(exchange=ex, token=tok).get('lp')); atm = int(round(spot / step) * step)
        r = api.searchscrip(exchange=oex, searchtext=f'{sym} {atm} CE') or {}
        now = datetime.datetime.now(); out = {}
        for v in r.get('values', []):
            if v.get('instname') == 'OPTIDX' and v.get('symname') == SYMN.get(sym, (sym, sym))[0]:
                d = pdate(v['exd'])
                if d > now.date() or (d == now.date() and now.hour < 15):
                    out[d] = (v['tsym'], int(float(v.get('strprc') or atm)))
        res = {d.strftime('%d%b%y').upper(): out[d] for d in sorted(out)}
        if res:
            EXP[sym] = (time.time(), res)
        return res
    except Exception as e:
        print('expiry discovery error', sym, e)
        return c[1] if c else {}

def expiries(sym):
    return list(discover(sym))[:6]

def find_future(sym):
    oex = IDX[sym][2]
    r = api.searchscrip(exchange=oex, searchtext=f'{sym} FUT') or {}
    c = [(pdate(v['exd']), v) for v in r.get('values', []) if v.get('instname') == 'FUTIDX' and v.get('symname') == SYMN.get(sym, (sym, sym))[1]]
    c = sorted(x for x in c if x[0] >= datetime.date.today())
    return (f"{oex}|{c[0][1]['token']}", c[0][1]['tsym']) if c else (None, None)

# ---------- Black-Scholes IV and greeks ----------
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
def bs(S, K, t, v, cp, r=0.07):
    d1 = (math.log(S / K) + (r + v * v / 2) * t) / (v * math.sqrt(t)); d2 = d1 - v * math.sqrt(t)
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi); e = K * math.exp(-r * t)
    if cp == 'c':
        p, dl, th2 = S * N(d1) - e * N(d2), N(d1), -r * e * N(d2)
    else:
        p, dl, th2 = e * N(-d2) - S * N(-d1), N(d1) - 1, r * e * N(-d2)
    th = (-S * pdf * v / (2 * math.sqrt(t)) + th2) / 365
    return p, dl, pdf / (S * v * math.sqrt(t)), th, S * pdf * math.sqrt(t) / 100

def greeks(S, K, t, price, cp):
    if price <= 0 or t <= 0 or S <= 0:
        return {}
    lo, hi = 0.01, 3.0
    if bs(S, K, t, lo, cp)[0] > price or bs(S, K, t, hi, cp)[0] < price:
        return {}
    for _ in range(50):
        m = (lo + hi) / 2
        if bs(S, K, t, m, cp)[0] < price: lo = m
        else: hi = m
    v = (lo + hi) / 2; _, d, g, th, vg = bs(S, K, t, v, cp)
    return dict(iv=round(v * 100, 2), d=round(d, 3), g=round(g, 5), t=round(th, 2), vg=round(vg, 2))

# ---------- option chain (built on demand per expiry) ----------
def oi_backfill(keys):                        # full-session OI per minute for each contract (so charts start at 09:15)
    st = int((datetime.datetime.now() - datetime.timedelta(days=4)).timestamp())
    for k in keys:
        ex, tk = k.split('|')
        try:
            r = api.get_time_price_series(exchange=ex, token=tk, starttime=st, endtime=int(time.time()), interval=1)
            if isinstance(r, list) and r:
                dts = {x['time'][:10] for x in r}; last = max(dts, key=dkey)
                h = {x['time'][11:16]: f(x.get('intoi') or x.get('oi')) for x in r if x['time'][:10] == last}
                h = {l: v for l, v in h.items() if v}
                if h:
                    with lock: OIH.setdefault(k, {}).update({l: v for l, v in h.items() if l not in OIH.get(k, {})})
        except Exception as e:
            print('oi backfill error', k, e); break
        time.sleep(0.08)

def ensure_chain(sym, exp):
    key = (sym, exp)
    if key in CH or time.time() < ERR.get(key, 0):
        return
    CH[key] = {}
    oex = IDX[sym][2]
    try:
        anchor, k0 = discover(sym)[exp]
        r = api.get_option_chain(exchange=oex, tradingsymbol=anchor, strikeprice=k0, count=10)
        toks = []
        for c in r['values']:
            K = int(float(c['strprc'])); s = 'ce' if c['optt'].upper().startswith('C') else 'pe'
            k = f"{c['exch']}|{c['token']}"
            CH[key].setdefault(K, {})[s] = k; SER.setdefault(k, []); SMP.setdefault(k, []); toks.append(k)
        api.subscribe(toks)
        print('chain ready', sym, exp, len(toks), 'contracts')
        oi_backfill(toks)
    except Exception as e:
        print('chain error', sym, exp, e); CH.pop(key, None); ERR[key] = time.time() + 30

def save_hist():
    try:
        with lock: txt = json.dumps({'date': str(datetime.date.today()), 'SMP': SMP, 'SER': SER, 'OIH': OIH})
        open(HF, 'w').write(txt)
    except Exception as e:
        print('history save error', e)

def load_hist():
    try:
        d = json.load(open(HF))
        if d['date'] == str(datetime.date.today()):
            SMP.update(d['SMP']); SER.update(d['SER']); OIH.update(d.get('OIH', {})); print('today history restored')
    except Exception:
        pass

def sampler():                                # one sample per minute per contract
    n = 0
    while True:
        time.sleep(60); now = time.time(); n += 1; today = str(datetime.date.today())
        with lock:
            if S['day'] != today:             # new trading day: start fresh
                S['day'] = today
                for d in (CH, EXP, SER, SMP, BAR, CC, BC, OIH, LAST): d.clear()
                SIG.clear()
            for k in list(SER):
                t = T.get(k, {}); lp, oi, poi = f(t.get('lp')), f(t.get('oi')), f(t.get('poi'))
                SMP[k].append((now, lp, oi, f(t.get('bq1')), f(t.get('sq1')))); SER[k].append(round((oi - poi) / 1000, 2))
                if 555 <= datetime.datetime.now().hour * 60 + datetime.datetime.now().minute <= 930 and oi: OIH.setdefault(k, {})[time.strftime('%H:%M')] = oi
                del SMP[k][:-480]; del SER[k][:-480]
            m = datetime.datetime.now(); mm = m.hour * 60 + m.minute
            if 555 <= mm <= 930:              # keep today's 5-minute closes of all tracked stocks current
                b = f'{mm // 5 * 5 // 60:02d}:{mm // 5 * 5 % 60:02d}'
                for toks in BRG.values():
                    for t in toks:
                        lp = f(T.get(t, {}).get('lp'))
                        if lp and t in BAR: BAR[t].setdefault(datetime.date.today().strftime('%d-%m-%Y'), {})[b] = lp
        try: log_signals()
        except Exception as e: print('signal log error', e)
        if n % 2 == 0: save_hist(); save_signals()

# ---------- market breadth (advances / declines) ----------
def load_groups():
    out = {}
    for g, name in (('NIFTY', 'ind_nifty50list'), ('BANKNIFTY', 'ind_niftybanklist'), ('ALL', 'ind_nifty500list')):
        try:
            r = requests.get(f'https://nsearchives.nseindia.com/content/indices/{name}.csv', headers={'User-Agent': 'Mozilla/5.0'}, timeout=15)
            out[g] = [x['Symbol'].strip() for x in csv.DictReader(io.StringIO(r.text)) if x.get('Symbol')]
        except Exception as e:
            print('stock list download failed for', g, e)
        if len(out.get(g, [])) < 5:
            out[g] = (FB.get(g) or FB['NIFTY']).split()
    out['SENSEX'] = cfg.get('sensex_stocks') or FB['SENSEX'].split()
    return out

def resolve(sym):
    r = api.searchscrip(exchange='NSE', searchtext=sym + '-EQ') or {}
    time.sleep(0.1)
    for v in r.get('values', []):
        if v.get('tsym') == sym + '-EQ':
            STK[sym] = 'NSE|' + v['token']; return STK[sym]

def bars_for(tok):                            # last few sessions of 5m closes for one stock: {date: {HH:MM: close}}
    ex, tk = tok.split('|')
    st = int((datetime.datetime.now() - datetime.timedelta(days=6)).timestamp())
    r = api.get_time_price_series(exchange=ex, token=tk, starttime=st, endtime=int(time.time()), interval=5)
    if not isinstance(r, list) or not r: return None
    out = {}
    for x in r:
        out.setdefault(x['time'][:10], {})[x['time'][11:16]] = f(x['intc'])
    return out if len(out) >= 2 else None

def breadth_setup():
    BRS.update(msg='loading', done=0, total=0)
    try:
        if not STK and os.path.exists(STKF): STK.update(json.load(open(STKF)))
        G = load_groups()
        for g in ('NIFTY', 'BANKNIFTY', 'SENSEX', 'ALL'):
            toks = [STK.get(x) or resolve(x) for x in G[g]]; toks = [t for t in toks if t]
            BRG[g] = toks
            for i in range(0, len(toks), 100): api.subscribe(toks[i:i + 100])
            json.dump(STK, open(STKF, 'w'))
            need = [t for t in toks if t not in BAR]; BRS['total'] += len(need)
            for t in need:
                try:
                    b = bars_for(t)
                    if b: BAR[t] = b
                except Exception as e:
                    print('bars error', t, e)
                BRS['done'] += 1; time.sleep(0.1)
        BRS['msg'] = 'ready'; print('breadth ready')
    except Exception as e:
        BRS['msg'] = 'error: ' + str(e); print('breadth error', e)

def live_counts(g):
    a = d = u = 0
    for t in BRG.get(g, []):
        x = T.get(t, {}); pc = x.get('pc')
        if pc not in (None, ''): p = f(pc)
        else:
            c = f(x.get('c')); p = (f(x.get('lp')) - c) / c * 100 if c and x.get('lp') else None
        if p is None: continue
        if p > 0: a += 1
        elif p < 0: d += 1
        else: u += 1
    return a, d, u

dkey = lambda d: d[6:] + d[3:5] + d[:2]
def hist(g, tf):                              # advances / declines per time step, for the chart time frame
    toks = [BAR[t] for t in BRG.get(g, []) if t in BAR and len(BAR[t]) >= 2]
    if not toks: return []
    lastc = lambda b, d: b[d][max(b[d])]
    if tf == 'D':
        dates = sorted({d for b in toks for d in b}, key=dkey)[-6:]; out = []
        for i in range(1, len(dates)):
            a = dn = 0
            for b in toks:
                if dates[i] in b and dates[i - 1] in b:
                    c, p = lastc(b, dates[i]), lastc(b, dates[i - 1]); a += c > p; dn += c < p
            out.append([dates[i][:5].replace('-', '/'), a, dn])
        return out
    win = {'5': 5, '15': 15, '60': 60}.get(tf, 5); res = {}
    for b in toks:
        ds = sorted(b, key=dkey); p = lastc(b, ds[-2]); w = {}
        for lab in sorted(b[ds[-1]]):
            m = int(lab[:2]) * 60 + int(lab[3:]) - 555
            if m >= 0: w[m // win] = b[ds[-1]][lab]
        for k, c in w.items():
            r = res.setdefault(k, [0, 0]); r[0] += c > p; r[1] += c < p
    return [[f'{(555 + k * win) // 60:02d}:{(555 + k * win) % 60:02d}', v[0], v[1]] for k, v in sorted(res.items())]

def bdata(g, tf='5'):
    a, d, u = live_counts(g); c = BC.get((g, tf))
    if not c or time.time() - c[0] > 20:
        c = (time.time(), hist(g, tf)); BC[(g, tf)] = c
    return dict(adv=a, dec=d, un=u, ratio=round(a / d, 2) if d else float(a), hist=c[1], ready=a + d + u > 0, name=GN.get(g, g))

def levels(c5):                               # opening range (first 15 min) and previous-day high / low
    days = []
    for x in c5 or []:
        if not days or days[-1][0] != x['t'][:10]: days.append([x['t'][:10], []])
        days[-1][1].append(x)
    out = {}
    if days:
        cur = days[-1][1]
        if len(cur) >= 3: out['orb_hi'] = max(x['h'] for x in cur[:3]); out['orb_lo'] = min(x['l'] for x in cur[:3])
        if len(days) > 1: out['pdh'] = max(x['h'] for x in days[-2][1]); out['pdl'] = min(x['l'] for x in days[-2][1])
    return out

def candles(sym, tf):
    c = CC.get((sym, tf))
    if c and time.time() - c[0] < 15:
        return c[1]
    ex, tok = IDX[sym][:2]
    days = {'5': 7, '15': 14, '60': 60, 'D': 200}[tf]; iv = {'5': 5, '15': 15, '60': 60, 'D': 60}[tf]
    st = int((datetime.datetime.now() - datetime.timedelta(days=days)).timestamp())
    out = []
    try:
        r = api.get_time_price_series(exchange=ex, token=tok, starttime=st, endtime=int(time.time()), interval=iv)
        if isinstance(r, list) and r:
            rows = sorted(r, key=lambda x: datetime.datetime.strptime(x['time'], '%d-%m-%Y %H:%M:%S'))
            out = [{'t': x['time'], 'o': f(x['into']), 'h': f(x['inth']), 'l': f(x['intl']), 'c': f(x['intc'])} for x in rows]
            if tf == 'D':                     # merge hourly bars into daily bars
                dd = {}
                for x in out:
                    d = dd.setdefault(x['t'][:10], dict(x)); d['h'] = max(d['h'], x['h']); d['l'] = min(d['l'], x['l']); d['c'] = x['c']
                out = list(dd.values())
        else:
            print('no candles', sym, tf, str(r)[:150])
    except Exception as e:
        print('candle error', sym, tf, e)
    if out or not c:
        CC[(sym, tf)] = (time.time(), out)
    else:
        CC[(sym, tf)] = (time.time(), c[1])
    return CC[(sym, tf)][1]

_ip = [0, '']
def pubip():
    if time.time() - _ip[0] > 300:
        try: _ip[1] = requests.get('https://api.ipify.org', timeout=4).text.strip()
        except Exception: pass
        _ip[0] = time.time()
    return _ip[1]

def exchange(code):                              # request code -> token (same call as gettoken.py)
    h = hashlib.sha256((cfg['api_key'] + code + cfg['api_secret']).encode()).hexdigest()
    url = cfg.get('token_url', 'https://authapi.flattrade.in/trade/apitoken')
    return requests.post(url, json={'api_key': cfg['api_key'], 'request_code': code, 'api_secret': h}, timeout=15).json()

def start_session(userid, token):
    S.update(ok=False, msg='Starting...')
    api.set_session(userid=userid, password='', usertoken=token)
    r = api.get_limits()
    if not r or r.get('stat') != 'Ok':
        S['msg'] = 'Token rejected: ' + str((r or {}).get('emsg', 'no response')); return False
    json.dump({'userid': userid, 'token': token, 'ts': time.time()}, open(TOKEN_FILE, 'w'))
    with lock:
        CH.clear(); EXP.clear(); ERR.clear(); CC.clear()
    try:
        if S['ws']: api.close_websocket()
    except Exception: pass
    opened.clear()
    api.start_websocket(order_update_callback=lambda m: None, subscribe_callback=on_tick, socket_open_callback=opened.set)
    S['ws'] = True; opened.wait(20)
    api.subscribe([f'{e}|{t}' for e, t, _, _ in IDX.values()] + [f'{VIX[0]}|{VIX[1]}'])
    for sym in IDX:
        try:
            key, name = find_future(sym)
            if key:
                DEPTH[sym] = key; api.subscribe(key, feed_type='d'); print('depth:', sym, name)
        except Exception as e:
            print('depth setup error', sym, e)
    if not S['sampler']:
        threading.Thread(target=sampler, daemon=True).start(); S['sampler'] = True
    S.update(ok=True, msg='Logged in as ' + userid); print('session ready for', userid)
    threading.Thread(target=breadth_setup, daemon=True).start()
    threading.Thread(target=prebuild, daemon=True).start()
    return True

def ema(a, n):
    k = 2 / (n + 1); p = a[0] if a else 0
    for c in a[1:]: p += k * (c - p)
    return p

def flow_tilt(rows):                          # buyers vs sellers estimated from new OI and premium direction
    A = dict(cb=0, cs=0, pb=0, ps=0)
    for r in rows:
        for s_ in ('ce', 'pe'):
            o = r[s_]
            if o['lp'] and o['doi'] > 0: A[('c' if s_ == 'ce' else 'p') + ('b' if o['dlp'] >= 0 else 's')] += o['doi'] * o['lp'] / 1e7
    bu, be = A['cb'] + A['ps'], A['cs'] + A['pb']
    return 1 if bu > be * 1.15 else -1 if be > bu * 1.15 else 0

def rank_opts(rows, tilt):
    ok = [o for r in rows for o in (r['ce'], r['pe']) if o['lp'] > 0 and o.get('g') is not None]
    gm = max([o['g'] for o in ok] or [0]); vm = max([o['v'] for o in ok] or [0]); out = []
    for r in rows:
        for s_ in ('ce', 'pe'):
            o = r[s_]
            if not o['lp'] >= 1 or o.get('g') is None: continue
            sp = (o['sp'] - o['bp']) / o['lp'] if o['sp'] > 0 and o['bp'] > 0 else .08
            sc = (40 * (1 - min(1, abs(abs(o['d']) - .45) / .45)) + 25 * (o['g'] / gm if gm else 0) + 15 * (1 - min(1, abs(o['t']) / o['lp'] / .25))
                  + 10 * (1 - min(1, sp / .05)) + 10 * (min(1, o['v'] / vm) if vm else 0) + (5 if (s_ == 'ce' and tilt > 0) or (s_ == 'pe' and tilt < 0) else 0))
            out.append((sc, r['K'], s_, o))
    return out

def evaluate(d):
    R = d['rows']
    if not R: return {}
    tf = d.get('tf', '5'); sp = d['spot']; stp = R[1]['K'] - R[0]['K'] if len(R) > 1 else 50
    nr = [r for r in R if abs(r['K'] - sp) <= 5 * stp]; sm = lambda s_, k: sum(r[s_][k] for r in nr)
    cl = [c['c'] for c in d['candles']]; e9, e21 = ema(cl[-40:], 9), ema(cl[-60:], 21)
    tr = (1 if e9 > e21 and cl[-1] > e21 else -1 if e9 < e21 and cl[-1] < e21 else 0) if len(cl) > 25 else 0
    lv = d['lv']; orb = (1 if sp > lv['orb_hi'] else -1 if sp < lv['orb_lo'] else 0) if 'orb_hi' in lv else 0
    pdb = (1 if sp > lv['pdh'] else -1 if sp < lv['pdl'] else 0) if 'pdh' in lv else 0
    G, A = d['br']['G'], d['br']['ALL']; gr = G['ratio'] if G['ready'] else 1; ar = A['ratio'] if A['ready'] else 1
    coiC = sum(r['ce']['oi'] - r['ce']['poi'] for r in nr); coiP = sum(r['pe']['oi'] - r['pe']['poi'] for r in nr)
    pcr = sm('pe', 'oi') / sm('ce', 'oi') if sm('ce', 'oi') else 1; vC, vP = sm('ce', 'v'), sm('pe', 'v')
    B = [b for b in d['book'] if b['ap'] or b['bp']]; tb = sum(b['b'] for b in B); ta = sum(b['a'] for b in B); im = tb / (tb + ta) if tb + ta else .5
    ba = d['ba']; last = lambda k: ba[k][-1] if ba[k] else 0
    rC, rP = last('cb') / (last('ca') or 1), last('pb') / (last('pa') or 1)
    mC = max(((r['K'], r['ce']['oi']) for r in R if r['K'] > sp), key=lambda x: x[1], default=(0, 0))
    mP = max(((r['K'], r['pe']['oi']) for r in R if r['K'] < sp), key=lambda x: x[1], default=(0, 0))
    vix, vpc = d['ind']['VIX']['ltp'], d['ind']['VIX']['pc']
    atm = min(R, key=lambda r: abs(r['K'] - sp)); ivs = [x['iv'] for x in (atm['ce'], atm['pe']) if x.get('iv')]
    ivA = sum(ivs) / 2 if len(ivs) == 2 else (vix or 14); mv = sp * ivA / 100 * math.sqrt(1 / 1575)
    now = datetime.datetime.now(); m = now.hour * 60 + now.minute; win = 570 <= m <= 690 or 810 <= m <= 885
    tilt = flow_tilt(R); rk = rank_opts(R, tilt); res = {}
    for s_ in ('ce', 'pe'):
        c = [x for x in rk if x[2] == s_]
        if not c: continue
        sc, K, _, o = max(c, key=lambda x: x[0]); bu = s_ == 'ce'; z = 1 if bu else -1
        spr = (o['sp'] - o['bp']) / o['lp'] if o['sp'] > 0 and o['bp'] > 0 else 1
        room = (mC[0] - sp if mC[0] else 1e9) if bu else (sp - mP[0] if mP[0] else 1e9)
        th = abs(o['t']) / 6.25; gain = abs(o['d']) * mv + .5 * o['g'] * mv * mv - th; loss = -abs(o['d']) * mv + .5 * o['g'] * mv * mv - th; rr = gain / -loss if loss < 0 else 0
        ck = [(f"Trend: EMA 9 vs 21 ({TN[tf]} chart)", tr == z, 10),
              (f"Breakout: {'above' if bu else 'below'} first-15-min range or previous day {'high' if bu else 'low'}", orb == z or pdb == z, 10),
              (f"{G['name']} breadth {gr:.2f}" + (' (need 1.5+)' if bu else ' (need 0.67 or less)'), gr >= 1.5 if bu else gr <= .67, 10),
              (f"Whole market breadth {ar:.2f}", ar >= 1.3 if bu else ar <= .77, 5),
              ("Option flow: " + ('call buying / put selling' if bu else 'put buying / call selling'), tilt == z, 12),
              ("COI near ATM: " + ('put OI added more than call' if bu else 'call OI added more than put'), coiP > coiC if bu else coiC > coiP, 10),
              (f"PCR {pcr:.2f}" + (' (above 1)' if bu else ' (below 0.9)'), pcr > 1 if bu else pcr < .9, 5),
              ("Volume: " + ('calls' if bu else 'puts') + " more active", vC > vP * 1.1 if bu else vP > vC * 1.1, 5),
              (f"Futures depth {im * 100:.0f}% bids", im > .55 if bu else im < .45, 5),
              (f"Club bid/ask: call {rC:.2f} vs put {rP:.2f}", rC > rP if bu else rP > rC, 8),
              (f"Contract spread {spr * 100:.1f}% (under 3%)", spr < .03, 5),
              (f"Greeks: delta {o['d']:.2f}, theta loss {abs(o['t']) / o['lp'] * 100:.0f}%/day, IV {o['iv']:.1f}", .35 <= abs(o['d']) <= .65 and abs(o['t']) / o['lp'] <= .15 and (not vix or o['iv'] <= vix * 1.2), 10),
              (f"Reward:risk on a 1-hour move {rr:.2f} (need 1.3+)", rr >= 1.3, 7),
              ("Good trading window (9:30-11:30 or 13:30-14:45)", win, 5), ("India VIX not falling", vpc >= 0, 3),
              (f"Room to {'call' if bu else 'put'} OI wall {(mC if bu else mP)[0]}", room > sp * .0025, 5)]
        en = o['sp'] if o['sp'] > 0 else o['lp']; tot = sum(x[2] for x in ck)
        res[s_] = dict(side=s_, K=K, entry=en, stop=en * .75, t1=en * 1.35, t2=en * 1.6, lp=o['lp'], gp=gain / o['lp'] * 100, ls=loss / o['lp'] * 100, rr=rr,
                       pct=round(sum(x[2] for x in ck if x[1]) / tot * 100), checks=[[x[0], bool(x[1]), x[2]] for x in ck])
    def vd(a, b):
        if not a: return ''
        if a['pct'] >= 70 and (not b or b['pct'] <= 45): return 'STRONG'
        if a['pct'] >= 55 and (not b or a['pct'] >= b['pct'] + 10): return 'MODERATE'
        return 'NO TRADE'
    for s_, o_ in (('ce', 'pe'), ('pe', 'ce')):
        if s_ in res: res[s_]['verdict'] = vd(res[s_], res.get(o_))
    res['mv'] = mv
    return res

def save_signals():
    try: open(SIGF, 'w').write(json.dumps({'date': str(datetime.date.today()), 'sigs': SIG}))
    except Exception as e: print('signal save error', e)

def load_signals():
    try:
        d = json.load(open(SIGF))
        if d['date'] == str(datetime.date.today()): SIG.extend(d['sigs'])
    except Exception: pass

def notify(r):                                # optional Telegram alert (set telegram_token and telegram_chat in config.json)
    tk, ch = cfg.get('telegram_token'), cfg.get('telegram_chat')
    if not (tk and ch): return
    try:
        requests.post(f'https://api.telegram.org/bot{tk}/sendMessage', timeout=8, json={'chat_id': ch, 'text':
            f"{r['time']} {r['sym']} {TN[r['tf']]} BUY {'CALL' if r['side'] == 'ce' else 'PUT'} {r['K']} @ {r['entry']:.1f} | {r['verdict']} {r['pct']}% | SL {r['stop']:.1f} T1 {r['t1']:.1f} T2 {r['t2']:.1f}"})
    except Exception as e: print('telegram error', e)

def log_signals():                            # every minute: evaluate each index on 5m / 15m / 1h and record new signals
    now = datetime.datetime.now(); mm = now.hour * 60 + now.minute
    if not S['ok'] or not 560 <= mm <= 915: return
    for sym in IDX:
        ex = expiries(sym)
        if not ex or not CH.get((sym, ex[0])): continue
        for tf in ('5', '15', '60'):
            d = build(sym, ex[0], tf, 3, 0); sg = evaluate(d)
            for side in ('ce', 'pe'):
                x = sg.get(side); key = (sym, tf, side)
                if not x or x['verdict'] == 'NO TRADE': LAST.pop(key, None); continue
                p = LAST.get(key)
                if p and p['v'] == x['verdict'] and time.time() - p['ts'] < 1800: continue
                LAST[key] = {'v': x['verdict'], 'ts': time.time()}
                rec = dict(ts=int(time.time()), time=now.strftime('%H:%M'), sym=sym, exp=ex[0], tf=tf, side=side, K=x['K'], entry=round(x['entry'], 2), stop=round(x['stop'], 2),
                           t1=round(x['t1'], 2), t2=round(x['t2'], 2), pct=x['pct'], verdict=x['verdict'], gp=round(x['gp']), ls=round(x['ls']), rr=round(x['rr'], 2), spot=round(d['spot'], 1))
                SIG.insert(0, rec); del SIG[300:]; notify(rec)

def prebuild():                               # build the first expiry chain of every index so signals keep logging with no page open
    for sym in IDX:
        try:
            e = expiries(sym)
            if e: ensure_chain(sym, e[0])
        except Exception as ex: print('prebuild error', sym, ex)

app = Flask(__name__)

PAGE = '<meta name=viewport content="width=device-width,initial-scale=1"><body style="font:15px system-ui;max-width:520px;margin:20px auto;padding:0 12px;background:#0d1117;color:#e6edf3">'
def saved_user():
    try: return json.load(open(TOKEN_FILE)).get('userid', '')
    except Exception: return ''

def login_page(note=''):
    if not cfg.get('api_key') or not cfg.get('api_secret'):
        return PAGE + '<h3>First, save your Flattrade API details</h3><p><a style="color:#4aa3ff" href="/setup">Open settings</a></p>'
    link = 'https://auth.flattrade.in/?app_key=' + cfg.get('api_key', ''); uid = cfg.get('userid') or saved_user()
    return PAGE + f"""<h3>Flattrade login</h3><p>{note}</p><p><a style="background:#26a69a;color:#000;padding:12px 16px;border-radius:8px;text-decoration:none;font-weight:600" href="{link}">1. Login with Flattrade</a></p>
<p>Flattrade sends you back here and the dashboard starts by itself. If you only get a token, paste it below.</p>
<form method=post action=/token><input name=userid value="{uid}" placeholder="User ID" style="width:100%;padding:10px;margin:4px 0"><input name=token placeholder="Paste token here" style="width:100%;padding:10px;margin:4px 0"><button style="padding:12px 16px;font-weight:600">2. Update token and start</button></form>
<p style="color:#8b949e">Status: {S['msg']}<br>Server public IP: {pubip() or 'unknown'} (must equal the Primary IP in your Flattrade API key)<br>No .py file needs editing: the server saves the token itself.</p><p><a style="color:#4aa3ff" href="/">Back to dashboard</a></p>"""

@app.route('/')
def home():
    code = request.args.get('code')
    if code:
        try:
            r = exchange(code)
        except Exception as e:
            return login_page('Token request failed: ' + str(e))
        if r.get('stat') == 'Ok':
            threading.Thread(target=start_session, args=(r['client'], r['token']), daemon=True).start()
            return redirect('/')
        return login_page('Flattrade said: ' + str(r.get('emsg')) + (' (your current public IP is ' + pubip() + ')' if 'IP' in str(r.get('emsg')) else ''))
    return Response(open('dashboard.html', encoding='utf-8').read(), mimetype='text/html')

SETUP_FIELDS = ('api_key', 'api_secret', 'userid', 'telegram_token', 'telegram_chat')
@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if request.method == 'POST':
        for k in SETUP_FIELDS:
            v = (request.form.get(k) or '').strip()
            if v: cfg[k] = v
        json.dump(cfg, open('config.json', 'w')); os.chmod('config.json', 0o600)
        return redirect('/login')
    inp = lambda k, ph, t='text': f'<input name={k} type={t} placeholder="{ph}" style="width:100%;padding:10px;margin:4px 0">'
    st = lambda k: 'saved' if cfg.get(k) else 'not set'
    return PAGE + f'''<h3>Settings (one time)</h3><p>Enter your Flattrade API details. Leave a box empty to keep the saved value.</p>
<form method=post>{inp('api_key', 'API key (' + st('api_key') + ')')}{inp('api_secret', 'API secret (' + st('api_secret') + ')', 'password')}{inp('userid', 'User ID, for example FZ12345 (' + st('userid') + ')')}
<p style="color:#8b949e">Optional Telegram alerts</p>{inp('telegram_token', 'Telegram bot token (' + st('telegram_token') + ')')}{inp('telegram_chat', 'Telegram chat id (' + st('telegram_chat') + ')')}
<button style="padding:12px 16px;font-weight:600">Save</button></form><p><a style="color:#4aa3ff" href="/login">Go to login</a></p>'''

@app.route('/login')
def login():
    return login_page()

@app.route('/token', methods=['POST'])
def token():
    uid = (request.form.get('userid') or '').strip() or cfg.get('userid') or saved_user(); tok = (request.form.get('token') or '').strip()
    api.set_session(userid=uid, password='', usertoken=tok); r = api.get_limits()
    if not r or r.get('stat') != 'Ok':
        return login_page('Token not accepted: ' + str((r or {}).get('emsg', 'no response')) + '. Check the user ID and the token.')
    threading.Thread(target=start_session, args=(uid, tok), daemon=True).start()
    return PAGE + '<meta http-equiv=refresh content="3;url=/"><h3 style="color:#26a69a">Token updated successfully</h3><p>Starting the live dashboard...</p>'

def build(sym, exp, tf, club, center):
    exps = expiries(sym) or ['-']; exp = exp if exp in exps else exps[0]
    key = (sym, exp)
    if key not in CH and exp != '-':
        threading.Thread(target=ensure_chain, args=(sym, exp), daemon=True).start()
    cd = candles(sym, tf); lv = levels(cd if tf == '5' else candles(sym, '5'))
    with lock:
        g = lambda e, t: T.get(f'{e}|{t}', {})
        ind = {s: {'ltp': f(g(e, t).get('lp')), 'pc': f(g(e, t).get('pc'))} for s, (e, t, _, _) in IDX.items()}
        ind['VIX'] = {'ltp': f(g(*VIX).get('lp')), 'pc': f(g(*VIX).get('pc'))}
        spot = ind[sym]['ltp']
        ft = g(*IDX[sym][:2]).get('ft'); feed = time.strftime('%d %b %H:%M:%S', time.localtime(int(float(ft)))) if ft else ''
        if cd and spot:
            cd[-1]['c'] = spot; cd[-1]['h'] = max(cd[-1]['h'], spot); cd[-1]['l'] = min(cd[-1]['l'], spot)
        try:
            ty = max((datetime.datetime.strptime(exp, '%d%b%y').replace(hour=15, minute=30) - datetime.datetime.now()).total_seconds() / 31536000, 1e-5)
        except ValueError:
            ty = 1e-5
        ks = sorted(CH.get(key, {})); rows = []
        for K in ks:
            row = {'K': K}
            for s in ('ce', 'pe'):
                k = CH[key][K].get(s); t = T.get(k, {}) if k else {}
                lp, oi, poi = f(t.get('lp')), f(t.get('oi')), f(t.get('poi'))
                old = next((x for x in SMP.get(k, []) if 60 <= time.time() - x[0] <= 330), None)
                dlp, doi, w = (lp - old[1], oi - old[2], '5m') if old else (lp - f(t.get('c')), oi - poi, 'day')
                o = dict(lp=lp, oi=oi, poi=poi, v=f(t.get('v')), bp=f(t.get('bp1')), bq=f(t.get('bq1')), sp=f(t.get('sp1')),
                         sq=f(t.get('sq1')), ls=f(t.get('ls')) or 1, dlp=round(dlp, 2), doi=doi, w=w)
                o.update(greeks(spot, K, ty, lp, 'c' if s == 'ce' else 'p'))
                row[s] = o
            rows.append(row)
        if ks and center not in ks:
            center = min(ks, key=lambda x: abs(x - spot))
        ser = {'ce': [], 'pe': []}
        if ks:
            i = ks.index(center); h = (club - 1) // 2; grp = ks[max(0, i - h):i + h + 1]
            for s in ser:
                arrs = [SER.get(CH[key][K].get(s), []) for K in grp]
                live = sum((f(T.get(CH[key][K].get(s), {}).get('oi')) - f(T.get(CH[key][K].get(s), {}).get('poi'))) / 1000 for K in grp)
                n = min((len(a) for a in arrs), default=0)
                ser[s] = [round(sum(a[len(a) - n + j] for a in arrs), 1) for j in range(n)] + [round(live, 1)]
        bt, ba = [], {'cb': [], 'ca': [], 'pb': [], 'pa': []}
        if ks:
            for tag, s_, j in (('cb', 'ce', 3), ('ca', 'ce', 4), ('pb', 'pe', 3), ('pa', 'pe', 4)):
                arrs = [SMP.get(CH[key][K].get(s_), []) for K in grp]; n = min((len(a) for a in arrs), default=0)
                ba[tag] = [sum(a[len(a) - n + i][j] for a in arrs) for i in range(n)]
                ba[tag].append(sum(f(T.get(CH[key][K].get(s_), {}).get('bq1' if j == 3 else 'sq1')) for K in grp))
            a0 = SMP.get(CH[key][grp[0]].get('ce'), []); n = len(ba['cb']) - 1
            bt = [time.strftime('%H:%M', time.localtime(x[0])) for x in a0[len(a0) - n:]] + [time.strftime('%H:%M')]
        gs = sym if sym in ('NIFTY', 'BANKNIFTY', 'SENSEX') else 'ALL'
        br = {'ALL': bdata('ALL', tf), 'G': bdata(gs, tf)}
        tn = time.strftime('%H:%M'); oiS = {'t': [], 'oi': {'ce': [], 'pe': []}, 'coi': {'ce': [], 'pe': []}}
        if ks:
            hs = {s_: [(K, OIH.get(CH[key][K].get(s_), {}), f(T.get(CH[key][K].get(s_), {}).get('oi'))) for K in grp] for s_ in ('ce', 'pe')}
            labs = sorted({l for s_ in hs for _, h, _ in hs[s_] for l in h} | ({tn} if 555 <= int(tn[:2]) * 60 + int(tn[3:]) <= 930 else set()))
            for s_ in hs:
                cur, base = {}, {}
                for l in labs:
                    to = tc = 0
                    for K, h, live in hs[s_]:
                        v = h.get(l, live if l == tn else None)
                        if v is not None: cur[K] = v; base.setdefault(K, v)
                        if K in cur: to += cur[K]; tc += cur[K] - base[K]
                    oiS['oi'][s_].append(to); oiS['coi'][s_].append(tc)
            oiS['t'] = labs
        book = []
        t = T.get(DEPTH.get(sym, ''), {}); ls = f(t.get('ls')) or 1
        for i in range(1, 6):
            book.append({'ap': f(t.get(f'sp{i}')), 'a': f(t.get(f'sq{i}')) / ls, 'bp': f(t.get(f'bp{i}')), 'b': f(t.get(f'bq{i}')) / ls})
    return dict(auth=True, tf=tf, oiS=oiS, feed=feed, bt=bt, ba=ba, br=br, lv=lv, bstat=f"{BRS['done']}/{BRS['total']}",  ind=ind, spot=spot, exps=exps, exp=exp, center=center, rows=rows, ser=ser, candles=cd[-500:], book=book)

@app.route('/api/state')
def state():
    if not S['ok']:
        return jsonify(auth=False, msg=S['msg'] if cfg.get('api_secret') else 'API keys are not saved yet.', ip=pubip(), auto=(S['msg'] == 'Not logged in' and bool(cfg.get('api_key'))), login_url='https://auth.flattrade.in/?app_key=' + cfg.get('api_key', ''))
    sym = request.args.get('sym', 'NIFTY'); sym = sym if sym in IDX else 'NIFTY'
    tf = request.args.get('tf', '5'); tf = tf if tf in ('5', '15', '60', 'D') else '5'
    d = build(sym, request.args.get('exp') or '', tf, int(request.args.get('club', 3)), int(float(request.args.get('c') or 0)))
    d['sig'] = evaluate(d); d['sigs'] = SIG[:100]
    return jsonify(d)


if __name__ == '__main__':
    load_hist(); load_signals()
    try:                                          # reuse today's token if it is still valid (tokens expire 5:00 AM IST)
        d = json.load(open(TOKEN_FILE)); n = datetime.datetime.now(); l5 = n.replace(hour=5, minute=0, second=0, microsecond=0)
        if n < l5: l5 -= datetime.timedelta(days=1)
        if d['ts'] >= l5.timestamp(): start_session(d['userid'], d['token'])
        else: print('saved token is old: log in from the dashboard')
    except Exception as e:
        print('no saved login:', e)
    try: ip = socket.gethostbyname(socket.gethostname())
    except Exception: ip = '<laptop-ip>'
    print(f'\nOpen on laptop: http://localhost:8000   Same Wi-Fi phone: http://{ip}:8000   Tailscale phone: http://<tailscale-ip>:8000\n')
    app.run(host='0.0.0.0', port=8000, threaded=True)
