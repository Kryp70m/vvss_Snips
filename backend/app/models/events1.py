from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Literal
from pydantic import BaseModel, Field

class TakerSide(StrEnum):
    BUY = "buy"
    SELL = "sell"

@dataclass(slots=True)
class TradeEvent:
    symbol: str
    event_time: int
    trade_time: int
    price: float
    quantity: float
    quote_quantity: float
    taker_side: TakerSide
    aggregate: bool
    exchange: str = "binance"

@dataclass(slots=True)
class BookTickerEvent:
    symbol: str
    event_time: int
    bid_price: float
    bid_quantity: float
    ask_price: float
    ask_quantity: float
    exchange: str = "binance"

@dataclass(slots=True)
class OpenInterestEvent:
    symbol: str
    event_time: int
    open_interest: float
    exchange: str = "binance"

@dataclass(slots=True)
class KlineEvent:
    symbol: str
    event_time: int
    open_time: int
    close_time: int
    interval: str
    high: float
    low: float
    close: float
    closed: bool
    exchange: str = "binance"

class SMCMarketStructure(BaseModel):
    """Advanced structural market matrix metrics for V2 ICT confluences."""
    trend_bias: Literal["BULLISH", "BEARISH", "NEUTRAL"]
    has_bos: bool = False
    has_choch: bool = False
    fvg_detected: bool = False
    fvg_top: Optional[float] = None
    fvg_bottom: Optional[float] = None
    order_block_detected: bool = False
    ob_zone_high: Optional[float] = None
    ob_zone_low: Optional[float] = None