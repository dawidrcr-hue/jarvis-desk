# Jarvis PAPER copy desk (no real money)
Copies public Solana wallets on a $25 paper book. No wallet, no Phantom, no keys, no signing, no secrets.

- `config.json` – wallets + allocation (@unipcs 25%, nAWy…8qNc 10%, @SOL589 10% of equity per new position), costs.
- `core.py` – pure logic (tx parsing for legacy/v0/v1, sizing, Phantom cost model, book, Polish notes). `net.py` – public RPC,
  DexScreener, Jupiter public quote API (quotes only). `desk.py` – runner (poll ~6 s per wallet, mark 20 s, publish ≥ 65 s).
- Workflow `.github/workflows/copy-desk.yml` ("copy-desk"): cron 7,37 * * * *, concurrency group copy-desk, ~5h40m per run,
  commits `data.json`, `copydesk/state/book.json`, `copydesk/notes.jsonl` at most once per ~65 s.
- Graceful stop of a run: commit `copydesk/control/stop.json` = {"run_id": "<run id>"}. Stop for good: disable the workflow.
- Notes: `copydesk/notes.jsonl` (last 300; id, ts, type, title, message, data). Box relay: `python3 /workspace/jarvis-paper/relay_copy_notes.py`.
- Reset paper book: disable workflow, delete `copydesk/state/book.json` + `copydesk/notes.jsonl`, commit, enable.
- Tests: `cd copydesk && python3 -m unittest discover -s tests`.
Copy rules: new buys only after the start; sells proportional; paid add-ons proportional (≤ 40% equity per token); airdrops skipped;
fill at the price at our detection time (Jupiter quote = pool fee + impact), Phantom 0.85% per side, network 0.000055 SOL,
token-account rent 0.00204 SOL (refunded on full exit); buys older than 10 min skipped.

## Big entries only (per-wallet filter)

Any wallet in `config.json` can set `"min_buy_usd"` and `"filter_label"`. New buys where the wallet spent less than `min_buy_usd` (SOL × SOL/USD + USDC/USDT) are skipped silently and counted in `small_skipped`. Big ones are copied at the wallet's `alloc_pct`. Sells always copy proportionally. To add another wallet (for example Cupsey) with its own threshold, add one entry to `wallets`. No code change is needed.

A leftover dust balance (less than `dust_pre_fraction` of the balance after the buy) still counts as a new entry.

## Webhook (optional)

When a `copy_buy` / `copy_sell` note is written, the desk POSTs `{note_id, side, ticker, mint, wallet, ts}` to `LIVE_COPY_WEBHOOK_URL` with `Authorization: LIVE_COPY_WEBHOOK_AUTH`. Both are GitHub Actions secrets. The POST runs in a background thread with a 5 s timeout and 2 retries. The URL and auth are never logged. If the secrets are missing, nothing is sent.

## Real view (read-only)

`real_wallet.address` is read from the public RPC every ~30 s: SOL and token balances, prices from DexScreener, and swaps since `real_wallet.since`. The result is shown on the site's "Prawdziwe" tab with Solscan links. No keys are used, and nothing is signed or sent.
