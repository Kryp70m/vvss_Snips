import asyncio
import logging
import random
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from app.alerts.telegram import TelegramAlerter
from app.core.config import Settings
from app.marketdata.binance import BinanceFuturesClient, chunked
from app.marketdata.discovery import ExchangeDiscoveryClient
from app.marketdata.mexc import MexcSpotClient
from app.marketdata.perp import BinancePerpClient, MexcPerpClient
from app.models.events import BookTickerEvent, KlineEvent, OpenInterestEvent, TradeEvent
from app.models.scanner import AlertEvent, LiquidationEvent, MetricSnapshot
from app.persistence.cache import RedisCache
from app.persistence.postgres import PostgresStore
from app.services.scoring import ExpansionScorer
from app.services.symbol_state import SymbolState, now_ms

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

    def liquidation_events(self) -> list[LiquidationEvent]:
        return self.liquidations[:500]

    def health_status(self) -> dict:
        spot_dead = sum(1 for task in self._stream_tasks if task.done())
        perp_dead = sum(1 for task in self._perp_stream_tasks if task.done())
        return {
            "running": self._running,
            "spot_stream_tasks": len(self._stream_tasks),
            "perp_stream_tasks": len(self._perp_stream_tasks),
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
        counters = dict(self._debug_counters)
        return {
            "trades_received": counters.get("trades_received", 0),
            "perp_trades_received": counters.get("perp_trades_received", 0),
            "volume_filter_pass_count": counters.get("volume_filter_pass_count", 0),
            "natr_filter_pass_count": counters.get("natr_filter_pass_count", 0),
            "big_print_pass_count": counters.get("big_print_pass_count", 0),
            "score_pass_count": counters.get("score_pass_count", 0),
            "final_ranking_count": len(self.rankings),
            "final_perp_ranking_count": len(self.perp_rankings),
            "last_scan_at": counters.get("last_scan_at", 0),
        }

    def move_category(self, snapshot: MetricSnapshot) -> str:
        expected = float(snapshot.expected_move_pct or snapshot.target_move_pct or 0.0)
        if expected >= 10.0:
            return "Expansion Move"
        if expected >= 5.0:
            return "Momentum Move"
        if expected >= 3.0:
            return "Precision Move"
        return "Watching"

    def _with_move_category(self, snapshot: MetricSnapshot) -> MetricSnapshot:
        return snapshot.model_copy(update={"move_category": self.move_category(snapshot)})

    def update_priority_watchlist(self, symbols: list[str] | dict[str, list[str]], exchange: str | None = None) -> dict:
        if isinstance(symbols, dict):
            for key in ("binance", "mexc"):
                self.priority_watchlist[key] = set(self._clean_symbols(symbols.get(key, [])))
        else:
            exchange_key = self._clean_exchange(exchange or "binance")
            self._assert_enabled_spot(exchange_key)
            self.priority_watchlist[exchange_key] = set(self._clean_symbols(symbols))
        return {
            "binance": sorted(self.priority_watchlist.get("binance", set())),
            "mexc": sorted(self.priority_watchlist.get("mexc", set())),
            "counts": {key: len(value) for key, value in self.priority_watchlist.items()},
            "count": sum(len(value) for value in self.priority_watchlist.values()),
        }

    def _watchlist_priority(self, snapshot: MetricSnapshot) -> bool:
        exchange = str(snapshot.exchange or "binance").replace("_perp", "").replace("_future", "").replace("_swap", "")
        return snapshot.symbol in self.priority_watchlist.get(exchange, set())

    async def replace_exchange_universe(
        self,
        exchange: str,
        symbols: list[str],
        market_caps: dict[str, float] | None = None,
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_spot(exchange_key)
        if exchange_key == "binance":
            cleaned = await self.replace_universe(symbols, market_caps)
            return self._exchange_summary("binance", cleaned, active=True)
        cleaned_caps = self._clean_market_caps(market_caps or {})
        if exchange_key == "mexc":
            cleaned = self.mexc.clean_symbols(symbols, self.settings.max_symbols_per_exchange)
            if not cleaned:
                raise ValueError("No valid MEXC Spot symbols found")
            async with self._symbol_lock:
                if cleaned == self.mexc_symbols:
                    return self._exchange_summary("mexc", self.exchange_universes.get("mexc", []), active=True)
                await self._stop_streams()
                self.mexc_symbols = cleaned
                self.exchange_universes["mexc"] = list(cleaned)
                self.states = {key: state for key, state in self.states.items() if not key.startswith("mexc:")}
                self.states.update(
                    {
                        self._state_key("mexc", symbol): SymbolState(
                            symbol=symbol,
                            exchange="mexc",
                            market_cap_usd=cleaned_caps.get(symbol, 0.0),
                        )
                        for symbol in cleaned
                    }
                )
                if self._running:
                    if not self._streams_started:
                        await self._start_streams()
            return self._exchange_summary("mexc", self.exchange_universes["mexc"], active=True)
        raise ValueError("Unsupported exchange")

    async def auto_exchange_universe(self, exchange: str, limit: int | None = None) -> dict:
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

    async def auto_common_spot_perp_universe(self, exchange: str, limit: int | None = None) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_spot(exchange_key)
        self._assert_enabled_perp(exchange_key)
        limit = min(limit or self.settings.max_symbols_per_exchange, self.settings.max_symbols_per_exchange)
        # IMPORTANT: discover a much larger pool first, THEN intersect Spot ∩ Perp.
        # If we only request the top 700 spot and top 700 perp separately, many valid
        # common coins are missed. We still LIVE-LOAD only `limit` symbols.
        discovery_limit = max(self.settings.max_symbols_per_exchange * 10, 5000)
        spot_symbols, perp_symbols = await asyncio.gather(
            self.discovery.discover_spot_symbols(
                exchange_key,
                limit=discovery_limit,
                min_quote_volume=self.settings.universe_min_quote_volume,
                max_quote_volume=self.settings.universe_max_quote_volume,
                max_price=self.settings.universe_max_price,
                excluded_bases=set(self.settings.excluded_bases),
            ),
            self.discovery.discover_perp_symbols(
                exchange_key,
                limit=discovery_limit,
                min_quote_volume=self.settings.universe_min_quote_volume,
                max_quote_volume=self.settings.universe_max_quote_volume,
                max_price=self.settings.universe_max_price,
                excluded_bases=set(self.settings.excluded_bases),
            ),
        )
        perp_set = set(perp_symbols)
        common_symbols = [symbol for symbol in spot_symbols if symbol in perp_set][:limit]
        if not common_symbols:
            raise ValueError(f"No common {exchange_key.upper()} spot + perp USDT symbols discovered")
        # Live-load both the spot and perp streams for this exchange. Volume remains
        # exchange-specific and old duplicate same-exchange streams are replaced.
        spot_summary = await (self.replace_universe(common_symbols) if exchange_key == "binance" else self.replace_exchange_universe(exchange_key, common_symbols))
        perp_summary = await self.replace_perp_universe(exchange_key, common_symbols)
        self.combo_exchange_universes[exchange_key] = list(common_symbols)
        result = self._exchange_summary(exchange_key, common_symbols, active=True)
        result.update(
            {
                "mode": "spot_perp_common",
                "common_with_perp": True,
                "active": True,
                "status": "live_scanning",
                "spot_discovered": len(spot_symbols),
                "perp_discovered": len(perp_symbols),
                "spot_loaded": spot_summary.get("count", len(common_symbols)) if isinstance(spot_summary, dict) else len(common_symbols),
                "perp_loaded": perp_summary.get("count", len(common_symbols)) if isinstance(perp_summary, dict) else len(common_symbols),
                "note": "Loaded max available common Spot+Perp coins up to the 700 cap. If count is below 700, the exchange does not currently provide 700 eligible common low-price/liquid symbols under filters.",
            }
        )
        return result

    async def auto_unified_universe(self, limit: int | None = None) -> dict:
        """
        Production one-click Spot+Perp auto-load for Binance + MEXC only.

        The previous 4-exchange version created too much websocket pressure for a VPS.
        This version discovers only the enabled exchanges, intersects Spot ∩ Perp per
        exchange, and live-loads those per-exchange universes separately so exchange
        volume remains independent.
        """
        exchanges = [ex for ex in ["binance", "mexc"] if self._enabled_spot(ex) and self._enabled_perp(ex)]
        if not exchanges:
            raise ValueError("No enabled exchanges for unified auto-load")
        per_exchange_cap = min(limit or self.settings.max_symbols_per_exchange, self.settings.max_symbols_per_exchange)
        # Discover a larger candidate pool first, then select up to 700 common coins.
        # This gives the UI the maximum possible Spot+Perp symbols instead of only
        # intersecting the first 700 spot candidates with the first 700 perp candidates.
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

        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
        n = len(exchanges)
        spot_results = results[:n]
        perp_results = results[n:]

        per_exchange_common: dict[str, list[str]] = {}
        exchange_meta: dict[str, list[str]] = {}
        unified_seen: set[str] = set()
        unified_ordered: list[str] = []

        for ex, spot_raw, perp_raw in zip(exchanges, spot_results, perp_results):
            spot_list: list[str] = spot_raw if isinstance(spot_raw, list) else []
            perp_list: list[str] = perp_raw if isinstance(perp_raw, list) else []
            perp_set = set(perp_list)
            common = [s for s in spot_list if s in perp_set][:per_exchange_cap]
            per_exchange_common[ex] = common
            for symbol in common:
                exchange_meta.setdefault(symbol, []).append(ex)
                if symbol not in unified_seen:
                    unified_seen.add(symbol)
                    unified_ordered.append(symbol)

        if not any(per_exchange_common.values()):
            raise ValueError("No Binance/MEXC symbols found on both Spot and Perp")

        # Load live spot/perp streams per exchange. Volume remains exchange-specific.
        for ex, common in per_exchange_common.items():
            if not common:
                continue
            try:
                if ex == "binance":
                    await self.replace_universe(common)
                else:
                    await self.replace_exchange_universe(ex, common)
            except Exception:
                logger.exception("Could not load %s spot universe during unified auto-load", ex)
            try:
                await self.replace_perp_universe(ex, common)
            except Exception:
                logger.exception("Could not load %s perp universe during unified auto-load", ex)
            self.combo_exchange_universes[ex] = list(common)

        final_symbols = unified_ordered
        return {
            "mode": "spot_perp_binance_mexc",
            "count": len(final_symbols),
            "symbols": final_symbols,
            "exchange_meta": {s: exchange_meta[s] for s in final_symbols},
            "per_exchange": {
                ex: {
                    "spot_discovered": len(spot_results[i]) if isinstance(spot_results[i], list) else 0,
                    "perp_discovered": len(perp_results[i]) if isinstance(perp_results[i], list) else 0,
                    "common": len(per_exchange_common.get(ex, [])),
                    "cap": per_exchange_cap,
                }
                for i, ex in enumerate(exchanges)
            },
            "status": "live_scanning",
        }

    async def replace_common_spot_perp_universe(
        self,
        exchange: str,
        symbols: list[str],
        market_caps: dict[str, float] | None = None,
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        limit = self.settings.custom_universe_max_symbols
        discovery_limit = max(limit * 10, 5000)
        uploaded_symbols = self._clean_symbols(symbols)
        if not uploaded_symbols:
            raise ValueError("No valid symbols found")
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
        # Uploaded private lists should also become live Spot+Perp scanner lists.
        spot_summary = await (self.replace_universe(common_symbols) if exchange_key == "binance" else self.replace_exchange_universe(exchange_key, common_symbols, market_caps))
        perp_summary = await self.replace_perp_universe(exchange_key, common_symbols, market_caps)
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
                "spot_loaded": spot_summary.get("count", len(common_symbols)) if isinstance(spot_summary, dict) else len(common_symbols),
                "perp_loaded": perp_summary.get("count", len(common_symbols)) if isinstance(perp_summary, dict) else len(common_symbols),
            }
        )
        return result

    async def replace_perp_universe(
        self,
        exchange: str,
        symbols: list[str],
        market_caps: dict[str, float] | None = None,
    ) -> dict:
        exchange_key = self._clean_exchange(exchange)
        self._assert_enabled_perp(exchange_key)
        if exchange_key not in self.perp_exchange_universes:
            raise ValueError("Unsupported perp exchange")

        if exchange_key == "binance":
            cleaned = self.binance_perp.clean_symbols(symbols, self.settings.max_symbols_per_exchange)
            display_symbols = list(cleaned)
        elif exchange_key == "mexc":
            cleaned = self.mexc_perp.clean_symbols(symbols, self.settings.max_symbols_per_exchange)
            display_symbols = [self.mexc_perp.display_symbol(symbol) for symbol in cleaned]
        else:
            raise ValueError("Unsupported perp exchange")

        if not cleaned:
            raise ValueError("No valid perp symbols found")
        cleaned_caps = self._clean_market_caps(market_caps or {})

        async with self._symbol_lock:
            if display_symbols == self.perp_exchange_universes.get(exchange_key, []):
                return self._exchange_summary(exchange_key, display_symbols, active=bool(getattr(self, f"{exchange_key}_perp_symbols")))
            await self._stop_perp_streams()
            setattr(self, f"{exchange_key}_perp_symbols", cleaned)
            self.perp_exchange_universes[exchange_key] = display_symbols
            state_prefix = f"{exchange_key}_perp:"
            self.perp_states = {key: state for key, state in self.perp_states.items() if not key.startswith(state_prefix)}
            self.perp_states.update(
                {
                    self._state_key(f"{exchange_key}_perp", symbol): SymbolState(
                        symbol=symbol,
                        exchange=f"{exchange_key}_perp",
                        market_cap_usd=cleaned_caps.get(symbol, 0.0),
                    )
                    for symbol in display_symbols
                }
            )
            self.perp_rankings = []
            self._broadcast_perp_rankings([])
            if self._running and not self._perp_streams_started:
                await self._start_perp_streams()
        logger.info("Loaded %s perp universe with %s symbols", exchange_key, len(display_symbols))
        return self._exchange_summary(exchange_key, display_symbols, active=True)

    def exchange_universe_summary(self) -> list[dict]:
        self.exchange_universes["binance"] = list(self.symbols)
        return [
            self._exchange_summary("binance", self.exchange_universes.get("binance", []), active=True),
            self._exchange_summary("mexc", self.exchange_universes.get("mexc", []), active=bool(self.mexc_symbols)),
        ]

    def perp_exchange_universe_summary(self) -> list[dict]:
        return [
            self._exchange_summary("binance", self.perp_exchange_universes.get("binance", []), active=bool(self.binance_perp_symbols)),
            self._exchange_summary("mexc", self.perp_exchange_universes.get("mexc", []), active=bool(self.mexc_perp_symbols)),
        ]

    def combo_exchange_universe_summary(self) -> list[dict]:
        summaries = [
            self._exchange_summary("binance", self.combo_exchange_universes.get("binance", []), active=bool(self.binance_perp_symbols and self.symbols)),
            self._exchange_summary("mexc", self.combo_exchange_universes.get("mexc", []), active=bool(self.mexc_perp_symbols and self.mexc_symbols)),
        ]
        for summary in summaries:
            summary.update(
                {
                    "mode": "spot_perp_common",
                    "common_with_perp": True,
                    "status": "list_ready",
                }
            )
        return summaries

    def _exchange_summary(self, exchange: str, symbols: list[str], active: bool) -> dict:
        return {
            "exchange": exchange,
            "count": len(symbols),
            "symbols": symbols,
            "active": active,
            "status": "live_scanning" if active else "list_ready",
        }

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
            symbol = self._clean_symbols([raw_symbol])
            if not symbol:
                continue
            try:
                cap = float(raw_cap)
            except (TypeError, ValueError):
                continue
            if cap > 0:
                cleaned[symbol[0]] = cap
        return cleaned

    async def _start_streams(self) -> None:
        if self._streams_started and self._stream_tasks and all(not task.done() for task in self._stream_tasks):
            return
        if self._stream_tasks:
            await self._stop_streams()
        self._stream_tasks = [
            asyncio.create_task(self._stream_runner("binance spot", symbols, self._consume_stream))
            for symbols in chunked(self.symbols, self.settings.stream_chunk_size)
        ]
        if self.mexc_symbols:
            self._stream_tasks.extend(
                asyncio.create_task(self._stream_runner("mexc spot", symbols, self._consume_mexc_stream))
                for symbols in chunked(self.mexc_symbols, min(self.settings.stream_chunk_size, 30))
            )
        self._streams_started = True

    async def _stop_streams(self) -> None:
        for task in self._stream_tasks:
            task.cancel()
        await asyncio.gather(*self._stream_tasks, return_exceptions=True)
        self._stream_tasks.clear()
        self._streams_started = False

    async def _start_perp_streams(self) -> None:
        if self._perp_streams_started and self._perp_stream_tasks and all(not task.done() for task in self._perp_stream_tasks):
            return
        if self._perp_stream_tasks:
            await self._stop_perp_streams()
        self._perp_stream_tasks = []
        if self.binance_perp_symbols:
            self._perp_stream_tasks.extend(
                asyncio.create_task(self._stream_runner("binance perp", symbols, self._consume_binance_perp_stream))
                for symbols in chunked(self.binance_perp_symbols, self.settings.stream_chunk_size)
            )
        if self.mexc_perp_symbols:
            self._perp_stream_tasks.extend(
                asyncio.create_task(self._stream_runner("mexc perp", symbols, self._consume_mexc_perp_stream))
                for symbols in chunked(self.mexc_perp_symbols, min(self.settings.stream_chunk_size, 30))
            )
        self._perp_streams_started = True

    async def _stream_runner(self, label: str, symbols: list[str], consumer) -> None:
        backoff = 1.0
        attempts = 0
        status_key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        while self._running:
            try:
                self._exchange_status[status_key] = {
                    "status": "connected",
                    "symbols": len(symbols),
                    "attempts": attempts,
                    "backoff_seconds": 0,
                    "updated_at": now_ms(),
                }
                await consumer(symbols)
                backoff = 1.0
                attempts = 0
            except asyncio.CancelledError:
                self._exchange_status[status_key] = {
                    "status": "stopped",
                    "symbols": len(symbols),
                    "attempts": attempts,
                    "backoff_seconds": 0,
                    "updated_at": now_ms(),
                }
                raise
            except Exception:
                attempts += 1
                sleep_for = min(backoff, 30.0) + random.uniform(0, min(2.0, backoff * 0.25))
                self._exchange_status[status_key] = {
                    "status": "reconnecting",
                    "symbols": len(symbols),
                    "attempts": attempts,
                    "backoff_seconds": round(sleep_for, 2),
                    "updated_at": now_ms(),
                }
                logger.exception("%s stream task crashed; reconnecting in %.1fs", label, sleep_for)
                await asyncio.sleep(sleep_for)
                backoff = min(backoff * 2.0, 30.0)

    async def _stream_health_loop(self) -> None:
        while self._running:
            await asyncio.sleep(30)
            # Check dead tasks OUTSIDE the lock — just a quick boolean scan
            spot_needs_restart = self._streams_started and any(task.done() for task in self._stream_tasks)
            perp_needs_restart = self._perp_streams_started and any(task.done() for task in self._perp_stream_tasks)
            if not spot_needs_restart and not perp_needs_restart:
                continue
            # Only grab the lock for the actual stop+start, not for the check
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
            if self._last_universe_refresh_ms and now_ms() < self._next_universe_refresh_ms:
                await asyncio.sleep(max(30, (self._next_universe_refresh_ms - now_ms()) / 1000))
                continue
            self._next_universe_refresh_ms = now_ms() + refresh_seconds * 1000
            try:
                await self.refresh_universe_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._universe_refresh_error = str(exc)[:240]
                logger.exception("Automatic universe refresh failed")
            await asyncio.sleep(refresh_seconds)

    async def refresh_universe_once(self) -> dict:
        if self._universe_refresh_lock.locked():
            return {"status": "already_running"}
        async with self._universe_refresh_lock:
            result = await self.auto_unified_universe(self.settings.max_symbols_per_exchange)
            self._last_universe_refresh_ms = now_ms()
            self._next_universe_refresh_ms = self._last_universe_refresh_ms + max(30 * 60, min(60 * 60, int(self.settings.auto_universe_refresh_minutes) * 60)) * 1000
            self._universe_refresh_count += 1
            self._universe_refresh_error = ""
            logger.info("Automatic universe refresh loaded %s total symbols", result.get("count"))
            return result

    async def _stop_perp_streams(self) -> None:
        for task in self._perp_stream_tasks:
            task.cancel()
        await asyncio.gather(*self._perp_stream_tasks, return_exceptions=True)
        self._perp_stream_tasks.clear()
        self._perp_streams_started = False

    def subscribe_rankings(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._ranking_subscribers.add(queue)
        if self.rankings:
            queue.put_nowait(self.rankings)
        return queue

    def unsubscribe_rankings(self, queue: asyncio.Queue) -> None:
        self._ranking_subscribers.discard(queue)

    def subscribe_alerts(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._alert_subscribers.add(queue)
        return queue

    def unsubscribe_alerts(self, queue: asyncio.Queue) -> None:
        self._alert_subscribers.discard(queue)

    def subscribe_perp_rankings(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._perp_ranking_subscribers.add(queue)
        if self.perp_rankings:
            queue.put_nowait(self.perp_rankings)
        return queue

    def unsubscribe_perp_rankings(self, queue: asyncio.Queue) -> None:
        self._perp_ranking_subscribers.discard(queue)

    def subscribe_perp_alerts(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=20)
        self._perp_alert_subscribers.add(queue)
        return queue

    def unsubscribe_perp_alerts(self, queue: asyncio.Queue) -> None:
        self._perp_alert_subscribers.discard(queue)

    async def _consume_stream(self, symbols: list[str]) -> None:
        async for event in self.binance.stream_market_data(symbols):
            state = self.states.get(self._state_key(event.exchange, event.symbol))
            if not state:
                continue
            if isinstance(event, TradeEvent):
                self._debug_counters["trades_received"] += 1
                state.apply_trade(event)
            elif isinstance(event, BookTickerEvent):
                state.apply_book_ticker(event)
            elif isinstance(event, KlineEvent):
                state.apply_kline(event)

    async def _consume_mexc_stream(self, symbols: list[str]) -> None:
        async for event in self.mexc.stream_market_data(symbols):
            state = self.states.get(self._state_key(event.exchange, event.symbol))
            if state:
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
            self.liquidations = self.liquidations[:500]

    async def _poll_open_interest(self) -> None:
        if self.settings.oi_poll_seconds <= 0:
            logger.info("Open interest polling disabled")
            return
        semaphore = asyncio.Semaphore(16)

        async def fetch_one(symbol: str) -> None:
            async with semaphore:
                event = await self.binance.fetch_open_interest(symbol)
                key = self._state_key(event.exchange, event.symbol) if isinstance(event, OpenInterestEvent) else ""
                if isinstance(event, OpenInterestEvent) and key in self.states:
                    self.states[key].apply_open_interest(event)

        while self._running:
            await asyncio.gather(*(fetch_one(s) for s in list(self.symbols)), return_exceptions=True)
            await asyncio.sleep(self.settings.oi_poll_seconds)

    async def _roll_baselines(self) -> None:
        while self._running:
            ts = now_ms()
            for state in self.states.values():
                state.roll_minute_baseline(ts)
            for state in self.perp_states.values():
                state.roll_minute_baseline(ts)
            await asyncio.sleep(60)

    async def _scan_loop(self) -> None:
        interval = self.settings.scan_interval_ms / 1000
        while self._running:
            async with self._symbol_lock:
                states = list(self.states.values())
                perp_states = list(self.perp_states.values())
            snapshots = [self._with_move_category(self.scorer.build_snapshot(state)) for state in states if state.price > 0]
            snapshots.sort(key=lambda item: (item.natr_5m_14, item.ignition_probability), reverse=True)
            self._debug_counters["volume_filter_pass_count"] = sum(1 for item in snapshots if item.relative_volume > 0)
            self._debug_counters["natr_filter_pass_count"] = sum(1 for item in snapshots if item.natr_5m_14 >= self.settings.natr_min_5m_14)
            self._debug_counters["big_print_pass_count"] = sum(1 for item in snapshots if item.last_big_order_quote > 0)
            self._debug_counters["score_pass_count"] = sum(1 for item in snapshots if item.ignition_probability >= self._alert_score_floor())
            self._debug_counters["last_scan_at"] = now_ms()
            # Keep a broad candidate pool.  Per-user target filtering happens in
            # websocket.py so User A's slider never affects User B.  Do not use
            # the singleton scorer target here for display/user filtering.
            self.rankings = [
                snapshot
                for snapshot in snapshots
                if snapshot.last_big_order_quote > 0 and self._meets_target_move(snapshot, 1.0)
            ]
            self.rankings.sort(key=lambda item: (self._watchlist_priority(item), item.ignition_probability), reverse=True)
            perp_snapshots = [self._with_move_category(self.perp_scorer.build_snapshot(state)) for state in perp_states if state.price > 0]
            self.perp_rankings = [
                snapshot
                for snapshot in perp_snapshots
                if snapshot.last_big_order_quote > 0 and self._meets_target_move(snapshot, 1.0)
            ]
            self.perp_rankings.sort(
                key=lambda item: (self._watchlist_priority(item), item.margin_pressure_score, item.ignition_probability, item.last_big_order_quote),
                reverse=True,
            )
            self._update_alert_statuses()
            self._update_perp_alert_statuses()
            await self.cache.publish_rankings(self.rankings)
            self._broadcast_rankings(self.rankings)
            self._broadcast_perp_rankings(self.perp_rankings)
            await self._maybe_alert(self.rankings)
            await self._maybe_perp_alert(self.perp_rankings)
            await asyncio.sleep(interval)

    async def _load_universe(self) -> None:
        file_symbols = self._load_symbols_file()
        if file_symbols:
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
            logger.warning("Configured Spot universe file had no tradable Binance USDT symbols")

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
                    self.states.update({self._state_key("binance", symbol): SymbolState(symbol=symbol, exchange="binance") for symbol in self.symbols})
                    self.universe_mode = "auto"
                    self.exchange_universes["binance"] = list(self.symbols)
                    logger.info("Loaded low-cap universe with %s symbols", len(self.symbols))
                    return
            except Exception:
                logger.exception("Failed to load low-cap universe; falling back to configured symbols")

        if not self.symbols:
            self.symbols = [
                "PEPEUSDT",
                "WIFUSDT",
                "POPCATUSDT",
                "MEMEUSDT",
                "TURBOUSDT",
                "NEIROUSDT",
                "BONKUSDT",
                "FLOKIUSDT",
                "ORDIUSDT",
                "BIGTIMEUSDT",
            ]
        self.symbols = self._clean_symbols(self.symbols)
        self.states.update({self._state_key("binance", symbol): SymbolState(symbol=symbol, exchange="binance") for symbol in self.symbols})
        self.universe_mode = "configured"
        self.exchange_universes["binance"] = list(self.symbols)

    def _load_symbols_file(self) -> list[str]:
        path = Path(self.settings.universe_symbols_file)
        if not path.exists():
            return []
        return self._clean_symbols(path.read_text(encoding="utf-8").splitlines())

    async def _bootstrap_natr(self) -> None:
        if not self.settings.bootstrap_natr_klines:
            return
        semaphore = asyncio.Semaphore(16)

        async def bootstrap_symbol(symbol: str) -> None:
            state = self.states.get(self._state_key("binance", symbol))
            if not state:
                return
            async with semaphore:
                try:
                    for event in await self.binance.fetch_5m_klines(symbol, limit=16):
                        state.apply_kline(event)
                except Exception:
                    logger.exception("Failed to bootstrap 5m NATR for %s", symbol)

        await asyncio.gather(*(bootstrap_symbol(symbol) for symbol in self.symbols))

    def _schedule_bootstrap_natr(self) -> None:
        if not self.settings.bootstrap_natr_klines:
            return
        if self._bootstrap_task and not self._bootstrap_task.done():
            self._bootstrap_task.cancel()
        self._bootstrap_task = asyncio.create_task(self._bootstrap_natr())

    def _broadcast_rankings(self, snapshots: list[MetricSnapshot]) -> None:
        for queue in list(self._ranking_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshots)

    def _broadcast_perp_rankings(self, snapshots: list[MetricSnapshot]) -> None:
        for queue in list(self._perp_ranking_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(snapshots)

    async def _maybe_alert(self, snapshots: list[MetricSnapshot]) -> None:
        ts = now_ms()
        score_floor = self._alert_score_floor()
        for snapshot in snapshots[:20]:
            if not self._meets_target_move(snapshot, self.settings.min_alert_expected_move_pct):
                continue
            if self.move_category(snapshot) == "Watching":
                continue
            if snapshot.ignition_probability < score_floor:
                continue
            if not self._is_high_priority(snapshot):
                continue
            alert_key = self._state_key(snapshot.exchange, snapshot.symbol)
            last_alert = self._last_alert_by_symbol[alert_key]
            if ts - last_alert < self.settings.alert_cooldown_seconds * 1000:
                continue
            alert = AlertEvent(
                symbol=snapshot.symbol,
                exchange=snapshot.exchange,
                direction=snapshot.direction,
                score=snapshot.ignition_probability,
                label=snapshot.probability_label,
                expected_move=snapshot.expected_move,
                move_category=snapshot.move_category,
                snapshot=snapshot,
                created_at=ts,
                entry_price=snapshot.entry_price,
                target_price=snapshot.target_price,
                stop_loss_price=snapshot.stop_loss_price,
                last_price=snapshot.price,
                status="open",
                status_at=ts,
            )
            self._last_alert_by_symbol[alert_key] = ts
            self.alerts.insert(0, alert)
            self.alerts = self.alerts[:200]
            await self.cache.publish_alert(alert)
            await self.store.save_alert(alert)
            await self.telegram.send(alert)
            self._broadcast_alert(alert)

    async def _maybe_perp_alert(self, snapshots: list[MetricSnapshot]) -> None:
        ts = now_ms()
        score_floor = self._alert_score_floor()
        for snapshot in snapshots[:30]:
            if not self._meets_target_move(snapshot, self.settings.min_alert_expected_move_pct):
                continue
            if self.move_category(snapshot) == "Watching":
                continue
            if snapshot.ignition_probability < score_floor:
                continue
            if not self._is_high_priority_perp(snapshot):
                continue
            alert_key = self._state_key(snapshot.exchange, snapshot.symbol)
            last_alert = self._last_perp_alert_by_symbol[alert_key]
            if ts - last_alert < self.settings.alert_cooldown_seconds * 1000:
                continue
            alert = AlertEvent(
                symbol=snapshot.symbol,
                exchange=snapshot.exchange,
                direction=snapshot.direction,
                score=snapshot.ignition_probability,
                label=snapshot.probability_label,
                expected_move=snapshot.expected_move,
                move_category=snapshot.move_category,
                snapshot=snapshot,
                created_at=ts,
                entry_price=snapshot.entry_price,
                target_price=snapshot.target_price,
                stop_loss_price=snapshot.stop_loss_price,
                last_price=snapshot.price,
                status="open",
                status_at=ts,
            )
            self._last_perp_alert_by_symbol[alert_key] = ts
            self.perp_alerts.insert(0, alert)
            self.perp_alerts = self.perp_alerts[:200]
            await self.telegram.send(alert)
            self._broadcast_perp_alert(alert)

    def _update_alert_statuses(self) -> None:
        ts = now_ms()
        changed: list[AlertEvent] = []
        for index, alert in enumerate(self.alerts):
            state = self.states.get(self._state_key(alert.exchange, alert.symbol))
            if not state or state.price <= 0 or alert.status != "open":
                continue
            last_price = state.price
            hit_tp = (
                alert.direction == "long"
                and alert.target_price > 0
                and last_price >= alert.target_price
            ) or (
                alert.direction == "short"
                and alert.target_price > 0
                and last_price <= alert.target_price
            )
            hit_sl = (
                alert.direction == "long"
                and alert.stop_loss_price > 0
                and last_price <= alert.stop_loss_price
            ) or (
                alert.direction == "short"
                and alert.stop_loss_price > 0
                and last_price >= alert.stop_loss_price
            )
            if not hit_tp and not hit_sl:
                self.alerts[index] = alert.model_copy(update={"last_price": last_price})
                continue
            new_status = "tp_hit" if hit_tp else "sl_hit"
            updated = alert.model_copy(update={"last_price": last_price, "status": new_status, "status_at": ts})
            self.alerts[index] = updated
            changed.append(updated)
        for alert in changed:
            self._broadcast_alert(alert)

    def _update_perp_alert_statuses(self) -> None:
        ts = now_ms()
        changed: list[AlertEvent] = []
        for index, alert in enumerate(self.perp_alerts):
            state = self.perp_states.get(self._state_key(alert.exchange, alert.symbol))
            if not state or state.price <= 0 or alert.status != "open":
                continue
            last_price = state.price
            hit_tp = (
                alert.direction == "long"
                and alert.target_price > 0
                and last_price >= alert.target_price
            ) or (
                alert.direction == "short"
                and alert.target_price > 0
                and last_price <= alert.target_price
            )
            hit_sl = (
                alert.direction == "long"
                and alert.stop_loss_price > 0
                and last_price <= alert.stop_loss_price
            ) or (
                alert.direction == "short"
                and alert.stop_loss_price > 0
                and last_price >= alert.stop_loss_price
            )
            if not hit_tp and not hit_sl:
                self.perp_alerts[index] = alert.model_copy(update={"last_price": last_price})
                continue
            new_status = "tp_hit" if hit_tp else "sl_hit"
            updated = alert.model_copy(update={"last_price": last_price, "status": new_status, "status_at": ts})
            self.perp_alerts[index] = updated
            changed.append(updated)
        for alert in changed:
            self._broadcast_perp_alert(alert)

    def _is_high_priority(self, snapshot: MetricSnapshot) -> bool:
        if self.signal_mode == "high_confidence":
            return self._is_high_confidence_spot(snapshot)
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        return (
            (snapshot.last_big_order_quote > 0 or not self.settings.recent_print_required_for_alert)
            and snapshot.relative_volume >= 2
            and has_taker_surge
            and snapshot.liquidity_sensitivity >= 45
            and snapshot.expansion_efficiency >= 35
        )

    def _is_high_priority_perp(self, snapshot: MetricSnapshot) -> bool:
        if self.signal_mode == "high_confidence":
            return self._is_high_confidence_perp(snapshot)
        has_taker_surge = snapshot.aggressive_buy_flow in {"High", "Extreme"} or snapshot.aggressive_sell_flow in {"High", "Extreme"}
        has_pressure = snapshot.margin_pressure_score >= 55 or snapshot.last_big_order_multiple >= snapshot.required_order_multiple
        return (
            snapshot.last_big_order_quote > 0
            and has_taker_surge
            and has_pressure
            and snapshot.last_big_order_volume_share_pct >= max(snapshot.required_volume_share_pct * 0.75, 3.0)
            and snapshot.expansion_efficiency >= 25
        )

    def _alert_score_floor(self) -> float:
        return max(self.settings.high_priority_score, 85.0) if self.signal_mode == "high_confidence" else self.settings.high_priority_score

    @staticmethod
    def _meets_target_move(snapshot: MetricSnapshot, target_move_pct: float) -> bool:
        if snapshot.target_move_usd > 0:
            return snapshot.last_big_order_quote > 0 and snapshot.expected_move_pct > 0
        return snapshot.expected_move_pct + 1e-9 >= target_move_pct

    def _is_high_confidence_spot(self, snapshot: MetricSnapshot) -> bool:
        dominant_ratio = max(snapshot.taker_buy_ratio, snapshot.taker_sell_ratio)
        return (
            snapshot.last_big_order_quote > 0
            and snapshot.ignition_probability >= 85
            and snapshot.impact_score >= 55
            and snapshot.distribution_strength >= 55
            and snapshot.continuation_probability >= 50
            and (snapshot.manipulation_probability < 85 or snapshot.manipulation_phase == "Complete")
            and (
                self.scorer.market_cap_filter_millions <= 0
                or snapshot.market_cap_usd <= 0
                or snapshot.market_cap_usd <= self.scorer.market_cap_filter_millions * 1_000_000
            )
            and snapshot.impulse_confirmation == "Confirmed"
            and snapshot.impulse_efficiency >= 55
            and snapshot.relative_volume >= 3
            and dominant_ratio >= 0.66
            and snapshot.last_big_order_quote >= max(snapshot.required_order_quote * 1.10, snapshot.required_order_quote + 1)
            and snapshot.last_big_order_multiple >= snapshot.required_order_multiple
            and snapshot.last_big_order_volume_share_pct >= max(snapshot.required_volume_share_pct, 4.0)
            and snapshot.last_big_order_age_seconds <= 240
            and snapshot.expansion_efficiency >= 40
            and snapshot.liquidity_sensitivity >= 45
            and (snapshot.spread_bps <= 25 or snapshot.spread_bps == 0)
            and not snapshot.no_chase
        )

    def _is_high_confidence_perp(self, snapshot: MetricSnapshot) -> bool:
        dominant_ratio = max(snapshot.taker_buy_ratio, snapshot.taker_sell_ratio)
        return (
            snapshot.last_big_order_quote > 0
            and snapshot.ignition_probability >= 85
            and snapshot.impact_score >= 55
            and snapshot.distribution_strength >= 50
            and snapshot.continuation_probability >= 48
            and (snapshot.manipulation_probability < 85 or snapshot.manipulation_phase == "Complete")
            and (
                self.perp_scorer.market_cap_filter_millions <= 0
                or snapshot.market_cap_usd <= 0
                or snapshot.market_cap_usd <= self.perp_scorer.market_cap_filter_millions * 1_000_000
            )
            and snapshot.impulse_confirmation == "Confirmed"
            and snapshot.impulse_efficiency >= 50
            and snapshot.relative_volume >= 3
            and dominant_ratio >= 0.66
            and snapshot.margin_pressure_score >= 60
            and snapshot.last_big_order_multiple >= snapshot.required_order_multiple
            and snapshot.last_big_order_volume_share_pct >= max(snapshot.required_volume_share_pct * 0.9, 4.0)
            and snapshot.last_big_order_age_seconds <= 240
            and snapshot.expansion_efficiency >= 35
            and (snapshot.spread_bps <= 25 or snapshot.spread_bps == 0)
            and not snapshot.no_chase
        )

    def _broadcast_alert(self, alert: AlertEvent) -> None:
        for queue in list(self._alert_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(alert)

    def _broadcast_perp_alert(self, alert: AlertEvent) -> None:
        for queue in list(self._perp_alert_subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(alert)
