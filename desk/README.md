# Jarvis PAPER desk (no real money)
Paper-trading Solana momentum desk. No wallet, no Phantom, no signing. Public data only (DexScreener + GeckoTerminal).

Processes (watchdog.py supervises, restarts on crash / stale heartbeat):
- agents/scanner.py  – discovery (DS boosts/profiles/CTO, GT trending 5m/1h/6h, top tx, new pools) + rolling 6h watchlist, analyst score -> state/candidates.json (every scan_interval_s=75s)
- agents/filter.py   – hard rules, every decision with reasons -> logs/decisions.jsonl; passes -> signals/approved/
- agents/trader.py   – re-quotes + re-checks, sizes 5% of current paper equity, simulated fills with costs, polls open positions every 20s, trailing ladder; state/portfolio.json, logs/trades.jsonl, Polish notes notify/pending (tagged PAPIER)
Commands: ./start.sh | ./stop.sh | python3 status.py [--json] | python3 tests/test_ladder.py
Uptrend definition + cost model: see docstrings in lib/strategy.py. Ladder: stop -8%; peak>=+12% -> stop 0%; peak>=+24% trail 12 pp; peak>=+100% trail 20 pp; peak>=+200% trail 25 pp (never moves down). Entry mode pullback (P2): after a pass, buy on a 5% dip from the high plus a 3% bounce; cancel at -15% or after 15 min. If config stop_at is set, after that instant no new entries and no new pullback setups (open positions still managed). Current cutoff: 12:25 Europe/Warsaw on 2026-11-20.
Ladder measured on market price vs entry market price; exit fills at the observed price (gaps below the stop are realistic losses), costs then applied.
Reset paper account: ./stop.sh && rm state/portfolio.json && ./start.sh
- agents/publisher.py – builds public-safe data.json (no wallet/keys) + site/ template, copies into /workspace/jarvis-desk/site (git repo) and pushes to GitHub Pages every ~60 s when data changes (forced refresh ≥ 5 min). URL: https://dawikoro-tech.github.io/jarvis-desk/  Manual: python3 agents/publisher.py once
  NOTE: jarvis-live's publisher pushes to the same repo – do not run both publishers at once.

## GitHub Actions (since 19.11.2026)
The desk runs in this repo via `.github/workflows/desk.yml` (workflow "paper-desk"): cron every 30 min + manual dispatch,
concurrency group `paper-desk` (one run at a time, the next one waits queued). Each run trades ~5h40m (`desk/run_actions.py`),
then saves state and exits; the queued run resumes from the committed state.
- Committed every minute (publisher): `data.json` (site), `desk/state/portfolio.json`, `desk/state/equity_history.json`,
  `desk/logs/trades.jsonl`, `desk/notify/notes.jsonl`. Watchlist: `desk/persist/watchlist.json` (hourly + at shutdown).
- Polish trade notes: append-only `desk/notify/notes.jsonl` (last 300), one JSON per line: id, ts, type, title, message.
PAPER ONLY: no wallet, no keys, no secrets; uses the workflow's own GITHUB_TOKEN to commit.

## Variant S (since 20.11.2026 ~13:05, paper book reset to $25; run7 archived)
Strict filter: pair age >= 6 h, 1h change 0..+100%, 6h change >= +10%, plus the existing rules (mcap $40k-200k, liquidity >= $30k,
score >= 69, buy/sell ratios, m5 <= +25%, RugCheck, EOD ban after a loss). Entry: buy now (pullback off). Exits: stop -6%;
peak >= +10% -> stop +3%; at +15% sell 50% (config `take_profit`) and stop +3%; trailing 12/20/25 pp from +24/+100/+200%.
Max 2 open, 20% of equity each. No entry cutoff (`stop_at` null). Backtest scripts + results: `backtest/` (real GeckoTerminal
1-minute candles; no robust edge found, see results files).

## Paper copy book @unipcs (agents/copybook.py, state/copybook.json)
Separate $25 paper book mirroring public wallet 2heJbC32Tpfcb3nbUb5ER61K11FGZVfVGtVnDm6LDogF via public Solana RPC (no keys):
new buys paid >= 0.005 SOL or >= $1 USDC/USDT (add-ons/airdrops skipped), sells mirrored proportionally (>= 90% = full exit),
20% of copy equity per copy, max 4, desk cost model. Notes prefix "[PAPIER – kopia @unipcs]" in notify/notes.jsonl. Never touches a wallet.
