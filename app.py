import asyncio,json,os,time,uuid
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,HTTPException,Request,WebSocket,WebSocketDisconnect
from fastapi.responses import FileResponse,JSONResponse
from core import Store,packed,number
from adapters import CCXTAdapter
from paper import PaperEngine

ROOT=Path(__file__).resolve().parent
store=Store(os.getenv('ACCOUNTSCOPE_DB',str(ROOT/'runtime/accounts.sqlite3')))
paper=PaperEngine(store)
adapters={};tasks={};errors={};demo_lock=asyncio.Lock()
write_windows={}
def same_origin(origin,host):
    try:
        u=urlsplit(origin)
        return u.scheme in ('http','https') and u.netloc.lower()==host.lower() and u.path in ('','/') and not u.query and not u.fragment and not u.username
    except ValueError:return False
def profiles():
    path=ROOT/'profiles.json'
    rows=json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
    if not isinstance(rows,list):raise ValueError('profiles.json 须为数组')
    if len({p['id'] for p in rows})!=len(rows):raise ValueError('账户 ID 重复')
    return rows
def adapter(identifier):
    if identifier not in adapters:
        p=next((p for p in profiles() if p['id']==identifier),None)
        if not p:raise ValueError('未配置账户')
        a=CCXTAdapter(p);store.register(identifier,p['exchange'],p['product'],p.get('strategy','unassigned'),'live')
        state=next(r['state'] for r in store.accounts() if r['id']==identifier);a.sequence=state['sequence'] if state else 0;adapters[identifier]=a
    return adapters[identifier]
async def collect(identifier,transport):
    delay=2
    while True:
        try:
            a=adapter(identifier)
            try:e=await a.watch() if transport=='websocket' else await a.snapshot()
            except Exception:
                if transport!='websocket':raise
                e=await a.snapshot() # Reconnect/timeout compensation uses authoritative REST.
            store.ingest(e);store.checkpoint();errors.pop(identifier,None);delay=2
            await asyncio.sleep(10 if transport=='rest' else 1)
        except asyncio.CancelledError:raise
        except Exception as exc:
            errors[identifier]={'type':type(exc).__name__,'at':time.time(),'message':'采集失败，按退避重试；旧状态保留并按时效失效'}
            await asyncio.sleep(delay);delay=min(60,delay*2)
@asynccontextmanager
async def lifespan(app):
    async def public_loop():
        while True:
            try:await paper.refresh()
            except asyncio.CancelledError:raise
            except Exception:pass
            await asyncio.sleep(15)
    paper.initialize()
    public_task=asyncio.create_task(public_loop()) if os.getenv('ACCOUNTSCOPE_PUBLIC_AUTO','1')=='1' else None
    yield
    if public_task:public_task.cancel();await asyncio.gather(public_task,return_exceptions=True)
    for t in tasks.values():t.cancel()
    await asyncio.gather(*tasks.values(),return_exceptions=True)
    await asyncio.gather(*(a.close() for a in adapters.values()),return_exceptions=True)
app=FastAPI(title='AccountScope',lifespan=lifespan)
@app.middleware('http')
async def local_guard(req:Request,call_next):
    hosted=os.getenv('ACCOUNTSCOPE_HOSTED_DEMO','0')=='1'
    if hosted and req.url.path.startswith('/api/collect/'):
        return JSONResponse({'detail':'公开面试演示不开放私有账户连接'},403)
    if req.method!='GET':
        if req.headers.get('origin') and not same_origin(req.headers['origin'],req.headers.get('host','')):return JSONResponse({'detail':'拒绝跨站写请求'},403)
        try:size=int(req.headers.get('content-length','0'))
        except ValueError:return JSONResponse({'detail':'请求大小无效'},400)
        if size>1000000:return JSONResponse({'detail':'请求过大'},413)
        if hosted:
            now=time.monotonic();bucket=write_windows.get('shared',[]);bucket=[t for t in bucket if now-t<60]
            if len(bucket)>=60:return JSONResponse({'detail':'共享演示操作频率过高，请稍后重试'},429,headers={'Retry-After':'60'})
            bucket.append(now);write_windows['shared']=bucket
    return await call_next(req)
@app.exception_handler(ValueError)
async def invalid(req,exc):return __import__('starlette.responses',fromlist=['JSONResponse']).JSONResponse({'detail':str(exc)},400)
@app.get('/')
def index():return FileResponse(ROOT/'index.html')
@app.get('/healthz')
def health():
    with store.connect() as c:c.execute('SELECT 1').fetchone()
    return {'status':'ok','service':'accountscope'}
@app.get('/api/state')
def state(scope:str='paper'):
    mode='paper-'+paper.mode if scope=='paper' else scope
    if mode not in ('paper-public','paper-offline','demo','live'):raise ValueError('账户空间无效')
    if os.getenv('ACCOUNTSCOPE_HOSTED_DEMO','0')=='1' and mode=='live':raise ValueError('公开演示不提供真实账户空间')
    v=store.view(mode);v['paper']=paper.view();v['collectors']={};v['profiles']=[]
    v['deployment']={'hosted_demo':os.getenv('ACCOUNTSCOPE_HOSTED_DEMO','0')=='1','ephemeral':os.getenv('ACCOUNTSCOPE_EPHEMERAL','0')=='1'}
    if mode=='paper-'+paper.mode:
        books={b['id']:b for b in v['paper']['books']}
        for a in v['accounts']:
            b=books.get(a['id'])
            if not b:continue
            a['pnl']=b['unrealized'];a['realized']=b['realized'];a['margin_ratio']=None
            if b['equity'] and number(b['equity'])>0 and number(b['exposure'])/number(b['equity'])>number('.7'):
                v['alerts'].append({'account':a['id'],'severity':'warning','message':'模拟 BTC 仓位超过账户权益 70%，请检查集中度'})
        v['gross']=str(sum((number(b['exposure']) for b in books.values()),number(0))) if v['complete'] and all(b['exposure'] is not None for b in books.values()) else None
        v['net']=v['gross']
    return v

@app.post('/api/paper/{action}')
async def paper_action(action:str,request:Request):
    body=await request.json()
    if action=='refresh':return await paper.refresh()
    async with paper.lock:
        if action=='mode':
            mode=body.get('mode')
            if mode not in ('public','offline'):raise ValueError('行情模式无效')
            with store.connect() as c:c.execute("UPDATE paper_settings SET value=? WHERE key='mode'",(mode,))
            paper.initialize();return paper.view()
        if action=='trade':
            result=paper.trade(body.get('account'),body.get('side'),body.get('quantity'),body.get('request_id'))
            if result.get('duplicate'):paper.sync(body['account'])
            return result
        if action=='fault':return paper.fault(body.get('scenario'))
        if action=='reconcile':return paper.reconcile(body.get('account'))
        if action=='replay':
            if body.get('account') not in paper.accounts():raise ValueError('账户不在当前模拟空间')
            return store.replay(body['account'])
        if action=='recover':
            for identifier in paper.accounts():paper.sync(identifier)
            return {'recovered':True}
        raise ValueError('未知模拟业务操作')
@app.post('/api/collect/{identifier}/{action}')
async def collector(identifier:str,action:str,request:Request):
    if action=='stop':
        t=tasks.pop(identifier,None)
        if t:t.cancel();await asyncio.gather(t,return_exceptions=True)
        return {'stopped':True}
    body=await request.json();transport=body.get('transport','rest')
    if transport not in ('rest','websocket'):raise ValueError('transport 无效')
    a=adapter(identifier)
    if action=='once':
        try:return store.ingest(await a.snapshot())
        except ValueError:raise
        except Exception as exc:raise HTTPException(502,'只读采集失败：'+type(exc).__name__+'；请核对权限、网络及账户类型') from None
    if action!='start':raise ValueError('未知采集操作')
    if identifier not in tasks or tasks[identifier].done():tasks[identifier]=asyncio.create_task(collect(identifier,transport))
    return {'started':True,'transport':transport}
@app.post('/api/demo/{scenario}')
async def demo(scenario:str):
    if scenario not in ('normal','duplicate','gap','reconcile','recover','stale','restart'):raise ValueError('未知演练')
    async with demo_lock:
        identifier='demo-binance';store.register(identifier,'binance','linear_swap','trend','demo');store.register('demo-bybit','bybit','spot','hedge','demo')
        a=next(a for a in store.accounts() if a['id']==identifier);seq=(a['state'] or {}).get('sequence',0)+1;now=time.time()
        e={'event_id':uuid.uuid4().hex,'account':identifier,'sequence':seq,'kind':'snapshot','timestamp':now,'balances':[{'asset':'USDT','free':'18000','locked':'2000','debt':'0','usd_price':'1'}],'positions':[{'symbol':'BTC/USDT:USDT','side':'long','quantity':'0.25','entry':'65000','mark':str(65000+(seq%12)*90),'multiplier':'1','linear':True,'maintenance':'300','liquidation':'12000'}]}
        b=next(a for a in store.accounts() if a['id']=='demo-bybit');other={**e,'account':'demo-bybit','positions':[],'sequence':(b['state'] or {}).get('sequence',0)+1,'balances':[{'asset':'USDT','free':'12000','locked':'500','debt':'0','usd_price':'1'},{'asset':'ETH','free':'1.2','locked':'0','debt':'0','usd_price':'3200'}]}
        store.ingest(other)
        if scenario=='stale':
            # Do not roll an existing account's event time backwards.
            store.register('demo-stale','fixture','spot','fault','demo');e.update(account='demo-stale',positions=[],timestamp=now-120,sequence=int(now*1000));result=store.ingest(e)
        elif scenario=='gap':
            store.ingest(e);e={**e,'event_id':uuid.uuid4().hex,'kind':'update','sequence':seq+2};result=store.ingest(e)
        elif scenario=='reconcile':
            store.ingest(e);external=json.loads(packed(e));external['balances'][0]['free']='17990';result=store.reconcile(identifier,external)
        elif scenario=='restart':result=store.replay(identifier)
        else:
            result=store.ingest(e)
            if scenario=='duplicate':result=store.ingest(e)
            if scenario in ('recover','normal'):
                for a in store.accounts():
                    if a['id']=='demo-stale' and a['state']:
                        x=json.loads(packed(e));x.update(account=a['id'],positions=[],sequence=a['state']['sequence']+1,event_id=uuid.uuid4().hex);store.ingest(x)
        store.checkpoint();return result
@app.get('/api/export')
def export():return state()
@app.websocket('/ws')
async def stream(ws:WebSocket):
    if ws.headers.get('origin') and not same_origin(ws.headers['origin'],ws.headers.get('host','')):await ws.close(code=1008);return
    await ws.accept()
    try:
        while True:await ws.send_json(state(ws.query_params.get('scope','paper')));await asyncio.sleep(2)
    except (WebSocketDisconnect,RuntimeError):pass
