"""Read-only account projection. All amounts are decimal strings on disk/wire."""
import hashlib,json,sqlite3,time
from contextlib import contextmanager
from decimal import Decimal,InvalidOperation
from pathlib import Path

def packed(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
def number(x):
    if isinstance(x,bool):raise ValueError('布尔值不能作金额')
    try:d=Decimal(str(x))
    except (InvalidOperation,ValueError):raise ValueError('无效金额')
    if not d.is_finite():raise ValueError('金额必须有限')
    return d
def amount(x):return str(number(x))
def normalize(e):
    if not isinstance(e,dict):raise ValueError('事件须为对象')
    if not isinstance(e.get('event_id'),str) or not 1<=len(e['event_id'])<=160:raise ValueError('事件编号无效')
    if not isinstance(e.get('account'),str) or not 1<=len(e['account'])<=80:raise ValueError('账户无效')
    if type(e.get('sequence')) is not int or e['sequence']<0:raise ValueError('序号无效')
    if e.get('kind') not in ('snapshot','update'):raise ValueError('未知事件类型')
    if type(e.get('timestamp')) not in (int,float) or not 0<e['timestamp']<=time.time()+60:raise ValueError('事件时间无效或超前')
    out={k:e[k] for k in ('event_id','account','sequence','kind','timestamp')}
    if e.get('valuation_at') is not None:
        if type(e['valuation_at']) not in (int,float) or not 0<e['valuation_at']<=time.time()+60:raise ValueError('估值行情时间无效')
        out['valuation_at']=e['valuation_at']
    for key in ('balances','positions'):
        if not isinstance(e.get(key),list):raise ValueError('每个事件须明确提供 balances 和 positions 数组')
    balances=[];positions=[];seen=set()
    for b in e['balances']:
        asset=b['asset']
        if not isinstance(asset,str) or not asset or asset in seen:raise ValueError('资产为空或重复')
        seen.add(asset);free,locked,debt=(number(b.get(k,0)) for k in ('free','locked','debt'))
        if min(free,locked,debt)<0:raise ValueError('余额分项必须非负')
        if b.get('usd_price') is not None and number(b['usd_price'])<=0:raise ValueError('估值价格必须为正')
        balances.append({'asset':asset,'free':str(free),'locked':str(locked),'debt':str(debt),'usd_price':amount(b['usd_price']) if b.get('usd_price') is not None else None})
    seen=set()
    for p in e['positions']:
        key=(p['symbol'],p['side'])
        if key in seen or p['side'] not in ('long','short'):raise ValueError('持仓重复或方向无效')
        seen.add(key)
        row={'symbol':p['symbol'],'side':p['side']}
        for k in ('quantity','entry','mark','multiplier'):
            row[k]=amount(p.get(k,1 if k=='multiplier' else None))
            if number(row[k])<0 or k!='quantity' and number(row[k])==0:raise ValueError('持仓数量或价格无效')
        if p.get('linear') is not True or '/USDT' not in p['symbol']:raise ValueError('首版只支持线性 USDT 结算合约')
        row.update(linear=True,maintenance=amount(p['maintenance']) if p.get('maintenance') is not None else None,
            liquidation=amount(p['liquidation']) if p.get('liquidation') is not None else None)
        positions.append(row)
    out.update(balances=balances,positions=positions)
    return out

def project(state,e):
    if e['kind']=='snapshot':return {'sequence':e['sequence'],'timestamp':e['timestamp'],'balances':e['balances'],'positions':e['positions'],**({'valuation_at':e['valuation_at']} if 'valuation_at' in e else {})}
    if state is None or e['sequence']!=state['sequence']+1:raise ValueError('增量序列不连续')
    if e['timestamp']<state['timestamp']:raise ValueError('事件时间倒退')
    balances={b['asset']:b for b in state['balances']};balances.update({b['asset']:b for b in e['balances']})
    positions={(p['symbol'],p['side']):p for p in state['positions']};positions.update({(p['symbol'],p['side']):p for p in e['positions']})
    return {'sequence':e['sequence'],'timestamp':e['timestamp'],'balances':list(balances.values()),'positions':list(positions.values())}

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,venue TEXT,product TEXT,strategy TEXT,mode TEXT,state TEXT,needs_sync INTEGER DEFAULT 1,duplicates INTEGER DEFAULT 0,gaps INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,account TEXT,event_id TEXT,payload TEXT,digest TEXT,status TEXT,received REAL,UNIQUE(account,event_id));
        CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY,account TEXT,at REAL,result TEXT);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,at REAL,payload TEXT);
        ''')
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        try:
            with c:yield c
        finally:c.close()
    def register(self,identifier,venue,product,strategy,mode):
        if product not in ('spot','linear_swap') or mode not in ('demo','live','paper-public','paper-offline'):raise ValueError('账户模式无效')
        with self.connect() as c:
            old=c.execute('SELECT * FROM accounts WHERE id=?',(identifier,)).fetchone()
            if old and (old['venue'],old['product'],old['mode'])!=(venue,product,mode):raise ValueError('账户标识不可跨市场或模式复用')
            c.execute('INSERT OR IGNORE INTO accounts(id,venue,product,strategy,mode) VALUES(?,?,?,?,?)',(identifier,venue,product,strategy,mode))
    def ingest(self,event):
        e=normalize(event);payload=packed(e);digest=hashlib.sha256(payload.encode()).hexdigest()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE');a=c.execute('SELECT * FROM accounts WHERE id=?',(e['account'],)).fetchone()
            if not a:raise ValueError('请先注册账户')
            if a['product']=='spot' and e['positions']:raise ValueError('现货账户不能混入衍生品持仓')
            old=c.execute('SELECT digest FROM events WHERE account=? AND event_id=?',(e['account'],e['event_id'])).fetchone()
            if old:
                if old['digest']!=digest:raise ValueError('同一事件编号对应不同内容')
                c.execute('UPDATE accounts SET duplicates=duplicates+1 WHERE id=?',(e['account'],));return {'status':'duplicate'}
            state=json.loads(a['state']) if a['state'] else None;status='accepted';needs=a['needs_sync']
            if state and (e['sequence']<=state['sequence'] or e['timestamp']<state['timestamp']):status='stale'
            elif e['kind']=='update' and (needs or state is None or e['sequence']!=state['sequence']+1):status='gap';needs=1
            if status=='accepted':state=project(state,e);needs=0 if e['kind']=='snapshot' else needs
            c.execute('INSERT INTO events(account,event_id,payload,digest,status,received) VALUES(?,?,?,?,?,?)',(e['account'],e['event_id'],payload,digest,status,time.time()))
            c.execute('UPDATE accounts SET state=?,needs_sync=?,gaps=gaps+? WHERE id=?',(packed(state) if state else None,needs,int(status=='gap'),e['account']))
            return {'status':status,'needs_sync':bool(needs)}
    def accounts(self):
        with self.connect() as c:
            rows=[dict(r) for r in c.execute('SELECT * FROM accounts ORDER BY id')]
        for r in rows:r['state']=json.loads(r['state']) if r['state'] else None
        return rows
    def reconcile(self,identifier,external):
        e=normalize(external)
        if e['account']!=identifier or e['kind']!='snapshot':raise ValueError('对账需要同账户完整快照')
        a=next((a for a in self.accounts() if a['id']==identifier),None)
        if not a or not a['state']:raise ValueError('账户尚无内部状态')
        if e['timestamp']<a['state']['timestamp']:raise ValueError('外部快照早于内部状态，不比较不同时点')
        differences=[]
        for collection,key,fields in [('balances',lambda r:r['asset'],('free','locked','debt')),('positions',lambda r:r['symbol']+'|'+r['side'],('quantity','entry','mark','multiplier','maintenance','liquidation'))]:
            left={key(r):r for r in a['state'][collection]};right={key(r):r for r in e[collection]}
            for k in sorted(set(left)|set(right)):
                if k not in left or k not in right:differences.append({'item':k,'field':'presence','internal':k in left,'external':k in right});continue
                for f in fields:
                    x,y=left[k].get(f),right[k].get(f)
                    if (x is None)!=(y is None) or x is not None and number(x)!=number(y):differences.append({'item':k,'field':f,'internal':x,'external':y})
        result={'account':identifier,'differences':differences,'matched':not differences,'internal_at':a['state']['timestamp'],'external_at':e['timestamp'],'note':'同点回放对账可确定比较；实时跨接口采集存在时间差，差异不直接判定为账务错误。'}
        with self.connect() as c:c.execute('INSERT INTO checks(account,at,result) VALUES(?,?,?)',(identifier,time.time(),packed(result)))
        return result
    def replay(self,identifier):
        state=None
        with self.connect() as c:
            for r in c.execute("SELECT payload FROM events WHERE account=? AND status='accepted' ORDER BY id",(identifier,)):state=project(state,json.loads(r['payload']))
        a=next((a for a in self.accounts() if a['id']==identifier),None)
        return {'account':identifier,'matched':a is not None and state is not None and packed(state)==packed(a['state']),'state':state}
    def view(self,mode=None):
        now=time.time();accounts=[a for a in self.accounts() if mode is None or a['mode']==mode];totals={};alerts=[];gross=Decimal(0);net=Decimal(0);equities=[]
        for a in accounts:
            s=a['state'];equity=Decimal(0);unrealized=Decimal(0);exposure=Decimal(0);maintenance=Decimal(0);complete=True;margin_known=True
            age=max(now-s['timestamp'],now-s.get('valuation_at',s['timestamp'])) if s else None
            if not s or age>60 or a['needs_sync']:alerts.append({'account':a['id'],'severity':'critical','message':'等待快照 / 数据过期 / 需要补偿同步'})
            for b in (s or {}).get('balances',[]):
                if b['usd_price'] is None:complete=False;continue
                value=(number(b['free'])+number(b['locked'])-number(b['debt']))*number(b['usd_price']);equity+=value;totals[b['asset']]=totals.get(b['asset'],Decimal(0))+value
            for p in (s or {}).get('positions',[]):
                sign=1 if p['side']=='long' else -1;q=number(p['quantity'])*number(p['multiplier']);v=q*number(p['mark'])*sign
                exposure+=abs(v);gross+=abs(v);net+=v;unrealized+=q*(number(p['mark'])-number(p['entry']))*sign
                if p['maintenance'] is None:margin_known=False
                else:maintenance+=number(p['maintenance'])
            # Canonical swap balances must be wallet collateral, not equity including PnL.
            if a['product']=='linear_swap':equity+=unrealized
            if not complete:alerts.append({'account':a['id'],'severity':'critical','message':'存在未定价资产，账户权益不可汇总'})
            ratio=maintenance/equity if margin_known and equity>0 else None
            if ratio is not None and ratio>=Decimal('.7'):alerts.append({'account':a['id'],'severity':'warning','message':'维持保证金 / 权益达到阈值'})
            a.update(equity=str(equity) if complete and s else None,pnl=str(unrealized),gross=str(exposure),age=age,margin_ratio=str(ratio) if ratio is not None else None,usable=bool(s and complete and age<=60 and not a['needs_sync']))
            if a['usable']:equities.append(equity)
        with self.connect() as c:
            events=[dict(r) for r in c.execute('SELECT id,account,event_id,status,received FROM events ORDER BY id DESC LIMIT 40')]
            checks=[json.loads(r['result']) for r in c.execute('SELECT result FROM checks ORDER BY id DESC LIMIT 10')]
            history=[json.loads(r['payload']) for r in c.execute('SELECT payload FROM history ORDER BY id DESC LIMIT 80')][::-1]
        if mode is not None:
            ids={a['id'] for a in accounts};events=[e for e in events if e['account'] in ids];checks=[x for x in checks if x['account'] in ids];history=[x for x in history if x.get('scope')==mode]
        mixed=len({a['mode'] for a in accounts})>1
        if mixed:alerts.append({'account':'portfolio','severity':'critical','message':'演示与真实账户同时存在，停止跨模式聚合；请为真实运行使用独立数据目录'})
        complete=bool(accounts) and len(equities)==len(accounts) and not mixed
        return {'accounts':accounts,'equity':str(sum(equities,Decimal(0))) if complete else None,'gross':str(gross) if complete else None,'net':str(net) if complete else None,'assets':{k:str(v) for k,v in totals.items()} if complete else {},'alerts':alerts,'events':events,'checks':checks,'history':history,'complete':complete,'timestamp':now}
    def checkpoint(self,mode=None):
        v=self.view(mode);p={'timestamp':v['timestamp'],'equity':v['equity'],'gross':v['gross'],'complete':v['complete'],'scope':mode}
        with self.connect() as c:c.execute('INSERT INTO history(at,payload) VALUES(?,?)',(p['timestamp'],packed(p)))
        return v
