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
