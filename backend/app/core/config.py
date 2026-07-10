from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    binance_futures_ws_base: str = "wss://stream.binance.com:9443/stream"
    binance_futures_rest_base: str = "https://api.binance.com"
    binance_liquidation_ws_url: str = "wss://fstream.binance.com/ws/!forceOrder@arr"
    binance_insecure_ssl: bool = True
    bybit_spot_ws_base: str = "wss://stream.bybit.com/v5/public/spot"
    bingx_spot_ws_base: str = "wss://open-api-ws.bingx.com/market"
    mexc_spot_ws_base: str = "wss://wbs-api.mexc.com/ws"
    binance_perp_ws_base: str = "wss://fstream.binance.com/stream"
    bybit_perp_ws_base: str = "wss://stream.bybit.com/v5/public/linear"
    bingx_perp_ws_base: str = "wss://open-api-swap.bingx.com/swap-market"
    mexc_perp_ws_base: str = "wss://contract.mexc.com/edge"
    symbols: Annotated[list[str], Field(default_factory=list)]
    universe_symbols_file: str = "app/data/binance_spot_symbols.txt"
    auto_low_cap_universe: bool = True
    # Production launch limits: keep scanner stable on Hostinger VPS.
    # Binance + MEXC only, max 700 symbols per exchange.
    universe_max_symbols: int = 700
    custom_universe_max_symbols: int = 700
    enabled_spot_exchanges: list[str] = Field(default_factory=lambda: ["binance", "mexc"])
    enabled_perp_exchanges: list[str] = Field(default_factory=lambda: ["binance", "mexc"])
    max_symbols_per_exchange: int = 700
    universe_min_quote_volume: float = 0
    universe_max_quote_volume: float = 2_000_000_000
    universe_max_price: float = 1_000.0
    excluded_bases: Annotated[
        list[str],
        Field(
            default_factory=lambda: [
                "BTC",
                "ETH",
                "BNB",
                "SOL",
                "XRP",
                "DOGE",
                "ADA",
                "AVAX",
                "LINK",
                "LTC",
                "BCH",
                "TRX",
                "DOT",
                "NEAR",
                "SUI",
                "TON",
                "APT",
                "ICP",
                "ETC",
                "XLM",
                "UNI",
                "AAVE",
                "MKR",
                "FIL",
                "ATOM",
                "USD1",
                "USDC",
                "FDUSD",
                "TUSD",
                "BUSD",
                "DAI",
                "USDP",
                "USDE",
                "RLUSD",
                "EUR",
                "EURI",
                "AEUR",
            ]
        ),
    ]
    include_individual_trade_stream: bool = False
    include_book_ticker_stream: bool = False
    include_kline_stream: bool = True
    bootstrap_natr_klines: bool = True
    natr_min_5m_14: float = 0.35
    target_move_pct: float = 10.0
    perp_target_move_pct: float = 10.0
    min_alert_expected_move_pct: float = 3.0
    auto_universe_refresh_minutes: int = 45
    paxg_target_move_usd: float = 10.0
    xag_target_move_usd: float = 10.0
    large_order_min_quote: float = 5_000
    large_order_avg_multiple: float = 5.0
    large_order_min_volume_share_pct: float = 3.0
    large_order_recent_seconds: int = 600
    recent_print_required_for_alert: bool = True
    stream_chunk_size: int = 50
    oi_poll_seconds: int = 0
    metal_symbols: Annotated[list[str], Field(default_factory=lambda: ["PAXGUSDT", "XAGUSDT"])]
    scan_interval_ms: int = 1000
    alert_cooldown_seconds: int = 180
    high_priority_score: float = 80.0
    liquidation_min_notional: float = 50_000
    redis_url: str | None = None
    database_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


    @field_validator("enabled_spot_exchanges", "enabled_perp_exchanges", mode="before")
    @classmethod
    def parse_enabled_exchanges(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [exchange.strip().lower() for exchange in value.split(",") if exchange.strip()]
        return [str(exchange).lower() for exchange in value]

    @field_validator("symbols", mode="before")
    @classmethod
    def parse_symbols(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
        return [symbol.upper() for symbol in value]

    @field_validator("metal_symbols", mode="before")
    @classmethod
    def parse_metal_symbols(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [symbol.strip().upper() for symbol in value.split(",") if symbol.strip()]
        return [symbol.upper() for symbol in value]

    @field_validator("excluded_bases", mode="before")
    @classmethod
    def parse_excluded_bases(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [base.strip().upper() for base in value.split(",") if base.strip()]
        return [base.upper() for base in value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
