"""
Live Demo Test: Multi-Contract Verification (Coin-M SWAP + Coin-M Delivery FUTURES + USDC-M SWAP)
under Mode C + WebSocket `positions` Incremental Tracking.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from okx_pnl.client import OkxConfig, OkxRestClient
from okx_pnl.positions_only import calculate_positions_only_pnl, snapshot_open_positions
from okx_pnl.ws_positions_tracker import (
    IncrementalPositionsTracker,
    stream_positions_incremental,
)
from scripts.live_demo_experiment import _place_market, _post


async def main() -> None:
    load_dotenv()
    cfg = OkxConfig.from_env()
    if not cfg.simulated:
        raise RuntimeError("SAFETY ABORT: OKX_SIMULATED must be 1!")

    with OkxRestClient(cfg) as client:
        # Ensure we have some USDC in Trading account for USDC-M SWAP test
        bal = client.get_account_balance()
        usdc_bal = next(
            (float(d["cashBal"]) for d in bal.get("details", []) if d["ccy"] == "USDC"),
            0.0,
        )
        if usdc_bal < 20.0:
            print("=== PREP: Buying 50 USDC via USDC-USDT spot market order ===")
            _post(
                client,
                "/api/v5/trade/order",
                {
                    "instId": "USDC-USDT",
                    "tdMode": "cash",
                    "side": "buy",
                    "ordType": "market",
                    "sz": "50",
                    "tgtCcy": "quote_ccy",
                },
            )
            time.sleep(2)

        t0_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        t0_snap = snapshot_open_positions(client)
        tracker = IncrementalPositionsTracker(start_ts_ms=t0_ms, t0_snapshots=t0_snap)
        tracker.bootstrap_from_rest(client)

        stop_event = asyncio.Event()
        ready_event = asyncio.Event()
        ws_task = asyncio.create_task(
            stream_positions_incremental(
                cfg,
                tracker,
                rest_client=client,
                stop_event=stop_event,
                on_ready=ready_event.set,
            )
        )
        await asyncio.wait_for(ready_event.wait(), timeout=15.0)
        print(f"=== WS `positions` (instType=ANY) Ready! T0={t0_ms} ===\n")

        # 1. Coin-margined Perpetual (BTC-USD-SWAP, ccy=BTC)
        print("-> [1/3] Coin-M Perpetual: Open & Close BTC-USD-SWAP (+1 / -1)")
        await asyncio.to_thread(_place_market, client, "BTC-USD-SWAP", "buy", "1")
        await asyncio.sleep(3)
        await asyncio.to_thread(_place_market, client, "BTC-USD-SWAP", "sell", "1")
        await asyncio.sleep(3)

        # 2. Coin-margined Delivery Futures (BTC-USD-261225, instType=FUTURES, ccy=BTC)
        print("\n-> [2/3] Delivery Futures: Open & Close BTC-USD-261225 (+1 / -1)")
        await asyncio.to_thread(_place_market, client, "BTC-USD-261225", "buy", "1")
        await asyncio.sleep(3)
        await asyncio.to_thread(_place_market, client, "BTC-USD-261225", "sell", "1")
        await asyncio.sleep(3)

        # 3. USDC-settled Linear Delivery Futures (BTC-USD_UM-261225, ccy=USDC)
        print("\n-> [3/3] USDC-Settled Linear Contract: Open & Close BTC-USD_UM-261225 (+1 / -1)")
        await asyncio.to_thread(_place_market, client, "BTC-USD_UM-261225", "buy", "1")
        await asyncio.sleep(3)
        await asyncio.to_thread(_place_market, client, "BTC-USD_UM-261225", "sell", "1")
        await asyncio.sleep(4)

        stop_event.set()
        await ws_task

        print("\n=== GROUND-TRUTH AUDIT (WS Incremental vs REST Mode C) ===")
        rest_audit = calculate_positions_only_pnl(
            client, start_ts_ms=t0_ms, t0_snapshots=t0_snap
        )
        print("=" * 76)
        for d in rest_audit.details:
            print(
                f"  {d['instId']:18s} [{d['status']:21s}] | REST PnL=${d['with_snap_pnl']:+.4f}"
            )
        print("-" * 76)
        ws_pnl = tracker.monthly_pnl_usd
        rest_pnl = rest_audit.with_t0_snapshot_pnl_usd or 0.0
        print(f"  WS Incremental Events Processed : {tracker.ws_events_processed}")
        print(f"  WS-Only Incremental Monthly PnL : ${ws_pnl:>+10.4f}")
        print(f"  REST Ground-Truth (Mode C) PnL  : ${rest_pnl:>+10.4f}")
        print(f"  Difference (WS - REST)          : ${ws_pnl - rest_pnl:>+10.4f}")
        print("=" * 76)


if __name__ == "__main__":
    asyncio.run(main())
