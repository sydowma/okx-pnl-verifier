"""
Live Demo Experiment: Testing the BILL-FREE (positions + positions-history ONLY) approach!

Tests:
  1. Cross-boundary CLOSED position (ETH-USDT-SWAP: open before T0, close after T0)
  2. Cross-boundary SCALED open position (BTC-USDT-SWAP: open before T0, add +1 after T0)
  3. Intra-month position (SOL-USDT-SWAP: open & close after T0)
  4. Mid-month transfer (+50 USDT Funding -> Trading, verifying zero interference!)

Compares:
  - Ground-Truth Derivatives PnL (from exact bills + UPL delta, for benchmark ONLY)
  - Bill-Free Mode A: Pure 2-API (positions + positions-history, no snapshot, no candles)
  - Bill-Free Mode B: 2-API + mark candle split (no snapshot, no bills, no fills)
  - Bill-Free Mode C: 2-API + lightweight T0 positions snapshot (no bills, no fills)
"""

from __future__ import annotations

import time
from dotenv import load_dotenv

from okx_pnl.calculator import calculate_monthly_pnl
from okx_pnl.client import OkxConfig, OkxRestClient
from okx_pnl.positions_only import calculate_positions_only_pnl, snapshot_open_positions
from scripts.live_demo_experiment import _place_market, _post


def main() -> None:
    load_dotenv()
    cfg = OkxConfig.from_env()
    if not cfg.simulated:
        raise RuntimeError("SAFETY ABORT: OKX_SIMULATED must be 1!")

    with OkxRestClient(cfg) as client:
        print("=== STEP 1: Open 2 cross-boundary positions BEFORE T0 ===")
        _place_market(client, "BTC-USDT-SWAP", "buy", "2")
        _place_market(client, "ETH-USDT-SWAP", "buy", "2")
        print("  Opened BTC-USDT-SWAP (+2) and ETH-USDT-SWAP (+2)")

        now_s = time.time()
        next_min_s = (int(now_s) // 60 + 1) * 60
        wait_s = max(5.0, next_min_s - now_s + 1.0)
        print(f"\n=== STEP 2: Waiting {wait_s:.1f}s for T0 boundary ===")
        time.sleep(wait_s)

        t0_ms = int(next_min_s * 1000)
        t0_pos_snap = snapshot_open_positions(client)
        print(f"  Captured lightweight positions(T0) snapshot ({len(t0_pos_snap)} open positions):")
        for k, v in t0_pos_snap.items():
            print(
                f"    {v.inst_id}: realizedPnl@T0=${v.realized_pnl_usd:+.4f}, "
                f"upl@T0=${v.upl_usd:+.4f}, total@T0=${v.total_pnl_at_t0:+.4f}"
            )

        time.sleep(8)

        print("\n=== STEP 3: Post-T0 events (Transfer + Close ETH + Add BTC + Open/Close SOL) ===")
        _post(
            client,
            "/api/v5/asset/transfer",
            {"ccy": "USDT", "amt": "50", "from": "6", "to": "18"},
        )
        print("  Transferred +50 USDT (Funding -> Trading)")
        _place_market(client, "ETH-USDT-SWAP", "sell", "2")
        print("  Closed ETH-USDT-SWAP (-2)")
        _place_market(client, "BTC-USDT-SWAP", "buy", "1")
        print("  Added BTC-USDT-SWAP (+1, total 3 open)")
        _place_market(client, "SOL-USDT-SWAP", "buy", "1")
        time.sleep(2)
        _place_market(client, "SOL-USDT-SWAP", "sell", "1")
        print("  Opened & closed SOL-USDT-SWAP (+1 / -1 intra-month)")

        print("\n  Waiting 6s for OKX to settle...")
        time.sleep(6)

        print("\n=== STEP 4: Compare Bill-Free Modes vs Full Ledger Benchmark ===")
        pos_report = calculate_positions_only_pnl(
            client, start_ts_ms=t0_ms, t0_snapshots=t0_pos_snap
        )
        # Benchmark using full bills+fills calculator (pure derivatives ledger+UPL method)
        full_report = calculate_monthly_pnl(client, start_ts_ms=t0_ms)

        print("=" * 76)
        print("BILL-FREE (positions + positions-history ONLY) LIVE VERIFICATION")
        print("=" * 76)
        print(f"  Closed positions: {pos_report.closed_count} | Open: {pos_report.open_count} | Cross-boundary: {pos_report.cross_boundary_count}")
        print("-" * 76)
        for d in pos_report.details:
            ws_str = f"${d['with_snap_pnl']:+.4f}" if d["with_snap_pnl"] is not None else "N/A"
            print(
                f"  {d['instId']:14s} [{d['status']:23s}] | "
                f"Pure2API=${d['lifetime_pnl']:+.4f} | "
                f"NoSnapSplit=${d['no_snap_split_pnl']:+.4f} | "
                f"WithSnap={ws_str}"
            )
        print("-" * 76)
        print(f"  Benchmark (Full Bills + Fills Ledger+UPL) : ${full_report.pnl_ledger_method_usd:>+10.4f}")
        print(f"  Mode C: Bill-Free + T0 Positions Snapshot : ${pos_report.with_t0_snapshot_pnl_usd:>+10.4f}  (diff: ${pos_report.with_t0_snapshot_pnl_usd - full_report.pnl_ledger_method_usd:+.4f})")
        print(f"  Mode B: Bill-Free + Mark Candle (No Snap) : ${pos_report.no_snapshot_mark_split_pnl_usd:>+10.4f}  (diff: ${pos_report.no_snapshot_mark_split_pnl_usd - full_report.pnl_ledger_method_usd:+.4f})")
        print(f"  Mode A: Pure 2-API (No Snap, No Candles)  : ${pos_report.pure_two_api_pnl_usd:>+10.4f}  (diff: ${pos_report.pure_two_api_pnl_usd - full_report.pnl_ledger_method_usd:+.4f})")
        print("=" * 76)

        print("\n=== STEP 5: Cleanup (closing BTC-USDT-SWAP -3) ===")
        _place_market(client, "BTC-USDT-SWAP", "sell", "3")
        print("  Closed BTC-USDT-SWAP (-3)")


if __name__ == "__main__":
    main()
