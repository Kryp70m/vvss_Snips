import asyncio
import gzip
import json
import logging
import ssl
from collections.abc import AsyncIterator

import websockets

from app.models.events import TakerSide, TradeEvent

logger = logging.getLogger(__name__)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = 0
    value = 0
    while index < len(data):
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index
        shift += 7
        if shift > 70:
            raise ValueError("protobuf varint is too long")
    raise ValueError("protobuf varint is truncated")


def _read_length_delimited(data: bytes, index: int) -> tuple[bytes, int]:
    length, index = _read_varint(data, index)
    end = index + length
    if end > len(data):
        raise ValueError("protobuf field is truncated")
    return data[index:end], end


def _read_string(data: bytes, index: int) -> tuple[str, int]:
    raw, index = _read_length_delimited(data, index)
    return raw.decode("utf-8", errors="ignore"), index


def _skip_field(data: bytes, index: int, wire_type: int) -> int:
    if wire_type == 0:
        _, index = _read_varint(data, index)
        return index
    if wire_type == 1:
        return min(len(data), index + 8)
    if wire_type == 2:
        _, index = _read_length_delimited(data, index)
        return index
    if wire_type == 5:
        return min(len(data), index + 4)
    raise ValueError(f"unsupported protobuf wire type {wire_type}")


class MexcSpotClient:
    def __init__(self, ws_base: str = "wss://wbs-api.mexc.com/ws", insecure_ssl: bool = False) -> None:
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
        params = [f"spot@public.aggre.deals.v3.api.pb@100ms@{symbol}" for symbol in symbols]
        async with websockets.connect(
            self.ws_base,
            ping_interval=20,
            ping_timeout=20,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            await ws.send(json.dumps({"method": "SUBSCRIPTION", "params": params}))
            logger.info("Connected MEXC spot trade stream for %s symbols", len(symbols))
            async for message in ws:
                if isinstance(message, str):
                    if "PING" in message.upper():
                        await ws.send(json.dumps({"method": "PONG"}))
                    continue
                for event in self.parse_ws_message(message):
                    yield event

    def parse_ws_message(self, message: bytes | str) -> list[TradeEvent]:
        if isinstance(message, str):
            return []
        try:
            wrapper = self._parse_wrapper(self._decode_bytes(message))
        except ValueError:
            logger.debug("Could not decode MEXC protobuf trade payload", exc_info=True)
            return []
        symbol = str(wrapper.get("symbol") or self._symbol_from_channel(str(wrapper.get("channel") or ""))).upper()
        if not symbol:
            return []
        event_time = int(wrapper.get("send_time") or wrapper.get("create_time") or 0)
        events: list[TradeEvent] = []
        for deal in wrapper.get("deals", []):
            price = float(deal.get("price") or 0)
            quantity = float(deal.get("quantity") or 0)
            if price <= 0 or quantity <= 0:
                continue
            trade_time = int(deal.get("time") or event_time)
            trade_type = int(deal.get("trade_type") or 0)
            events.append(
                TradeEvent(
                    symbol=symbol,
                    event_time=event_time or trade_time,
                    trade_time=trade_time,
                    price=price,
                    quantity=quantity,
                    quote_quantity=price * quantity,
                    taker_side=TakerSide.BUY if trade_type == 1 else TakerSide.SELL,
                    aggregate=True,
                    exchange="mexc",
                )
            )
        return events

    def _decode_bytes(self, message: bytes) -> bytes:
        if len(message) >= 2 and message[0] == 0x1F and message[1] == 0x8B:
            return gzip.decompress(message)
        return message

    def _parse_wrapper(self, data: bytes) -> dict:
        index = 0
        result: dict = {"deals": []}
        while index < len(data):
            tag, index = _read_varint(data, index)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if field_number == 1 and wire_type == 2:
                result["channel"], index = _read_string(data, index)
            elif field_number == 3 and wire_type == 2:
                result["symbol"], index = _read_string(data, index)
            elif field_number == 5 and wire_type == 0:
                result["create_time"], index = _read_varint(data, index)
            elif field_number == 6 and wire_type == 0:
                result["send_time"], index = _read_varint(data, index)
            elif field_number == 314 and wire_type == 2:
                raw, index = _read_length_delimited(data, index)
                result["deals"] = self._parse_public_aggre_deals(raw)
            else:
                index = _skip_field(data, index, wire_type)
        return result

    def _parse_public_aggre_deals(self, data: bytes) -> list[dict]:
        index = 0
        deals: list[dict] = []
        while index < len(data):
            tag, index = _read_varint(data, index)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if field_number == 1 and wire_type == 2:
                raw, index = _read_length_delimited(data, index)
                deals.append(self._parse_deal(raw))
            else:
                index = _skip_field(data, index, wire_type)
        return deals

    def _parse_deal(self, data: bytes) -> dict:
        index = 0
        deal: dict = {}
        while index < len(data):
            tag, index = _read_varint(data, index)
            field_number = tag >> 3
            wire_type = tag & 0x07
            if field_number == 1 and wire_type == 2:
                deal["price"], index = _read_string(data, index)
            elif field_number == 2 and wire_type == 2:
                deal["quantity"], index = _read_string(data, index)
            elif field_number == 3 and wire_type == 0:
                deal["trade_type"], index = _read_varint(data, index)
            elif field_number == 4 and wire_type == 0:
                deal["time"], index = _read_varint(data, index)
            else:
                index = _skip_field(data, index, wire_type)
        return deal

    def _symbol_from_channel(self, channel: str) -> str:
        return channel.rsplit("@", 1)[-1] if "@" in channel else ""
