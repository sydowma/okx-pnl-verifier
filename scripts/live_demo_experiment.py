"""
Live End-to-End Ground-Truth Experiment on OKX Demo Trading (OKX_SIMULATED=1).

Tests ALL critical edge cases simultaneously:
  1. Cross-boundary CLOSED position (ETH-USDT-SWAP: opened before T0, closed after T0)
  2. Cross-boundary STILL-OPEN position with mid-month ADD (BTC-USDT-SWAP: opened before T0, added after T0)
  3. Mid-month Funding -> Trading transfer (+100 USDT after T0)
  4. Non-stablecoin spot balances held across T0 (BTC/ETH/OKB in demo trading account)

Compares ACTUAL ground-truth snapshot at T0 against our snapshot-free reconstruction!
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from okx_pnl.calculator import calculate_monthly_pnl
from okx_pnl.cli import print_report
from okx_pnl.client import OkxApiError, OkxConfig, OkxRestClient, sign_request


def _post(client: OkxRestClient, path: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    body_str = json.dumps(body, separators=(",", ":"))
    ts = (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
    headers = {
        "Content-Type": "application/json",
        "x-simulated-trading": "1",
        "OK-ACCESS-KEY": client.config.api_key,
        "OK-ACCESS-SIGN": sign_request(
            client.config.api_secret, ts, "POST", path, body_str
        ),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": client.config.passphrase,
    }
    resp = client._client.post(path, content=body_str, headers=headers)
    payload = resp.json()
    if resp.status_code >= 400 or payload.get("code") != "0":
        raise OkxApiError(
            str(payload.get("code", resp.status_code)),
            str(payload.get("msg", resp.text)),
            path,
        )
    return payload.get("data", [])


def _place_market(client: OkxRestClient, inst_id: str, side: str, sz: str) -> dict[str, Any]:
    res = _post(
        client,
        "/api/v5/trade/order",
        {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": "market",
            "sz": sz,
        },
    )
    return res[0]


def main() -> None:
    load_dotenv()
    cfg = OkxConfig.from_env()
    if not cfg.simulated:
        raise RuntimeError("SAFETY ABORT: This experiment must ONLY run with OKX_SIMULATED=1!")

    with OkxRestClient(cfg) as client:
        print("=== STEP 1: Open 2 cross-boundary positions BEFORE T0 ===")
        r_btc = _place_market(client, "BTC-USDT-SWAP", "buy", "2")
        r_eth = _place_market(client, "ETH-USDT-SWAP", "buy", "2")
        print(f"  Opened BTC-USDT-SWAP (+2): ordId={r_btc['ordId']}")
        print(f"  Opened ETH-USDT-SWAP (+2): ordId={r_eth['ordId']}")

        # Wait until the next exact minute boundary (:00) so T0 aligns cleanly with a 1m candle
        now_s = time.time()
        next_min_s = (int(now_s) // 60 + 1) * 60
        wait_s = max(5.0, next_min_s - now_s + 1.0)
        print(f"\n=== STEP 2: Waiting {wait_s:.1f}s for clean 1m boundary (T0) ===")
        time.sleep(wait_s)

        t0_ms = int(next_min_s * 1000)
        snap_bal = client.get_account_balance()
        snap_pos = client.get_positions()
        gt_total_eq_t0 = float(snap_bal["totalEq"])
        gt_upl_t0 = sum(float(p.get("upl") or 0) for p in snap_pos)
        gt_usdt_cash_t0 = next(
            (float(d["cashBal"]) for d in snap_bal.get("details", []) if d["ccy"] == "USDT"),
            0.0,
        )
        print(f"  [GROUND TRUTH SNAPSHOT @ T0={t0_ms}]")
        print(f"    totalEq(T0)  = ${gt_total_eq_t0:,.4f}")
        print(f"    USDT cashBal = ${gt_usdt_cash_t0:,.4f}")
        print(f"    Total UPL    = ${gt_upl_t0:+,.4f}")
        for p in snap_pos:
            print(
                f"    pos {p['instId']}: sz={p['pos']}, avgPx={p['avgPx']}, "
                f"markPx={p['markPx']}, upl=${float(p['upl']):+,.4f}"
            )

        # Wait 10s past T0 so post-T0 events are strictly > T0
        time.sleep(10)

        print("\n=== STEP 3: Post-T0 events (Transfer + Close ETH + Add BTC) ===")
        tr = _post(
            client,
            "/api/v5/asset/transfer",
            {"ccy": "USDT", "amt": "100", "from": "6", "to": "18"},
        )
        print(f"  Transferred +100 USDT (Funding -> Trading): transId={tr[0]['transId']}")

        r_eth_close = _place_market(client, "ETH-USDT-SWAP", "sell", "2")
        print(f"  Closed ETH-USDT-SWAP (-2): ordId={r_eth_close['ordId']}")

        r_btc_add = _place_market(client, "BTC-USDT-SWAP", "buy", "1")
        print(f"  Added BTC-USDT-SWAP (+1, total 3): ordId={r_btc_add['ordId']}")

        print("\n  Waiting 6s for OKX bills/fills/candles to settle...")
        time.sleep(6)

        print("\n=== STEP 4: Reconstruct T0 WITHOUT using the snapshot ===")
        report = calculate_monthly_pnl(client, start_ts_ms=t0_ms)
        print_report(report)

        gt_pnl = report.current_total_eq_usd - 100.0 - gt_total_eq_t0
        print("\n" + "=" * 72)
        print("GROUND TRUTH vs RECONSTRUCTED (NO SNAPSHOT) COMPARISON")
        print("=" * 72)
        print(f"  Ground-Truth totalEq(T0)   : ${gt_total_eq_t0:>12,.4f}")
        print(f"  Reconstructed totalEq(T0)  : ${report.reconstructed_total_eq_at_t0:>12,.4f}")
        print(f"  Baseline Error             : ${report.reconstructed_total_eq_at_t0 - gt_total_eq_t0:>+12,.4f}")
        print("-" * 72)
        print(f"  Ground-Truth UPL(T0)       : ${gt_upl_t0:>+12,.4f}")
        print(f"  Reconstructed UPL(T0)      : ${report.reconstructed_upl_usd_at_t0:>+12,.4f}")
        print(f"  UPL(T0) Error              : ${report.reconstructed_upl_usd_at_t0 - gt_upl_t0:>+12,.4f}")
        print("-" * 72)
        print(f"  Ground-Truth USDT Cash(T0) : ${gt_usdt_cash_t0:>12,.4f}")
        print(f"  Reconstructed USDT Cash(T0): ${report.cash_balances_at_t0.get('USDT', 0.0):>12,.4f}")
        print(f"  USDT Cash Error            : ${report.cash_balances_at_t0.get('USDT', 0.0) - gt_usdt_cash_t0:>+12,.4f}")
        print("-" * 72)
        print(f"  Ground-Truth PnL (T0->Now) : ${gt_pnl:>+12,.4f}")
        print(f"  Reconstructed Equity PnL   : ${report.pnl_equity_method_usd:>+12,.4f}")
        print(f"  PnL Error                  : ${report.pnl_equity_method_usd - gt_pnl:>+12,.4f}")
        print("=" * 72)

        print("\n=== STEP 5: Cleaning up demo position (closing BTC-USDT-SWAP -3) ===")
        r_btc_close = _place_market(client, "BTC-USDT-SWAP", "sell", "3")
        print(f"  Closed BTC-USDT-SWAP (-3): ordId={r_btc_close['ordId']}")


if __name__ == "__main__":
    main()
