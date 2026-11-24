"""
Lightweight bill-free & fill-free Monthly Position PnL Calculator.
Uses ONLY:
  - GET /api/v5/account/positions
  - GET /api/v5/account/positions-history
  - (Optional, only when T0 snapshot is missing) GET /api/v5/market/history-mark-price-candles
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .calculator import _compute_contract_upl_usd, _signed_position_size, _to_float
from .client import OkxRestClient


@dataclass
class PositionSnapshotAtT0:
    """Lightweight T0 snapshot from a single GET /api/v5/account/positions call."""

    inst_id: str
    pos_side: str
    c_time_ms: int
    realized_pnl_usd: float
    upl_usd: float

    @property
    def total_pnl_at_t0(self) -> float:
        return self.realized_pnl_usd + self.upl_usd


@dataclass
class PositionsOnlyReport:
    start_ts_ms: int
    end_ts_ms: int
    # Mode 1: Pure 2-API (positions + positions-history), no candles, no snapshot
    pure_two_api_pnl_usd: float
    # Mode 2: Bill-free & fill-free, NO snapshot, subtract estimated UPL(T0) via mark candle
    no_snapshot_mark_split_pnl_usd: float
    # Mode 3: Bill-free & fill-free, WITH lightweight T0 positions snapshot (exact!)
    with_t0_snapshot_pnl_usd: float | None
    # Breakdown
    closed_count: int
    open_count: int
    cross_boundary_count: int
    details: list[dict[str, Any]] = field(default_factory=list)


def snapshot_open_positions(client: OkxRestClient) -> dict[tuple[str, str, int], PositionSnapshotAtT0]:
    """Capture lightweight T0 snapshot using ONLY GET /api/v5/account/positions."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    snaps: dict[tuple[str, str, int], PositionSnapshotAtT0] = {}
    for p in client.get_positions():
        if _to_float(p.get("pos")) == 0.0:
            continue
        ccy = p.get("ccy") or "USDT"
        px = client.get_usd_price_at(ccy, now_ms)
        key = (p["instId"], (p.get("posSide") or "net").lower(), int(p.get("cTime") or 0))
        snaps[key] = PositionSnapshotAtT0(
            inst_id=p["instId"],
            pos_side=(p.get("posSide") or "net").lower(),
            c_time_ms=int(p.get("cTime") or 0),
            realized_pnl_usd=_to_float(p.get("realizedPnl")) * px,
            upl_usd=_to_float(p.get("upl")) * px,
        )
    return snaps


def calculate_positions_only_pnl(
    client: OkxRestClient,
    start_ts_ms: int,
    end_ts_ms: int | None = None,
    t0_snapshots: dict[tuple[str, str, int], PositionSnapshotAtT0] | None = None,
) -> PositionsOnlyReport:
    """
    Compute monthly derivatives PnL WITHOUT calling bills, bills-archive, or fills-history.
    """
    if end_ts_ms is None:
        end_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    open_positions = [p for p in client.get_positions() if _to_float(p.get("pos")) != 0.0]
    closed_positions = client.get_positions_history(start_ts_ms, end_ts_ms)

    pure_two_api = 0.0
    no_snap_mark_split = 0.0
    with_snap = 0.0 if t0_snapshots is not None else None
    cross_count = 0
    details: list[dict[str, Any]] = []

    def _estimate_t0_upl_without_fills(p: dict[str, Any], is_open: bool) -> float:
        inst_id = p["instId"]
        inst_type = p.get("instType", "SWAP")
        pos_side = p.get("posSide") or "net"
        inst = client.get_instrument(inst_type, inst_id)
        ct_val = _to_float(inst.get("ctVal"), 1.0)
        ct_type = inst.get("ctType", "linear")
        settle_ccy = inst.get("settleCcy") or p.get("ccy") or "USDT"
        mark_t0 = client.get_mark_price_at(inst_id, start_ts_ms)
        settle_px = client.get_usd_price_at(settle_ccy, start_ts_ms)

        if is_open:
            signed_pos = _signed_position_size(_to_float(p.get("pos")), pos_side)
            avg_px = _to_float(p.get("avgPx"))
        else:
            # positions-history has closeTotalPos (always positive) & direction ('long'/'short')
            direction = (p.get("direction") or pos_side or "long").lower()
            raw_sz = _to_float(p.get("closeTotalPos") or p.get("openMaxPos"))
            signed_pos = -abs(raw_sz) if direction == "short" else abs(raw_sz)
            avg_px = _to_float(p.get("openAvgPx"))

        return _compute_contract_upl_usd(
            signed_pos=signed_pos,
            avg_px=avg_px,
            mark_px=mark_t0,
            ct_val=ct_val,
            ct_type=ct_type,
            settle_ccy_usd_px=settle_px,
        )

    # 1. Closed positions in (T0, Now]
    for p in closed_positions:
        c_time = int(p.get("cTime") or 0)
        u_time = int(p.get("uTime") or end_ts_ms)
        ccy = p.get("ccy") or "USDT"
        px = client.get_usd_price_at(ccy, u_time)
        lifetime_pnl = _to_float(p.get("realizedPnl")) * px

        pure_two_api += lifetime_pnl

        if c_time < start_ts_ms:
            cross_count += 1
            est_upl_t0 = _estimate_t0_upl_without_fills(p, is_open=False)
            adj_no_snap = lifetime_pnl - est_upl_t0
            no_snap_mark_split += adj_no_snap

            snap_pnl = None
            if with_snap is not None and t0_snapshots is not None:
                key = (p["instId"], (p.get("posSide") or "net").lower(), c_time)
                snap = t0_snapshots.get(key)
                snap_val = snap.total_pnl_at_t0 if snap else 0.0
                snap_pnl = lifetime_pnl - snap_val
                with_snap += snap_pnl

            details.append(
                {
                    "instId": p["instId"],
                    "status": "closed (cross-boundary)",
                    "lifetime_pnl": lifetime_pnl,
                    "no_snap_split_pnl": adj_no_snap,
                    "with_snap_pnl": snap_pnl,
                }
            )
        else:
            no_snap_mark_split += lifetime_pnl
            if with_snap is not None:
                with_snap += lifetime_pnl
            details.append(
                {
                    "instId": p["instId"],
                    "status": "closed (intra-month)",
                    "lifetime_pnl": lifetime_pnl,
                    "no_snap_split_pnl": lifetime_pnl,
                    "with_snap_pnl": lifetime_pnl if with_snap is not None else None,
                }
            )

    # 2. Currently open positions
    for p in open_positions:
        c_time = int(p.get("cTime") or 0)
        ccy = p.get("ccy") or "USDT"
        px = client.get_usd_price_at(ccy, end_ts_ms)
        lifetime_pnl = (_to_float(p.get("realizedPnl")) + _to_float(p.get("upl"))) * px

        pure_two_api += lifetime_pnl

        if c_time < start_ts_ms:
            cross_count += 1
            est_upl_t0 = _estimate_t0_upl_without_fills(p, is_open=True)
            adj_no_snap = lifetime_pnl - est_upl_t0
            no_snap_mark_split += adj_no_snap

            snap_pnl = None
            if with_snap is not None and t0_snapshots is not None:
                key = (p["instId"], (p.get("posSide") or "net").lower(), c_time)
                snap = t0_snapshots.get(key)
                snap_val = snap.total_pnl_at_t0 if snap else 0.0
                snap_pnl = lifetime_pnl - snap_val
                with_snap += snap_pnl

            details.append(
                {
                    "instId": p["instId"],
                    "status": "open (cross-boundary)",
                    "lifetime_pnl": lifetime_pnl,
                    "no_snap_split_pnl": adj_no_snap,
                    "with_snap_pnl": snap_pnl,
                }
            )
        else:
            no_snap_mark_split += lifetime_pnl
            if with_snap is not None:
                with_snap += lifetime_pnl
            details.append(
                {
                    "instId": p["instId"],
                    "status": "open (intra-month)",
                    "lifetime_pnl": lifetime_pnl,
                    "no_snap_split_pnl": lifetime_pnl,
                    "with_snap_pnl": lifetime_pnl if with_snap is not None else None,
                }
            )

    return PositionsOnlyReport(
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        pure_two_api_pnl_usd=pure_two_api,
        no_snapshot_mark_split_pnl_usd=no_snap_mark_split,
        with_t0_snapshot_pnl_usd=with_snap,
        closed_count=len(closed_positions),
        open_count=len(open_positions),
        cross_boundary_count=cross_count,
        details=details,
    )
