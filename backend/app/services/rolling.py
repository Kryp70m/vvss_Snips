import collections
from collections import deque
from dataclasses import dataclass, field
from statistics import mean, pstdev
from typing import Dict, List, Tuple

@dataclass(slots=True)
class RollingWindow:
    maxlen: int
    values: deque[tuple[int, float]] = field(init=False)

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.maxlen)

    def add(self, timestamp: int, value: float) -> None:
        self.values.append((timestamp, value))

    def clear(self) -> None:
        self.values.clear()

    @property
    def is_full(self) -> bool:
        return len(self.values) == self.maxlen

    def get_values(self) -> list[float]:
        return [v[1] for v in self.values]

    def mean(self) -> float:
        vals = self.get_values()
        return mean(vals) if vals else 0.0

    def stddev(self) -> float:
        vals = self.get_values()
        return pstdev(vals) if len(vals) > 1 else 0.0

class Candle:
    def __init__(self, timestamp: int, open_p: float, high: float, low: float, close: float, volume: float):
        self.timestamp = timestamp
        self.open = open_p
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

class RollingDataManager:
    """V2 Extension to cache historical timeframes alongside native execution tracking."""
    def __init__(self, max_candles: int = 100):
        self.max_candles = max_candles
        self.candles: Dict[str, Dict[str, collections.deque]] = {}

    def update_candle(self, symbol: str, timeframe: str, timestamp: int, open_p: float, high: float, low: float, close: float, volume: float):
        if symbol not in self.candles:
            self.candles[symbol] = {
                "1m": collections.deque(maxlen=self.max_candles),
                "1h": collections.deque(maxlen=self.max_candles)
            }
        new_candle = Candle(timestamp, open_p, high, low, close, volume)
        self.candles[symbol][timeframe].append(new_candle)

    def get_candles(self, symbol: str, timeframe: str) -> List[Candle]:
        if symbol in self.candles and timeframe in self.candles[symbol]:
            return list(self.candles[symbol][timeframe])
        return []