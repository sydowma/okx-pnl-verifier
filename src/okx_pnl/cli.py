from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone

from dotenv import load_dotenv

from .calculator import PnlReport, calculate_monthly_pnl
from .client import OkxConfig, OkxRestClient
from .ws_listener import stream_realtime_pnl


def _parse_iso_to_ms(dt_str: str) -> int:
    dt = datetime.fromisoformat(dt_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _fmt_ts(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )


def print_report(report: PnlReport) -> None:
    print("=" * 72)
    print("OKX Monthly PnL & Missing-Snapshot Reconstruction Report")
    print("=" * 72)
    print(f"Window Start (T0) : {_fmt_ts(report.start_ts_ms)} ({report.start_ts_ms})")
    print(f"Window End (Now)  : {_fmt_ts(report.end_ts_ms)} ({report.end_ts_ms})")
    print(f"Bills Processed   : {report.bill_count} (transfers: {report.transfer_count})")
    print("-" * 72)
    print("1) Equity Formula (Reconstructed T0 Baseline)")
    print(f"   Current totalEq (Now)      : ${report.current_total_eq_usd:>14,.2f}")
    print(f"   Transfers In  (T0 -> Now)  : ${report.transfer_in_usd:>14,.2f}")
    print(f"   Transfers Out (T0 -> Now)  : ${report.transfer_out_usd:>14,.2f}")
    print(f"   Net Transfers (In - Out)   : ${report.net_transfers_usd:>14,.2f}")
    print(f"   Reconstructed Cash @ T0    : ${report.reconstructed_cash_usd_at_t0:>14,.2f}")
    print(f"   Reconstructed UPL  @ T0    : ${report.reconstructed_upl_usd_at_t0:>14,.2f}")
    print(f"   Reconstructed totalEq @ T0 : ${report.reconstructed_total_eq_at_t0:>14,.2f}")
    print(f"   ==> PnL (Equity Method)    : ${report.pnl_equity_method_usd:>+14,.2f}")
    print("-" * 72)
    print("2) Cross-Check: Ledger Bills + UPL Delta")
    print(f"   Realized Bills (T0 -> Now) : ${report.realized_bills_usd:>+14,.2f}")
    print(f"   Current UPL (Now)          : ${report.current_upl_usd:>+14,.2f}")
    print(f"   Reconstructed UPL @ T0     : ${report.reconstructed_upl_usd_at_t0:>+14,.2f}")
    print(f"   ==> PnL (Ledger Method)    : ${report.pnl_ledger_method_usd:>+14,.2f}")
    print(f"   Diff (Equity - Ledger)     : ${report.method_diff_usd:>+14,.2f}")
    print("-" * 72)
    print("3) Naive Comparison (positions-history realizedPnl + open upl)")
    print(f"   ==> Naive Positions PnL    : ${report.naive_positions_pnl_usd:>+14,.2f}")
    print(
        f"   Bias vs Equity Method      : "
        f"${report.naive_positions_pnl_usd - report.pnl_equity_method_usd:>+14,.2f}"
    )

    if report.cross_boundary_positions:
        print("-" * 72)
        print(f"Cross-Boundary Positions Open at T0 ({len(report.cross_boundary_positions)}):")
        for pos in report.cross_boundary_positions:
            tag = "EXACT" if pos.exact else "APPROX"
            print(
                f"  - [{tag}] {pos.inst_id} ({pos.pos_side}, {pos.source}): "
                f"pos@T0={pos.signed_pos_at_t0:g}, avgPx@T0={pos.avg_px_at_t0:,.4f}, "
                f"mark@T0={pos.mark_px_at_t0:,.4f}, UPL@T0=${pos.upl_usd_at_t0:+,.2f}"
            )
            if pos.note:
                print(f"      Note: {pos.note}")

    if report.warnings:
        print("-" * 72)
        print("Edge-Case Diagnostics / Warnings:")
        for w in report.warnings:
            print(f"  ! {w}")
    print("=" * 72)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        prog="okx-pnl",
        description="Verify OKX monthly PnL reconstruction and real-time WS tracking.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_verify = sub.add_parser("verify", help="Run REST reconstruction and verification")
    p_verify.add_argument(
        "--start",
        default="2026-11-01T00:00:00+08:00",
        help="ISO-8601 start timestamp (default: 2026-11-01T00:00:00+08:00)",
    )

    p_stream = sub.add_parser(
        "stream", help="Reconstruct T0 baseline via REST, then stream live PnL via WS"
    )
    p_stream.add_argument(
        "--start",
        default="2026-11-01T00:00:00+08:00",
        help="ISO-8601 start timestamp (default: 2026-11-01T00:00:00+08:00)",
    )

    args = parser.parse_args()
    start_ts_ms = _parse_iso_to_ms(args.start)
    config = OkxConfig.from_env()

    with OkxRestClient(config) as client:
        report = calculate_monthly_pnl(client, start_ts_ms=start_ts_ms)
    print_report(report)

    if args.cmd == "stream":
        asyncio.run(
            stream_realtime_pnl(
                config=config,
                baseline_eq_at_t0=report.reconstructed_total_eq_at_t0,
                net_transfers_usd=report.net_transfers_usd,
            )
        )


if __name__ == "__main__":
    main()
