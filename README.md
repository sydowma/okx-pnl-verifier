# OKX Monthly Leaderboard PnL Verifier (`okx-pnl-verifier`)

A Python verification toolkit and reference implementation for building a **monthly derivatives PnL leaderboard** on **OKX v5 OpenAPI** — validated live across **USDT-M Perpetual**, **Coin-M Perpetual**, **Coin-M Delivery Futures**, and **USDC-Settled Linear Contracts**.

For the full Chinese technical feasibility report (with architecture flowcharts, sequence diagrams, 100k-user midnight snapshot design, and live test evidence), open [report.html](./report.html) in a browser.

---

## Key Conclusions Verified Live

- **Recommended Architecture (`positions` + `positions-history` + WS `positions` incremental):**
  - **Bootstrap once via REST** (`GET /api/v5/account/positions-history` + `GET /api/v5/account/positions`), then switch to **Private WebSocket `positions` (`instType=ANY`, port `443`)** for real-time in-memory incremental updates (zero polling, immune to deposits/transfers and passive spot-holding price swings).
  - **Full-close handling (`pos == "0"`):** OKX clears `realizedPnl` to `""` on the `pos == "0"` WS close frame, and `positions-history` has a ~1–2s indexing delay. On `pos == "0"`, move the position to `pending_close` and enqueue a delayed lookup (`1s → 2s → 5s → 15s → 60s`) matching strictly on **`(posId, cTime)`** (note: `posId` alone is reused by OKX across trades!).
  - **Live WS vs. REST Reconciliation Diff:** **`$0.0000`** across USDT-M, Coin-M Perpetual (`BTC-USD-SWAP`), Coin-M Delivery (`BTC-USD-261225`), and USDC-Settled Delivery (`BTC-USD_UM-261225`).
- **100k-User Zero-Skew Midnight Snapshot:**
  - Pre-warm open positions and WS connections before the 1st at `00:00:00`. At `00:00:00.000`, freeze **1 shared public `markPx(T0)` / `idxPx(T0)` table** and re-price all 100k users locally from Redis/DB (0 private REST calls at midnight, 0ms price skew).
- **Mid-Month New Users (No 1st-of-Month Snapshot):**
  - Positions with `cTime >= T0` are immediately 100% exact (2 REST calls, 0 `bills`).
  - Only if cross-month positions (`cTime < T0`) exist: use either mark-candle `UPL(T0)` approximation (zero `bills`) or a one-time scoped `fills-history` + `bills-archive` backfill for that specific `instId`, then switch permanently to WS incremental.

- **Alternative Lightweight Architecture (`totalEq` + Daily `type=1` Transfer Aggregation):**
  - If your leaderboard should include **Spot + Derivatives combined** and you want a simpler WS state machine (no `pos == "0"` retry queue), use `totalEq(Now) - SUM(daily_net_transfer_usd) - totalEq(T0)` with WS `account`, filtering `bills-archive` strictly by `type=1` (usually <1 page/month) and reconstructing missing `Cash@T0` in 1 call via `bills-archive?ccy=<ccy>&end=T0&limit=1`.
  - Both architectures compute in **`O(1)` per WS event** at 100k-user scale (<150 MB RAM).

---

## Project Structure

```text
okx-pnl-verifier/
├── report.html                              # Full Chinese HTML feasibility report (with SVG diagrams)
├── pyproject.toml                           # Project metadata & dependencies (managed via uv)
├── .env.example                             # Environment variable template
├── src/okx_pnl/
│   ├── client.py                            # Read-only OKX v5 REST client (HMAC-SHA256, pagination, candles)
│   ├── calculator.py                        # Full equity + bills/fills T0 reconstruction & cross-check engine
│   ├── positions_only.py                    # Lightweight bill-free Mode C calculator & T0 positions snapshot
│   ├── ws_positions_tracker.py              # Mode C + Private WS `positions` incremental tracker (multi-ccy)
│   ├── ws_listener.py                       # Private WS `account` + `positions` live streamer
│   └── cli.py                               # `okx-pnl` CLI entry point (`verify` and `stream`)
├── scripts/
│   ├── live_demo_experiment.py              # Live Demo ground-truth test: Equity/Bills T0 reconstruction
│   ├── live_positions_only_experiment.py    # Live Demo test: Bill-free Modes A/B/C comparison
│   ├── live_ws_incremental_test.py          # Live Demo test: WS `positions` incremental vs REST audit
│   └── live_multi_contract_test.py          # Live Demo test: Coin-M SWAP + Delivery FUTURES + USDC-settled
└── tests/
    └── test_calculator.py                   # Offline unit tests for cross-month & spot-drift edge cases
```

---

## Quick Start

### 1. Setup Environment

```bash
cd /Users/mark/GitHub/sydowma/okx-pnl-verifier
cp .env.example .env
```

Edit `.env` with a **Read-Only** OKX API key (set `OKX_SIMULATED=1` for OKX Demo Trading, or `0` for live):

```env
OKX_API_KEY=your_api_key
OKX_API_SECRET=your_api_secret
OKX_PASSPHRASE=your_passphrase
OKX_SIMULATED=1
```

### 2. Run Offline Unit Tests (No API Key Required)

```bash
uv run pytest -v
```

### 3. Run CLI Verification

```bash
# REST monthly PnL verification & comparison report
uv run okx-pnl verify --start "2026-11-01T00:00:00+08:00"

# Reconstruct baseline via REST, then stream live updates via Private WS
uv run okx-pnl stream --start "2026-11-01T00:00:00+08:00"
```

### 4. Run Live Demo Trading Experiments (`OKX_SIMULATED=1` Only)

> All scripts under `scripts/` include a safety guard requiring `OKX_SIMULATED=1` and automatically close all test positions on completion.

```bash
# 1. WS `positions` incremental vs REST audit (USDT-M cross-month + partial/full close)
PYTHONPATH=. uv run python scripts/live_ws_incremental_test.py

# 2. Multi-contract WS incremental test (Coin-M SWAP + Delivery FUTURES + USDC-settled)
PYTHONPATH=. uv run python scripts/live_multi_contract_test.py

# 3. Bill-free Modes A / B / C live comparison (with mid-month transfer)
PYTHONPATH=. uv run python scripts/live_positions_only_experiment.py

# 4. Full Equity/Bills T0 missing-snapshot ground-truth experiment
PYTHONPATH=. uv run python scripts/live_demo_experiment.py
```

---

## Important OKX v5 Production Notes

1. **WebSocket Port:** Use port `443` (`wss://ws.okx.com/ws/v5/private`; demo: `wss://wspap.okx.com/ws/v5/private`). Port `8443` is being retired and emits `code: 64010` `notice` frames.
2. **Join Key for Closed Positions:** Always match on **`(posId, cTime)`** — OKX reuses `posId` across trades on the same instrument/side/margin mode.
3. **Coin-M USD Conversion:** `realizedPnl` and `upl` on inverse contracts (`BTC-USD-SWAP`, `BTC-USD-261225`) are denominated in `ccy` (e.g. `BTC`). Multiply by WS `idxPx` while open, and by historical index price at `uTime` when closed.
4. **Retention Limits:** `GET /api/v5/account/bills` only retains **7 days**; `bills-archive`, `positions-history`, and `fills-history` retain **3 months**.
5. **WS `fills` Channel:** Gated by VIP fee tier (`code: 64003` for retail accounts) — do not rely on it for a general user leaderboard.
