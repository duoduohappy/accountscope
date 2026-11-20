"""Credential-free public prices + explicitly simulated spot accounts.

The paper venue's ledger is the counterparty for reconciliation, not a real
exchange account. Quote acquisition failures never become synthetic quotes.
"""
import asyncio,json,time,uuid,hashlib
from decimal import Decimal
import requests
from core import number,packed

VENUES=('binance','bybit','okx')
def parse_quote(venue,symbol,payload,now=None):
    now=time.time() if now is None else now
    if venue=='okx':
        if str(payload.get('code'))!='0':raise ValueError('公开接口返回错误')
        r=payload['data'][0]
        if r['instId']!=symbol.replace('/','-'):raise ValueError('公开报价标的不符')
        price=number(r['last']);at=int(r['ts'])/1000
    elif venue=='bybit':
        if payload.get('retCode')!=0:raise ValueError('公开接口返回错误')
        r=payload['result']['list'][0]
        if r['symbol']!=symbol.replace('/',''):raise ValueError('公开报价标的不符')
        price=number(r['lastPrice']);at=int(payload['time'])/1000
    elif venue=='binance':
        if payload['symbol']!=symbol.replace('/',''):raise ValueError('公开报价标的不符')
        price=number(payload['lastPrice']);at=int(payload['closeTime'])/1000
    else:raise ValueError('未知公开行情源')
    if price<=0 or not -5<=now-at<=60:raise ValueError('公开报价无效、超前或超过 60 秒')
    return {'venue':venue,'symbol':symbol,'price':str(price),'timestamp':at,'received':now,'source':'real_public','note':'真实公开成交价；非私有账户数据'}

class PaperEngine:
    def __init__(self,store):
        self.store=store;self.lock=asyncio.Lock();self.errors={};self.polls=0
        with store.connect() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS paper_settings(key TEXT PRIMARY KEY,value TEXT);
        INSERT OR IGNORE INTO paper_settings VALUES('mode','public');
        CREATE TABLE IF NOT EXISTS paper_books(id TEXT PRIMARY KEY,body TEXT);
        CREATE TABLE IF NOT EXISTS paper_fills(id INTEGER PRIMARY KEY,request_id TEXT UNIQUE,digest TEXT,account TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS public_quotes(id INTEGER PRIMARY KEY,mode TEXT,venue TEXT,body TEXT);
        CREATE TABLE IF NOT EXISTS paper_faults(id INTEGER PRIMARY KEY,account TEXT,kind TEXT,at REAL);
        ''')
    @property
    def mode(self):
        with self.store.connect() as c:return c.execute("SELECT value FROM paper_settings WHERE key='mode'").fetchone()[0]
    def accounts(self):return [self.mode+'-'+v for v in VENUES]
    def initialize(self):
        for venue in VENUES:
            identifier=self.mode+'-'+venue
            self.store.register(identifier,venue,'spot','public-price-paper','paper-'+self.mode)
            book={'id':identifier,'venue':venue,'mode':self.mode,'cash':'10000','quantity':'0','cost_basis':'0','realized':'0','fees':'0','initial':'10000'}
            with self.store.connect() as c:c.execute('INSERT OR IGNORE INTO paper_books VALUES(?,?)',(identifier,packed(book)))
    def book(self,identifier):
        if identifier not in self.accounts():raise ValueError('账户不属于当前公开 / 离线模拟空间')
        with self.store.connect() as c:r=c.execute('SELECT body FROM paper_books WHERE id=?',(identifier,)).fetchone()
        if not r:raise ValueError('请先初始化模拟账户')
        return json.loads(r['body'])
    def quote(self,venue):
        with self.store.connect() as c:r=c.execute('SELECT body FROM public_quotes WHERE mode=? AND venue=? ORDER BY id DESC LIMIT 1',(self.mode,venue)).fetchone()
        return json.loads(r['body']) if r else None
    def sync(self,identifier):
        b=self.book(identifier);q=self.quote(b['venue']);qty=number(b['quantity'])
        if qty and (not q or time.time()-q['timestamp']>60):raise ValueError('持仓报价过期，停止刷新估值')
        a=next(a for a in self.store.accounts() if a['id']==identifier)
        event={'account':identifier,'event_id':uuid.uuid4().hex,'sequence':(a['state'] or {}).get('sequence',0)+1,'kind':'snapshot','timestamp':time.time(),'positions':[],
            'balances':[{'asset':'USDT','free':b['cash'],'locked':'0','debt':'0','usd_price':'1'}]}
        if qty:
            event['balances'].append({'asset':'BTC','free':b['quantity'],'locked':'0','debt':'0','usd_price':q['price']});event['valuation_at']=q['timestamp']
        self.store.ingest(event);return event
    def checkpoint(self):
        v=self.store.view('paper-'+self.mode);books=self.view()['books']
        gross=str(sum((number(b['exposure']) for b in books),Decimal(0))) if v['complete'] and all(b['exposure'] is not None for b in books) else None
        p={'timestamp':time.time(),'equity':v['equity'],'gross':gross,'complete':v['complete'],'scope':'paper-'+self.mode}
        with self.store.connect() as c:c.execute('INSERT INTO history(at,payload) VALUES(?,?)',(p['timestamp'],packed(p)))
    async def refresh(self):
        async with self.lock:
            self.initialize();mode=self.mode
            async def fetch(venue):
                try:
                    if mode=='offline':
                        self.polls+=1;q={'venue':venue,'symbol':'BTC/USDT','price':str(65000+(self.polls%20)*25),'timestamp':time.time(),'received':time.time(),'source':'synthetic_offline','note':'显式选择的合成离线行情，不是真实价格'}
                    else:
                        url={'okx':'https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT','bybit':'https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT','binance':'https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT'}[venue]
                        r=await asyncio.to_thread(requests.get,url,timeout=10,allow_redirects=False)
                        r.raise_for_status();q=parse_quote(venue,'BTC/USDT',r.json())
                    with self.store.connect() as c:c.execute('INSERT INTO public_quotes(mode,venue,body) VALUES(?,?,?)',(mode,venue,packed(q)))
                    self.errors.pop(venue,None)
                except Exception as exc:
                    status=exc.response.status_code if isinstance(exc,requests.HTTPError) and exc.response is not None else None
                    if status is not None:
                        reason={403:'服务拒绝访问（可能存在地区或访问策略限制）',429:'请求被限流',451:'因地区或法律策略限制访问'}.get(status,'公开接口返回非成功状态')
                        detail=f'HTTP {status}：{reason}'
                    elif isinstance(exc,requests.Timeout):detail='请求超时'
                    elif isinstance(exc,requests.ConnectionError):detail='网络连接失败'
                    elif isinstance(exc,ValueError):detail='报价内容、标的或时效校验失败'
                    else:detail='行情采集失败'
                    self.errors[venue]={'type':type(exc).__name__,'at':time.time(),'http_status':status,'message':f'公开行情不可用（{detail}）；未切换合成数据。可选择可用账户或手动切换离线模拟。'}
            await asyncio.gather(*(fetch(v) for v in VENUES))
            for identifier in self.accounts():
                try:self.sync(identifier)
                except ValueError:pass
            self.checkpoint()
            return self.view()
    def trade(self,identifier,side,quantity,request_id):
        if side not in ('buy','sell') or not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('模拟委托参数无效')
        qty=number(quantity)
        if qty<=0 or qty>10 or qty!=qty.quantize(Decimal('.00000001')):raise ValueError('数量须为 0–10 BTC，最多八位小数')
        digest=hashlib.sha256(packed([identifier,side,str(qty)]).encode()).hexdigest()
        b=self.book(identifier)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM paper_fills WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['digest']!=digest:raise ValueError('委托幂等编号对应不同参数')
                return {**json.loads(old['body']),'duplicate':True}
            b=json.loads(c.execute('SELECT body FROM paper_books WHERE id=?',(identifier,)).fetchone()['body'])
            q=self.quote(b['venue'])
            if not q or not -5<=time.time()-q['timestamp']<=60:raise ValueError('没有新鲜公开 / 离线行情，拒绝模拟成交')
            price=number(q['price'])*(Decimal('1.0005') if side=='buy' else Decimal('.9995'));value=qty*price;fee=value*Decimal('.001')
            cash=number(b['cash']);held=number(b['quantity']);basis=number(b['cost_basis']);realized=Decimal(0)
            if side=='buy':
                if value+fee>cash:raise ValueError('模拟现金不足')
                cash-=value+fee;held+=qty;basis+=value+fee
            else:
                if qty>held:raise ValueError('模拟可卖数量不足；首版不做空')
                released=basis*qty/held;realized=value-fee-released;basis-=released;cash+=value-fee;held-=qty
            b.update(cash=str(cash),quantity=str(held),cost_basis=str(basis),realized=str(number(b['realized'])+realized),fees=str(number(b['fees'])+fee))
            fill={'request_id':request_id,'account':identifier,'side':side,'quantity':str(qty),'price':str(price),'fee':str(fee),'realized':str(realized),'quote':q,'timestamp':time.time(),'simulation':True}
            c.execute('UPDATE paper_books SET body=? WHERE id=?',(packed(b),identifier));c.execute('INSERT INTO paper_fills(request_id,digest,account,body) VALUES(?,?,?,?)',(request_id,digest,identifier,packed(fill)))
        self.sync(identifier);self.checkpoint();return fill
    def reconcile(self,identifier):
        # Construct an external paper-venue snapshot without overwriting projection.
        b=self.book(identifier);q=self.quote(b['venue']);a=next(a for a in self.store.accounts() if a['id']==identifier)
        if number(b['quantity']) and (not q or time.time()-q['timestamp']>60):raise ValueError('行情过期，无法对账估值')
        balances=[{'asset':'USDT','free':b['cash'],'locked':'0','debt':'0','usd_price':'1'}]
        if number(b['quantity']):balances.append({'asset':'BTC','free':b['quantity'],'locked':'0','debt':'0','usd_price':q['price']})
        e={'account':identifier,'event_id':uuid.uuid4().hex,'sequence':(a['state'] or {}).get('sequence',0)+1,'kind':'snapshot','timestamp':time.time(),'balances':balances,'positions':[]}
        r=self.store.reconcile(identifier,e);r['note']='模拟场所账本与内部投影对账，不是真实交易所资产认证';return r
    def fault(self,action):
        identifier=self.accounts()[0];a=next(a for a in self.store.accounts() if a['id']==identifier)
        if not a['state']:self.sync(identifier);a=next(a for a in self.store.accounts() if a['id']==identifier)
        s=a['state']
        if action=='restart':return self.store.replay(identifier)
        if action=='recover':
            for identifier in self.accounts():self.sync(identifier)
            return {'recovered':True}
        if action=='duplicate':
            with self.store.connect() as c:r=c.execute("SELECT payload FROM events WHERE account=? AND status='accepted' ORDER BY id DESC LIMIT 1",(identifier,)).fetchone()
            return self.store.ingest(json.loads(r['payload']))
        if action=='gap':return self.store.ingest({'account':identifier,'event_id':uuid.uuid4().hex,'sequence':s['sequence']+2,'kind':'update','timestamp':time.time(),'balances':[],'positions':[]})
        if action not in ('stale','reconcile'):raise ValueError('未知故障演练')
        # Explicit local projection corruption, scoped only to current paper accounts.
        if action=='stale':s['timestamp']=time.time()-120
        else:s['balances'][0]['free']=str(number(s['balances'][0]['free'])-10)
        with self.store.connect() as c:
            c.execute('UPDATE accounts SET state=? WHERE id=?',(packed(s),identifier));c.execute('INSERT INTO paper_faults(account,kind,at) VALUES(?,?,?)',(identifier,action,time.time()))
        return self.reconcile(identifier) if action=='reconcile' else {'injected':'stale','simulation':True}
    def view(self):
        books=[]
        for identifier in self.accounts():
            try:b=self.book(identifier)
            except ValueError:continue
            q=self.quote(b['venue']);valid=bool(q and -5<=time.time()-q['timestamp']<=60)
            market=Decimal(0) if number(b['quantity'])==0 else number(b['quantity'])*number(q['price']) if valid else None
            b.update(quote=q,price_valid=valid,unrealized=str(market-number(b['cost_basis'])) if market is not None else None,
                equity=str(number(b['cash'])+market) if market is not None else None,
                average_cost=str(number(b['cost_basis'])/number(b['quantity'])) if number(b['quantity']) else None,
                exposure=str(market) if market is not None else None)
            books.append(b)
        with self.store.connect() as c:
            fills=[json.loads(r['body']) for r in c.execute('SELECT body FROM paper_fills ORDER BY id DESC LIMIT 100')]
            faults=[dict(r) for r in c.execute('SELECT account,kind,at FROM paper_faults ORDER BY id DESC LIMIT 20')]
        return {'mode':self.mode,'books':books,'fills':[f for f in fills if f['account'] in self.accounts()],'faults':faults,'errors':self.errors,'scope':'真实公开行情 / 模拟账户' if self.mode=='public' else '合成离线行情 / 独立模拟账户'}
