import os, sqlite3, secrets, hashlib, hmac, json, time
from datetime import datetime, timezone
from flask import Flask, render_template, request, jsonify, session, g
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DATABASE_PATH', os.path.join(BASE, 'smart_trading.db'))
app = Flask(__name__)
secret = os.environ.get('FLASK_SECRET_KEY')
if not secret:
    if os.environ.get('FLASK_ENV') == 'production' or os.environ.get('RENDER'):
        raise RuntimeError('Set FLASK_SECRET_KEY to a random value of at least 32 characters.')
    secret = secrets.token_hex(32)
app.secret_key = secret
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE','0') == '1', MAX_CONTENT_LENGTH=1_000_000)

# Provider responses are cached briefly to reduce API usage. Real quotes require a valid provider key.
TWELVE_DATA_API_KEY = os.environ.get('TWELVE_DATA_API_KEY', '').strip()
QUOTE_CACHE = {'at': 0.0, 'data': None, 'source': 'sample'}
SERIES_CACHE = {}
AUTH_ATTEMPTS = {}
PROVIDER_SYMBOLS = {
    'SPX':'SPX', 'NDX':'NDX', 'DJI':'DJI', 'XAUUSD':'XAU/USD', 'XAGUSD':'XAG/USD',
    'EURUSD':'EUR/USD', 'GBPUSD':'GBP/USD', 'USDJPY':'USD/JPY', 'BTCUSD':'BTC/USD',
    'ETHUSD':'ETH/USD', 'CL':'WTI', 'AAPL':'AAPL', 'NVDA':'NVDA', 'TSLA':'TSLA'
}

MARKETS = [
 {'symbol':'SPX','name':'S&P 500','price':5812.45,'change':0.84,'group':'Indices','currency':'USD'},
 {'symbol':'NDX','name':'Nasdaq 100','price':20438.20,'change':1.21,'group':'Indices','currency':'USD'},
 {'symbol':'DJI','name':'Dow Jones','price':42816.10,'change':0.32,'group':'Indices','currency':'USD'},
 {'symbol':'XAUUSD','name':'Gold / US Dollar','price':2678.42,'change':-0.28,'group':'Metals','currency':'USD'},
 {'symbol':'XAGUSD','name':'Silver / US Dollar','price':31.84,'change':0.46,'group':'Metals','currency':'USD'},
 {'symbol':'EURUSD','name':'Euro / US Dollar','price':1.08432,'change':0.19,'group':'Forex','currency':'USD'},
 {'symbol':'GBPUSD','name':'British Pound / US Dollar','price':1.29140,'change':-0.12,'group':'Forex','currency':'USD'},
 {'symbol':'USDJPY','name':'US Dollar / Japanese Yen','price':149.820,'change':0.27,'group':'Forex','currency':'JPY'},
 {'symbol':'BTCUSD','name':'Bitcoin / US Dollar','price':67482.50,'change':2.38,'group':'Crypto','currency':'USD'},
 {'symbol':'ETHUSD','name':'Ethereum / US Dollar','price':3528.80,'change':1.72,'group':'Crypto','currency':'USD'},
 {'symbol':'CL','name':'Crude Oil WTI','price':72.84,'change':-0.67,'group':'Energy','currency':'USD'},
 {'symbol':'AAPL','name':'Apple Inc.','price':228.76,'change':0.91,'group':'Stocks','currency':'USD'},
 {'symbol':'NVDA','name':'NVIDIA Corporation','price':141.98,'change':1.87,'group':'Stocks','currency':'USD'},
 {'symbol':'TSLA','name':'Tesla Inc.','price':249.50,'change':-1.14,'group':'Stocks','currency':'USD'},
]

def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    conn = g.pop('db', None)
    if conn is not None: conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL)')
        conn.execute("CREATE TABLE IF NOT EXISTS prefs (user_id INTEGER PRIMARY KEY, language TEXT DEFAULT 'en', watchlist TEXT DEFAULT '[]', FOREIGN KEY(user_id) REFERENCES users(id))")

init_db()

def csrf_token():
    token = session.get('_csrf')
    if not token:
        token = secrets.token_urlsafe(32); session['_csrf'] = token
    return token

@app.before_request
def protect_mutations():
    if request.method in ('POST','PUT','PATCH','DELETE') and request.endpoint not in ('health',):
        expected = session.get('_csrf','')
        supplied = request.headers.get('X-CSRFToken') or request.form.get('csrf_token','')
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            return jsonify({'error':'Invalid or missing CSRF token'}), 403

@app.context_processor
def inject_csrf(): return {'csrf_token': csrf_token}

@app.get('/')
def home(): return render_template('index.html')

@app.get('/health')
def health(): return jsonify({'status':'ok','service':'Smart Trading','time':datetime.now(timezone.utc).isoformat()})

def provider_request(endpoint, params, timeout=8):
    if not TWELVE_DATA_API_KEY:
        return None
    try:
        response = requests.get('https://api.twelvedata.com/' + endpoint,
            params={**params, 'apikey': TWELVE_DATA_API_KEY}, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and (payload.get('status') == 'error' or payload.get('code')):
            app.logger.warning('Market-data provider returned an error: %s', payload.get('message', 'unknown error'))
            return None
        return payload
    except (requests.RequestException, ValueError) as exc:
        app.logger.warning('Market-data provider unavailable: %s', exc)
        return None

@app.get('/api/markets')
def markets():
    now = time.time()
    if QUOTE_CACHE['data'] is not None and now - QUOTE_CACHE['at'] < 30:
        return jsonify({'data':QUOTE_CACHE['data'], 'source':QUOTE_CACHE['source'], 'delayed':QUOTE_CACHE['source'] != 'twelvedata', 'note':'Some symbols may fall back to sample values; inspect data_source per asset.' if QUOTE_CACHE['source'] == 'mixed' else None, 'as_of':datetime.now(timezone.utc).isoformat()})
    data = [dict(x, data_source='sample') for x in MARKETS]
    source = 'sample'
    # Batch quote request: provider/account entitlements determine supported symbols and freshness.
    if TWELVE_DATA_API_KEY:
        symbols = ','.join(dict.fromkeys(PROVIDER_SYMBOLS.values()))
        payload = provider_request('quote', {'symbol': symbols})
        if isinstance(payload, dict):
            updates = {}
            # Batch endpoints can return a symbol-keyed object; single-symbol responses use the root object.
            if 'symbol' in payload and ('close' in payload or 'price' in payload):
                updates[str(payload['symbol']).upper()] = payload
            else:
                for key, value in payload.items():
                    if isinstance(value, dict) and ('close' in value or 'price' in value): updates[key.upper()] = value
            for item in data:
                provider_symbol = PROVIDER_SYMBOLS.get(item['symbol'], '').upper()
                q = updates.get(provider_symbol)
                if q:
                    try:
                        price = float(q.get('close') or q.get('price'))
                        pct = float(q.get('percent_change', item['change']))
                        item['price'], item['change'] = price, pct
                        item['data_source'] = 'twelvedata'
                        item['provider_as_of'] = q.get('datetime') or q.get('timestamp')
                    except (TypeError, ValueError):
                        continue
            real_count = sum(1 for x in data if x.get('data_source') == 'twelvedata')
            if real_count == len(data): source = 'twelvedata'
            elif real_count: source = 'mixed'
    QUOTE_CACHE.update({'at':now, 'data':data, 'source':source})
    return jsonify({'data':data, 'source':source, 'delayed':source != 'twelvedata', 'note':'Some symbols may fall back to sample values; inspect data_source per asset.' if source == 'mixed' else None, 'as_of':datetime.now(timezone.utc).isoformat()})

@app.get('/api/series/<symbol>')
def series(symbol):
    symbol = symbol.upper()
    item = next((x for x in MARKETS if x['symbol'].upper()==symbol), None)
    if not item: return jsonify({'error':'Unknown symbol'}),404
    now = time.time()
    cached = SERIES_CACHE.get(symbol)
    if cached and now - cached['at'] < 60:
        return jsonify(cached['payload'])
    if TWELVE_DATA_API_KEY:
        provider_symbol = PROVIDER_SYMBOLS.get(symbol)
        payload = provider_request('time_series', {'symbol':provider_symbol, 'interval':'1day', 'outputsize':90, 'format':'JSON'}) if provider_symbol else None
        values = payload.get('values') if isinstance(payload, dict) else None
        if isinstance(values, list) and values:
            points=[]
            try:
                for point in reversed(values):
                    points.append({'t':point.get('datetime'), 'v':float(point['close'])})
                result={'symbol':symbol,'data':points,'source':'twelvedata','delayed':False}
                SERIES_CACHE[symbol]={'at':now,'payload':result}
                return jsonify(result)
            except (KeyError, TypeError, ValueError):
                pass
    import math, random
    seed = sum(ord(c) for c in symbol)
    rng = random.Random(seed)
    base = item['price']; pts=[]
    for i in range(90):
        drift = item['change'] / 100 * (i/90) * .55
        wave = math.sin(i/7 + seed) * .009 + math.sin(i/17) * .012
        noise = rng.uniform(-.006,.006)
        value = base * (1 - drift + wave + noise)
        pts.append({'t':i, 'v':round(value, 5 if value<100 else 2)})
    result={'symbol':symbol,'data':pts,'source':'sample','delayed':True}
    SERIES_CACHE[symbol]={'at':now,'payload':result}
    return jsonify(result)

def auth_rate_limited(action):
    now = time.time()
    key = (request.remote_addr or 'unknown', action)
    attempts = [t for t in AUTH_ATTEMPTS.get(key, []) if now - t < 60]
    if len(attempts) >= (5 if action == 'login' else 10):
        AUTH_ATTEMPTS[key] = attempts
        return True
    attempts.append(now); AUTH_ATTEMPTS[key] = attempts
    return False

@app.post('/api/register')
def register():
    if auth_rate_limited('register'): return jsonify({'error':'Too many registration attempts. Try again in a minute.'}),429
    body=request.get_json(silent=True) or {}
    email=str(body.get('email','')).strip().lower(); password=str(body.get('password',''))
    if len(email)>254 or '@' not in email or len(password)<10:
        return jsonify({'error':'Enter a valid email and a password with at least 10 characters.'}),400
    salt=secrets.token_hex(16); ph=hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),260000).hex()
    conn=db()
    try:
        cur=conn.execute('INSERT INTO users(email,salt,password_hash,created_at) VALUES(?,?,?,?)',(email,salt,ph,datetime.now(timezone.utc).isoformat()))
        uid=cur.lastrowid; conn.execute('INSERT INTO prefs(user_id,language,watchlist) VALUES(?,?,?)',(uid,'en',json.dumps(['SPX','XAUUSD','BTCUSD','EURUSD']))); conn.commit()
    except sqlite3.IntegrityError: return jsonify({'error':'An account with this email already exists.'}),409
    session.clear(); session['user_id']=uid; session['email']=email; csrf_token()
    return jsonify({'user':{'email':email},'csrf_token':session['_csrf']})

@app.post('/api/login')
def login():
    if auth_rate_limited('login'): return jsonify({'error':'Too many login attempts. Try again in a minute.'}),429
    body=request.get_json(silent=True) or {}; email=str(body.get('email','')).strip().lower(); password=str(body.get('password',''))
    row=db().execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    if not row: return jsonify({'error':'Email or password is incorrect.'}),401
    candidate=hashlib.pbkdf2_hmac('sha256',password.encode(),row['salt'].encode(),260000).hex()
    if not hmac.compare_digest(candidate,row['password_hash']): return jsonify({'error':'Email or password is incorrect.'}),401
    session.clear(); session['user_id']=row['id']; session['email']=row['email']; csrf_token()
    return jsonify({'user':{'email':row['email']},'csrf_token':session['_csrf']})

@app.post('/api/logout')
def logout():
    session.clear(); return jsonify({'ok':True})

@app.get('/api/me')
def me():
    if not session.get('user_id'): return jsonify({'user':None,'csrf_token':csrf_token()})
    return jsonify({'user':{'email':session.get('email')},'csrf_token':csrf_token()})

@app.get('/api/preferences')
def get_preferences():
    if not session.get('user_id'): return jsonify({'error':'Sign in required'}),401
    row=db().execute('SELECT language,watchlist FROM prefs WHERE user_id=?',(session['user_id'],)).fetchone()
    return jsonify({'language':row['language'],'watchlist':json.loads(row['watchlist'])})

@app.put('/api/preferences')
def save_preferences():
    if not session.get('user_id'): return jsonify({'error':'Sign in required'}),401
    body=request.get_json(silent=True) or {}; language=body.get('language','en'); watchlist=body.get('watchlist',[])
    if language not in ('en','ar') or not isinstance(watchlist,list) or len(watchlist)>100 or any(not isinstance(s,str) or len(s)>20 for s in watchlist): return jsonify({'error':'Invalid preferences'}),400
    valid={m['symbol'] for m in MARKETS}; watchlist=list(dict.fromkeys(s.upper() for s in watchlist if s.upper() in valid))
    db().execute('INSERT INTO prefs(user_id,language,watchlist) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET language=excluded.language,watchlist=excluded.watchlist',(session['user_id'],language,json.dumps(watchlist))); db().commit()
    return jsonify({'ok':True,'language':language,'watchlist':watchlist})

if __name__ == '__main__': app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5000')),debug=False)
