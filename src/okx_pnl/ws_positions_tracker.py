"""
Mode C + WebSocket `positions` Incremental Tracker.

Architecture:
  1. Bootstrap ONCE via REST (positions + positions-history) -> seed in-memory state.
  2. Subscribe to Private WS `positions` channel (instType=ANY) -> ZERO REST calls afterward!
  3. Update Monthly PnL incrementally on every WS push (open, add, partial close, full close).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import websockets

from .calculator import _to_float
from .client import OkxConfig, OkxRestClient, sign_request
from .positions_only import PositionSnapshotAtT0


def _pos_key(inst_id: str, pos_side: str, c_time_ms: int) -> tuple[str, str, int]:
    return (inst_id, (pos_side or "net").lower(), c_time_ms)


@dataclass
class IncrementalPositionsTracker:
    start_ts_ms: int
    t0_snapshots: dict[tuple[str, str, int], PositionSnapshotAtT0] = field(default_factory=dict)

    # In-memory incremental state (updated purely from WS after bootstrap)
    closed_pnl_usd: float = 0.0
    closed_keys: set[tuple[str, str, int]] = field(default_factory=set)
    open_positions_pnl: dict[tuple[str, str, int], float] = field(default_factory=dict)
    ws_events_processed: int = 0
    raw_close_frames: list[dict[str, Any]] = field(default_factory=list)

    @property
    def monthly_pnl_usd(self) -> float:
        return self.closed_pnl_usd + sum(self.open_positions_pnl.values())

    def _t0_offset(self, key: tuple[str, str, int], c_time_ms: int) -> float:
        if c_time_ms < self.start_ts_ms:
            snap = self.t0_snapshots.get(key)
            return snap.total_pnl_at_t0 if snap else 0.0
        return 0.0

    def _ccy_usd_px_from_ws(self, p: dict[str, Any]) -> float:
        """Convert position settlement ccy to USD directly from WS payload (zero REST calls!)."""
        ccy = (p.get("ccy") or "USDT").upper()
        if ccy in ("USD", "USDT", "USDC", "DAI", "FDUSD"):
            return 1.0
        # For coin-margined inverse contracts (e.g. BTC-USD-SWAP, BTC-USD-261225),
        # ccy is the base coin (BTC) and idxPx / markPx is its USD price!
        for field_name in ("idxPx", "markPx", "last"):
            val = _to_float(p.get(field_name))
            if val > 0.0:
                return val
        return 1.0

    def bootstrap_from_rest(self, client: OkxRestClient) -> None:
        """One-time cold-start bootstrap (2 REST calls: positions-history + positions)."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # 1. Seed already-closed positions in [T0, now]
        for p in client.get_positions_history(self.start_ts_ms, now_ms):
            c_time = int(p.get("cTime") or 0)
            u_time = int(p.get("uTime") or now_ms)
            ccy = p.get("ccy") or "USDT"
            px = client.get_usd_price_at(ccy, u_time)
            key = _pos_key(p["instId"], p.get("posSide") or "net", c_time)
            if key in self.closed_keys:
                continue
            lifetime = _to_float(p.get("realizedPnl")) * px
            self.closed_pnl_usd += lifetime - self._t0_offset(key, c_time)
            self.closed_keys.add(key)

        # 2. Seed currently open positions
        for p in client.get_positions():
            if _to_float(p.get("pos")) == 0.0:
                continue
            c_time = int(p.get("cTime") or 0)
            ccy = p.get("ccy") or "USDT"
            px = client.get_usd_price_at(ccy, now_ms)
            key = _pos_key(p["instId"], p.get("posSide") or "net", c_time)
            lifetime = (_to_float(p.get("realizedPnl")) + _to_float(p.get("upl"))) * px
            self.open_positions_pnl[key] = lifetime - self._t0_offset(key, c_time)

    def apply_ws_position(self, p: dict[str, Any]) -> str:
        """
        Apply a single incremental `positions` WS item in memory (ZERO REST calls).
        Handles USDT-M, USDC-M, Coin-M (inverse), and Delivery FUTURES.
        """
        self.ws_events_processed += 1
        inst_id = p.get("instId", "")
        inst_type = p.get("instType", "")
        ccy = (p.get("ccy") or "USDT").upper()
        pos_side = (p.get("posSide") or "net").lower()
        c_time = int(p.get("cTime") or 0)
        pos = _to_float(p.get("pos"))
        px = self._ccy_usd_px_from_ws(p)
        realized_usd = _to_float(p.get("realizedPnl")) * px
        upl_usd = _to_float(p.get("upl")) * px
        key = _pos_key(inst_id, pos_side, c_time)
        offset = self._t0_offset(key, c_time)

        if pos == 0.0:
            self.raw_close_frames.append(p)
            was_open = key in self.open_positions_pnl
            self.open_positions_pnl.pop(key, None)
            if key not in self.closed_keys and (was_open or c_time >= self.start_ts_ms):
                return f"NEED_CLOSE_FETCH:{inst_id}:{pos_side}:{c_time}"
            return f"CLOSED(dup) {inst_id} | MonthlyPnL=${self.monthly_pnl_usd:+.4f}"

        # Position open / updated
        net_pnl = (realized_usd + upl_usd) - offset
        self.open_positions_pnl[key] = net_pnl
        return (
            f"OPEN/UPD {inst_id} [{inst_type}/{ccy}] pos={pos:g} (ccyPx=${px:,.2f}) | "
            f"realized=${realized_usd:+.4f}, upl=${upl_usd:+.4f} -> "
            f"posPnL=${net_pnl:+.4f} | MonthlyPnL=${self.monthly_pnl_usd:+.4f}"
        )


    def book_closed_position_from_history(
        self, client: OkxRestClient, inst_id: str, pos_side: str, c_time_ms: int
    ) -> str:
        """
        Event-triggered 1-row REST lookup called ONLY when WS pushes pos == "0"
        (because OKX clears realizedPnl to "" on the WS close frame).
        Strictly matches `cTime == c_time_ms` with short retry for OKX's ~1-2s indexing lag.
        """
        key = _pos_key(inst_id, pos_side, c_time_ms)
        if key in self.closed_keys:
            return f"CLOSED(already booked) {inst_id}"

        matched = None
        for attempt in range(4):
            rows = client._get(
                "/api/v5/account/positions-history",
                params={"instId": inst_id, "limit": 5},
            )
            matched = next(
                (r for r in rows if int(r.get("cTime") or 0) == c_time_ms), None
            )
            if matched is not None:
                break
            time.sleep(1.0 * (attempt + 1))

        if matched is None:
            return f"CLOSED(not yet indexed in history) {inst_id} cTime={c_time_ms}"

        ccy = (matched.get("ccy") or "USDT").upper()
        u_time = int(matched.get("uTime") or int(datetime.now(timezone.utc).timestamp() * 1000))
        px = client.get_usd_price_at(ccy, u_time)
        raw_realized = _to_float(matched.get("realizedPnl"))
        lifetime_usd = raw_realized * px
        offset = self._t0_offset(key, c_time_ms)
        final_pnl = lifetime_usd - offset
        self.closed_pnl_usd += final_pnl
        self.closed_keys.add(key)
        return (
            f"CLOSED {inst_id} [{ccy}@${px:,.2f}] | "
            f"raw={raw_realized:+.8f} {ccy} (${lifetime_usd:+.4f}), t0_offset=${offset:+.4f} -> "
            f"booked=${final_pnl:+.4f} | MonthlyPnL=${self.monthly_pnl_usd:+.4f}"
        )


async def stream_positions_incremental(
    config: OkxConfig,
    tracker: IncrementalPositionsTracker,
    rest_client: OkxRestClient | None = None,
    stop_event: asyncio.Event | None = None,
    on_ready: Callable[[], None] | None = None,
) -> None:
    """Connect to OKX Private WS, subscribe to `positions`, and update tracker incrementally."""
    async with websockets.connect(config.ws_url, ping_interval=20, ping_timeout=10) as ws:
        # 1. Login
        ts = str(int(time.time()))
        sign = sign_request(config.api_secret, ts, "GET", "/users/self/verify")
        await ws.send(
            json.dumps(
                {
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
            )
        )
        while True:
            resp = json.loads(await ws.recv())
            if resp.get("event") == "notice":
                continue
            if resp.get("event") != "login" or resp.get("code") != "0":
                raise RuntimeError(f"OKX WS login failed: {resp}")
            break

        # 2. Subscribe to `positions` (ANY)
        await ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "args": [{"channel": "positions", "instType": "ANY"}],
                }
            )
        )

        subscribed = False
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            msg = json.loads(raw)
            if "event" in msg:
                if msg.get("event") == "subscribe" and not subscribed:
                    subscribed = True
                    if on_ready:
                        on_ready()
                continue

            if msg.get("arg", {}).get("channel") == "positions":
                for p in msg.get("data", []):
                    summary = tracker.apply_ws_position(p)
                    if summary.startswith("NEED_CLOSE_FETCH:") and rest_client is not None:
                        _, inst_id, pos_side, c_time_str = summary.split(":")
                        await asyncio.sleep(0.5)  # brief settle for positions-history
                        summary = await asyncio.to_thread(
                            tracker.book_closed_position_from_history,
                            rest_client,
                            inst_id,
                            pos_side,
                            int(c_time_str),
                        )
                    now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                    print(f"  [WS {now_str}] {summary}")
