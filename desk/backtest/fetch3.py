import json,time,urllib.request,os
def get(u,tries=6):
    for i in range(tries):
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'}),timeout=20).read())
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(10*(i+1)); continue
            if e.code==404: return None
            time.sleep(3)
        except Exception: time.sleep(3)
pools={}
urls=[f'https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?duration={d}&page={p}' for d in ('24h','6h') for p in range(1,11)]
urls+=[f'https://api.geckoterminal.com/api/v2/networks/solana/dexes/pumpswap/pools?sort=h24_volume_usd_desc&page={p}' for p in range(1,11)]
for u in urls:
    time.sleep(2.2); r=get(u) or {}
    for x in r.get('data') or []:
        a=x['attributes']; base=x['relationships']['base_token']['data']['id'].split('_',1)[1]
        pools[base]={'pool':a['address'],'created':a.get('pool_created_at'),'fdv':a.get('fdv_usd'),'liq':a.get('reserve_in_usd'),'price':a.get('base_token_price_usd'),'name':a.get('name'),'dex':x['relationships']['dex']['data']['id']}
print(len(pools),'pools',flush=True)
from datetime import datetime
START=time.time()-31*3600; n=0
for m,p in pools.items():
    fn=f'c/{m}.json'
    if os.path.exists(fn): continue
    try:
        created=datetime.fromisoformat(p['created'].replace('Z','+00:00')).timestamp()
        fdv=float(p['fdv'] or 0); liq=float(p['liq'] or 0); px=float(p['price'] or 0)
    except Exception: continue
    if created<time.time()-21*86400 or fdv>20e6 or fdv<=0 or liq<=0 or px<=0: continue
    cand=[]; before=int(time.time())
    for page in range(2):
        time.sleep(2.1)
        r=get(f"https://api.geckoterminal.com/api/v2/networks/solana/pools/{p['pool']}/ohlcv/minute?aggregate=1&limit=1000&currency=usd&token={m}&before_timestamp={before}")
        L=((r or {}).get('data') or {}).get('attributes',{}).get('ohlcv_list') or []
        cand+=L
        if len(L)<1000 or min(x[0] for x in L)<=START: break
        before=min(x[0] for x in L)
    cand=sorted({x[0]:x for x in cand}.values())
    json.dump({'pool':p['pool'],'dex':p['dex'],'sym':(p['name'] or '').split(' /')[0],'created':created*1000,'liq_now':liq,'fdv_now':fdv,'mcap_now':None,'price_now':px,'candles':cand,'src':'gt_trending'},open(fn,'w'))
    n+=1; print(n,m,p['name'],p['dex'],len(cand),flush=True)
print('DONE')
