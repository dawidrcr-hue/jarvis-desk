import json,time,urllib.request,os,sys
U=json.load(open('/workspace/bt/universe.json'))
def get(u,tries=6):
    for i in range(tries):
        try:
            r=urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'}),timeout=20)
            return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code==429: time.sleep(10*(i+1)); continue
            if e.code==404: return None
            time.sleep(3)
        except Exception: time.sleep(3)
    return None
# 1) dexscreener pairs
if not os.path.exists('pairs.json'):
    mints=list(U); P={}
    for i in range(0,len(mints),30):
        d=get('https://api.dexscreener.com/tokens/v1/solana/'+','.join(mints[i:i+30])) or []
        for p in d:
            m=p['baseToken']['address']
            if m not in mints: continue
            if m not in P or (p.get('liquidity') or {}).get('usd',0)>(P[m].get('liquidity') or {}).get('usd',0): P[m]=p
        time.sleep(1.1)
    json.dump(P,open('pairs.json','w'))
P=json.load(open('pairs.json'))
START=time.time()-31*3600   # 19.11 ~05:30
sel=[]
for m,p in P.items():
    created=(p.get('pairCreatedAt') or 0)/1000
    fdv=p.get('fdv') or p.get('marketCap') or 0
    if created<time.time()-21*86400: continue   # memecoin-age pairs only
    if fdv>20e6: continue
    sel.append(m)
print(len(P),'pairs',len(sel),'selected',flush=True)
for k,m in enumerate(sel):
    fn=f'c/{m}.json'
    if os.path.exists(fn): continue
    p=P[m]; pool=p['pairAddress']; cand=[]; before=int(time.time())
    for page in range(2):
        time.sleep(2.1)
        r=get(f'https://api.geckoterminal.com/api/v2/networks/solana/pools/{pool}/ohlcv/minute?aggregate=1&limit=1000&currency=usd&token={m}&before_timestamp={before}')
        L=((r or {}).get('data') or {}).get('attributes',{}).get('ohlcv_list') or []
        cand+=L
        if len(L)<1000 or min(x[0] for x in L)<=START: break
        before=min(x[0] for x in L)
    cand=sorted({x[0]:x for x in cand}.values())
    json.dump({'pool':pool,'dex':p.get('dexId'),'sym':p['baseToken']['symbol'],'created':p.get('pairCreatedAt'),
               'liq_now':(p.get('liquidity') or {}).get('usd'),'fdv_now':p.get('fdv'),'mcap_now':p.get('marketCap'),'price_now':p.get('priceUsd'),'candles':cand},open(fn,'w'))
    print(k,m,p['baseToken']['symbol'],p.get('dexId'),len(cand),flush=True)
print('DONE')
