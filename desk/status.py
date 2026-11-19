#!/usr/bin/env python3
"""PAPER desk status: python3 status.py [--json]"""
import os, sys, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.common import P, load, cfg
from lib.strategy import portfolio_equity, position_mark

pf = load(P("state", "portfolio.json"), {})
if not pf: print("brak portfolio (desk nie startował)"); sys.exit()
c = cfg()
pos, closed = pf.get("positions", []), pf.get("closed", [])
eq = portfolio_equity(pf, cfg=c)
wins = [t for t in closed if t["pnl_usd"] > 0]
tot = sum(t["pnl_usd"] for t in closed)
unreal = sum(position_mark(p, cfg=c) - p["cost_usd"] for p in pos)
out = {"mode": "PAPER", "equity": eq, "cash": pf["cash"], "start": pf["start_balance"], "open": len(pos), "closed": len(closed),
       "win_rate": len(wins) / len(closed) if closed else None, "avg_pnl_pct": sum(t["pnl_pct"] for t in closed) / len(closed) if closed else None,
       "total_realized_pnl_net": tot, "unrealized_mark": unreal}
if "--json" in sys.argv: print(json.dumps(out, indent=1)); sys.exit()
print(f"=== JARVIS PAPER DESK (bez prawdziwych pieniędzy) ===")
print(f"Equity ${eq:.2f} (start ${pf['start_balance']:.2f}, {100*(eq/pf['start_balance']-1):+.2f}%)  cash ${pf['cash']:.2f}")
hb = {n: load(P("state", "hb", f"{n}.json"), {}) for n in ("watchdog", "scanner", "filter", "trader")}
print("Agenci: " + ", ".join(f"{n} {int(time.time()-h.get('ts',0))}s temu" if h else f"{n} brak" for n, h in hb.items()))
print(f"\nOtwarte pozycje: {len(pos)}")
for p in pos:
    print(f"  {p['symbol']:<10} koszt ${p['cost_usd']:.2f}  ruch {p.get('last_gain',0)*100:+.1f}%  szczyt {p.get('peak_gain',0)*100:+.1f}%  stop {p['stop_pct']*100:+.1f}%  od {p['entry_iso'][11:16]}")
print(f"\nZamknięte: {len(closed)}  win rate {('%.0f%%' % (100*out['win_rate'])) if closed else '-'}  "
      f"śr. PnL {('%+.2f%%' % (100*out['avg_pnl_pct'])) if closed else '-'}  PnL netto ${tot:+.2f}  (niezreal. ${unreal:+.2f} przed kosztem wyjścia)")
for t in closed[-15:]:
    print(f"  {t['exit_iso'][5:16]} {t['symbol']:<10} {t['gross_pct']*100:+6.1f}% brutto  {t['pnl_pct']*100:+6.1f}% netto  ${t['pnl_usd']:+.2f}  {t['reason']}")
