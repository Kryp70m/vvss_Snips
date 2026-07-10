import aiohttp

from app.models.scanner import WhaleTradeEvent


class CoinGlassClient:
    def __init__(self, api_key: str | None = None, base_url: str = "https://open-api-v4.coinglass.com") -> None:
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def configure(self, api_key: str) -> dict:
        self.api_key = api_key.strip()
        return self.status()

    def status(self) -> dict:
        return {"enabled": bool(self.api_key), "key_set": bool(self.api_key)}

    async def large_orders(self, exchange: str, symbol: str) -> list[WhaleTradeEvent]:
        if not self.api_key:
            return []
        await self.connect()
        assert self._session is not None
        headers = {"CG-API-KEY": self.api_key}
        params = {"exchange": exchange, "symbol": symbol}
        url = f"{self.base_url}/api/futures/orderbook/large-limit-order"
        async with self._session.get(url, params=params, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()
        return self._parse_large_orders(payload, exchange, symbol)

    def _parse_large_orders(self, payload: object, exchange: str, fallback_symbol: str) -> list[WhaleTradeEvent]:
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        if isinstance(rows, dict):
            for key in ("list", "items", "orders", "data"):
                if isinstance(rows.get(key), list):
                    rows = rows[key]
                    break
        if not isinstance(rows, list):
            return []
        events: list[WhaleTradeEvent] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or row.get("pair") or fallback_symbol).upper()
            price = float(row.get("price") or row.get("px") or 0)
            amount = float(row.get("amount") or row.get("volUsd") or row.get("value") or row.get("notional") or 0)
            quantity = float(row.get("quantity") or row.get("qty") or row.get("size") or 0)
            notional = amount if amount > 0 else price * quantity
            if notional <= 0:
                continue
            side_raw = str(row.get("side") or row.get("direction") or "").lower()
            side = "buy" if "buy" in side_raw or side_raw in {"1", "bid"} else "sell" if "sell" in side_raw or side_raw in {"2", "ask"} else "wall"
            events.append(
                WhaleTradeEvent(
                    source="coinglass",
                    symbol=symbol,
                    side=side,
                    price=price,
                    quantity=quantity,
                    notional=notional,
                    event_time=int(row.get("time") or row.get("timestamp") or row.get("ts") or 0),
                    venue=exchange,
                    bias="whale support wall" if side == "buy" else "whale resistance wall" if side == "sell" else "whale wall",
                    severity="monster" if notional >= 2_000_000 else "huge" if notional >= 500_000 else "large",
                )
            )
        return events
