"""Local SQLite ingest timing, not venue latency or production SLA."""
import json,tempfile,time,statistics
from pathlib import Path
from core import Store
with tempfile.TemporaryDirectory() as d:
    s=Store(Path(d)/'bench.sqlite');s.register('bench','fixture','spot','benchmark','demo');lat=[]
    for i in range(500):
        e={'account':'bench','sequence':i,'event_id':str(i),'kind':'snapshot','timestamp':time.time(),'balances':[{'asset':'USDT','free':'100','locked':'0','debt':'0','usd_price':'1'}],'positions':[]}
        start=time.perf_counter();s.ingest(e);lat.append((time.perf_counter()-start)*1000)
    print(json.dumps({'samples':len(lat),'p50_ms':statistics.median(lat),'p95_ms':sorted(lat)[int(.95*(len(lat)-1))],'scope':'本机单线程 SQLite 每事件独立事务；不含网络、排队或生产负载','replay_matched':s.replay('bench')['matched']},ensure_ascii=False,indent=2))
