import asyncio
import gzip
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator

import websockets

from app.models.events import TakerSide, TradeEvent

logger = logging.getLogger(__name__)


class BingXSpotClient:
    def __init__(self, ws_base: str = "wss://open-api-ws.bingx.com/market", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base
        self._ssl_context = ssl._create_unverified_context() if insecure_ssl else None

    def clean_symbols(self, symbols: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = raw.upper().strip().split(":")[-1].replace("_", "-").replace("/", "-")
            symbol = "".join(char for char in symbol if char.isalnum() or char == "-")
            if not symbol:
                continue
            if "-" not in symbol:
                base = symbol[:-4] if symbol.endswith("USDT") else symbol
                symbol = f"{base}-USDT"
            if not symbol.endswith("-USDT"):
                symbol = f"{symbol}-USDT"
            if symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned[:limit]

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[TradeEvent]:
        # No inner reconnect loop here — _stream_runner in scanner.py owns retry/backoff.
        # We just connect once, stream events, and raise on any error so the runner can handle it.
        async with websockets.connect(
            self.ws_base,
            ping_interval=None,   # BingX uses its own application-level ping; disable library ping
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            for symbol in symbols:
                await ws.send(
                    json.dumps(
                        {
                            "id": str(uuid.uuid4()),
                            "reqType": "sub",
                            "dataType": f"{symbol}@trade",
                        }
                    )
                )
            logger.info("Connected BingX spot trade stream for %s symbols", len(symbols))
            # Keepalive ping task — BingX closes idle connections after ~5 min without it
            pinger = asyncio.create_task(self._ping(ws))
            last_message_at = asyncio.get_event_loop().time()
            try:
                async for message in ws:
                    last_message_at = asyncio.get_event_loop().time()
                    # Stale watchdog: if >35 s with no frame, force reconnect
                    if asyncio.get_event_loop().time() - last_message_at > 35:
                        raise ConnectionError("BingX spot stream stale — no message for 35 s")
                    text = self._decode_message(message)
                    if "ping" in text.lower():
                        await ws.send("Pong")
                        continue
                    for event in self.parse_ws_message(text):
                        yield event
            finally:
                pinger.cancel()
                await asyncio.gather(pinger, return_exceptions=True)

    async def _ping(self, ws) -> None:
        """Send a keepalive ping to BingX every 20 s to prevent server-side idle disconnect."""
        while True:
            await asyncio.sleep(20)
            try:
                await ws.send("Pong")
            except Exception:
                return

    def _decode_message(self, message: str | bytes) -> str:
        if isinstance(message, str):
            return message
        try:
            return gzip.decompress(message).decode("utf-8")
        except OSError:
            return message.decode("utf-8", errors="ignore")

    def parse_ws_message(self, message: str) -> list[TradeEvent]:
        payload = json.loads(message)
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        events: list[TradeEvent] = []
        for item in data:
            price = float(item.get("p") or 0)
            quantity = float(item.get("q") or 0)
            if price <= 0 or quantity <= 0:
                continue
            symbol = str(item.get("s") or payload.get("dataType", "").split("@", 1)[0]).upper()
            taker_side = TakerSide.SELL if item.get("m") else TakerSide.BUY
            trade_time = int(item.get("T") or 0)
            events.append(
                TradeEvent(
                    symbol=symbol.replace("-", ""),
                    event_time=trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=taker_side,
                    aggregate=False,
                    exchange="bingx",
                )
            )
        return events
