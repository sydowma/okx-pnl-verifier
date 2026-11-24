from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx


@dataclass(frozen=True)
class OkxConfig:
    api_key: str
    api_secret: str
    passphrase: str
    simulated: bool = False
    base_url: str = "https://www.okx.com"
    ws_url: str = "wss://ws.okx.com/ws/v5/private"

    @classmethod
    def from_env(cls) -> "OkxConfig":
        simulated = os.getenv("OKX_SIMULATED", "0").strip() == "1"
        default_ws = (
            "wss://wspap.okx.com/ws/v5/private"
            if simulated
            else "wss://ws.okx.com/ws/v5/private"
        )
        return cls(
            api_key=os.environ["OKX_API_KEY"].strip(),
            api_secret=os.environ["OKX_API_SECRET"].strip(),
            passphrase=os.environ["OKX_PASSPHRASE"].strip(),
            simulated=simulated,
            base_url=os.getenv("OKX_BASE_URL", "https://www.okx.com").rstrip("/"),
            ws_url=os.getenv("OKX_WS_URL", default_ws).strip(),
        )


class OkxApiError(RuntimeError):
    def __init__(self, code: str, msg: str, endpoint: str) -> None:
        super().__init__(f"OKX API error on {endpoint}: code={code}, msg={msg}")
        self.code = code
        self.msg = msg
        self.endpoint = endpoint


def sign_request(
    secret: str, timestamp: str, method: str, request_path: str, body: str = ""
) -> str:
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


class OkxRestClient:
    """Minimal, read-only OKX v5 REST client with cursor pagination and price caching."""

    def __init__(self, config: OkxConfig, timeout: float = 15.0) -> None:
        self.config = config
        self._client = httpx.Client(base_url=config.base_url, timeout=timeout)
        self._price_cache: dict[tuple[str, str, int], float] = {}
        self._instrument_cache: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "OkxRestClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def _get(
        self, path: str, params: dict[str, Any] | None = None, private: bool = True
    ) -> list[dict[str, Any]]:
        clean_params = {
            k: str(v) for k, v in (params or {}).items() if v is not None and v != ""
        }
        query = urlencode(clean_params)
        request_path = f"{path}?{query}" if query else path

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.config.simulated:
            headers["x-simulated-trading"] = "1"

        if private:
            ts = (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
            headers.update(
                {
                    "OK-ACCESS-KEY": self.config.api_key,
                    "OK-ACCESS-SIGN": sign_request(
                        self.config.api_secret, ts, "GET", request_path
                    ),
                    "OK-ACCESS-TIMESTAMP": ts,
                    "OK-ACCESS-PASSPHRASE": self.config.passphrase,
                }
            )

        resp = self._client.get(request_path, headers=headers)
        try:
            payload = resp.json()
        except Exception:
            resp.raise_for_status()
            raise
        if resp.status_code >= 400 or payload.get("code") != "0":
            raise OkxApiError(
                str(payload.get("code", resp.status_code)),
                str(payload.get("msg", resp.text)),
                request_path,
            )
        return payload.get("data", [])

    # ── Account & Positions ────────────────────────────────────────────────

    def get_account_balance(self) -> dict[str, Any]:
        """GET /api/v5/account/balance -> trading account totalEq and per-ccy balances."""
        data = self._get("/api/v5/account/balance")
        return data[0] if data else {}

    def get_positions(self) -> list[dict[str, Any]]:
        """GET /api/v5/account/positions -> currently open positions."""
        return self._get("/api/v5/account/positions")

    def get_positions_history(
        self, begin_ms: int, end_ms: int, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        """
        GET /api/v5/account/positions-history (last 3 months).
        Returns closed/updated positions whose uTime falls in [begin_ms, end_ms].
        """
        results: list[dict[str, Any]] = []
        cursor_before_u_time: str | None = None

        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 100}
            if cursor_before_u_time:
                params["after"] = cursor_before_u_time
            batch = self._get("/api/v5/account/positions-history", params=params)
            if not batch:
                break

            reached_older = False
            for row in batch:
                u_time = int(row["uTime"])
                if u_time < begin_ms:
                    reached_older = True
                    continue
                if u_time <= end_ms:
                    results.append(row)

            if reached_older or len(batch) < 100:
                break
            cursor_before_u_time = batch[-1]["uTime"]
            time.sleep(0.15)

        return results

    def _paginate_bills(
        self, endpoint: str, begin_ms: int, end_ms: int, max_pages: int = 100
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        cursor_after_id: str | None = None

        for _ in range(max_pages):
            params: dict[str, Any] = {
                "begin": begin_ms,
                "end": end_ms,
                "limit": 100,
            }
            if cursor_after_id:
                params["after"] = cursor_after_id

            batch = self._get(endpoint, params=params)
            if not batch:
                break

            results.extend(batch)
            if len(batch) < 100:
                break

            oldest_ts = int(batch[-1]["ts"])
            if oldest_ts < begin_ms:
                break
            cursor_after_id = batch[-1]["billId"]
            time.sleep(0.15)

        return results

    def get_bills(self, begin_ms: int, end_ms: int) -> list[dict[str, Any]]:
        """
        Merge /api/v5/account/bills (last 7 days, real-time) and
        /api/v5/account/bills-archive (last 3 months) deduplicated by billId.
        Sorted chronologically (oldest -> newest).
        """
        merged: dict[str, dict[str, Any]] = {}
        for ep in ("/api/v5/account/bills-archive", "/api/v5/account/bills"):
            for row in self._paginate_bills(ep, begin_ms, end_ms):
                ts = int(row["ts"])
                if begin_ms <= ts <= end_ms:
                    merged[row["billId"]] = row

        return sorted(merged.values(), key=lambda r: (int(r["ts"]), int(r["billId"])))

    def get_fills_history(
        self, begin_ms: int, end_ms: int, inst_type: str = "SWAP", max_pages: int = 100
    ) -> list[dict[str, Any]]:
        """
        GET /api/v5/trade/fills-history (last 3 months) + /api/v5/trade/fills (last 3 days).
        Deduplicated by billId/tradeId, sorted oldest -> newest.
        """
        merged: dict[str, dict[str, Any]] = {}
        for ep in ("/api/v5/trade/fills-history", "/api/v5/trade/fills"):
            cursor_after: str | None = None
            for _ in range(max_pages):
                params: dict[str, Any] = {
                    "instType": inst_type,
                    "begin": begin_ms,
                    "end": end_ms,
                    "limit": 100,
                }
                if cursor_after:
                    params["after"] = cursor_after
                batch = self._get(ep, params=params)
                if not batch:
                    break
                for row in batch:
                    ts = int(row["ts"])
                    if begin_ms <= ts <= end_ms:
                        key = row.get("billId") or f"{row.get('ordId')}:{row.get('tradeId')}"
                        merged[key] = row
                if len(batch) < 100:
                    break
                cursor_after = batch[-1]["billId"]
                time.sleep(0.15)

        return sorted(merged.values(), key=lambda r: int(r["ts"]))

    # ── Market / Reference Data for Historical Valuation ───────────────────

    def get_instrument(self, inst_type: str, inst_id: str) -> dict[str, Any]:
        if inst_id in self._instrument_cache:
            return self._instrument_cache[inst_id]
        rows = self._get(
            "/api/v5/public/instruments",
            params={"instType": inst_type, "instId": inst_id},
            private=False,
        )
        if not rows:
            raise ValueError(f"Instrument not found: {inst_type} {inst_id}")
        self._instrument_cache[inst_id] = rows[0]
        return rows[0]

    def get_mark_price_at(self, inst_id: str, ts_ms: int) -> float:
        """Fetch historical 1m mark price candle open/close nearest to ts_ms."""
        minute_ts = (ts_ms // 60_000) * 60_000
        cache_key = ("mark", inst_id, minute_ts)
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]

        # OKX `after` returns candles strictly older than `after`
        rows = self._get(
            "/api/v5/market/history-mark-price-candles",
            params={"instId": inst_id, "bar": "1m", "after": minute_ts + 60_000, "limit": 2},
            private=False,
        )
        if not rows:
            rows = self._get(
                "/api/v5/market/mark-price-candles",
                params={"instId": inst_id, "bar": "1m", "after": minute_ts + 60_000, "limit": 2},
                private=False,
            )
        if not rows:
            raise ValueError(f"No mark price candle for {inst_id} at {ts_ms}")

        # Candle format: [ts, o, h, l, c, confirm]
        price = float(rows[0][1])  # open of the 1m candle at minute_ts
        self._price_cache[cache_key] = price
        return price

    def get_usd_price_at(self, ccy: str, ts_ms: int) -> float:
        """Fetch historical USD index price of a currency at ts_ms."""
        upper = ccy.upper()
        if upper in ("USD", "USDT", "USDC", "DAI", "FDUSD"):
            return 1.0

        minute_ts = (ts_ms // 60_000) * 60_000
        cache_key = ("index", upper, minute_ts)
        if cache_key in self._price_cache:
            return self._price_cache[cache_key]

        inst_id = f"{upper}-USD"
        rows = self._get(
            "/api/v5/market/history-index-candles",
            params={"instId": inst_id, "bar": "1m", "after": minute_ts + 60_000, "limit": 2},
            private=False,
        )
        if not rows:
            # Fallback to USDT pair if USD index doesn't exist
            inst_id = f"{upper}-USDT"
            rows = self._get(
                "/api/v5/market/history-index-candles",
                params={"instId": inst_id, "bar": "1m", "after": minute_ts + 60_000, "limit": 2},
                private=False,
            )
        if not rows:
            raise ValueError(f"No index price candle for {ccy} at {ts_ms}")

        price = float(rows[0][1])
        self._price_cache[cache_key] = price
        return price
