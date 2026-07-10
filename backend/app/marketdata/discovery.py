import asyncio
import logging
import math
import re
import ssl
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DiscoveredSymbol:
    symbol: str
    price: float = 0.0
    quote_volume: float = 0.0
    change_pct: float = 0.0
    score: float = 0.0


def _ssl_context(insecure_ssl: bool) -> ssl.SSLContext | None:
    return ssl._create_unverified_context() if insecure_ssl else None


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _clean_base(symbol: str) -> str:
    clean = re.sub(r"[^A-Z0-9]", "", symbol.upper())
    if clean.endswith("USDT"):
        return clean[:-4]
    return clean


def _symbol_from_pair(raw: str) -> str:
    clean = re.sub(r"[^A-Z0-9]", "", raw.upper())
    if not clean:
        return ""
    return clean if clean.endswith("USDT") else f"{clean}USDT"


def _rank_symbol(item: DiscoveredSymbol) -> float:
    price = item.price if item.price > 0 else 1.0
    volume_score = min(math.log10(max(item.quote_volume, 1.0)) * 9.0, 80.0)
    volatility_score = min(abs(item.change_pct) * 1.4, 70.0)
    price_sensitivity = 28.0 if price < 0.01 else 22.0 if price < 0.10 else 15.0 if price < 1 else 6.0 if price < 10 else 0.0
    return volume_score + volatility_score + price_sensitivity


class ExchangeDiscoveryClient:
    def __init__(self, insecure_ssl: bool = False) -> None:
        self._ssl_context = _ssl_context(insecure_ssl)

    async def discover_spot_symbols(
        self,
        exchange: str,
        *,
        limit: int,
        min_quote_volume: float,
        max_quote_volume: float,
        max_price: float,
        excluded_bases: set[str],
    ) -> list[str]:
        exchange_key = exchange.lower()
        if exchange_key == "binance":
            rows = await self._binance_spot()
        elif exchange_key == "bybit":
            rows = await self._bybit("spot")
        elif exchange_key == "mexc":
            rows = await self._mexc_spot()
        elif exchange_key == "bingx":
            rows = await self._bingx_spot()
        else:
            raise ValueError("Unsupported exchange")
        return self._refine(rows, limit, min_quote_volume, max_quote_volume, max_price, excluded_bases)

    async def discover_perp_symbols(
        self,
        exchange: str,
        *,
        limit: int,
        min_quote_volume: float,
        max_quote_volume: float,
        max_price: float,
        excluded_bases: set[str],
    ) -> list[str]:
        exchange_key = exchange.lower()
        if exchange_key == "binance":
            rows = await self._binance_perp()
        elif exchange_key == "bybit":
            rows = await self._bybit("linear")
        elif exchange_key == "mexc":
            rows = await self._mexc_perp()
        elif exchange_key == "bingx":
            rows = await self._bingx_perp()
        else:
            raise ValueError("Unsupported exchange")
        return self._refine(rows, limit, min_quote_volume, max_quote_volume, max_price, excluded_bases)

    def _refine(
        self,
        rows: list[DiscoveredSymbol],
        limit: int,
        min_quote_volume: float,
        max_quote_volume: float,
        max_price: float,
        excluded_bases: set[str],
    ) -> list[str]:
        blocked_suffixes = ("UP", "DOWN", "BULL", "BEAR")
        candidates: list[DiscoveredSymbol] = []
        seen: set[str] = set()
        for row in rows:
            symbol = _symbol_from_pair(row.symbol)
            base = _clean_base(symbol)
            if not symbol or symbol in seen or base in excluded_bases:
                continue
            if any(base.endswith(suffix) for suffix in blocked_suffixes):
                continue
            if row.price > 0 and row.price > max_price:
                continue
            if row.quote_volume > 0 and row.quote_volume < min_quote_volume:
                continue
            if max_quote_volume > 0 and row.quote_volume > max_quote_volume:
                continue
            row.symbol = symbol
            row.score = _rank_symbol(row)
            candidates.append(row)
            seen.add(symbol)
        candidates.sort(key=lambda item: (item.score, abs(item.change_pct), item.quote_volume), reverse=True)
        return [item.symbol for item in candidates[:limit]]

    async def _get_json(self, url: str, params: dict | None = None) -> Any:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, ssl=self._ssl_context) as response:
                response.raise_for_status()
                return await response.json(content_type=None)

    async def _binance_spot(self) -> list[DiscoveredSymbol]:
        exchange_info, tickers = await asyncio.gather(
            self._get_json("https://api.binance.com/api/v3/exchangeInfo"),
            self._get_json("https://api.binance.com/api/v3/ticker/24hr"),
        )
        allowed = {
            item.get("symbol")
            for item in exchange_info.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and item.get("isSpotTradingAllowed", True)
        }
        return [
            DiscoveredSymbol(
                symbol=str(item.get("symbol", "")).upper(),
                price=_float(item.get("lastPrice")),
                quote_volume=_float(item.get("quoteVolume")),
                change_pct=_float(item.get("priceChangePercent")),
            )
            for item in tickers
            if item.get("symbol") in allowed
        ]

    async def _binance_perp(self) -> list[DiscoveredSymbol]:
        exchange_info, tickers = await asyncio.gather(
            self._get_json("https://fapi.binance.com/fapi/v1/exchangeInfo"),
            self._get_json("https://fapi.binance.com/fapi/v1/ticker/24hr"),
        )
        allowed = {
            item.get("symbol")
            for item in exchange_info.get("symbols", [])
            if item.get("quoteAsset") == "USDT"
            and item.get("status") == "TRADING"
            and item.get("contractType") == "PERPETUAL"
        }
        return [
            DiscoveredSymbol(
                symbol=str(item.get("symbol", "")).upper(),
                price=_float(item.get("lastPrice")),
                quote_volume=_float(item.get("quoteVolume")),
                change_pct=_float(item.get("priceChangePercent")),
            )
            for item in tickers
            if item.get("symbol") in allowed
        ]

    async def _bybit(self, category: str) -> list[DiscoveredSymbol]:
        instruments = await self._bybit_instruments(category)
        tickers = await self._get_json("https://api.bybit.com/v5/market/tickers", {"category": category})
        ticker_by_symbol = {
            item.get("symbol"): item
            for item in tickers.get("result", {}).get("list", [])
            if item.get("symbol")
        }
        rows: list[DiscoveredSymbol] = []
        for item in instruments:
            symbol = str(item.get("symbol", "")).upper()
            quote = item.get("quoteCoin") or item.get("quoteCoinName")
            status = str(item.get("status") or "Trading").lower()
            if quote != "USDT" or "trading" not in status:
                continue
            ticker = ticker_by_symbol.get(symbol, {})
            rows.append(
                DiscoveredSymbol(
                    symbol=symbol,
                    price=_float(ticker.get("lastPrice")),
                    quote_volume=_float(ticker.get("turnover24h")),
                    change_pct=_float(ticker.get("price24hPcnt")) * 100,
                )
            )
        return rows

    async def _bybit_instruments(self, category: str) -> list[dict]:
        rows: list[dict] = []
        cursor = ""
        for _ in range(12):
            params = {"category": category}
            if cursor:
                params["cursor"] = cursor
            payload = await self._get_json("https://api.bybit.com/v5/market/instruments-info", params)
            result = payload.get("result", {})
            rows.extend(result.get("list", []))
            cursor = result.get("nextPageCursor") or ""
            if not cursor:
                break
        return rows

    async def _mexc_spot(self) -> list[DiscoveredSymbol]:
        exchange_info, tickers = await asyncio.gather(
            self._get_json("https://api.mexc.com/api/v3/exchangeInfo"),
            self._get_json("https://api.mexc.com/api/v3/ticker/24hr"),
        )
        allowed = set()
        for item in exchange_info.get("symbols", []):
            status = str(item.get("status") or "").upper()
            if item.get("quoteAsset") == "USDT" and status in {"1", "ENABLED", "TRADING"}:
                allowed.add(item.get("symbol"))
        return [
            DiscoveredSymbol(
                symbol=str(item.get("symbol", "")).upper(),
                price=_float(item.get("lastPrice")),
                quote_volume=_float(item.get("quoteVolume")),
                change_pct=_float(item.get("priceChangePercent")),
            )
            for item in tickers
            if item.get("symbol") in allowed
        ]

    async def _mexc_perp(self) -> list[DiscoveredSymbol]:
        details, tickers = await asyncio.gather(
            self._get_json("https://contract.mexc.com/api/v1/contract/detail"),
            self._get_json("https://contract.mexc.com/api/v1/contract/ticker"),
        )
        allowed = set()
        for item in details.get("data", []):
            symbol = str(item.get("symbol") or "").replace("_", "").upper()
            quote = item.get("quoteCoin") or item.get("quoteCoinName")
            state = _float(item.get("state"))
            if symbol.endswith("USDT") and quote in {"USDT", None, ""} and state in {0.0, 1.0}:
                allowed.add(symbol)
        ticker_rows = tickers.get("data", [])
        if isinstance(ticker_rows, dict):
            ticker_rows = list(ticker_rows.values())
        rows: list[DiscoveredSymbol] = []
        for item in ticker_rows:
            symbol = str(item.get("symbol") or "").replace("_", "").upper()
            if symbol not in allowed:
                continue
            price = _float(item.get("lastPrice") or item.get("last"))
            volume = _float(item.get("amount24") or item.get("turnover24") or item.get("volume24"))
            change = _float(item.get("riseFallRate") or item.get("change24")) * 100
            rows.append(DiscoveredSymbol(symbol=symbol, price=price, quote_volume=volume, change_pct=change))
        return rows

    async def _bingx_spot(self) -> list[DiscoveredSymbol]:
        symbols, tickers = await asyncio.gather(
            self._get_json("https://open-api.bingx.com/openApi/spot/v1/common/symbols"),
            self._get_json("https://open-api.bingx.com/openApi/spot/v1/ticker/24hr"),
        )
        symbol_rows = symbols.get("data", {}).get("symbols", symbols.get("data", []))
        allowed = set()
        for item in symbol_rows:
            raw_symbol = str(item.get("symbol") or item.get("tradingPair") or "").upper()
            quote = item.get("quoteAsset") or item.get("quoteCoin")
            status = str(item.get("status") or "1").lower()
            if raw_symbol.endswith("-USDT") or quote == "USDT":
                if status in {"1", "trading", "online", "true"}:
                    allowed.add(raw_symbol.replace("-", ""))
        ticker_rows = tickers.get("data", tickers)
        if isinstance(ticker_rows, dict):
            ticker_rows = ticker_rows.get("tickers", ticker_rows.get("ticker", []))
        return self._bingx_ticker_rows(ticker_rows, allowed)

    async def _bingx_perp(self) -> list[DiscoveredSymbol]:
        symbols, tickers = await asyncio.gather(
            self._get_json("https://open-api.bingx.com/openApi/swap/v2/quote/contracts"),
            self._get_json("https://open-api.bingx.com/openApi/swap/v2/quote/ticker"),
        )
        symbol_rows = symbols.get("data", [])
        allowed = set()
        for item in symbol_rows:
            raw_symbol = str(item.get("symbol") or "").upper()
            quote = item.get("quoteAsset") or item.get("quoteCoin")
            status = str(item.get("status") or "1").lower()
            if raw_symbol.endswith("-USDT") or quote == "USDT":
                if status in {"1", "trading", "online", "true"}:
                    allowed.add(raw_symbol.replace("-", ""))
        ticker_rows = tickers.get("data", tickers)
        if isinstance(ticker_rows, dict):
            ticker_rows = ticker_rows.get("tickers", ticker_rows.get("ticker", []))
        return self._bingx_ticker_rows(ticker_rows, allowed)

    def _bingx_ticker_rows(self, ticker_rows: Any, allowed: set[str]) -> list[DiscoveredSymbol]:
        if not isinstance(ticker_rows, list):
            return []
        rows: list[DiscoveredSymbol] = []
        for item in ticker_rows:
            symbol = str(item.get("symbol") or "").upper().replace("-", "")
            if symbol not in allowed:
                continue
            price = _float(item.get("lastPrice") or item.get("last") or item.get("close"))
            volume = _float(item.get("quoteVolume") or item.get("quoteVol") or item.get("turnover"))
            change = _float(item.get("priceChangePercent") or item.get("priceChangeRate"))
            if abs(change) <= 2:
                change *= 100
            rows.append(DiscoveredSymbol(symbol=symbol, price=price, quote_volume=volume, change_pct=change))
        return rows
