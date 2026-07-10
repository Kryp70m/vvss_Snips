import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator

import websockets

from app.models.events import TakerSide, TradeEvent

logger = logging.getLogger(__name__)


class BybitSpotClient:
    def __init__(self, ws_base: str = "wss://stream.bybit.com/v5/public/spot", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base
        self._ssl_context = ssl._create_unverified_context() if insecure_ssl else None

    def clean_symbols(self, symbols: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = "".join(char for char in raw.upper().strip().split(":")[-1] if char.isalnum())
            if not symbol:
                continue
            if not symbol.endswith("USDT"):
                symbol = f"{symbol}USDT"
            if symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned[:limit]

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[TradeEvent]:
        args = [f"publicTrade.{symbol}" for symbol in symbols]
        async with websockets.connect(
            self.ws_base,
            ping_interval=20,
            ping_timeout=20,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            logger.info("Connected Bybit spot trade stream for %s symbols", len(symbols))
            async for message in ws:
                for event in self.parse_ws_message(message):
                    yield event

    def parse_ws_message(self, message: str) -> list[TradeEvent]:
        payload = json.loads(message)
        if not str(payload.get("topic", "")).startswith("publicTrade."):
            return []
        events: list[TradeEvent] = []
        for item in payload.get("data", []):
            price = float(item.get("p") or 0)
            quantity = float(item.get("v") or 0)
            if price <= 0 or quantity <= 0:
                continue
            side = TakerSide.BUY if item.get("S") == "Buy" else TakerSide.SELL
            trade_time = int(item.get("T") or payload.get("ts") or 0)
            events.append(
                TradeEvent(
                    symbol=str(item.get("s", "")).upper(),
                    event_time=trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=side,
                    aggregate=False,
                    exchange="bybit",
                )
            )
        return events
