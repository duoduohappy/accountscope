"""CCXT is reused as a library; no trading or transfer API is exposed."""
import asyncio,os,time,uuid
from core import number

def balance_snapshot(raw,prices,account,sequence):
    if not isinstance(raw.get('total'),dict) or not isinstance(raw.get('free'),dict) or not isinstance(raw.get('used'),dict):raise ValueError('交易所未提供完整余额分项')
    balances=[]
    for asset,total in raw['total'].items():
        if total is None or number(total)==0:continue
        free,locked=raw['free'].get(asset),raw['used'].get(asset)
        if free is None or locked is None or abs(number(total)-number(free)-number(locked))>number('0.00000001'):raise ValueError('余额 total 与 free / used 不一致，停止更新')
        balances.append({'asset':asset,'free':str(free),'locked':str(locked),'debt':'0','usd_price':prices.get(asset)})
    return {'event_id':str(uuid.uuid4()),'account':account,'sequence':sequence,'kind':'snapshot','timestamp':time.time(),'balances':balances,'positions':[]}

class CCXTAdapter:
    def __init__(self,profile):
        if profile['exchange'] not in ('binance','bybit') or profile.get('product')!='spot':raise ValueError('首版真实接入仅开放 Binance / Bybit spot；杠杆、合约真实口径待专项验收')
        prefix=profile['env_prefix'];key=os.getenv(prefix+'_API_KEY');secret=os.getenv(prefix+'_API_SECRET')
        if not key or not secret:raise ValueError('环境变量中缺少该账户凭据')
        import ccxt.pro as ccxt
        self.profile=profile;self.client=getattr(ccxt,profile['exchange'])({'apiKey':key,'secret':secret,'enableRateLimit':True,'timeout':15000,'options':{'defaultType':'spot'}})
        self.lock=asyncio.Lock();self.sequence=0
    async def snapshot(self):
        async with self.lock:
            raw=await self.client.fetch_balance({'type':'spot',**({'accountType':'SPOT'} if self.profile['exchange']=='bybit' else {})})
            observed_at=time.time();price_times={}
            prices={'USDT':'1'} # Explicit USDT reference unit, no USD peg guarantee.
            for asset,total in raw.get('total',{}).items():
                if total and asset!='USDT':
                    try:
                        t=await self.client.fetch_ticker(asset+'/USDT')
                        if t.get('timestamp') and abs(time.time()*1000-t['timestamp'])<=60000 and t.get('last'):
                            prices[asset]=str(t['last']);price_times[asset]=t['timestamp']/1000
                    except Exception:pass # Missing prices make portfolio totals unavailable.
            self.sequence+=1
            for asset,at in price_times.items():
                if time.time()-at>60:prices.pop(asset,None)
            e=balance_snapshot(raw,prices,self.profile['id'],self.sequence);e['timestamp']=observed_at
            return e
    async def watch(self):
        if not self.client.has.get('watchBalance'):raise ValueError('该适配器未声明 watchBalance 支持')
        # Private WS notification is a refresh trigger, not a complete account ledger.
        await asyncio.wait_for(self.client.watch_balance(),timeout=45)
        return await self.snapshot()
    async def close(self):await self.client.close()
