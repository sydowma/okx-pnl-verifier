from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class MarketDataProvider(Protocol):
    def get_account_balance(self) -> dict[str, Any]: ...
    def get_positions(self) -> list[dict[str, Any]]: ...
    def get_positions_history(self, begin_ms: int, end_ms: int) -> list[dict[str, Any]]: ...
    def get_bills(self, begin_ms: int, end_ms: int) -> list[dict[str, Any]]: ...
    def get_fills_history(
        self, begin_ms: int, end_ms: int, inst_type: str = "SWAP"
    ) -> list[dict[str, Any]]: ...
    def get_instrument(self, inst_type: str, inst_id: str) -> dict[str, Any]: ...
    def get_mark_price_at(self, inst_id: str, ts_ms: int) -> float: ...
    def get_usd_price_at(self, ccy: str, ts_ms: int) -> float: ...


@dataclass
class CrossBoundaryPosition:
    inst_id: str
    inst_type: str
    pos_side: str
    source: str  # "open" or "closed_history"
    c_time_ms: int
    signed_pos_at_t0: float
    avg_px_at_t0: float
    mark_px_at_t0: float
    upl_usd_at_t0: float
    exact: bool
    note: str = ""


@dataclass
class PnlReport:
    start_ts_ms: int
    end_ts_ms: int

    # Core equity formula components
    current_total_eq_usd: float
    net_transfers_usd: float
    transfer_in_usd: float
    transfer_out_usd: float
    reconstructed_cash_usd_at_t0: float
    reconstructed_upl_usd_at_t0: float
    reconstructed_total_eq_at_t0: float
    pnl_equity_method_usd: float

    # Cross-check: Ledger + UPL delta
    realized_bills_usd: float
    current_upl_usd: float
    pnl_ledger_method_usd: float
    method_diff_usd: float

    # Naive comparison (positions-history realizedPnl + open upl)
    naive_positions_pnl_usd: float

    # Diagnostics
    cash_balances_at_t0: dict[str, float] = field(default_factory=dict)
    cross_boundary_positions: list[CrossBoundaryPosition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bill_count: int = 0
    transfer_count: int = 0


def _to_float(val: Any, default: float = 0.0) -> float:
    if val is None or val == "":
        return default
    return float(val)


def _signed_position_size(pos: float, pos_side: str) -> float:
    """
    OKX position sizing:
    - net mode (posSide == 'net'): pos is already signed (+long, -short)
    - long/short hedge mode: pos is always >= 0, direction comes from posSide
    """
    side = (pos_side or "net").lower()
    if side == "short":
        return -abs(pos)
    if side == "long":
        return abs(pos)
    return pos


def _compute_contract_upl_usd(
    signed_pos: float,
    avg_px: float,
    mark_px: float,
    ct_val: float,
    ct_type: str,
    settle_ccy_usd_px: float,
) -> float:
    if signed_pos == 0.0 or avg_px <= 0.0 or mark_px <= 0.0:
        return 0.0
    if ct_type == "inverse":
        upl_coin = signed_pos * ct_val * (1.0 / avg_px - 1.0 / mark_px)
        return upl_coin * settle_ccy_usd_px
    # linear (USDT/USDC margined)
    upl_settle = signed_pos * ct_val * (mark_px - avg_px)
    return upl_settle * settle_ccy_usd_px


def _reconstruct_pos_at_t0(
    inst_id: str,
    pos_side: str,
    signed_pos_now: float,
    avg_px_now: float,
    fills_after_t0: list[dict[str, Any]],
) -> tuple[float, float, bool, str]:
    """
    Walk backwards from current (or closed=0) state using fills in (t0, now].
    Returns (signed_pos_at_t0, avg_px_at_t0, is_exact, note).
    """
    matching_fills = [
        f
        for f in fills_after_t0
        if f.get("instId") == inst_id
        and (f.get("posSide") or "net").lower() == (pos_side or "net").lower()
    ]

    if not matching_fills:
        return signed_pos_now, avg_px_now, True, "No fills after T0; exact."

    # Compute exact signed position size at T0
    net_delta_after_t0 = 0.0
    opening_fills: list[tuple[float, float]] = []
    closing_fills_count = 0

    for f in matching_fills:
        sz = _to_float(f.get("fillSz"))
        px = _to_float(f.get("fillPx"))
        side = (f.get("side") or "").lower()
        signed_delta = sz if side == "buy" else -sz
        net_delta_after_t0 += signed_delta

        # Heuristic to detect whether fill increased position vs reduced position
        if pos_side.lower() == "long":
            is_open = side == "buy"
        elif pos_side.lower() == "short":
            is_open = side == "sell"
        else:
            # net mode: same sign as current position (or opposite of closing)
            is_open = (signed_pos_now != 0 and (signed_delta * signed_pos_now > 0))
        if is_open:
            opening_fills.append((sz, px))
        else:
            closing_fills_count += 1

    signed_pos_at_t0 = signed_pos_now - net_delta_after_t0

    if not opening_fills:
        # Only closing/reducing fills happened after T0 -> avgPx never changed!
        return (
            signed_pos_at_t0,
            avg_px_now,
            True,
            f"Only {closing_fills_count} reducing fill(s) after T0; avgPx unchanged (exact).",
        )

    if closing_fills_count == 0 and abs(signed_pos_at_t0) > 1e-12:
        # Only adding fills after T0 -> invert weighted average exactly
        total_now = abs(signed_pos_now)
        added_notional = sum(sz * px for sz, px in opening_fills)
        avg_px_at_t0 = (total_now * avg_px_now - added_notional) / abs(signed_pos_at_t0)
        if avg_px_at_t0 > 0:
            return (
                signed_pos_at_t0,
                avg_px_at_t0,
                True,
                f"Inverted {len(opening_fills)} adding fill(s) after T0 (exact).",
            )

    # Interleaved adds & closes after T0: avg_px_now is an approximation unless we replay from cTime
    return (
        signed_pos_at_t0,
        avg_px_now,
        False,
        (
            f"Interleaved {len(opening_fills)} add(s) and {closing_fills_count} close(s) "
            "after T0; position size is exact, avgPx approximated using latest avgPx."
        ),
    )


def calculate_monthly_pnl(
    client: MarketDataProvider,
    start_ts_ms: int,
    end_ts_ms: int | None = None,
) -> PnlReport:
    """
    Calculate PnL over [start_ts_ms, end_ts_ms] WITHOUT requiring a pre-existing
    snapshot at start_ts_ms, and cross-verify Equity Method vs Ledger Method.
    """
    if end_ts_ms is None:
        end_ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    warnings: list[str] = []

    # Check OKX 3-month retention window (~90 days)
    age_days = (end_ts_ms - start_ts_ms) / (1000 * 86400)
    if age_days > 88:
        warnings.append(
            f"Requested window is {age_days:.1f} days; OKX bills-archive and "
            "positions-history only retain the last 3 months (~90 days)."
        )

    # 1. Fetch current trading account balance & open positions
    balance = client.get_account_balance()
    current_total_eq_usd = _to_float(balance.get("totalEq"))
    details = balance.get("details", [])

    cash_bal_now: dict[str, float] = {}
    current_upl_usd = 0.0
    for d in details:
        ccy = d["ccy"]
        cash_bal_now[ccy] = _to_float(d.get("cashBal"))
        upl_ccy = _to_float(d.get("upl"))
        if upl_ccy != 0.0:
            current_upl_usd += upl_ccy * client.get_usd_price_at(ccy, end_ts_ms)

    open_positions = client.get_positions()
    pos_history = client.get_positions_history(start_ts_ms, end_ts_ms)
    bills = client.get_bills(start_ts_ms, end_ts_ms)

    # 2. Walk bills in (start_ts_ms, end_ts_ms]
    #    - Reconstruct per-ccy cashBal at T0
    #    - Separate transfers (type == "1") from trading/fee/funding bills
    bal_chg_by_ccy: dict[str, float] = {}
    transfer_in_usd = 0.0
    transfer_out_usd = 0.0
    transfer_count = 0
    realized_bills_usd = 0.0

    for b in bills:
        ccy = b["ccy"]
        chg = _to_float(b.get("balChg"))
        ts = int(b["ts"])
        bal_chg_by_ccy[ccy] = bal_chg_by_ccy.get(ccy, 0.0) + chg

        usd_px = client.get_usd_price_at(ccy, ts)
        chg_usd = chg * usd_px

        if str(b.get("type")) == "1":  # Trading account transfer
            transfer_count += 1
            if chg_usd >= 0:
                transfer_in_usd += chg_usd
            else:
                transfer_out_usd += -chg_usd
        else:
            realized_bills_usd += chg_usd

    net_transfers_usd = transfer_in_usd - transfer_out_usd

    # Reconstruct CashBal at T0 and value in USD at T0
    all_ccys = set(cash_bal_now.keys()) | set(bal_chg_by_ccy.keys())
    cash_balances_at_t0: dict[str, float] = {}
    reconstructed_cash_usd_at_t0 = 0.0
    non_stable_ccys: list[str] = []

    for ccy in sorted(all_ccys):
        bal_t0 = cash_bal_now.get(ccy, 0.0) - bal_chg_by_ccy.get(ccy, 0.0)
        if abs(bal_t0) < 1e-12:
            bal_t0 = 0.0
        cash_balances_at_t0[ccy] = bal_t0
        if bal_t0 != 0.0:
            px_t0 = client.get_usd_price_at(ccy, start_ts_ms)
            reconstructed_cash_usd_at_t0 += bal_t0 * px_t0
            if ccy.upper() not in ("USD", "USDT", "USDC", "DAI", "FDUSD"):
                non_stable_ccys.append(ccy)

    if non_stable_ccys:
        warnings.append(
            f"Non-stablecoin balances held at T0 ({', '.join(non_stable_ccys)}): "
            "Equity Method includes spot price appreciation/depreciation of these holdings, "
            "whereas pure Ledger+UPL Method excludes unrealized spot price drift."
        )

    # 3. Identify cross-boundary derivatives positions (opened before T0, still open at T0)
    cross_candidates: list[tuple[str, dict[str, Any]]] = []
    inst_types_needed: set[str] = set()

    for p in open_positions:
        pos_sz = _to_float(p.get("pos"))
        c_time = int(p.get("cTime") or 0)
        if pos_sz != 0.0 and c_time < start_ts_ms:
            cross_candidates.append(("open", p))
            inst_types_needed.add(p.get("instType", "SWAP"))

    for p in pos_history:
        c_time = int(p.get("cTime") or 0)
        u_time = int(p.get("uTime") or 0)
        if c_time < start_ts_ms < u_time:
            cross_candidates.append(("closed_history", p))
            inst_types_needed.add(p.get("instType", "SWAP"))

    fills_by_type: dict[str, list[dict[str, Any]]] = {}
    for itype in inst_types_needed:
        fills_by_type[itype] = client.get_fills_history(
            start_ts_ms, end_ts_ms, inst_type=itype
        )

    cross_boundary_positions: list[CrossBoundaryPosition] = []
    reconstructed_upl_usd_at_t0 = 0.0

    for source, p in cross_candidates:
        inst_id = p["instId"]
        inst_type = p.get("instType", "SWAP")
        pos_side = p.get("posSide") or "net"
        c_time = int(p.get("cTime") or 0)

        if source == "open":
            signed_now = _signed_position_size(_to_float(p.get("pos")), pos_side)
            avg_px_now = _to_float(p.get("avgPx"))
        else:
            signed_now = 0.0
            avg_px_now = _to_float(p.get("openAvgPx"))

        signed_t0, avg_px_t0, is_exact, note = _reconstruct_pos_at_t0(
            inst_id=inst_id,
            pos_side=pos_side,
            signed_pos_now=signed_now,
            avg_px_now=avg_px_now,
            fills_after_t0=fills_by_type.get(inst_type, []),
        )

        inst = client.get_instrument(inst_type, inst_id)
        ct_val = _to_float(inst.get("ctVal"), default=1.0)
        ct_type = inst.get("ctType", "linear")
        settle_ccy = inst.get("settleCcy") or p.get("ccy") or "USDT"

        mark_px_t0 = client.get_mark_price_at(inst_id, start_ts_ms)
        settle_usd_t0 = client.get_usd_price_at(settle_ccy, start_ts_ms)

        upl_t0 = _compute_contract_upl_usd(
            signed_pos=signed_t0,
            avg_px=avg_px_t0,
            mark_px=mark_px_t0,
            ct_val=ct_val,
            ct_type=ct_type,
            settle_ccy_usd_px=settle_usd_t0,
        )
        reconstructed_upl_usd_at_t0 += upl_t0

        if not is_exact:
            warnings.append(f"Cross-boundary position {inst_id} ({pos_side}): {note}")

        cross_boundary_positions.append(
            CrossBoundaryPosition(
                inst_id=inst_id,
                inst_type=inst_type,
                pos_side=pos_side,
                source=source,
                c_time_ms=c_time,
                signed_pos_at_t0=signed_t0,
                avg_px_at_t0=avg_px_t0,
                mark_px_at_t0=mark_px_t0,
                upl_usd_at_t0=upl_t0,
                exact=is_exact,
                note=note,
            )
        )

    # 4. Compute PnL under all 3 formulas
    reconstructed_total_eq_at_t0 = (
        reconstructed_cash_usd_at_t0 + reconstructed_upl_usd_at_t0
    )
    pnl_equity_method_usd = (
        current_total_eq_usd - net_transfers_usd - reconstructed_total_eq_at_t0
    )

    pnl_ledger_method_usd = (
        realized_bills_usd + current_upl_usd - reconstructed_upl_usd_at_t0
    )
    method_diff_usd = pnl_equity_method_usd - pnl_ledger_method_usd

    # Naive method: sum(positions-history.realizedPnl) + sum(open_positions.upl)
    # Intentionally ignores T0 split to demonstrate cross-month bias
    naive_realized = sum(
        _to_float(p.get("realizedPnl"))
        * client.get_usd_price_at(p.get("ccy") or "USDT", int(p.get("uTime") or end_ts_ms))
        for p in pos_history
    )
    naive_open_upl = sum(
        _to_float(p.get("upl"))
        * client.get_usd_price_at(p.get("ccy") or "USDT", end_ts_ms)
        for p in open_positions
    )
    naive_positions_pnl_usd = naive_realized + naive_open_upl

    return PnlReport(
        start_ts_ms=start_ts_ms,
        end_ts_ms=end_ts_ms,
        current_total_eq_usd=current_total_eq_usd,
        net_transfers_usd=net_transfers_usd,
        transfer_in_usd=transfer_in_usd,
        transfer_out_usd=transfer_out_usd,
        reconstructed_cash_usd_at_t0=reconstructed_cash_usd_at_t0,
        reconstructed_upl_usd_at_t0=reconstructed_upl_usd_at_t0,
        reconstructed_total_eq_at_t0=reconstructed_total_eq_at_t0,
        pnl_equity_method_usd=pnl_equity_method_usd,
        realized_bills_usd=realized_bills_usd,
        current_upl_usd=current_upl_usd,
        pnl_ledger_method_usd=pnl_ledger_method_usd,
        method_diff_usd=method_diff_usd,
        naive_positions_pnl_usd=naive_positions_pnl_usd,
        cash_balances_at_t0=cash_balances_at_t0,
        cross_boundary_positions=cross_boundary_positions,
        warnings=warnings,
        bill_count=len(bills),
        transfer_count=transfer_count,
    )
