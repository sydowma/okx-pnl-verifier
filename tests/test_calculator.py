from __future__ import annotations

from typing import Any

import pytest

from okx_pnl.calculator import calculate_monthly_pnl


class FakeOkxClient:
    """Deterministic in-memory mock of OKX v5 REST responses for edge-case testing."""

    def __init__(
        self,
        *,
        balance: dict[str, Any],
        open_positions: list[dict[str, Any]] | None = None,
        positions_history: list[dict[str, Any]] | None = None,
        bills: list[dict[str, Any]] | None = None,
        fills: list[dict[str, Any]] | None = None,
        instruments: dict[str, dict[str, Any]] | None = None,
        mark_prices: dict[tuple[str, int], float] | None = None,
        usd_prices: dict[tuple[str, int], float] | None = None,
    ) -> None:
        self._balance = balance
        self._open_positions = open_positions or []
        self._positions_history = positions_history or []
        self._bills = bills or []
        self._fills = fills or []
        self._instruments = instruments or {}
        self._mark_prices = mark_prices or {}
        self._usd_prices = usd_prices or {}

    def get_account_balance(self) -> dict[str, Any]:
        return self._balance

    def get_positions(self) -> list[dict[str, Any]]:
        return self._open_positions

    def get_positions_history(self, begin_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self._positions_history

    def get_bills(self, begin_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return self._bills

    def get_fills_history(
        self, begin_ms: int, end_ms: int, inst_type: str = "SWAP"
    ) -> list[dict[str, Any]]:
        return self._fills

    def get_instrument(self, inst_type: str, inst_id: str) -> dict[str, Any]:
        return self._instruments[inst_id]

    def get_mark_price_at(self, inst_id: str, ts_ms: int) -> float:
        return self._mark_prices[(inst_id, ts_ms)]

    def get_usd_price_at(self, ccy: str, ts_ms: int) -> float:
        if ccy.upper() in ("USD", "USDT", "USDC"):
            return 1.0
        return self._usd_prices[(ccy.upper(), ts_ms)]


T0 = 1_761_926_400_000  # 2026-11-01 00:00:00 UTC+8
T_OCT25 = T0 - 7 * 86_400_000
T_NOV05 = T0 + 4 * 86_400_000
T_NOV10 = T0 + 9 * 86_400_000
T_NOW = T0 + 23 * 86_400_000


def test_cross_month_closed_position_and_midmonth_transfer() -> None:
    """
    Scenario:
    - Oct 25: Account starts with 10,000 USDT. User opens LONG 1 BTC (100 contracts, ctVal=0.01)
      of BTC-USDT-SWAP at avgPx = 60,000.
    - Nov 1 00:00 (T0, NO snapshot saved!):
      Mark price = 65,000 -> UPL@T0 = +5,000 USDT.
      True totalEq@T0 = 10,000 (cash) + 5,000 (UPL) = 15,000 USDT.
    - Nov 5: User transfers +2,000 USDT from Funding -> Trading.
    - Nov 10: User closes entire LONG at 68,000 -> lifetime realizedPnl = +8,000 (minus 20 fee = +7,980).
      Cash now = 10,000 + 2,000 + 7,980 = 19,980 USDT.
    - True Nov 1 -> Now PnL = 19,980 - 2,000 - 15,000 = +2,980 USDT!
      (Notice naive positions-history would falsely report +7,980!)
    """
    client = FakeOkxClient(
        balance={
            "totalEq": "19980",
            "details": [{"ccy": "USDT", "cashBal": "19980", "upl": "0"}],
        },
        open_positions=[],
        positions_history=[
            {
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "posSide": "long",
                "ccy": "USDT",
                "openAvgPx": "60000",
                "realizedPnl": "7980",
                "cTime": str(T_OCT25),
                "uTime": str(T_NOV10),
            }
        ],
        bills=[
            {"billId": "1", "ts": str(T_NOV05), "ccy": "USDT", "type": "1", "balChg": "2000"},
            {"billId": "2", "ts": str(T_NOV10), "ccy": "USDT", "type": "2", "balChg": "7980"},
        ],
        fills=[
            {
                "billId": "2",
                "ts": str(T_NOV10),
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "sell",
                "fillSz": "100",
                "fillPx": "68000",
            }
        ],
        instruments={
            "BTC-USDT-SWAP": {"ctVal": "0.01", "ctType": "linear", "settleCcy": "USDT"}
        },
        mark_prices={("BTC-USDT-SWAP", T0): 65000.0},
    )

    report = calculate_monthly_pnl(client, start_ts_ms=T0, end_ts_ms=T_NOW)

    assert report.reconstructed_cash_usd_at_t0 == pytest.approx(10000.0)
    assert report.reconstructed_upl_usd_at_t0 == pytest.approx(5000.0)
    assert report.reconstructed_total_eq_at_t0 == pytest.approx(15000.0)
    assert report.net_transfers_usd == pytest.approx(2000.0)
    assert report.pnl_equity_method_usd == pytest.approx(2980.0)
    assert report.pnl_ledger_method_usd == pytest.approx(2980.0)
    assert report.method_diff_usd == pytest.approx(0.0)
    # Naive method overstates PnL by the entire Oct 25 -> Nov 1 UPL (+5,000)!
    assert report.naive_positions_pnl_usd == pytest.approx(7980.0)
    assert report.cross_boundary_positions[0].exact is True


def test_cross_month_still_open_position_with_midmonth_add() -> None:
    """
    Scenario:
    - Oct 25: Account has 10,000 USDT. Opens LONG 100 contracts (1 BTC) at 60,000.
    - Nov 1 (T0): Mark = 62,000 -> UPL@T0 = +2,000 -> true totalEq@T0 = 12,000.
    - Nov 5: Adds LONG 100 contracts (1 BTC) at 64,000 (fee -10 USDT).
      Now holds 200 contracts at avgPx = 62,000. Cash = 9,990.
    - Now: Mark = 65,000 -> current UPL = 2 * (65,000 - 62,000) = +6,000.
      current totalEq = 9,990 + 6,000 = 15,990.
    - True Nov 1 -> Now PnL = 15,990 - 12,000 = +3,990
      (1 BTC went 62k->65k = +3,000; added 1 BTC went 64k->65k = +1,000; fee = -10).
    """
    client = FakeOkxClient(
        balance={
            "totalEq": "15990",
            "details": [{"ccy": "USDT", "cashBal": "9990", "upl": "6000"}],
        },
        open_positions=[
            {
                "instId": "BTC-USDT-SWAP",
                "instType": "SWAP",
                "posSide": "long",
                "ccy": "USDT",
                "pos": "200",
                "avgPx": "62000",
                "upl": "6000",
                "cTime": str(T_OCT25),
            }
        ],
        bills=[
            {"billId": "10", "ts": str(T_NOV05), "ccy": "USDT", "type": "2", "balChg": "-10"},
        ],
        fills=[
            {
                "billId": "10",
                "ts": str(T_NOV05),
                "instId": "BTC-USDT-SWAP",
                "posSide": "long",
                "side": "buy",
                "fillSz": "100",
                "fillPx": "64000",
            }
        ],
        instruments={
            "BTC-USDT-SWAP": {"ctVal": "0.01", "ctType": "linear", "settleCcy": "USDT"}
        },
        mark_prices={("BTC-USDT-SWAP", T0): 62000.0},
    )

    report = calculate_monthly_pnl(client, start_ts_ms=T0, end_ts_ms=T_NOW)

    assert report.reconstructed_total_eq_at_t0 == pytest.approx(12000.0)
    assert report.pnl_equity_method_usd == pytest.approx(3990.0)
    assert report.pnl_ledger_method_usd == pytest.approx(3990.0)
    assert report.cross_boundary_positions[0].signed_pos_at_t0 == pytest.approx(100.0)
    assert report.cross_boundary_positions[0].avg_px_at_t0 == pytest.approx(60000.0)
    assert report.cross_boundary_positions[0].exact is True


def test_non_stablecoin_spot_holding_flags_spot_drift() -> None:
    """
    Scenario:
    - User holds 1 BTC in Trading Account across Nov 1 -> Now, no trades.
    - BTC price at T0 = 60,000; BTC price now = 70,000.
    - Equity Method reports +10,000 USD (spot appreciation).
    - Ledger Method reports 0 USD (no bills, no derivatives UPL).
    - Report flags the non-stablecoin holding warning so business logic can decide
      whether spot price appreciation should count in the leaderboard!
    """
    client = FakeOkxClient(
        balance={
            "totalEq": "70000",
            "details": [{"ccy": "BTC", "cashBal": "1", "upl": "0"}],
        },
        usd_prices={("BTC", T0): 60000.0, ("BTC", T_NOW): 70000.0},
    )

    report = calculate_monthly_pnl(client, start_ts_ms=T0, end_ts_ms=T_NOW)

    assert report.reconstructed_total_eq_at_t0 == pytest.approx(60000.0)
    assert report.pnl_equity_method_usd == pytest.approx(10000.0)
    assert report.pnl_ledger_method_usd == pytest.approx(0.0)
    assert report.method_diff_usd == pytest.approx(10000.0)
    assert any("Non-stablecoin balances" in w for w in report.warnings)
