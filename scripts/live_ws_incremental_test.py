"""
Live Demo Test: Mode C + WebSocket `positions` Incremental Listener.

Proves whether REST-bootstrap-once + WS-only incremental updates match
ground-truth REST `positions` + `positions-history` to the cent across:
  1. Cross-boundary position full close (ETH-USDT-SWAP opened before T0, closed during WS)
  2. Intra-month position open -> partial close -> full close (SOL-USDT-SWAP during WS)
  3. Inspects the EXACT raw WS `pos == "0"` close frame from OKX!
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from okx_pnl.client import OkxConfig, OkxRestClient
from okx_pnl.positions_only import calculate_positions_only_pnl, snapshot_open_positions
from okx_pnl.ws_positions_tracker import (
    IncrementalPositionsTracker,
    stream_positions_incremental,
)
from scripts.live_demo_experiment import _place_market


async def main() -> None:
    load_dotenv()
    cfg = OkxConfig.from_env()
    if not cfg.simulated:
        raise RuntimeError("SAFETY ABORT: OKX_SIMULATED must be 1!")

    with OkxRestClient(cfg) as client:
        print("=== STEP 1: Open cross-boundary ETH-USDT-SWAP (+2) BEFORE T0 ===")
        _place_market(client, "ETH-USDT-SWAP", "buy", "2")
        time.sleep(3)

        t0_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        t0_snap = snapshot_open_positions(client)
        print(f"=== STEP 2: T0={t0_ms} lightweight snapshot & ONE-TIME REST bootstrap ===")
        for k, v in t0_snap.items():
            print(f"  T0 snap {v.inst_id}: realizedPnl=${v.realized_pnl_usd:+.4f}, upl=${v.upl_usd:+.4f}, total=${v.total_pnl_at_t0:+.4f}")

        tracker = IncrementalPositionsTracker(start_ts_ms=t0_ms, t0_snapshots=t0_snap)
        tracker.bootstrap_from_rest(client)
        print(f"  Bootstrapped MonthlyPnL = ${tracker.monthly_pnl_usd:+.4f} (ZERO REST calls from here on!)")

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
        print("\n=== STEP 3: WS `positions` subscribed! Executing live trades ===")
        await asyncio.sleep(2)

        print("  -> Action 1: Open SOL-USDT-SWAP (+2) [intra-month]")
        await asyncio.to_thread(_place_market, client, "SOL-USDT-SWAP", "buy", "2")
        await asyncio.sleep(3)

        print("  -> Action 2: Partial close SOL-USDT-SWAP (-1)")
        await asyncio.to_thread(_place_market, client, "SOL-USDT-SWAP", "sell", "1")
        await asyncio.sleep(3)

        print("  -> Action 3: Full close SOL-USDT-SWAP (-1)")
        await asyncio.to_thread(_place_market, client, "SOL-USDT-SWAP", "sell", "1")
        await asyncio.sleep(3)

        print("  -> Action 4: Full close ETH-USDT-SWAP (-2) [cross-boundary]")
        await asyncio.to_thread(_place_market, client, "ETH-USDT-SWAP", "sell", "2")
        await asyncio.sleep(4)

        stop_event.set()
        await ws_task

        print("\n=== STEP 4: Inspect RAW OKX WS `pos == 0` close frames ===")
        for i, frame in enumerate(tracker.raw_close_frames, 1):
            subset = {
                k: frame.get(k)
                for k in ("instId", "pos", "posSide", "realizedPnl", "pnl", "fee", "fundingFee", "upl", "cTime", "uTime")
            }
            print(f"  Close frame #{i}: {json.dumps(subset)}")

        print("\n=== STEP 5: Ground-Truth Audit (WS Incremental vs REST Mode C) ===")
        rest_audit = calculate_positions_only_pnl(
            client, start_ts_ms=t0_ms, t0_snapshots=t0_snap
        )
        ws_pnl = tracker.monthly_pnl_usd
        rest_pnl = rest_audit.with_t0_snapshot_pnl_usd or 0.0
        print("=" * 72)
        print(f"  WS Incremental Events Processed : {tracker.ws_events_processed}")
        print(f"  WS-Only Incremental Monthly PnL : ${ws_pnl:>+10.4f}")
        print(f"  REST Ground-Truth (Mode C) PnL  : ${rest_pnl:>+10.4f}")
        print(f"  Difference (WS - REST)          : ${ws_pnl - rest_pnl:>+10.4f}")
        print("=" * 72)


if __name__ == "__main__":
    asyncio.run(main())
