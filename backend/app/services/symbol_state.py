from collections import deque
from dataclasses import dataclass, field
from time import time

from app.models.events import BookTickerEvent, KlineEvent, OpenInterestEvent, TakerSide, TradeEvent
from app.services.rolling import RollingWindow


def now_ms() -> int:
    return int(time() * 1000)


@dataclass(slots=True)
class SymbolState:
    symbol: str
    exchange: str = "binance"
    price: float = 0.0
    last_update_ms: int = 0
    prices: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    quote_volume: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    buy_quote_volume: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    sell_quote_volume: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    trade_quote_sizes: RollingWindow = field(default_factory=lambda: RollingWindow(1500))
    big_order_quote: RollingWindow = field(default_factory=lambda: RollingWindow(200))
    big_order_side: str = "none"
    big_order_time_ms: int = 0
    big_order_price: float = 0.0
    big_order_multiple: float = 0.0
    big_order_candidate_quote: float = 0.0
    one_minute_volume_history: RollingWindow = field(default_factory=lambda: RollingWindow(240))
    spreads_bps: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    top_book_depth: RollingWindow = field(default_factory=lambda: RollingWindow(900))
    open_interest: RollingWindow = field(default_factory=lambda: RollingWindow(240))
    five_minute_candles: deque[tuple[int, float, float, float]] = field(default_factory=lambda: deque(maxlen=32))
    natr_5m_14: float = 0.0
    market_cap_usd: float = 0.0

    def apply_trade(self, event: TradeEvent) -> None:
        self.price = event.price
        self.last_update_ms = event.trade_time
        self.prices.add(event.trade_time, event.price)
        self.quote_volume.add(event.trade_time, event.quote_quantity)
        avg_trade_size = self.trade_quote_sizes.mean()
        multiple = event.quote_quantity / avg_trade_size if avg_trade_size > 0 else 0.0
        self.trade_quote_sizes.add(event.trade_time, event.quote_quantity)
        if event.taker_side == TakerSide.BUY:
            self.buy_quote_volume.add(event.trade_time, event.quote_quantity)
        else:
            self.sell_quote_volume.add(event.trade_time, event.quote_quantity)
        self.big_order_quote.add(event.trade_time, event.quote_quantity)
        if avg_trade_size > 0:
            # Find the true best order in the rolling 300 s window — never reset on age alone.
            # This prevents rankings from wiping every 5 min when a single tiny trade
            # replaced the candidate after the old timestamp expired.
            window_start = event.trade_time - 300_000
            window_quotes = [(ts, q) for ts, q in self.big_order_quote.values if ts >= window_start]
            if window_quotes:
                best_ts, best_quote = max(window_quotes, key=lambda x: x[1])
                # Only update the stored candidate if this trade or any window trade beats it
                if event.quote_quantity >= self.big_order_candidate_quote or best_quote > self.big_order_candidate_quote:
                    # Use the current trade's metadata if it's the best, otherwise keep
                    # the existing metadata but update the quote to reflect the true window max
                    if event.quote_quantity >= best_quote:
                        self.big_order_side = event.taker_side.value
                        self.big_order_time_ms = event.trade_time
                        self.big_order_price = event.price
                        self.big_order_multiple = multiple
                        self.big_order_candidate_quote = event.quote_quantity
                    elif best_quote > self.big_order_candidate_quote:
                        # The window has a better trade we haven't recorded yet
                        self.big_order_candidate_quote = best_quote
                        self.big_order_time_ms = best_ts

    def apply_book_ticker(self, event: BookTickerEvent) -> None:
        mid = (event.bid_price + event.ask_price) / 2 if event.bid_price and event.ask_price else 0.0
        if mid <= 0:
            return
        ts = event.event_time or now_ms()
        self.price = mid
        self.prices.add(ts, mid)
        spread_bps = ((event.ask_price - event.bid_price) / mid) * 10_000
        depth = event.bid_price * event.bid_quantity + event.ask_price * event.ask_quantity
        self.spreads_bps.add(ts, max(spread_bps, 0.0))
        self.top_book_depth.add(ts, depth)
        self.last_update_ms = ts

    def apply_open_interest(self, event: OpenInterestEvent) -> None:
        self.open_interest.add(event.event_time, event.open_interest)
        self.last_update_ms = event.event_time

    def apply_kline(self, event: KlineEvent) -> None:
        if event.interval != "5m":
            return
        self.price = event.close
        self.last_update_ms = event.event_time
        current = (event.open_time, event.high, event.low, event.close)
        if self.five_minute_candles and self.five_minute_candles[-1][0] == event.open_time:
            self.five_minute_candles[-1] = current
        else:
            self.five_minute_candles.append(current)
        self.natr_5m_14 = self._calculate_natr(period=14)

    def _calculate_natr(self, period: int) -> float:
        candles = list(self.five_minute_candles)
        if len(candles) < period + 1:
            return 0.0
        ranges: list[float] = []
        for index in range(len(candles) - period, len(candles)):
            _, high, low, close = candles[index]
            previous_close = candles[index - 1][3]
            ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        close = candles[-1][3]
        if close <= 0:
            return 0.0
        return (sum(ranges) / period / close) * 100

    def roll_minute_baseline(self, timestamp_ms: int | None = None) -> None:
        ts = timestamp_ms or now_ms()
        one_min_ago = ts - 60_000
        current = self.quote_volume.sum_since(one_min_ago)
        if current > 0:
            self.one_minute_volume_history.add(ts, current)
