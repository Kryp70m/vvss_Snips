import asyncio
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass

import aiohttp
import websockets

from app.models.events import TakerSide, TradeEvent
from app.models.scanner import WhaleTradeEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class HyperliquidMarket:
    display_symbol: str
    subscription_coin: str


class HyperliquidClient:
    def __init__(
        self,
        ws_base: str = "wss://api.hyperliquid.xyz/ws",
        rest_base: str = "https://api.hyperliquid.xyz",
        insecure_ssl: bool = False,
    ) -> None:
        self.ws_base = ws_base
        self.rest_base = rest_base.rstrip("/")
        self.insecure_ssl = insecure_ssl
        self._session: aiohttp.ClientSession | None = None
        self._ssl_context = ssl._create_unverified_context() if insecure_ssl else None

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def resolve_spot_symbols(self, symbols: list[str]) -> list[HyperliquidMarket]:
        meta = await self.fetch_spot_meta()
        tokens = {item["index"]: item for item in meta.get("tokens", [])}
        markets: list[HyperliquidMarket] = []
        seen: set[str] = set()
        for symbol in symbols:
            market = self._resolve_symbol(symbol, meta.get("universe", []), tokens)
            if market and market.subscription_coin not in seen:
                seen.add(market.subscription_coin)
                markets.append(market)
        return markets

    async def fetch_spot_meta(self) -> dict:
        await self.connect()
        assert self._session is not None
        async with self._session.post(
            f"{self.rest_base}/info",
            json={"type": "spotMeta"},
            ssl=self._ssl_context,
        ) as response:
            response.raise_for_status()
            return await response.json()

    def _resolve_symbol(self, symbol: str, universe: list[dict], tokens: dict[int, dict]) -> HyperliquidMarket | None:
        normalized = self._normalize_symbol(symbol)
        for market in universe:
            display = self._display_name(market, tokens)
            market_name = str(market.get("name", "")).upper()
            if normalized in {market_name, display}:
                return HyperliquidMarket(display_symbol=display, subscription_coin=market_name)
        if "/" not in normalized and not normalized.startswith("@"):
            normalized = f"{normalized}/USDC"
        base = normalized.split("/", 1)[0] if "/" in normalized else normalized
        quote = normalized.split("/", 1)[1] if "/" in normalized else "USDC"
        for market in universe:
            token_names = [str(tokens.get(index, {}).get("name", "")).upper() for index in market.get("tokens", [])]
            if len(token_names) >= 2 and token_names[0] == base and token_names[1] == quote:
                return HyperliquidMarket(display_symbol=f"{base}/{quote}", subscription_coin=str(market.get("name", "")).upper())
        return None

    def _display_name(self, market: dict, tokens: dict[int, dict]) -> str:
        token_names = [str(tokens.get(index, {}).get("name", "")).upper() for index in market.get("tokens", [])]
        if len(token_names) >= 2 and token_names[0] and token_names[1]:
            return f"{token_names[0]}/{token_names[1]}"
        return str(market.get("name", "")).upper()

    def _normalize_symbol(self, symbol: str) -> str:
        normalized = symbol.upper().strip()
        if ":" in normalized:
            normalized = normalized.rsplit(":", 1)[-1]
        normalized = normalized.replace("_", "/").replace("-", "/")
        return re.sub(r"[^A-Z0-9@/]", "", normalized)

    async def stream_market_data(self, markets: list[HyperliquidMarket]) -> AsyncIterator[TradeEvent]:
        backoff = 1.0
        display_by_coin = {market.subscription_coin: market.display_symbol for market in markets}
        while True:
            try:
                async with websockets.connect(self.ws_base, ping_interval=20, ping_timeout=20, max_queue=10_000, ssl=self._ssl_context) as ws:
                    for market in markets:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": market.subscription_coin}}))
                    logger.info("Connected Hyperliquid trade stream for %s spot symbols", len(markets))
                    backoff = 1.0
                    async for message in ws:
                        for event in self.parse_ws_message(message, display_by_coin):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Hyperliquid websocket disconnected; reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30)

    async def stream_whale_trades(self, coins: list[str], min_notional: float) -> AsyncIterator[WhaleTradeEvent]:
        backoff = 1.0
        clean_coins = [coin.upper().strip() for coin in coins if coin.strip()]
        while True:
            try:
                async with websockets.connect(self.ws_base, ping_interval=20, ping_timeout=20, max_queue=10_000, ssl=self._ssl_context) as ws:
                    for coin in clean_coins:
                        await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))
                    logger.info("Connected Hyperliquid whale trade stream for %s symbols", len(clean_coins))
                    backoff = 1.0
                    async for message in ws:
                        for event in self.parse_whale_message(message, min_notional):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Hyperliquid whale websocket disconnected; reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30)

    def parse_ws_message(self, message: str, display_by_coin: dict[str, str] | None = None) -> list[TradeEvent]:
        payload = json.loads(message)
        if payload.get("channel") != "trades":
            return []
        display_by_coin = display_by_coin or {}
        events: list[TradeEvent] = []
        for item in payload.get("data", []):
            coin = str(item.get("coin", "")).upper()
            symbol = display_by_coin.get(coin, coin)
            price = float(item.get("px") or 0)
            quantity = float(item.get("sz") or 0)
            if price <= 0 or quantity <= 0:
                continue
            side = TakerSide.BUY if item.get("side") == "B" else TakerSide.SELL
            trade_time = int(item.get("time") or 0)
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
                    exchange="hyperliquid",
                )
            )
        return events

    def parse_whale_message(self, message: str, min_notional: float) -> list[WhaleTradeEvent]:
        payload = json.loads(message)
        if payload.get("channel") != "trades":
            return []
        events: list[WhaleTradeEvent] = []
        for item in payload.get("data", []):
            symbol = str(item.get("coin", "")).upper()
            price = float(item.get("px") or 0)
            quantity = float(item.get("sz") or 0)
            notional = price * quantity
            if price <= 0 or quantity <= 0 or notional < min_notional:
                continue
            side = "buy" if item.get("side") == "B" else "sell"
            severity = "monster" if notional >= 2_000_000 else "huge" if notional >= 500_000 else "large"
            events.append(
                WhaleTradeEvent(
                    source="hyperliquid",
                    symbol=symbol,
                    side=side,
                    price=price,
                    quantity=quantity,
                    notional=notional,
                    event_time=int(item.get("time") or 0),
                    venue="Hyperliquid",
                    bias="large buyer active" if side == "buy" else "large seller active",
                    severity=severity,
                )
            )
        return events
