import sim,itertools,json,sys
co=sim.load_coins()
import os
if os.environ.get('UNI')=='wl': co=[d for d in co if d.get('src')!='gt_trending']
if os.environ.get('UNI')=='gt': co=[d for d in co if d.get('src')=='gt_trending']
base=dict(mc_lo=40000,mc_hi=200000,liq=30000,vh1=5000,age=20,h1_lo=0,h1_hi=300,h6_lo=0,h6_hi=1e9,accel=0.8,m5_hi=25,entry='pullback',pb=(0.05,0.03,15,0.15),stop=-0.08,ladder=[[0.12,0.0]],trail=[[0.24,0.12],[1.0,0.2],[2.0,0.25]])
EX={'L0_s8':dict(stop=-0.08,ladder=[[0.12,0.0]]),
    'L3_s8':dict(stop=-0.08,ladder=[[0.12,0.03]]),
    'L3_s6':dict(stop=-0.06,ladder=[[0.10,0.03]]),
    'TP_s6':dict(stop=-0.06,ladder=[[0.10,0.03]],tp=(0.15,0.5),tp_be=0.03),
    'L3_s5':dict(stop=-0.05,ladder=[[0.08,0.03]])}
SPLIT=None
allsig_t=sorted(t for d in co for t in [c[0] for c in d['C']])
rows=[]
def trim(tr):
    if len(tr)<3: return None
    x=sorted(t['pct'] for t in tr)[:-1]; return sum(x)/len(x)
for age,liq,h1hi,h6lo,entry,ex,m5hi,maxo in itertools.product([20,120,360],[30000,50000],[300,100,60,40],[0,10],['now','pullback'],list(EX),[25,12],[4,2]):
    v=dict(base,age=age,liq=liq,h1_hi=h1hi,h6_lo=h6lo,entry=entry,m5_hi=m5hi,max_open=maxo,**EX[ex])
    sg=sim.signals(co,v)
    if len(sg)<10: continue
    rows.append(dict(key=(age,liq,h1hi,h6lo,entry,ex,m5hi,maxo),sg=sg,v=v))
# fixed time split for all variants: 20.11 00:00 Warsaw
SPLIT=1795129200
out=[]
for r in rows:
    a=sim.run(co,r['v'],sigs=r['sg'],t_to=SPLIT); b=sim.run(co,r['v'],sigs=r['sg'],t_from=SPLIT); t=sim.run(co,r['v'],sigs=r['sg'])
    out.append(dict(key=r['key'],tr=sim.stats(a),te=sim.stats(b),all=sim.stats(t),tr_trim=trim(a),te_trim=trim(b),all_trim=trim(t)))
json.dump(out,open('grid2.json','w'),default=str)
def f(s,tm): return (f"n={s['n']:3d} wr={s.get('wr',0)*100:3.0f}% exp={s.get('exp',0)*100:+6.2f}% trim={tm*100 if tm is not None else 0:+6.2f}%") if s['n'] else 'n=0'
ok=[o for o in out if o['tr']['n']>=20]
ok.sort(key=lambda o:-(o['tr_trim'] or -9))
print(len(co),'coins',len(out),'variants')
for o in ok[:30]: print(o['key'],'| TR',f(o['tr'],o['tr_trim']),'| TE',f(o['te'],o['te_trim']),'| ALL',f(o['all'],o['all_trim']))
cur=[o for o in out if o['key']==(20,30000,300,0,'pullback','L0_s8',25,4)][0]
print('CURRENT',cur['key'],'| TR',f(cur['tr'],cur['tr_trim']),'| TE',f(cur['te'],cur['te_trim']),'| ALL',f(cur['all'],cur['all_trim']))
pos_te=sum(1 for o in ok if (o['te_trim'] or -1)>0); print('variants with n>=20 train:',len(ok),'test trimmed exp>0:',pos_te)
