import asyncio
import logging
import random
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from app.alerts.telegram import TelegramAlerter, send_telegram_alert
from app.core.config import Settings
from app.marketdata.binance import BinanceFuturesClient, chunked
from app.marketdata.discovery import ExchangeDiscoveryClient
from app.marketdata.mexc import MexcSpotClient
from app.marketdata.perp import BinancePerpClient, MexcPerpClient
from app.models.events import BookTickerEvent, KlineEvent, OpenInterestEvent, TradeEvent, SMCMarketStructure
from app.models.scanner import AlertEvent, LiquidationEvent, MetricSnapshot
from app.persistence.cache import RedisCache
from app.persistence.postgres import PostgresStore
from app.services.scoring import ExpansionScorer
from app.services.symbol_state import SymbolState, now_ms
from app.services.rolling import RollingDataManager
from app.services.smc_analyzer import SMCAnalyzer

logger = logging.getLogger(__name__)
Subscriber = Callable[[list[MetricSnapshot]], None]
AlertSubscriber = Callable[[AlertEvent], None]


def clamp_target(value: float) -> float:
    return max(1.0, min(30.0, float(value)))


class ScannerService:
    def __init__(
        self,
        settings: Settings,
        binance: BinanceFuturesClient,
        cache: RedisCache,
        store: PostgresStore,
        telegram: TelegramAlerter,
    ) -> None:
        self.settings = settings
        self.binance = binance
        self.mexc = MexcSpotClient(settings.mexc_spot_ws_base, insecure_ssl=settings.binance_insecure_ssl)
        self.binance_perp = BinancePerpClient(settings.binance_perp_ws_base, insecure_ssl=settings.binance_insecure_ssl)
        self.mexc_perp = MexcPerpClient(settings.mexc_perp_ws_base, insecure_ssl=settings.binance_insecure_ssl)
        self.discovery = ExchangeDiscoveryClient(insecure_ssl=settings.binance_insecure_ssl)
        self.cache = cache
        self.store = store
        self.telegram = telegram
        self.symbols = list(settings.symbols)
        self.states: dict[str, SymbolState] = {
            self._state_key("binance", symbol): SymbolState(symbol=symbol, exchange="binance") for symbol in self.symbols
        }
        self.scorer = ExpansionScorer(
            large_order_min_quote=settings.large_order_min_quote,
            large_order_avg_multiple=settings.large_order_avg_multiple,
            large_order_recent_seconds=settings.large_order_recent_seconds,
            large_order_min_volume_share_pct=settings.large_order_min_volume_share_pct,
            natr_min_5m_14=settings.natr_min_5m_14,
            target_move_pct=settings.target_move_pct,
            metal_targets_usd={
                "PAXGUSDT": settings.paxg_target_move_usd,
                "XAGUSDT": settings.xag_target_move_usd,
            },
        )
        self.perp_scorer = ExpansionScorer(
            large_order_min_quote=settings.large_order_min_quote * 1.25,
            large_order_avg_multiple=max(settings.large_order_avg_multiple, 6.0),
            large_order_recent_seconds=settings.large_order_recent_seconds,
            large_order_min_volume_share_pct=max(settings.large_order_min_volume_share_pct * 1.5, 5.0),
            natr_min_5m_14=settings.natr_min_5m_14,
            target_move_pct=settings.perp_target_move_pct,
        )
        self.rankings: list[MetricSnapshot] = []
        self.alerts: list[AlertEvent] = []
        self.perp_rankings: list[MetricSnapshot] = []
        self.perp_alerts: list[AlertEvent] = []
        self.liquidations: list[LiquidationEvent] = []
        self.signal_mode = "high_confidence"
        self.universe_mode = "auto"
        self.exchange_universes: dict[str, list[str]] = {
            "binance": list(self.symbols),
            "mexc": [],
        }
        self.mexc_symbols: list[str] = []
        self.perp_states: dict[str, SymbolState] = {}
        self.perp_exchange_universes: dict[str, list[str]] = {
            "binance": [],
            "mexc": [],
        }
        self.combo_exchange_universes: dict[str, list[str]] = {
            "binance": [],
            "mexc": [],
        }
        self.binance_perp_symbols: list[str] = []
        self.mexc_perp_symbols: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self._stream_tasks: list[asyncio.Task] = []
        self._perp_stream_tasks: list[asyncio.Task] = []
        self._streams_started = False
        self._perp_streams_started = False
        self._bootstrap_task: asyncio.Task | None = None
        self._symbol_lock = asyncio.Lock()
        self._running = False
        self._ranking_subscribers: set[asyncio.Queue] = set()
        self._alert_subscribers: set[asyncio.Queue] = set()
        self._perp_ranking_subscribers: set[asyncio.Queue] = set()
        self._perp_alert_subscribers: set[asyncio.Queue] = set()
        self._last_alert_by_symbol: dict[str, int] = defaultdict(int)
        self._last_perp_alert_by_symbol: dict[str, int] = defaultdict(int)
        self._exchange_status: dict[str, dict] = {}
        self.priority_watchlist: dict[str, set[str]] = {"binance": set(), "mexc": set()}
        self._universe_refresh_lock = asyncio.Lock()
        self._last_universe_refresh_ms = 0
        self._next_universe_refresh_ms = 0
        self._universe_refresh_count = 0
        self._universe_refresh_error = ""
        self._debug_counters: dict[str, int] = defaultdict(int)

        # V2 Structural Engine Instances Initializations
        self.v2_data_manager = RollingDataManager()
        self.v2_analyzer = SMCAnalyzer()

    def _enabled_spot(self, exchange: str) -> bool:
        return exchange.lower() in {ex.lower() for ex in self.settings.enabled_spot_exchanges}

    def _enabled_perp(self, exchange: str) -> bool:
        return exchange.lower() in {ex.lower() for ex in self.settings.enabled_perp_exchanges}

    def _assert_enabled_spot(self, exchange: str) -> None:
        if not self._enabled_spot(exchange):
            raise ValueError(f"{exchange.upper()} is disabled in this production build. Only Binance and MEXC are active.")

    def _assert_enabled_perp(self, exchange: str) -> None:
        if not self._enabled_perp(exchange):
            raise ValueError(f"{exchange.upper()} perp is disabled in this production build. Only Binance and MEXC are active.")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self.binance.connect()
        await self.cache.connect()
        await self.store.connect()
        await self.telegram.connect()
        await self._load_universe()
        try:
            await self.refresh_universe_once()
        except Exception:
            logger.exception("Initial common Spot+Perp universe refresh failed; continuing with fallback universe")
        await self._bootstrap_natr()
        async with self._symbol_lock:
            if not self._streams_started:
                await self._start_streams()
            if not self._perp_streams_started:
                await self._start_perp_streams()
        self._tasks.extend(
            [
                asyncio.create_task(self._poll_open_interest()),
                asyncio.create_task(self._roll_baselines()),
                asyncio.create_task(self._scan_loop()),
                asyncio.create_task(self._stream_health_loop()),
                asyncio.create_task(self._auto_universe_refresh_loop()),
            ]
        )

    async def stop(self) -> None:
        self._running = False
        await self._stop_streams()
        await self._stop_perp_streams()
        for task in self._tasks:
            task.cancel()
        if self._bootstrap_task:
            self._bootstrap_task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._bootstrap_task:
            await asyncio.gather(self._bootstrap_task, return_exceptions=True)
            self._bootstrap_task = None
        self._tasks.clear()
        await self.cache.close()
        await self.store.close()
        await self.telegram.close()
        await self.binance.close()

    async def replace_universe(self, symbols: list[str], market_caps: dict[str, float] | None = None) -> list[str]:
        cleaned = self._clean_symbols(symbols)
        if not cleaned:
            raise ValueError("No valid symbols found")
        cleaned = await self.binance.filter_spot_symbols(cleaned)
        cleaned = cleaned[: self.settings.max_symbols_per_exchange]
        if not cleaned:
            raise ValueError("No valid Binance Spot USDT symbols found")
        cleaned_caps = self._clean_market_caps(market_caps or {})
        async with self._symbol_lock:
            if cleaned == self.symbols:
                self.exchange_universes["binance"] = list(self.symbols)
                return self.symbols
            await self._stop_streams()
            self.symbols = cleaned
            self.states = {
                key: state for key, state in self.states.items() if not key.startswith("binance:")
            }
            self.states.update(
                {
                    self._state_key("binance", symbol): SymbolState(
                        symbol=symbol,
                        exchange="binance",
                        market_cap_usd=cleaned_caps.get(symbol, 0.0),
                    )
                    for symbol in self.symbols
                }
            )
            self.universe_mode = "custom"
            self.exchange_universes["binance"] = list(self.symbols)
            self.rankings = []
            self._broadcast_rankings([])
            await self.cache.publish_rankings([])
            if self._running:
                if not self._streams_started:
                    await self._start_streams()
                self._schedule_bootstrap_natr()
            logger.info("Loaded custom Spot universe with %s symbols", len(self.symbols))
        return self.symbols

    def universe_summary(self) -> dict:
        return {
            "count": len(self.symbols),
            "symbols": self.symbols,
            "mode": self.universe_mode,
            "exchanges": self.exchange_universe_summary(),
            "target_move_pct": self.scorer.target_move_pct,
        }

    def target_settings(self) -> dict:
        return {
            "target_move_pct": self.scorer.target_move_pct,
            "min": 1,
            "max": 30,
            "metal_min": 1,
            "metal_max": 100,
            "paxg_target_move_usd": self.scorer.metal_targets_usd.get("PAXGUSDT", 10.0),
            "xag_target_move_usd": self.scorer.metal_targets_usd.get("XAGUSDT", 10.0),
        }

    def update_target_settings(
        self,
        target_move_pct: float | None = None,
        paxg_target_move_usd: float | None = None,
        xag_target_move_usd: float | None = None,
    ) -> dict:
        if target_move_pct is not None:
            self.scorer.target_move_pct = clamp_target(target_move_pct)
        if paxg_target_move_usd is not None:
            self.scorer.metal_targets_usd["PAXGUSDT"] = max(1.0, min(100.0, float(paxg_target_move_usd)))
        if xag_target_move_usd is not None:
            self.scorer.metal_targets_usd["XAGUSDT"] = max(1.0, min(100.0, float(xag_target_move_usd)))
        self.rankings = []
        self._broadcast_rankings([])
        return self.target_settings()

    def perp_target_settings(self) -> dict:
        return {
            "target_move_pct": self.perp_scorer.target_move_pct,
            "min": 1,
            "max": 30,
        }

    def update_perp_target_settings(self, target_move_pct: float) -> dict:
        self.perp_scorer.target_move_pct = clamp_target(target_move_pct)
        self.perp_rankings = []
        self._broadcast_perp_rankings([])
        return self.perp_target_settings()

    def signal_mode_settings(self) -> dict:
        return {
            "mode": self.signal_mode,
            "label": "High Confidence" if self.signal_mode == "high_confidence" else "Balanced",
            "score_floor": 85 if self.signal_mode == "high_confidence" else self.settings.high_priority_score,
        }

    def advanced_signal_settings(self) -> dict:
        return {
            "desired_move_sensitivity": self.scorer.target_move_pct,
            "manipulation_sensitivity": self.scorer.manipulation_sensitivity,
            "retracement_percentage": self.scorer.retracement_percentage,
            "liquidity_sensitivity": self.scorer.liquidity_sensitivity_setting,
            "volume_shock_multiplier": self.scorer.volume_shock_multiplier,
            "market_cap_filter": self.scorer.market_cap_filter_millions,
        }

    def update_advanced_signal_settings(
        self,
        desired_move_sensitivity: float | None = None,
        manipulation_sensitivity: float | None = None,
        retracement_percentage: float | None = None,
        liquidity_sensitivity: float | None = None,
        volume_shock_multiplier: float | None = None,
        market_cap_filter: float | None = None,
    ) -> dict:
        if desired_move_sensitivity is not None:
            self.scorer.target_move_pct = clamp_target(desired_move_sensitivity)
            self.perp_scorer.target_move_pct = clamp_target(desired_move_sensitivity)
        if manipulation_sensitivity is not None:
            value = max(1.0, min(100.0, float(manipulation_sensitivity)))
            self.scorer.manipulation_sensitivity = value
            self.perp_scorer.manipulation_sensitivity = value
        if retracement_percentage is not None:
            value = max(30.0, min(50.0, float(retracement_percentage)))
            self.scorer.retracement_percentage = value
            self.perp_scorer.retracement_percentage = value
        if liquidity_sensitivity is not None:
            value = max(1.0, min(100.0, float(liquidity_sensitivity)))
            self.scorer.liquidity_sensitivity_setting = value
            self.perp_scorer.liquidity_sensitivity_setting = value
        if volume_shock_multiplier is not None:
            value = max(0.5, min(3.0, float(volume_shock_multiplier)))
            self.scorer.volume_shock_multiplier = value
            self.perp_scorer.volume_shock_multiplier = value
        if market_cap_filter is not None:
            value = max(0.0, min(10_000.0, float(market_cap_filter)))
            self.scorer.market_cap_filter_millions = value
            self.perp_scorer.market_cap_filter_millions = value
        self.rankings = []
        self.perp_rankings = []
        self._broadcast_rankings([])
        self._broadcast_perp_rankings([])
        return self.advanced_signal_settings()

    def update_signal_mode(self, mode: str) -> dict:
        clean_mode = re.sub(r"[^a-z_]", "", str(mode or "").lower())
        if clean_mode not in {"balanced", "high_confidence"}:
            raise ValueError("Signal mode must be balanced or high_confidence")
        self.signal_mode = clean_mode
        self.rankings = []
        self.perp_rankings = []
        self._broadcast_rankings([])
        self._broadcast_perp_rankings([])
        return self.signal_mode_settings()

    def perp_universe_summary(self) -> dict:
        return {
            "count": sum(len(symbols) for symbols in self.perp_exchange_universes.values()),
            "exchanges": self.perp_exchange_universe_summary(),
            "target_move_pct": self.perp_scorer.target_move_pct,
        }

    def combo_universe_summary(self) -> dict:
        return {
            "count": sum(len(symbols) for symbols in self.combo_exchange_universes.values()),
            "exchanges": self.combo_exchange_universe_summary(),
            "target_move_pct": self.scorer.target_move_pct,
        }

    def exchange_universe_summary(self) -> dict:
        return {ex: len(syms) for ex, syms in self.exchange_universes.items()}

    def perp_exchange_universe_summary(self) -> dict:
        return {ex: len(syms) for ex, syms in self.perp_exchange_universes.items()}

    def combo_exchange_universe_summary(self) -> dict:
        return {ex: len(syms) for ex, syms in self.combo_exchange_universes.items()}

    async def replace_exchange_universe(
        self, exchange: str, symbols: list[str], market_caps: dict[str, float] | None = None
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_spot(exchange_key)
        cleaned_symbols = self._clean_exchange_symbols(exchange_key, symbols)
        if exchange_key == "binance":
            cleaned_symbols = await self.binance.filter_spot_symbols(cleaned_symbols)
        elif exchange_key == "mexc":
            cleaned_symbols = await self.mexc.filter_spot_symbols(cleaned_symbols)
        cleaned_symbols = cleaned_symbols[: self.settings.max_symbols_per_exchange]
        if not cleaned_symbols:
            raise ValueError(f"No valid Spot USDT symbols found for {exchange.upper()}")
        cleaned_caps = self._clean_market_caps(market_caps or {})
        async with self._symbol_lock:
            await self._stop_streams()
            if exchange_key == "binance":
                self.symbols = cleaned_symbols
            elif exchange_key == "mexc":
                self.mexc_symbols = cleaned_symbols
            self.states = {
                key: state for key, state in self.states.items() if not key.startswith(f"{exchange_key}:")
            }
            self.states.update(
                {
                    self._state_key(exchange_key, symbol): SymbolState(
                        symbol=symbol,
                        exchange=exchange_key,
                        market_cap_usd=cleaned_caps.get(symbol, 0.0),
                    )
                    for symbol in cleaned_symbols
                }
            )
            self.universe_mode = "custom"
            self.exchange_universes[exchange_key] = list(cleaned_symbols)
            self.rankings = [r for r in self.rankings if r.exchange != exchange_key]
            self._broadcast_rankings([])
            await self.cache.publish_rankings([])
            if self._running:
                if not self._streams_started:
                    await self._start_streams()
                self._schedule_bootstrap_natr()
            logger.info("Loaded custom Spot universe for %s with %s symbols", exchange_key.upper(), len(cleaned_symbols))
        return self._exchange_summary(exchange_key, cleaned_symbols, active=True)

    async def replace_perp_universe(
        self, exchange: str, symbols: list[str], market_caps: dict[str, float] | None = None
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_perp(exchange_key)
        cleaned_symbols = self._clean_exchange_symbols(exchange_key, symbols)
        if exchange_key == "binance":
            cleaned_symbols = await self.binance_perp.filter_perp_symbols(cleaned_symbols)
        elif exchange_key == "mexc":
            cleaned_symbols = await self.mexc_perp.filter_perp_symbols(cleaned_symbols)
        cleaned_symbols = cleaned_symbols[: self.settings.max_symbols_per_exchange]
        if not cleaned_symbols:
            raise ValueError(f"No valid Perp USDT symbols found for {exchange.upper()}")
        cleaned_caps = self._clean_market_caps(market_caps or {})
        async with self._symbol_lock:
            await self._stop_perp_streams()
            if exchange_key == "binance":
                self.binance_perp_symbols = cleaned_symbols
            elif exchange_key == "mexc":
                self.mexc_perp_symbols = cleaned_symbols
            self.perp_states = {
                key: state for key, state in self.perp_states.items() if not key.startswith(f"{exchange_key}:")
            }
            self.perp_states.update(
                {
                    self._state_key(exchange_key, symbol): SymbolState(
                        symbol=symbol,
                        exchange=exchange_key,
                        market_cap_usd=cleaned_caps.get(symbol, 0.0),
                    )
                    for symbol in cleaned_symbols
                }
            )
            self.perp_exchange_universes[exchange_key] = list(cleaned_symbols)
            self.perp_rankings = [r for r in self.perp_rankings if r.exchange != exchange_key]
            self._broadcast_perp_rankings([])
            if self._running:
                if not self._perp_streams_started:
                    await self._start_perp_streams()
                self._schedule_bootstrap_natr()
            logger.info("Loaded custom Perp universe for %s with %s symbols", exchange_key.upper(), len(cleaned_symbols))
        return self._exchange_summary(exchange_key, cleaned_symbols, active=False)

    async def auto_spot_universe(self, exchange: str, limit: int | None = None) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_spot(exchange_key)
        limit = min(limit or self.settings.max_symbols_per_exchange, self.settings.max_symbols_per_exchange)
        symbols = await self.discovery.discover_spot_symbols(
            exchange_key,
            limit=limit,
            min_quote_volume=self.settings.universe_min_quote_volume,
            max_quote_volume=self.settings.universe_max_quote_volume,
            max_price=self.settings.universe_max_price,
            excluded_bases=set(self.settings.excluded_bases),
        )
        if not symbols:
            raise ValueError(f"No {exchange_key.upper()} spot symbols discovered")
        return await self.replace_exchange_universe(exchange_key, symbols)

    async def auto_perp_universe(self, exchange: str, limit: int | None = None) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_perp(exchange_key)
        limit = min(limit or self.settings.max_symbols_per_exchange, self.settings.max_symbols_per_exchange)
        symbols = await self.discovery.discover_perp_symbols(
            exchange_key,
            limit=limit,
            min_quote_volume=self.settings.universe_min_quote_volume,
            max_quote_volume=self.settings.universe_max_quote_volume,
            max_price=self.settings.universe_max_price,
            excluded_bases=set(self.settings.excluded_bases),
        )
        if not symbols:
            raise ValueError(f"No {exchange_key.upper()} perp symbols discovered")
        return await self.replace_perp_universe(exchange_key, symbols)

    async def auto_spot_perp_common_universe(self, limit: int | None = None) -> list[dict]:
        exchanges = [ex for ex in ["binance", "mexc"] if self._enabled_spot(ex) and self._enabled_perp(ex)]
        if not exchanges:
            raise ValueError("No enabled exchanges for unified auto-load")
        per_exchange_cap = min(limit or self.settings.max_symbols_per_exchange, self.settings.max_symbols_per_exchange)
        disc_limit = max(self.settings.max_symbols_per_exchange * 10, 5000)
        fetch_tasks = [
            self.discovery.discover_spot_symbols(
                ex,
                limit=disc_limit,
                min_quote_volume=self.settings.universe_min_quote_volume,
                max_quote_volume=self.settings.universe_max_quote_volume,
                max_price=self.settings.universe_max_price,
                excluded_bases=set(self.settings.excluded_bases),
            )
            for ex in exchanges
        ] + [
            self.discovery.discover_perp_symbols(
                ex,
                limit=disc_limit,
                min_quote_volume=self.settings.universe_min_quote_volume,
                max_quote_volume=self.settings.universe_max_quote_volume,
                max_price=self.settings.universe_max_price,
                excluded_bases=set(self.settings.excluded_bases),
            )
            for ex in exchanges
        ]
        fetched = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        results: list[dict] = []
        half = len(exchanges)
        for i, ex in enumerate(exchanges):
            spot_res = fetched[i]
            perp_res = fetched[half + i]
            if isinstance(spot_res, Exception) or isinstance(perp_res, Exception):
                logger.error(f"Failed to fetch auto universe pool for {ex.upper()}: spot={spot_res}, perp={perp_res}")
                continue
            perp_set = set(perp_res or [])
            common = [s for s in (spot_res or []) if s in perp_set][:per_exchange_cap]
            if not common:
                logger.warning(f"No common Spot+Perp symbols discovered for {ex.upper()}")
                continue
            await self.replace_exchange_universe(ex, common)
            await self.replace_perp_universe(ex, common)
            self.combo_exchange_universes[ex] = list(common)
            summary = self._exchange_summary(ex, common, active=True)
            summary.update({"mode": "spot_perp_common", "common_with_perp": True, "active": True})
            results.append(summary)
        if not results:
            raise ValueError("Common Spot+Perp intersection yielded empty set across all active venues.")
        self.universe_mode = "auto"
        return results

    async def upload_private_universe(
        self, exchange: str, uploaded_symbols: list[str], market_caps: dict[str, float] | None = None, limit: int = 700
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_spot(exchange_key)
        self._assert_enabled_perp(exchange_key)
        if not uploaded_symbols:
            raise ValueError("Private tracking list payload is empty")
        discovery_limit = max(limit * 10, 5000)
        perp_symbols = await self.discovery.discover_perp_symbols(
            exchange_key,
            limit=discovery_limit,
            min_quote_volume=0,
            max_quote_volume=0,
            max_price=self.settings.universe_max_price,
            excluded_bases=set(self.settings.excluded_bases),
        )
        perp_set = set(perp_symbols)
        common_symbols = [symbol for symbol in uploaded_symbols if symbol in perp_set][:limit]
        if not common_symbols:
            raise ValueError(f"No uploaded {exchange_key.upper()} symbols also exist on perp/futures")
        await self.replace_exchange_universe(exchange_key, common_symbols, market_caps)
        await self.replace_perp_universe(exchange_key, common_symbols, market_caps)
        self.combo_exchange_universes[exchange_key] = list(common_symbols)
        result = self._exchange_summary(exchange_key, common_symbols, active=True)
        result.update(
            {
                "mode": "spot_perp_common",
                "common_with_perp": True,
                "active": True,
                "status": "live_scanning",
                "uploaded": len(uploaded_symbols),
                "perp_discovered": len(perp_symbols),
            }
        )
        return result

    def _clean_exchange(self, exchange: str) -> str:
        return re.sub(r"[^a-z0-9]", "", exchange.lower().strip())

    def _state_key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def _clean_exchange_symbols(self, exchange: str, symbols: list[str]) -> list[str]:
        return self._clean_symbols(symbols)

    def _clean_symbols(self, symbols: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw_symbol in symbols:
            symbol = raw_symbol.upper().strip()
            if ":" in symbol:
                symbol = symbol.rsplit(":", 1)[-1]
            symbol = re.sub(r"[^A-Z0-9]", "", symbol)
            if not symbol:
                continue
            if not symbol.endswith("USDT"):
                symbol = f"{symbol}USDT"
            if symbol not in seen:
                seen.add(symbol)
                cleaned.append(symbol)
        return cleaned[: self.settings.custom_universe_max_symbols]

    def _clean_market_caps(self, market_caps: dict[str, float]) -> dict[str, float]:
        cleaned: dict[str, float] = {}
        for raw_symbol, raw_cap in market_caps.items():
            symbol_list = self._clean_symbols([raw_symbol])
            if not symbol_list:
                continue
            try:
                cap = float(raw_cap)
                cleaned[symbol_list[0]] = cap
            except (ValueError, TypeError):
                continue
        return cleaned

    def _exchange_summary(self, exchange: str, symbols: list[str], active: bool) -> dict:
        return {
            "exchange": exchange,
            "count": len(symbols),
            "symbols": symbols,
            "active": active,
            "timestamp": now_ms(),
        }

    async def add_to_priority_watchlist(self, exchange: str, symbol: str) -> dict:
        ex_key = self._clean_exchange(exchange)
        sym_list = self._clean_symbols([symbol])
        if not sym_list:
            raise ValueError("Invalid tracking target token specification")
        target = sym_list[0]
        if ex_key not in self.priority_watchlist:
            self.priority_watchlist[ex_key] = set()
        self.priority_watchlist[ex_key].add(target)
        logger.info(f"Added structural target {ex_key.upper()}:{target} directly to priority acceleration watchlist.")
        return {"exchange": ex_key, "symbol": target, "watchlist_count": len(self.priority_watchlist[ex_key])}

    async def remove_from_priority_watchlist(self, exchange: str, symbol: str) -> dict:
        ex_key = self._clean_exchange(exchange)
        sym_list = self._clean_symbols([symbol])
        if not sym_list:
            raise ValueError("Invalid target tracking symbol provided")
        target = sym_list[0]
        if ex_key in self.priority_watchlist and target in self.priority_watchlist[ex_key]:
            self.priority_watchlist[ex_key].remove(target)
        count = len(self.priority_watchlist.get(ex_key, set()))
        return {"exchange": ex_key, "symbol": target, "watchlist_count": count}

    def get_priority_watchlist(self) -> dict[str, list[str]]:
        return {ex: sorted(list(syms)) for ex, syms in self.priority_watchlist.items()}

    async def refresh_universe_once(self) -> dict:
        async with self._universe_refresh_lock:
            ts = now_ms()
            self._universe_refresh_count += 1
            try:
                summary = await self.auto_spot_perp_common_universe()
                self._last_universe_refresh_ms = ts
                self._universe_refresh_error = ""
                return {"status": "success", "refreshed_at": ts, "count": len(summary), "details": summary}
            except Exception as e:
                self._universe_refresh_error = str(e)
                logger.exception("On-demand common tracking intersection calculation boundary failed")
                raise

    def get_system_status_summary(self) -> dict:
        spot_dead = self._streams_started and any(task.done() for task in self._stream_tasks)
        perp_dead = self._perp_streams_started and any(task.done() for task in self._perp_stream_tasks)
        return {
            "running": self._running,
            "spot_streams_active": self._streams_started,
            "perp_streams_active": self._perp_streams_started,
            "spot_dead_tasks": spot_dead,
            "perp_dead_tasks": perp_dead,
            "spot_symbols": len(self.states),
            "perp_symbols": len(self.perp_states),
            "binance_active_symbols": len(self.exchange_universes.get("binance", [])),
            "mexc_active_symbols": len(self.exchange_universes.get("mexc", [])),
            "total_active_symbols": len(self.exchange_universes.get("binance", [])) + len(self.exchange_universes.get("mexc", [])),
            "rankings": len(self.rankings),
            "perp_rankings": len(self.perp_rankings),
            "exchange_status": self._exchange_status,
            "reconnect_count": sum(int(item.get("attempts", 0) or 0) for item in self._exchange_status.values()),
            "last_universe_refresh": self._last_universe_refresh_ms,
            "next_universe_refresh": self._next_universe_refresh_ms,
            "universe_refresh_count": self._universe_refresh_count,
            "universe_refresh_error": self._universe_refresh_error,
            "priority_watchlist_count": sum(len(symbols) for symbols in self.priority_watchlist.values()),
            "priority_watchlist": {exchange: len(symbols) for exchange, symbols in self.priority_watchlist.items()},
        }

    def scanner_debug_status(self) -> dict:
        return {
            "counters": dict(self._debug_counters),
            "memory_states": {
                "spot_keys": list(self.states.keys())[:5],
                "perp_keys": list(self.perp_states.keys())[:5],
                "total_spot": len(self.states),
                "total_perp": len(self.perp_states),
            },
            "timestamp": now_ms(),
        }

    async def _start_streams(self) -> None:
        if self._streams_started:
            return
        self._streams_started = True
        self._stream_tasks = []
        if self._enabled_spot("binance") and self.symbols:
            self._stream_tasks.append(asyncio.create_task(self._consume_binance_stream(list(self.symbols))))
        if self._enabled_spot("mexc") and self.mexc_symbols:
            self._stream_tasks.append(asyncio.create_task(self._consume_mexc_stream(list(self.mexc_symbols))))
        self._stream_tasks.append(asyncio.create_task(self._consume_liquidations()))
        logger.info("Spawned %s multi-exchange execution stream connections successfully.", len(self._stream_tasks))

    async def _stop_streams(self) -> None:
        self._streams_started = False
        for task in self._stream_tasks:
            task.cancel()
        await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        self._stream_tasks.clear()
        await self.binance.disconnect()
        await self.mexc.disconnect()

    async def _start_perp_streams(self) -> None:
        if self._perp_streams_started:
            return
        self._perp_streams_started = True
        self._perp_stream_tasks = []
        if self._enabled_perp("binance") and self.binance_perp_symbols:
            self._perp_stream_tasks.append(asyncio.create_task(self._consume_binance_perp_stream(list(self.binance_perp_symbols))))
        if self._enabled_perp("mexc") and self.mexc_perp_symbols:
            self._perp_stream_tasks.append(asyncio.create_task(self._consume_mexc_perp_stream(list(self.mexc_perp_symbols))))
        logger.info("Spawned %s structural futures perp execution streams successfully.", len(self._perp_stream_tasks))

    async def _stop_perp_streams(self) -> None:
        self._perp_streams_started = False
        for task in self._perp_stream_tasks:
            task.cancel()
        await asyncio.gather(*self._perp_stream_tasks, return_exceptions=True)
        self._perp_stream_tasks.clear()
        await self.binance_perp.disconnect()
        await self.mexc_perp.disconnect()

    async def _consume_binance_stream(self, symbols: list[str]) -> None:
        async for event in self.binance.stream_market_data(symbols):
            state = self.states.get(self._state_key(event.exchange, event.symbol))
            if not state:
                continue
            if isinstance(event, TradeEvent):
                self._debug_counters["trades_received"] += 1
                state.apply_trade(event)
                # Feed live trade vectors into the V2 tracking engine
                self.v2_data_manager.record_tick(event.symbol, event.price, event.quantity * event.price)
            elif isinstance(event, BookTickerEvent):
                state.apply_book_ticker(event)
            elif isinstance(event, KlineEvent):
                state.apply_kline(event)
                # Map completed candles dynamically to support structural analysis loops
                if event.interval == "1m":
                    self.v2_data_manager.update_candle(event.symbol, "1m", event.event_time, event.close, event.high, event.low, event.close, 0.0)
                elif event.interval == "1h":
                    self.v2_data_manager.update_candle(event.symbol, "1h", event.event_time, event.close, event.high, event.low, event.close, 0.0)

    async def _consume_mexc_stream(self, symbols: list[str]) -> None:
        async for event in self.mexc.stream_market_data(symbols):
            state = self.states.get(self._state_key(event.exchange, event.symbol))
            if not state:
                continue
            if isinstance(event, TradeEvent):
                self._debug_counters["trades_received"] += 1
                state.apply_trade(event)

    async def _consume_binance_perp_stream(self, symbols: list[str]) -> None:
        async for event in self.binance_perp.stream_market_data(symbols):
            state = self.perp_states.get(self._state_key(event.exchange, event.symbol))
            if not state:
                continue
            if isinstance(event, TradeEvent):
                self._debug_counters["perp_trades_received"] += 1
                state.apply_trade(event)
            elif isinstance(event, BookTickerEvent):
                state.apply_book_ticker(event)
            elif isinstance(event, KlineEvent):
                state.apply_kline(event)

    async def _consume_mexc_perp_stream(self, symbols: list[str]) -> None:
        async for event in self.mexc_perp.stream_market_data(symbols):
            state = self.perp_states.get(self._state_key(event.exchange, event.symbol))
            if state:
                if isinstance(event, TradeEvent):
                    self._debug_counters["perp_trades_received"] += 1
                    state.apply_trade(event)

    async def _consume_liquidations(self) -> None:
        async for event in self.binance.stream_liquidations(
            self.settings.binance_liquidation_ws_url,
            self.settings.liquidation_min_notional,
        ):
            self.liquidations.insert(0, event)
            self.liquidations = self.liquidations[:300]
            await self.cache.publish_liquidation(event)

    async def evaluate_ict_confluences(self, symbol: str, base_price: float) -> dict:
        """
        Runs the V2 math validations over the historical sliding windows to filter out
        Smart Money Traps (SMT) and establish clear institutional parameters.
        """
        ltf = self.v2_data_manager.get_candles(symbol, "1m")
        htf = self.v2_data_manager.get_candles(symbol, "1h")
        btc_ref = self.v2_data_manager.get_candles("BTCUSDT", "1m")

        analysis = self.v2_analyzer.analyze_structure(htf_candles=htf, ltf_candles=ltf, correlated_candles=btc_ref)
        
        if analysis and analysis.high_conviction:
            return {
                "confluences": SMCMarketStructure(
                    trend_bias=analysis.bias, 
                    fvg_detected=True, 
                    fvg_top=analysis.entry,
                    has_bos=True
                ),
                "confidence_score": 85.0,
                "entry_point": analysis.entry,
                "stop_loss": analysis.stop_loss,
                "take_profit": analysis.take_profit,
                "alert_reasoning": analysis.reasoning
            }
        
        return {
            "confluences": None,
            "confidence_score": 40.0,
            "entry_point": base_price,
            "stop_loss": None,
            "take_profit": None,
            "alert_reasoning": "Standard momentum breakout without multi-timeframe confirmation."
        }

    async def _poll_open_interest(self) -> None:
        if not self._enabled_perp("binance"):
            return
        await asyncio.sleep(5)
        while self._running:
            try:
                async with self._symbol_lock:
                    symbols = list(self.binance_perp_symbols)
                if symbols:
                    for batch in chunked(symbols, 50):
                        if not self._running:
                            break
                        tasks = [self.binance_perp.fetch_open_interest(s) for s in batch]
                        events = await asyncio.gather(*tasks, return_exceptions=True)
                        for event in events:
                            if isinstance(event, OpenInterestEvent):
                                state = self.perp_states.get(self._state_key(event.exchange, event.symbol))
                                if state:
                                    state.apply_open_interest(event)
                        await asyncio.sleep(1.0)
            except Exception:
                logger.exception("Failed to execute open interest aggregation step")
            await asyncio.sleep(self.settings.open_interest_poll_seconds)

    async def _roll_baselines(self) -> None:
        while self._running:
            await asyncio.sleep(60)
            async with self._symbol_lock:
                all_states = list(self.states.values()) + list(self.perp_states.values())
            for state in all_states:
                state.roll_minute_windows()

    async def _stream_health_loop(self) -> None:
        await asyncio.sleep(30)
        while self._running:
            await asyncio.sleep(5)
            spot_needs_restart = self._streams_started and any(task.done() for task in self._stream_tasks)
            perp_needs_restart = self._perp_streams_started and any(task.done() for task in self._perp_stream_tasks)
            if not spot_needs_restart and not perp_needs_restart:
                continue
            async with self._symbol_lock:
                if spot_needs_restart and self._streams_started and any(task.done() for task in self._stream_tasks):
                    logger.warning("Detected stopped spot stream task; restarting spot stream group")
                    await self._stop_streams()
                    await self._start_streams()
                if perp_needs_restart and self._perp_streams_started and any(task.done() for task in self._perp_stream_tasks):
                    logger.warning("Detected stopped perp stream task; restarting perp stream group")
                    await self._stop_perp_streams()
                    await self._start_perp_streams()

    async def _auto_universe_refresh_loop(self) -> None:
        refresh_seconds = max(30 * 60, min(60 * 60, int(self.settings.auto_universe_refresh_minutes) * 60))
        await asyncio.sleep(10)
        while self._running:
            if self._last_universe_refresh_ms and now_ms() < self._last_universe_refresh_ms + (refresh_seconds * 1000):
                self._next_universe_refresh_ms = self._last_universe_refresh_ms + (refresh_seconds * 1000)
                await asyncio.sleep(10)
                continue
            if self.universe_mode != "auto":
                await asyncio.sleep(60)
                continue
            try:
                await self.refresh_universe_once()
            except Exception:
                logger.exception("Automated rolling shared token intersection computation encountered boundaries")
            await asyncio.sleep(10)

    async def _scan_loop(self) -> None:
        await asyncio.sleep(2)
        while self._running:
            await asyncio.sleep(float(self.settings.scan_interval_seconds))
            try:
                async with self._symbol_lock:
                    spot_snapshots = [state.snapshot() for state in self.states.values()]
                    perp_snapshots = [state.snapshot() for state in self.perp_states.values()]
                self._exchange_status = {
                    "binance_spot": {"connected": self.binance.is_connected(), "attempts": self.binance._reconnect_attempts},
                    "mexc_spot": {"connected": self.mexc.is_connected(), "attempts": self.mexc._reconnect_attempts},
                    "binance_perp": {"connected": self.binance_perp.is_connected(), "attempts": self.binance_perp._reconnect_attempts},
                    "mexc_perp": {"connected": self.mexc_perp.is_connected(), "attempts": self.mexc_perp._reconnect_attempts},
                }
                computed_rankings = self.scorer.score_and_rank(spot_snapshots)
                self.rankings = computed_rankings
                self._broadcast_rankings(computed_rankings)
                await self.cache.publish_rankings(computed_rankings)
                await self._process_alerts(computed_rankings)
                computed_perp_rankings = self.perp_scorer.score_and_rank(perp_snapshots)
                self.perp_rankings = computed_perp_rankings
                self._broadcast_perp_rankings(computed_perp_rankings)
                await self._process_perp_alerts(computed_perp_rankings)
            except Exception:
                logger.exception("Error during execution of core scanning calculation iteration loop")

    async def _process_alerts(self, snapshots: list[MetricSnapshot]) -> None:
        ts = now_ms()
        score_floor = self._alert_score_floor()
        for snapshot in snapshots[:30]:
            if not self._meets_target_move(snapshot, self.settings.min_alert_expected_move_pct):
                continue
            if snapshot.move_category == "Watching":
                continue
            if snapshot.ignition_probability < score_floor:
                continue
            if not self._is_high_priority_spot(snapshot):
                continue
            alert_key = self._state_key(snapshot.exchange, snapshot.symbol)
            last_alert = self._last_alert_by_symbol[alert_key]
            if ts - last_alert < self.settings.alert_cooldown_seconds * 1000:
                continue

            # Compute V2 structural conditions before dispatching notification events
            v2_context = await self.evaluate_ict_confluences(snapshot.symbol, snapshot.price)

            alert = AlertEvent(
                symbol=snapshot.symbol,
                exchange=snapshot.exchange,
                direction=snapshot.direction,
                score=v2_context["confidence_score"] if v2_context["confluences"] else snapshot.ignition_probability,
                label="HIGH CONVICTION" if v2_context["confluences"] else snapshot.probability_label,
                expected_move=snapshot.expected_move,
                move_category=snapshot.move_category,
                snapshot=snapshot,
                created_at=ts,
                entry_price=v2_context["entry_point"],
                target_price=v2_context["take_profit"] or snapshot.target_price,
                stop_loss_price=v2_context["stop_loss"] or snapshot.stop_loss_price,
                last_price=snapshot.price,
                status="open",
            )
            self._last_alert_by_symbol[alert_key] = ts
            self.alerts.insert(0, alert)
            self.alerts = self.alerts[:100]
            self._broadcast_alert(alert)
            await self.cache.publish_alert(alert)
            await self.store.save_alert(alert)
            
            # Send High-Conviction notification to Telegram with reason narratives
            if v2_context["confidence_score"] >= 80.0:
                await send_telegram_alert(
                    symbol=snapshot.symbol,
                    side="BUY" if snapshot.direction == "Long" else "SELL",
                    price=snapshot.price,
                    raw_vol=snapshot.relative_volume,
                    smc_info={
                        "bias": v2_context["confluences"].trend_bias,
                        "entry": v2_context["entry_point"],
                        "stop_loss": v2_context["stop_loss"],
                        "take_profit": v2_context["take_profit"],
                        "reasoning": v2_context["alert_reasoning"]
                    }
                )
            else:
                await self.telegram.send_alert(alert)

    async def _process_perp_alerts(self, snapshots: list[MetricSnapshot]) -> None:
        ts = now_ms()
        score_floor = self._alert_score_floor()
        for snapshot in snapshots[:30]:
            if not self._meets_target_move(snapshot, self.settings.min_alert_expected_move_pct):
                continue
            if snapshot.move_category == "Watching":
                continue
            if snapshot.ignition_probability < score_floor:
                continue
            if not self._is_high_priority_perp(snapshot):
                continue
            alert_key = self._state_key(snapshot.exchange, snapshot.symbol)
            last_alert = self._last_perp_alert_by_symbol[alert_key]
            if ts - last_alert < self.settings.alert_cooldown_seconds * 1000:
                continue

            v2_context = await self.evaluate_ict_confluences(snapshot.symbol, snapshot.price)

            alert = AlertEvent(
                symbol=snapshot.symbol,
                exchange=snapshot.exchange,
                direction=snapshot.direction,
                score=v2_context["confidence_score"] if v2_context["confluences"] else snapshot.ignition_probability,
                label="HIGH CONVICTION" if v2_context["confluences"] else snapshot.probability_label,
                expected_move=snapshot.expected_move,
                move_category=snapshot.move_category,
                snapshot=snapshot,
                created_at=ts,
                entry_price=v2_context["entry_point"],
                target_price=v2_context["take_profit"] or snapshot.target_price,
                stop_loss_price=v2_context["stop_loss"] or snapshot.stop_loss_price,
                last_price=snapshot.price,
                status="open",
            )
            self._last_perp_alert_by_symbol[alert_key] = ts
            self.perp_alerts.insert(0, alert)
            self.perp_alerts = self.perp_alerts[:100]
            self._broadcast_perp_alert(alert)
            await self.cache.publish_perp_alert(alert)
            await self.store.save_alert(alert)

            if v2_context["confidence_score"] >= 80.0:
                await send_telegram_alert(
                    symbol=snapshot.symbol,
                    side="BUY" if snapshot.direction == "Long" else "SELL",
                    price=snapshot.price,
                    raw_vol=snapshot.relative_volume,
                    smc_info={
                        "bias": v2_context["confluences"].trend_bias,
                        "entry": v2_context["entry_point"],
                        "stop_loss": v2_context["stop_loss"],
                        "take_profit": v2_context["take_profit"],
                        "reasoning": v2_context["alert_reasoning"]
                    }
                )
            else:
                await self.telegram.send_alert(alert)

    def _alert_score_floor(self) -> float:
        if self.signal_mode == "high_confidence":
            return 85.0
        return float(self.settings.high_priority_score)

    def _meets_target_move(self, snapshot: MetricSnapshot, required_pct: float) -> bool:
        if snapshot.symbol in {"PAXGUSDT", "XAGUSDT"}:
            return True
        return snapshot.expected_move >= required_pct

    def _is_high_priority_spot(self, snapshot: MetricSnapshot) -> bool:
        if self.signal_mode == "high_confidence":
            return self._is_high_confidence_spot(snapshot)
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        return (
            snapshot.ignition_probability >= self.settings.high_priority_score
            and snapshot.relative_volume >= 1.5
            and has_taker_surge
        )

    def _is_high_confidence_spot(self, snapshot: MetricSnapshot) -> bool:
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        return (
            snapshot.ignition_probability >= 85.0
            and (snapshot.relative_volume >= 2.0 or not self.settings.recent_print_required_for_alert)
            and has_taker_surge
            and snapshot.liquidity_sensitivity >= 45.0
            and snapshot.expansion_efficiency >= 35.0
        )

    def _is_high_priority_perp(self, snapshot: MetricSnapshot) -> bool:
        if self.signal_mode == "high_confidence":
            return self._is_high_confidence_perp(snapshot)
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        return (
            snapshot.ignition_probability >= self.settings.high_priority_score
            and snapshot.relative_volume >= 1.5
            and has_taker_surge
        )

    def _is_high_confidence_perp(self, snapshot: MetricSnapshot) -> bool:
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        return (
            snapshot.ignition_probability >= 85.0
            and (snapshot.relative_volume >= 2.0 or not self.settings.recent_print_required_for_alert)
            and has_taker_surge
            and snapshot.liquidity_sensitivity >= 45.0
            and snapshot.expansion_efficiency >= 35.0
        )

    async def _load_universe(self) -> None:
        p = Path("app/data/binance_spot_symbols.txt")
        if p.exists():
            try:
                with p.open(encoding="utf-8") as f:
                    file_symbols = [line.strip() for line in f if line.strip() and not line.startswith("#")]
                filtered = await self.binance.filter_spot_symbols(file_symbols)
                if filtered:
                    self.symbols = filtered
                    self.states = {
                        key: state for key, state in self.states.items() if not key.startswith("binance:")
                    }
                    self.states.update({self._state_key("binance", symbol): SymbolState(symbol=symbol, exchange="binance") for symbol in self.symbols})
                    self.universe_mode = "configured"
                    self.exchange_universes["binance"] = list(self.symbols)
                    logger.info("Loaded configured Spot universe file with %s symbols", len(self.symbols))
                    return
            except Exception:
                logger.exception("Failed parsing static local universe baseline array template.")
        if self.settings.auto_low_cap_universe:
            try:
                discovered = await self.binance.fetch_low_cap_universe(
                    max_symbols=self.settings.universe_max_symbols,
                    min_quote_volume=self.settings.universe_min_quote_volume,
                    max_quote_volume=self.settings.universe_max_quote_volume,
                    max_price=self.settings.universe_max_price,
                    excluded_bases=set(self.settings.excluded_bases),
                )
                if discovered:
                    self.symbols = await self.binance.filter_spot_symbols(
                        self._clean_symbols([*discovered, *self.settings.metal_symbols])
                    )
                    self.states.update({self._state_key("binance", s): SymbolState(symbol=s, exchange="binance") for s in self.symbols})
                    self.universe_mode = "auto"
                    self.exchange_universes["binance"] = list(self.symbols)
                    logger.info("Loaded automated low cap Spot universe with %s symbols", len(self.symbols))
                    return
            except Exception:
                logger.exception("Failed fetching automated low-cap exchange candidates baseline portfolio.")
        logger.info("Falling back to static configuration settings string universe symbols")

    async def _bootstrap_natr(self) -> None:
        async with self._symbol_lock:
            all_spot = list(self.states.items())
            all_perp = list(self.perp_states.items())
        logger.info("Bootstrapping NATR values for %s spot and %s perp pairs...", len(all_spot), len(all_perp))
        for key, state in all_spot:
            try:
                candles = await self.binance.fetch_historical_klines(state.symbol, "5m", limit=30)
                state.bootstrap_natr_from_klines(candles)
            except Exception:
                pass
        for key, state in all_perp:
            try:
                candles = await self.binance_perp.fetch_historical_klines(state.symbol, "5m", limit=30)
                state.bootstrap_natr_from_klines(candles)
            except Exception:
                pass
        logger.info("NATR bootstrapping complete across active asset universes.")

    def _schedule_bootstrap_natr(self) -> None:
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()
        self._bootstrap_task = asyncio.create_task(self._bootstrap_natr())

    def subscribe_rankings(self, queue: asyncio.Queue) -> None:
        self._ranking_subscribers.add(queue)

    def unsubscribe_rankings(self, queue: asyncio.Queue) -> None:
        self._ranking_subscribers.discard(queue)

    def subscribe_alerts(self, queue: asyncio.Queue) -> None:
        self._alert_subscribers.add(queue)

    def unsubscribe_alerts(self, queue: asyncio.Queue) -> None:
        self._alert_subscribers.discard(queue)

    def subscribe_perp_rankings(self, queue: asyncio.Queue) -> None:
        self._perp_ranking_subscribers.add(queue)

    def unsubscribe_perp_rankings(self, queue: asyncio.Queue) -> None:
        self._perp_ranking_subscribers.discard(queue)

    def subscribe_perp_alerts(self, queue: asyncio.Queue) -> None:
        self._perp_alert_subscribers.add(queue)

    def unsubscribe_perp_alerts(self, queue: asyncio.Queue) -> None:
        self._perp_alert_subscribers.discard(queue)

    def _broadcast_rankings(self, rankings: list[MetricSnapshot]) -> None:
        for q in list(self._ranking_subscribers):
            try:
                q.put_nowait(rankings)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(rankings)
                except Exception:
                    pass

    def _broadcast_alert(self, alert: AlertEvent) -> None:
        for q in list(self._alert_subscribers):
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(alert)
                except Exception:
                    pass

    def _broadcast_perp_rankings(self, rankings: list[MetricSnapshot]) -> None:
        for q in list(self._perp_ranking_subscribers):
            try:
                q.put_nowait(rankings)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(rankings)
                except Exception:
                    pass

    def _broadcast_perp_alert(self, alert: AlertEvent) -> None:
        for q in list(self._perp_alert_subscribers):
            try:
                q.put_nowait(alert)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(alert)
                except Exception:
                    pass

async def dispatch_to_subscribers(self, event: VolumeSurgeEvent) -> None:
        """Broadcasts unified V2 data payloads to all active UI websockets."""
        if not self._alert_subscribers:
            return
        for queue in list(self._alert_subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass