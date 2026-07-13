import asyncio
import json
import logging
import ssl
from collections.abc import AsyncIterator, Iterable

import aiohttp
import websockets

from app.models.events import BookTickerEvent, KlineEvent, OpenInterestEvent, TakerSide, TradeEvent
from app.models.scanner import LiquidationEvent

logger = logging.getLogger(__name__)


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


class BinanceFuturesClient:
    def __init__(
        self,
        ws_base: str,
        rest_base: str,
        insecure_ssl: bool = False,
        include_individual_trade_stream: bool = False,
        include_book_ticker_stream: bool = False,
        include_kline_stream: bool = True,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.ws_base = ws_base.rstrip("/")
        self.rest_base = rest_base.rstrip("/")
        self.insecure_ssl = insecure_ssl
        self.include_individual_trade_stream = include_individual_trade_stream
        self.include_book_ticker_stream = include_book_ticker_stream
        self.include_kline_stream = include_kline_stream
        self._external_session = session
        self._session: aiohttp.ClientSession | None = session
        self._ssl_context = self._build_ssl_context()

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not self.insecure_ssl:
            return None
        return ssl._create_unverified_context()

    async def connect(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def close(self) -> None:
        if self._external_session is None and self._session:
            await self._session.close()
            self._session = None

    def combined_stream_url(self, symbols: Iterable[str]) -> str:
        streams: list[str] = []
        for symbol in symbols:
            lower = symbol.lower()
            streams.append(f"{lower}@trade")
            if self.include_kline_stream:
                streams.append(f"{lower}@kline_5m")
            if self.include_book_ticker_stream:
                streams.append(f"{lower}@bookTicker")
        return f"{self.ws_base}?streams={'/'.join(streams)}"

    async def fetch_low_cap_universe(
        self,
        *,
        max_symbols: int,
        min_quote_volume: float,
        max_quote_volume: float,
        max_price: float,
        excluded_bases: set[str],
    ) -> list[str]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        exchange_info_url = f"{self.rest_base}/api/v3/exchangeInfo"
        ticker_url = f"{self.rest_base}/api/v3/ticker/24hr"
        async with self._session.get(exchange_info_url, ssl=self._ssl_context) as exchange_response:
            exchange_response.raise_for_status()
            exchange_info = await exchange_response.json()
        async with self._session.get(ticker_url, ssl=self._ssl_context) as ticker_response:
            ticker_response.raise_for_status()
            tickers = await ticker_response.json()

        tradable = {
            item["symbol"]: item["baseAsset"]
            for item in exchange_info.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and item.get("isSpotTradingAllowed", True)
            and item.get("baseAsset") not in excluded_bases
        }
        candidates: list[tuple[float, str]] = []
        for ticker in tickers:
            symbol = ticker.get("symbol")
            if symbol not in tradable:
                continue
            price = float(ticker.get("lastPrice") or 0)
            quote_volume = float(ticker.get("quoteVolume") or 0)
            change_pct = abs(float(ticker.get("priceChangePercent") or 0))
            if price <= 0 or price > max_price:
                continue
            if quote_volume < min_quote_volume or quote_volume > max_quote_volume:
                continue
            activity_score = change_pct * 1_000_000 + quote_volume
            candidates.append((activity_score, symbol))

        candidates.sort(key=lambda item: item[0], reverse=True)
        return [symbol for _, symbol in candidates[:max_symbols]]

    async def filter_spot_symbols(self, symbols: list[str]) -> list[str]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        exchange_info_url = f"{self.rest_base}/api/v3/exchangeInfo"
        async with self._session.get(exchange_info_url, ssl=self._ssl_context) as exchange_response:
            exchange_response.raise_for_status()
            exchange_info = await exchange_response.json()
        requested = set(symbols)
        available = {
            item["symbol"]
            for item in exchange_info.get("symbols", [])
            if item.get("symbol") in requested
            and item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and item.get("isSpotTradingAllowed", True)
        }
        return [symbol for symbol in symbols if symbol in available]

    async def fetch_24h_tickers(self, symbols: list[str]) -> list[dict]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        url = f"{self.rest_base}/api/v3/ticker/24hr"
        async with self._session.get(url, params={"symbols": json.dumps(symbols, separators=(",", ":"))}, ssl=self._ssl_context) as response:
            response.raise_for_status()
            payload = await response.json()
        if not isinstance(payload, list):
            return []
        return [
            {
                "symbol": str(item.get("symbol", "")).upper(),
                "lastPrice": float(item.get("lastPrice") or 0),
                "priceChangePercent": float(item.get("priceChangePercent") or 0),
                "quoteVolume": float(item.get("quoteVolume") or 0),
            }
            for item in payload
            if item.get("symbol")
        ]

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[TradeEvent | BookTickerEvent | KlineEvent]:
        # Single-connect generator — no inner reconnect loop.
        # _stream_runner in scanner.py owns all retry/backoff logic.
        url = self.combined_stream_url(symbols)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_queue=50_000,
            ssl=self._ssl_context,
            open_timeout=20,
        ) as ws:
            logger.info("Connected Binance stream for %s symbols", len(symbols))
            async for message in ws:
                event = self.parse_ws_message(message)
                if event is not None:
                    yield event

    async def fetch_open_interest(self, symbol: str) -> OpenInterestEvent | None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        url = f"{self.rest_base}/fapi/v1/openInterest"
        try:
            async with self._session.get(url, params={"symbol": symbol}, ssl=self._ssl_context) as response:
                response.raise_for_status()
                payload = await response.json()
                return OpenInterestEvent(
                    symbol=payload["symbol"].upper(),
                    event_time=int(payload["time"]),
                    open_interest=float(payload["openInterest"]),
                )
        except Exception:
            logger.exception("Failed to fetch open interest for %s", symbol)
            return None

    async def fetch_5m_klines(self, symbol: str, limit: int = 16) -> list[KlineEvent]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        url = f"{self.rest_base}/api/v3/klines"
        async with self._session.get(url, params={"symbol": symbol, "interval": "5m", "limit": limit}, ssl=self._ssl_context) as response:
            response.raise_for_status()
            payload = await response.json()
        events: list[KlineEvent] = []
        for row in payload:
            events.append(
                KlineEvent(
                    symbol=symbol.upper(),
                    event_time=int(row[6]),
                    open_time=int(row[0]),
                    close_time=int(row[6]),
                    interval="5m",
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    closed=True,
                )
            )
        return events

    async def stream_liquidations(self, url: str, min_notional: float) -> AsyncIterator[LiquidationEvent]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_queue=10_000, ssl=self._ssl_context) as ws:
                    logger.info("Connected Binance liquidation stream")
                    backoff = 1.0
                    async for message in ws:
                        for event in self.parse_liquidation_message(message, min_notional):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Binance liquidation websocket disconnected; reconnecting")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.8, 30)

    def parse_ws_message(self, message: str) -> TradeEvent | BookTickerEvent | KlineEvent | None:
        payload = json.loads(message)
        data = payload.get("data", payload)
        event_type = data.get("e")
        if event_type in {"aggTrade", "trade"}:
            price = float(data["p"])
            quantity = float(data["q"])
            trade_time = int(data.get("T") or data.get("E"))
            buyer_is_maker = bool(data["m"])
            return TradeEvent(
                symbol=data["s"].upper(),
                event_time=int(data.get("E", trade_time)),
                trade_time=trade_time,
                price=price,
                quantity=quantity,
                quote_quantity=price * quantity,
                taker_side=TakerSide.SELL if buyer_is_maker else TakerSide.BUY,
                aggregate=event_type == "aggTrade",
            )
        if event_type == "bookTicker" or {"s", "b", "B", "a", "A"}.issubset(data):
            return BookTickerEvent(
                symbol=data["s"].upper(),
                event_time=int(data.get("E") or data.get("T") or 0),
                bid_price=float(data["b"]),
                bid_quantity=float(data["B"]),
                ask_price=float(data["a"]),
                ask_quantity=float(data["A"]),
            )
        if event_type == "kline":
            kline = data["k"]
            return KlineEvent(
                symbol=data["s"].upper(),
                event_time=int(data["E"]),
                open_time=int(kline["t"]),
                close_time=int(kline["T"]),
                interval=kline["i"],
                high=float(kline["h"]),
                low=float(kline["l"]),
                close=float(kline["c"]),
                closed=bool(kline["x"]),
            )
        return None

    def parse_liquidation_message(self, message: str, min_notional: float) -> list[LiquidationEvent]:
        payload = json.loads(message)
        rows = payload if isinstance(payload, list) else [payload]
        events: list[LiquidationEvent] = []
        for row in rows:
            data = row.get("o", row)
            symbol = str(data.get("s") or "").upper()
            side = str(data.get("S") or "").lower()
            price = float(data.get("ap") or data.get("p") or 0)
            quantity = float(data.get("q") or 0)
            event_time = int(row.get("E") or data.get("T") or 0)
            notional = price * quantity
            if not symbol or price <= 0 or quantity <= 0 or notional < min_notional:
                continue
            is_sell = side == "sell"
            severity = "monster" if notional >= 1_000_000 else "huge" if notional >= 250_000 else "large"
            events.append(
                LiquidationEvent(
                    symbol=symbol,
                    side=side,
                    price=price,
                    quantity=quantity,
                    notional=notional,
                    event_time=event_time,
                    hunt_side="downside" if is_sell else "upside",
                    reversal_bias="long reversal watch" if is_sell else "short reversal watch",
                    severity=severity,
                )
            )
        return events
