from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

import websockets

from .client import OkxConfig, sign_request


async def stream_realtime_pnl(
    config: OkxConfig,
    baseline_eq_at_t0: float,
    net_transfers_usd: float,
) -> None:
    """
    Connect to OKX Private WebSocket, authenticate, subscribe to `account` and
    `positions` channels, and print live monthly PnL whenever equity or positions update.
    """
    print(f"[WS] Connecting to {config.ws_url} ...")
    async with websockets.connect(config.ws_url, ping_interval=20, ping_timeout=10) as ws:
        # 1. Authenticate
        ts = str(int(time.time()))
        sign = sign_request(config.api_secret, ts, "GET", "/users/self/verify")
        login_msg = {
            "op": "login",
            "args": [
                {
                    "apiKey": config.api_key,
                    "passphrase": config.passphrase,
                    "timestamp": ts,
                    "sign": sign,
                }
            ],
        }
        await ws.send(json.dumps(login_msg))
        while True:
            login_resp = json.loads(await ws.recv())
            if login_resp.get("event") == "notice":
                print(f"[WS notice] {login_resp.get('msg')}")
                continue
            if login_resp.get("event") != "login" or login_resp.get("code") != "0":
                raise RuntimeError(f"OKX WS login failed: {login_resp}")
            break
        print("[WS] Authenticated successfully.")

        # 2. Subscribe to `account` (totalEq) and `positions` (live position updates)
        sub_msg = {
            "op": "subscribe",
            "args": [
                {"channel": "account"},
                {"channel": "positions", "instType": "ANY"},
            ],
        }
        await ws.send(json.dumps(sub_msg))
        print("[WS] Subscribed to `account` and `positions` channels. Waiting for updates...")

        latest_total_eq: float | None = None

        async for raw in ws:
            msg: dict[str, Any] = json.loads(raw)
            if "event" in msg:
                print(f"[WS event] {msg}")
                continue

            channel = msg.get("arg", {}).get("channel")
            data = msg.get("data", [])
            now_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            if channel == "account" and data:
                latest_total_eq = float(data[0].get("totalEq") or 0.0)
                live_pnl = latest_total_eq - net_transfers_usd - baseline_eq_at_t0
                print(
                    f"[{now_str}] ACCOUNT  | totalEq=${latest_total_eq:,.2f} | "
                    f"baseline(T0)=${baseline_eq_at_t0:,.2f} | "
                    f"netTransfers=${net_transfers_usd:,.2f} | "
                    f"Monthly PnL=${live_pnl:+,.2f}"
                )
            elif channel == "positions":
                active = [p for p in data if float(p.get("pos") or 0) != 0]
                summary = ", ".join(
                    f"{p['instId']}({p.get('posSide','net')}): pos={p['pos']} upl=${float(p.get('upl') or 0):+.2f}"
                    for p in active
                ) or "no open positions"
                print(f"[{now_str}] POSITION | {summary}")
