from dataclasses import dataclass
from enum import StrEnum


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
