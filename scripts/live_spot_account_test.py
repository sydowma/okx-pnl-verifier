"""
Live Demo Test: Spot + Transfer + WS `account` Channel + Fast `end=T0&limit=1` Reconstruction.

Proves whether Option B (totalEq + WS `account` + `bills?end=T0&limit=1` + `type=1` transfers)
accurately reconstructs Spot holdings & Spot trades WITHOUT a saved T0 snapshot!
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import websockets
from dotenv import load_dotenv

from okx_pnl.client import OkxConfig, OkxRestClient, sign_request
from scripts.live_demo_experiment import _post


def _get_cash_bal_at_t0_fast(client: OkxRestClient, ccy: str, t0_ms: int, current_cash: float) -> tuple[float, str]:
    """
    1-call fast CashBal(T0) lookup:
    Check /account/bills (real-time, last 7d) then /account/bills-archive (last 3m)
    for the single latest bill with ts <= T0, and read its `bal` field directly!
    If no bill exists before T0 in the last 3m, balance hasn't changed before T0 ->
    subtract bills in (T0, Now] from current_cash.
    """
    for ep in ("/api/v5/account/bills", "/api/v5/account/bills-archive"):
        rows = client._get(ep, params={"ccy": ccy, "end": t0_ms, "limit": 1})
        if rows:
            return float(rows[0]["bal"]), f"{ep.split('/')[-1]} (1-call bal)"
    # No bill <= T0 in last 3 months -> walk back from current_cash using (T0, Now] bills
    bills_after = client._get("/api/v5/account/bills", params={"ccy": ccy, "begin": t0_ms, "limit": 100})
    delta = sum(float(b["balChg"]) for b in bills_after if int(b["ts"]) > t0_ms)
    return current_cash - delta, "unchanged_before_T0 (walkback)"


def _get_exact_usd_px_at(client: OkxRestClient, ccy: str, ts_ms: int) -> float:
    upper = ccy.upper()
    if upper == "USD":
        return 1.0
    try:
        return client.get_usd_price_at(upper, ts_ms) if upper not in ("USDT", "USDC") else _get_stable_index(client, upper, ts_ms)
    except Exception:
        return 1.0


def _get_stable_index(client: OkxRestClient, ccy: str, ts_ms: int) -> float:
    minute_ts = (ts_ms // 60_000) * 60_000
    rows = client._get(
        "/api/v5/market/history-index-candles",
        params={"instId": f"{ccy}-USD", "bar": "1m", "after": minute_ts + 60_000, "limit": 1},
        private=False,
    )
    if rows:
        return float(rows[0][1])
    return 1.0


async def main() -> None:
    load_dotenv()
    cfg = OkxConfig.from_env()
    if not cfg.simulated:
        raise RuntimeError("SAFETY ABORT: OKX_SIMULATED must be 1!")

    with OkxRestClient(cfg) as client:
        print("=== STEP 1: Buy 0.2 SOL Spot BEFORE T0 (cross-boundary Spot holding) ===")
        _post(
            client,
            "/api/v5/trade/order",
            {"instId": "SOL-USDT", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "0.2", "tgtCcy": "base_ccy"},
        )
        print("  Bought 0.2 SOL spot.")

        # Wait for next exact 1m boundary
        now_s = time.time()
        next_min_s = (int(now_s) // 60 + 1) * 60
        wait_s = max(5.0, next_min_s - now_s + 1.0)
        print(f"\n=== STEP 2: Waiting {wait_s:.1f}s for T0 boundary ===")
        time.sleep(wait_s)

        t0_ms = int(next_min_s * 1000)
        snap_bal = client.get_account_balance()
        gt_total_eq_t0 = float(snap_bal["totalEq"])
        gt_cash_t0 = {d["ccy"]: float(d["cashBal"]) for d in snap_bal.get("details", []) if float(d["cashBal"] or 0) != 0}
        print(f"  [GROUND TRUTH @ T0={t0_ms}] totalEq=${gt_total_eq_t0:,.4f}")
        print(f"  [GROUND TRUTH Cash @ T0]   SOL={gt_cash_t0.get('SOL', 0):.6f}, USDT={gt_cash_t0.get('USDT', 0):.4f}")

        time.sleep(6)

        print("\n=== STEP 3: Post-T0 Spot Trades + Transfer (+50 USDT) ===")
        _post(client, "/api/v5/asset/transfer", {"ccy": "USDT", "amt": "50", "from": "6", "to": "18"})
        print("  Transferred +50 USDT (Funding -> Trading)")

        _post(
            client,
            "/api/v5/trade/order",
            {"instId": "SOL-USDT", "tdMode": "cash", "side": "buy", "ordType": "market", "sz": "0.1", "tgtCcy": "base_ccy"},
        )
        print("  Bought +0.1 SOL spot (post-T0)")
        time.sleep(2)

        # Sell entire SOL spot balance back to USDT
        sol_now = next(
            (d["availBal"] for d in client.get_account_balance().get("details", []) if d["ccy"] == "SOL"),
            "0",
        )
        if float(sol_now) > 0:
            _post(
                client,
                "/api/v5/trade/order",
                {"instId": "SOL-USDT", "tdMode": "cash", "side": "sell", "ordType": "market", "sz": sol_now, "tgtCcy": "base_ccy"},
            )
            print(f"  Sold all {sol_now} SOL spot back to USDT")

        time.sleep(4)

        print("\n=== STEP 4: Read LIVE totalEq(Now) from WS `account` channel ===")
        ws_total_eq_now = None
        async with websockets.connect(cfg.ws_url, ping_interval=20) as ws:
            ts = str(int(time.time()))
            await ws.send(json.dumps({
                "op": "login",
                "args": [{"apiKey": cfg.api_key, "passphrase": cfg.passphrase, "timestamp": ts, "sign": sign_request(cfg.api_secret, ts, "GET", "/users/self/verify")}]
            }))
            while True:
                r = json.loads(await ws.recv())
                if r.get("event") == "login":
                    break
            await ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "account"}]}))
            end = time.time() + 5
            while time.time() < end:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
                if m.get("arg", {}).get("channel") == "account" and m.get("data"):
                    ws_total_eq_now = float(m["data"][0]["totalEq"])
                    print(f"  [WS account] Live totalEq(Now) = ${ws_total_eq_now:,.4f}")
                    break

        print("\n=== STEP 5: Reconstruct T0 WITHOUT Snapshot (1-call bal + type=1 transfers) ===")
        now_ms = int(time.time() * 1000)
        bal_now = client.get_account_balance()
        cur_cash_map = {d["ccy"]: float(d["cashBal"] or 0) for d in bal_now.get("details", [])}
        # Also include any ccy that had bills in (T0, Now] (like SOL which is now 0!)
        recent_bills = client._get("/api/v5/account/bills", params={"begin": t0_ms, "limit": 100})
        all_ccys = sorted(set(cur_cash_map.keys()) | {b["ccy"] for b in recent_bills})

        recon_total_eq_t0 = 0.0
        recon_cash_t0: dict[str, float] = {}
        for ccy in all_ccys:
            c_t0, src = _get_cash_bal_at_t0_fast(client, ccy, t0_ms, cur_cash_map.get(ccy, 0.0))
            if abs(c_t0) < 1e-10:
                continue
            px_t0 = _get_exact_usd_px_at(client, ccy, t0_ms)
            usd_t0 = c_t0 * px_t0
            recon_cash_t0[ccy] = c_t0
            recon_total_eq_t0 += usd_t0
            print(f"  {ccy:5s}: Cash@T0={c_t0:12.6f} ({src}) × ${px_t0:10.4f} = ${usd_t0:10.4f}")

        # Net transfers: type=1, subType in (11, 12)
        transfer_bills = client._get("/api/v5/account/bills", params={"type": "1", "begin": t0_ms, "end": now_ms, "limit": 100})
        net_transfers_usd = 0.0
        for tb in transfer_bills:
            if str(tb.get("subType")) in ("11", "12") and int(tb["ts"]) > t0_ms:
                t_px = _get_exact_usd_px_at(client, tb["ccy"], int(tb["ts"]))
                net_transfers_usd += float(tb["balChg"]) * t_px

        gt_pnl = (ws_total_eq_now or 0.0) - net_transfers_usd - gt_total_eq_t0
        recon_pnl = (ws_total_eq_now or 0.0) - net_transfers_usd - recon_total_eq_t0

        print("\n" + "=" * 76)
        print("SPOT + WS `account` GROUND TRUTH vs RECONSTRUCTED COMPARISON")
        print("=" * 76)
        print(f"  SOL  Cash@T0 : GT={gt_cash_t0.get('SOL', 0):.6f} | Recon={recon_cash_t0.get('SOL', 0):.6f} | Diff={recon_cash_t0.get('SOL', 0) - gt_cash_t0.get('SOL', 0):+.6f}")
        print(f"  USDT Cash@T0 : GT={gt_cash_t0.get('USDT', 0):.4f} | Recon={recon_cash_t0.get('USDT', 0):.4f} | Diff={recon_cash_t0.get('USDT', 0) - gt_cash_t0.get('USDT', 0):+.4f}")
        print(f"  NetTransfers : ${net_transfers_usd:+.4f} (type=1, subType in 11/12)")
        print("-" * 76)
        print(f"  Ground-Truth totalEq(T0)  : ${gt_total_eq_t0:>12,.4f}")
        print(f"  Reconstructed totalEq(T0) : ${recon_total_eq_t0:>12,.4f}")
        print(f"  Baseline Diff             : ${recon_total_eq_t0 - gt_total_eq_t0:>+12,.4f} ({abs(recon_total_eq_t0 - gt_total_eq_t0)/gt_total_eq_t0*100:.4f}%)")
        print("-" * 76)
        print(f"  Ground-Truth Monthly PnL  : ${gt_pnl:>+12,.4f}")
        print(f"  Reconstructed Monthly PnL : ${recon_pnl:>+12,.4f}")
        print("=" * 76)


if __name__ == "__main__":
    asyncio.run(main())
