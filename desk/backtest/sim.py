"""Backtest on real GeckoTerminal 1-minute candles (point-in-time features computed from candles only).
Liquidity at time t estimated as liq_now*sqrt(price_t/price_now) (constant-product pool, fixed LP)."""
import json,glob,math,bisect,sys,os
sys.path.insert(0,os.path.join(os.path.dirname(os.path.abspath(__file__)),'..'))
from lib.strategy import ladder_update,sim_buy,sim_sell
CFG=json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),'..','tests','fixtures','config_ref_20261120.json'))); CC=CFG['costs']
STOP_OVERSHOOT=0.02   # observed: -8% stops filled at -10.1% gross on average (15 real desk stop exits)

def load_coins():
    coins=[]
    for f in glob.glob('c/*.json'):
        d=json.load(open(f)); C=d.get('candles') or []
        try: pn=float(d['price_now']); fdv=float(d.get('fdv_now') or d.get('mcap_now') or 0); ln=float(d.get('liq_now') or 0)
        except Exception: continue
        if len(C)<30 or pn<=0 or fdv<=0 or ln<=0: continue
        d['mint']=os.path.basename(f)[:-5]; d['supply']=fdv/pn
        d['T']=[c[0] for c in C]; d['C']=C
        coins.append(d)
    return coins

def feats(d,i):
    C,T=d['C'],d['T']; t=T[i]+60; px=C[i][4]
    def px_at(tt):
        j=bisect.bisect_right(T,tt-60)-1
        return C[j][4] if j>=0 else C[0][1]
    def vol(a,b):
        j0=bisect.bisect_left(T,a); return sum(C[k][5] for k in range(j0,i+1))
    vh1=vol(t-3600,t); vm5=vol(t-300,t)
    age=(t-d['created']/1000)/60 if d.get('created') else None
    return {'t':t,'px':px,'mcap':px*d['supply'],'liq':d['liq_now']*math.sqrt(px/float(d['price_now'])),
            'm5':(px/px_at(t-300)-1)*100,'h1':(px/px_at(t-3600)-1)*100,'h6':(px/px_at(t-21600)-1)*100,
            'vh1':vh1,'accel':vm5*12/vh1 if vh1>0 else 0,'age':age,'n5':sum(1 for k in range(bisect.bisect_left(T,t-300),i+1))}

def passes(f,v):
    return (v['mc_lo']<=f['mcap']<=v['mc_hi'] and f['liq']>=v['liq'] and f['vh1']>=v['vh1'] and f['age'] is not None and f['age']>=v['age']
            and f['m5']>0 and v['h1_lo']<f['h1']<=v['h1_hi'] and f['h6']>=v['h6_lo'] and f['h6']<=v['h6_hi'] and f['accel']>=v['accel']
            and f['m5']<=v['m5_hi'] and f['n5']>=v.get('n5',0))

def exit_walk(C,j0,entry,v):
    """From candle j0 (entry candle, entered at its open) walk to exit. Returns (gross list of (frac,price)), exit_t, reason, peak."""
    cfg=dict(CFG); cfg['initial_stop']=v['stop']; cfg['ladder']=v['ladder']; cfg['trail_tiers']=v['trail']
    pos={'entry_price':entry,'stop_pct':v['stop']}; left=1.0; fills=[]; tp=v.get('tp')  # tp=(gain,frac)
    for c in C[j0:]:
        t,o,h,l,cl=c[:5]
        path=[o,l,h,cl] if cl>=o else [o,h,l,cl]
        for k,p in enumerate(path):
            st=pos.get('stop_pct',v['stop']); spx=entry*(1+st)
            if p<=spx:
                fill=min(p if k==0 else spx, spx*(1-STOP_OVERSHOOT))
                fills.append((left,fill)); return fills,t+60,'stop' if st<0 else ('be' if st==0 else 'trail'),pos.get('peak_gain',0)
            if tp and left==1.0 and p>=entry*(1+tp[0]):
                fills.append((tp[1],entry*(1+tp[0]))); left-=tp[1]
                if v.get('tp_be') is not None: pos['stop_pct']=max(pos.get('stop_pct',-1),v['tp_be'])
            ex,r=ladder_update(pos,p,cfg)
            if ex:
                fills.append((left,entry*(1+pos['stop_pct'])*(1-STOP_OVERSHOOT/2))); return fills,t+60,'trail',pos['peak_gain']
    fills.append((left,C[-1][4])); return fills,C[-1][0]+60,'open_end',pos.get('peak_gain',0)

def pullback(C,i,v):
    X,Y,T,cancel=v['pb']; H=C[i][4]; L=None; t0=C[i][0]+60
    for j in range(i+1,len(C)):
        t,o,h,l,cl=C[j][:5]
        if t>t0+T*60: return None
        if L is None:
            H=max(H,h)
            if l<=H*(1-cancel): return None
            if l<=H*(1-X): L=l
        else:
            if h>=L*(1+Y): return j, max(o,L*(1+Y)), t+30
            L=min(L,l)
            if L<=H*(1-cancel): return None
    return None

def signals(coins,v):
    """Chronological candidate signals with per-coin cooldown between consecutive passes (reentry handled in portfolio)."""
    out=[]
    for d in coins:
        C=d['C']; last=-1e18
        if 'F' not in d: d['F']=[feats(d,i) for i in range(len(C)-1)]
        for i in range(len(C)-1):
            if C[i][0]-last<300: continue
            f=d['F'][i]
            if passes(f,v):
                out.append((f['t'],d,i,f)); last=C[i][0]
    out.sort(key=lambda x:x[0]); return out

def run(coins,v,size_usd=5.0,start=25.0,t_from=0,t_to=9e18,sigs=None):
    sigs=sigs if sigs is not None else signals(coins,v)
    trades=[]; busy_until={}; open_ends=[]; ban_day={}
    for t,d,i,f in sigs:
        if not (t_from<=t<t_to): continue
        m=d['mint']
        if busy_until.get(m,0)>t: continue
        open_ends=[e for e in open_ends if e>t]
        if len(open_ends)>=v.get('max_open',4): continue
        if v['entry']=='now':
            if i+1>=len(d['C']) or d['C'][i+1][0]-t>180: continue
            j=i+1; e=d['C'][j][1]; te=d['C'][j][0]
        else:
            r=pullback(d['C'],i,v)
            if not r: busy_until[m]=t+v['pb'][2]*60; continue
            j,e,te=r
        fills,tx,reason,peak=exit_walk(d['C'],j,e,v)
        liq=f['liq']; b=sim_buy(size_usd,e,liq,CC); proceeds=0
        for frac,px in fills:
            s=sim_sell(b['qty']*frac,px,liq*math.sqrt(max(px,1e-18)/e),CC); proceeds+=s['proceeds']
        pnl=proceeds-size_usd
        trades.append({'t':te,'sym':d['sym'],'mint':m,'dex':d['dex'],'pct':pnl/size_usd,'gross':sum(fr*px for fr,px in fills)/e-1,'reason':reason,'peak':peak,'hold':(tx-te)/60,'f':f})
        open_ends.append(tx)
        cool = 86400 if pnl<0 and v.get('ban_loss',True) else CFG['reentry_cooldown_min']*60
        busy_until[m]=tx+cool
    return trades

def stats(tr):
    n=len(tr)
    if not n: return {'n':0}
    w=[x for x in tr if x['pct']>0]; ex=sum(x['pct'] for x in tr)/n
    eq=1.0
    for x in sorted(tr,key=lambda x:x['t']): eq*=1+0.2*x['pct']
    return {'n':n,'wr':len(w)/n,'exp':ex,'med':sorted(x['pct'] for x in tr)[n//2],'eq20':eq-1}
