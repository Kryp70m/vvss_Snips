from collections import deque
from dataclasses import dataclass, field
from statistics import mean, pstdev


@dataclass(slots=True)
class RollingWindow:
    maxlen: int
    values: deque[tuple[int, float]] = field(init=False)

    def __post_init__(self) -> None:
        self.values = deque(maxlen=self.maxlen)

    def add(self, timestamp_ms: int, value: float) -> None:
        self.values.append((timestamp_ms, value))

    def since(self, timestamp_ms: int) -> list[float]:
        return [value for ts, value in self.values if ts >= timestamp_ms]

    def sum_since(self, timestamp_ms: int) -> float:
        return sum(self.since(timestamp_ms))

    def values_only(self) -> list[float]:
        return [value for _, value in self.values]

    def mean(self) -> float:
        vals = self.values_only()
        return mean(vals) if vals else 0.0

    def stdev(self) -> float:
        vals = self.values_only()
        return pstdev(vals) if len(vals) > 1 else 0.0

    def first_last_since(self, timestamp_ms: int) -> tuple[float, float] | None:
        vals = [(ts, value) for ts, value in self.values if ts >= timestamp_ms]
        if len(vals) < 2:
            return None
        return vals[0][1], vals[-1][1]

    def min_max_since(self, timestamp_ms: int) -> tuple[float, float] | None:
        vals = self.since(timestamp_ms)
        if not vals:
            return None
        return min(vals), max(vals)

