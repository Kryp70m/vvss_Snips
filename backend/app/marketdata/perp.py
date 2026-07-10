import asyncio
import gzip
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Iterable

import websockets

from app.models.events import BookTickerEvent, KlineEvent, TakerSide, TradeEvent

logger = logging.getLogger(__name__)


def _ssl_context(insecure_ssl: bool) -> ssl.SSLContext | None:
    return ssl._create_unverified_context() if insecure_ssl else None


def _safe_float(value: object) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value: object) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _clean_usdt_symbol(raw: str) -> str:
    symbol = "".join(char for char in raw.upper().strip().split(":")[-1] if char.isalnum())
    if not symbol:
        return ""
    return symbol if symbol.endswith("USDT") else f"{symbol}USDT"


class BinancePerpClient:
    def __init__(self, ws_base: str = "wss://fstream.binance.com/stream", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base.rstrip("/")
        self._ssl_context = _ssl_context(insecure_ssl)

    def clean_symbols(self, symbols: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = _clean_usdt_symbol(raw)
            if symbol and symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned[:limit]

    def combined_stream_url(self, symbols: Iterable[str]) -> str:
        streams: list[str] = []
        for symbol in symbols:
            lower = symbol.lower()
            streams.extend([f"{lower}@aggTrade", f"{lower}@bookTicker", f"{lower}@kline_5m"])
        return f"{self.ws_base}?streams={'/'.join(streams)}"

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[TradeEvent | BookTickerEvent | KlineEvent]:
        url = self.combined_stream_url(symbols)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            logger.info("Connected Binance perp stream for %s symbols", len(symbols))
            async for message in ws:
                event = self.parse_ws_message(message)
                if event is not None:
                    yield event

    def parse_ws_message(self, message: str) -> TradeEvent | BookTickerEvent | KlineEvent | None:
        payload = json.loads(message)
        data = payload.get("data", payload)
        event_type = data.get("e")
        if event_type in {"aggTrade", "trade"}:
            price = _safe_float(data.get("p"))
            quantity = _safe_float(data.get("q"))
            trade_time = _safe_int(data.get("T") or data.get("E"))
            if price <= 0 or quantity <= 0:
                return None
            return TradeEvent(
                symbol=str(data.get("s", "")).upper(),
                event_time=_safe_int(data.get("E") or trade_time),
                trade_time=trade_time,
                price=price,
                quantity=quantity,
                quote_quantity=price * quantity,
                taker_side=TakerSide.SELL if bool(data.get("m")) else TakerSide.BUY,
                aggregate=event_type == "aggTrade",
                exchange="binance_perp",
            )
        if event_type == "bookTicker" or {"s", "b", "B", "a", "A"}.issubset(data):
            return BookTickerEvent(
                symbol=str(data.get("s", "")).upper(),
                event_time=_safe_int(data.get("E") or data.get("T")),
                bid_price=_safe_float(data.get("b")),
                bid_quantity=_safe_float(data.get("B")),
                ask_price=_safe_float(data.get("a")),
                ask_quantity=_safe_float(data.get("A")),
                exchange="binance_perp",
            )
        if event_type == "kline":
            kline = data.get("k", {})
            return KlineEvent(
                symbol=str(data.get("s", "")).upper(),
                event_time=_safe_int(data.get("E")),
                open_time=_safe_int(kline.get("t")),
                close_time=_safe_int(kline.get("T")),
                interval=str(kline.get("i") or ""),
                high=_safe_float(kline.get("h")),
                low=_safe_float(kline.get("l")),
                close=_safe_float(kline.get("c")),
                closed=bool(kline.get("x")),
                exchange="binance_perp",
            )
        return None


class BybitPerpClient:
    def __init__(self, ws_base: str = "wss://stream.bybit.com/v5/public/linear", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base
        self._ssl_context = _ssl_context(insecure_ssl)

    def clean_symbols(self, symbols: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = _clean_usdt_symbol(raw)
            if symbol and symbol not in seen:
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
            logger.info("Connected Bybit perp trade stream for %s symbols", len(symbols))
            async for message in ws:
                for event in self.parse_ws_message(message):
                    yield event

    def parse_ws_message(self, message: str) -> list[TradeEvent]:
        payload = json.loads(message)
        if not str(payload.get("topic", "")).startswith("publicTrade."):
            return []
        events: list[TradeEvent] = []
        for item in payload.get("data", []):
            price = _safe_float(item.get("p"))
            quantity = _safe_float(item.get("v"))
            if price <= 0 or quantity <= 0:
                continue
            trade_time = _safe_int(item.get("T") or payload.get("ts"))
            events.append(
                TradeEvent(
                    symbol=str(item.get("s", "")).upper(),
                    event_time=trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=TakerSide.BUY if item.get("S") == "Buy" else TakerSide.SELL,
                    aggregate=False,
                    exchange="bybit_perp",
                )
            )
        return events


class BingXPerpClient:
    def __init__(self, ws_base: str = "wss://open-api-swap.bingx.com/swap-market", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base
        self._ssl_context = _ssl_context(insecure_ssl)

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
        # Single-connect generator — _stream_runner owns retry/backoff.
        async with websockets.connect(
            self.ws_base,
            ping_interval=None,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            for symbol in symbols:
                await ws.send(json.dumps({"id": str(uuid.uuid4()), "reqType": "sub", "dataType": f"{symbol}@trade"}))
            logger.info("Connected BingX perp trade stream for %s symbols", len(symbols))
            pinger = asyncio.create_task(self._bingx_ping(ws))
            try:
                async for message in ws:
                    text = self._decode_message(message)
                    if "ping" in text.lower():
                        await ws.send("Pong")
                        continue
                    for event in self.parse_ws_message(text):
                        yield event
            finally:
                pinger.cancel()
                await asyncio.gather(pinger, return_exceptions=True)

    async def _bingx_ping(self, ws) -> None:
        """Send keepalive ping every 20 s to prevent BingX server-side idle disconnect."""
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
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        events: list[TradeEvent] = []
        for item in data:
            price = _safe_float(item.get("p") or item.get("price"))
            quantity = _safe_float(item.get("q") or item.get("qty") or item.get("quantity"))
            if price <= 0 or quantity <= 0:
                continue
            symbol = str(item.get("s") or payload.get("dataType", "").split("@", 1)[0]).upper().replace("-", "")
            maker_flag = item.get("m")
            side_text = str(item.get("side") or item.get("S") or "").lower()
            if side_text in {"sell", "short"}:
                side = TakerSide.SELL
            elif side_text in {"buy", "long"}:
                side = TakerSide.BUY
            else:
                side = TakerSide.SELL if maker_flag else TakerSide.BUY
            trade_time = _safe_int(item.get("T") or item.get("t") or payload.get("ts"))
            events.append(
                TradeEvent(
                    symbol=symbol,
                    event_time=trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=side,
                    aggregate=False,
                    exchange="bingx_perp",
                )
            )
        return events


class MexcPerpClient:
    def __init__(self, ws_base: str = "wss://contract.mexc.com/edge", insecure_ssl: bool = False) -> None:
        self.ws_base = ws_base
        self._ssl_context = _ssl_context(insecure_ssl)

    def clean_symbols(self, symbols: list[str], limit: int) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in symbols:
            symbol = raw.upper().strip().split(":")[-1].replace("-", "_").replace("/", "_")
            symbol = "".join(char for char in symbol if char.isalnum() or char == "_")
            if not symbol:
                continue
            if "_" not in symbol:
                base = symbol[:-4] if symbol.endswith("USDT") else symbol
                symbol = f"{base}_USDT"
            if not symbol.endswith("_USDT"):
                symbol = f"{symbol}_USDT"
            if symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned[:limit]

    def display_symbol(self, symbol: str) -> str:
        return symbol.replace("_", "")

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[TradeEvent]:
        async with websockets.connect(
            self.ws_base,
            ping_interval=None,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            pinger = asyncio.create_task(self._ping(ws))
            try:
                for symbol in symbols:
                    await ws.send(json.dumps({"method": "sub.deal", "param": {"symbol": symbol}, "gzip": False}))
                logger.info("Connected MEXC perp trade stream for %s symbols", len(symbols))
                async for message in ws:
                    text = self._decode_message(message)
                    for event in self.parse_ws_message(text):
                        yield event
            finally:
                pinger.cancel()
                await asyncio.gather(pinger, return_exceptions=True)

    async def _ping(self, ws) -> None:
        while True:
            await asyncio.sleep(18)
            await ws.send(json.dumps({"method": "ping"}))

    def _decode_message(self, message: str | bytes) -> str:
        if isinstance(message, str):
            return message
        try:
            return gzip.decompress(message).decode("utf-8")
        except OSError:
            return message.decode("utf-8", errors="ignore")

    def parse_ws_message(self, message: str) -> list[TradeEvent]:
        payload = json.loads(message)
        if payload.get("channel") != "push.deal":
            return []
        data = payload.get("data")
        rows = data if isinstance(data, list) else [data]
        symbol = self.display_symbol(str(payload.get("symbol") or "").upper())
        event_time = _safe_int(payload.get("ts"))
        events: list[TradeEvent] = []
        for item in rows:
            if not isinstance(item, dict):
                continue
            price = _safe_float(item.get("p") or item.get("price"))
            quantity = _safe_float(item.get("v") or item.get("vol") or item.get("quantity"))
            if price <= 0 or quantity <= 0:
                continue
            direction = _safe_int(item.get("T") or item.get("side"))
            trade_time = _safe_int(item.get("t") or item.get("time") or event_time)
            events.append(
                TradeEvent(
                    symbol=symbol,
                    event_time=event_time or trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=TakerSide.BUY if direction == 1 else TakerSide.SELL,
                    aggregate=True,
                    exchange="mexc_perp",
                )
            )
        return events
