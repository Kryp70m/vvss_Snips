from app.models.scanner import MetricSnapshot
from app.services.symbol_state import SymbolState, now_ms


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def label_flow(ratio: float, rel_volume: float) -> str:
    if ratio >= 0.72 and rel_volume >= 5:
        return "Extreme"
    if ratio >= 0.64 and rel_volume >= 3:
        return "High"
    if ratio >= 0.57 and rel_volume >= 1.8:
        return "Elevated"
    return "Quiet"


def probability_label(score: float) -> str:
    if score >= 85:
        return "Very High"
    if score >= 70:
        return "High"
    if score >= 55:
        return "Moderate"
    return "Low"


def required_natr_for_target(target_pct: float) -> float:
    if target_pct <= 10:
        return max(0.18, target_pct * 0.16)
    return target_pct * 0.2


def target_move_pct(natr: float, preferred_target_pct: float = 10.0) -> float:
    preferred_target_pct = clamp(preferred_target_pct, 1.0, 30.0)
    if natr >= required_natr_for_target(preferred_target_pct):
        return preferred_target_pct
    return 0.0


def clamp_metal_target(value: float) -> float:
    return max(1.0, min(100.0, float(value)))


def required_natr_for_metal_target(target_usd: float, price: float) -> float:
    if price <= 0 or target_usd <= 0:
        return float("inf")
    target_pct = (target_usd / price) * 100
    return max(0.08, min(2.5, target_pct * 0.45))


def metal_target_move_usd(symbol: str, natr: float, price: float, targets_usd: dict[str, float]) -> float:
    target_usd = clamp_metal_target(targets_usd.get(symbol, 10.0))
    return target_usd if natr >= required_natr_for_metal_target(target_usd, price) else 0.0


def expected_move(score: float, target_pct: float, target_usd: float = 0.0) -> str:
    if target_usd >= 1:
        return f"Metal ${target_usd:.0f} move watch" if score >= 58 else f"Metal ${target_usd:.0f} setup forming"
    if target_pct >= 1:
        if score >= 68:
            return f"{target_pct:.0f}% move watch"
        return f"{target_pct:.0f}% setup forming"
    return "Target setup forming"


def volatility_label(natr: float) -> str:
    if natr >= 1.5:
        return "Explosive"
    if natr >= 0.8:
        return "Hot"
    if natr >= 0.35:
        return "Active"
    return "Cold"


def market_cap_tier(market_cap_usd: float) -> str:
    if market_cap_usd <= 0:
        return "Proxy"
    if market_cap_usd < 50_000_000:
        return "Micro"
    if market_cap_usd < 300_000_000:
        return "Low"
    if market_cap_usd < 1_500_000_000:
        return "Mid"
    return "Large"


def market_cap_factor(market_cap_usd: float, price: float, natr: float) -> float:
    if market_cap_usd > 0:
        if market_cap_usd < 25_000_000:
            base = 0.68
        elif market_cap_usd < 100_000_000:
            base = 0.82
        elif market_cap_usd < 500_000_000:
            base = 1.0
        elif market_cap_usd < 1_500_000_000:
            base = 1.22
        else:
            base = 1.55
    elif price > 0:
        if price < 0.01:
            base = 0.78
        elif price < 0.10:
            base = 0.88
        elif price < 1:
            base = 1.0
        elif price < 10:
            base = 1.12
        else:
            base = 1.28
    else:
        base = 1.0

    volatility_adjustment = max(0.82, min(1.18, 1.08 - natr * 0.035))
    return max(0.55, min(1.85, base * volatility_adjustment))


def market_cap_sensitivity_score(factor: float) -> float:
    return clamp((1.85 - factor) / 1.30 * 100)


def price_impact_quote_floor(
    price: float,
    target_pct: float,
    target_usd: float = 0.0,
    market_cap_usd: float = 0.0,
) -> float:
    if price <= 0:
        return 0.0
    if target_usd > 0:
        target_scale = max(0.85, min(3.5, (target_usd / max(price, 1.0)) * 8.0))
    else:
        target_scale = max(0.70, min(3.0, target_pct / 10.0))

    if price < 0.01:
        base_floor = 2_500.0
    elif price < 0.10:
        base_floor = 5_000.0
    elif price < 1:
        base_floor = 8_000.0
    elif price < 5:
        base_floor = 16_000.0
    elif price < 10:
        base_floor = 28_000.0
    elif price < 25:
        base_floor = 45_000.0
    elif price < 50:
        base_floor = 70_000.0
    elif price < 100:
        base_floor = 105_000.0
    else:
        base_floor = 155_000.0

    if market_cap_usd > 0:
        if market_cap_usd < 25_000_000:
            cap_multiplier = 0.70
        elif market_cap_usd < 100_000_000:
            cap_multiplier = 0.85
        elif market_cap_usd < 500_000_000:
            cap_multiplier = 1.0
        elif market_cap_usd < 1_500_000_000:
            cap_multiplier = 1.25
        else:
            cap_multiplier = 1.60
    else:
        cap_multiplier = 1.0

    return base_floor * target_scale * cap_multiplier


def impulse_required_pct(target_pct: float, target_usd: float, price: float) -> float:
    if target_usd > 0 and price > 0:
        absolute_target_pct = target_usd / price * 100
        return max(0.01, absolute_target_pct * 0.10)
    if target_pct >= 1:
        return max(0.10, target_pct * 0.10)
    return 0.10


def price_candles_after_print(points: list[tuple[int, float]], start_ms: int, interval_ms: int = 60_000) -> list[dict]:
    candles: dict[int, dict] = {}
    for ts, price in sorted(points):
        if price <= 0 or ts < start_ms:
            continue
        bucket = max(0, int((ts - start_ms) // interval_ms))
        candle = candles.get(bucket)
        if not candle:
            candles[bucket] = {
                "bucket": bucket,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "start": ts,
                "end": ts,
                "ticks": 1,
            }
            continue
        candle["high"] = max(candle["high"], price)
        candle["low"] = min(candle["low"], price)
        candle["close"] = price
        candle["end"] = ts
        candle["ticks"] += 1
    return [candles[index] for index in sorted(candles)]


def candle_impulse_stats(candles: list[dict], direction: str) -> tuple[float, float, float, float, float]:
    if not candles:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    open_price = candles[0]["open"]
    close_price = candles[-1]["close"]
    high = max(candle["high"] for candle in candles)
    low = min(candle["low"] for candle in candles)
    if open_price <= 0:
        return 0.0, 0.0, 0.0, high, low
    range_pct = max((high - low) / open_price * 100, 0.0)
    if direction == "long":
        max_move_pct = max(0.0, (high - open_price) / open_price * 100)
        body_pct = max(0.0, (close_price - open_price) / open_price * 100)
    elif direction == "short":
        max_move_pct = max(0.0, (open_price - low) / open_price * 100)
        body_pct = max(0.0, (open_price - close_price) / open_price * 100)
    else:
        return 0.0, 0.0, 0.0, high, low
    efficiency = body_pct / range_pct * 100 if range_pct > 0 else 0.0
    return max_move_pct, body_pct, clamp(efficiency), high, low


def impulse_confirmation(
    state: SymbolState,
    direction: str,
    target_pct: float,
    target_usd: float,
    current_price: float,
) -> dict:
    required = impulse_required_pct(target_pct, target_usd, current_price)
    base = {
        "impulse_confirmation": "Waiting",
        "impulse_move_pct": 0.0,
        "impulse_required_pct": required,
        "impulse_candles": 0,
        "impulse_efficiency": 0.0,
        "impulse_wick_high": 0.0,
        "impulse_wick_low": 0.0,
        "impulse_stop_price": 0.0,
        "impulse_confirmed_at": 0,
        "impulse_note": "Wait for a strong same-direction 1-2 candle push after the print",
    }
    if direction not in {"long", "short"} or state.big_order_time_ms <= 0:
        return base
    points = [(ts, price) for ts, price in state.prices.values if ts >= state.big_order_time_ms]
    candles = price_candles_after_print(points, state.big_order_time_ms)
    if not candles:
        return base

    best_move = 0.0
    best_efficiency = 0.0
    fragmented_move = 0.0
    for index, candle in enumerate(candles):
        for window_size in (1, 2):
            window = candles[index : index + window_size]
            if len(window) != window_size:
                continue
            move_pct, body_pct, efficiency, high, low = candle_impulse_stats(window, direction)
            best_move = max(best_move, move_pct)
            best_efficiency = max(best_efficiency, efficiency)
            body_requirement = required * (0.55 if window_size == 1 else 0.65)
            efficiency_requirement = 48 if window_size == 1 else 55
            if move_pct >= required and body_pct >= body_requirement and efficiency >= efficiency_requirement:
                stop_price = low if direction == "long" else high
                return {
                    "impulse_confirmation": "Confirmed",
                    "impulse_move_pct": move_pct,
                    "impulse_required_pct": required,
                    "impulse_candles": window_size,
                    "impulse_efficiency": efficiency,
                    "impulse_wick_high": high,
                    "impulse_wick_low": low,
                    "impulse_stop_price": stop_price,
                    "impulse_confirmed_at": int(window[-1]["end"]),
                    "impulse_note": f"CONFIRMED: {required:.2f}%+ same-direction impulse in {window_size} candle{'s' if window_size > 1 else ''}",
                }

    if points:
        print_price = state.big_order_price or points[0][1]
        prices = [price for _, price in points]
        if direction == "long" and print_price > 0:
            fragmented_move = max(0.0, (max(prices) - print_price) / print_price * 100)
        elif direction == "short" and print_price > 0:
            fragmented_move = max(0.0, (print_price - min(prices)) / print_price * 100)

    if fragmented_move >= required:
        note = "Move reached the level but was fragmented; wait for a fresh 1-2 candle impulse"
    elif best_move > 0:
        note = "Impulse is building but not strong enough yet"
    else:
        note = "No same-direction impulse after the print yet"
    return {
        **base,
        "impulse_move_pct": max(best_move, fragmented_move),
        "impulse_efficiency": best_efficiency,
        "impulse_note": note,
    }


def trade_levels(price: float, direction: str, target_pct: float, order_share_pct: float, natr: float) -> tuple[float, float, float, float]:
    if price <= 0 or target_pct <= 0 or direction not in {"long", "short"}:
        return 0.0, 0.0, 0.0, 0.0
    reward_pct = target_pct
    risk_pct = max(0.6, min(reward_pct * 0.42, max(natr * 0.85, 0.9)))
    if direction == "long":
        target_price = price * (1 + reward_pct / 100)
        stop_loss_price = price * (1 - risk_pct / 100)
    else:
        target_price = price * (1 - reward_pct / 100)
        stop_loss_price = price * (1 + risk_pct / 100)
    return target_price, stop_loss_price, risk_pct, reward_pct


def partial_target_prices(price: float, direction: str, reward_pct: float) -> tuple[float, float, float]:
    if price <= 0 or reward_pct <= 0 or direction not in {"long", "short"}:
        return 0.0, 0.0, 0.0
    levels = (reward_pct * 0.30, reward_pct * 0.60, reward_pct)
    if direction == "long":
        return tuple(price * (1 + level / 100) for level in levels)
    return tuple(price * (1 - level / 100) for level in levels)


def execution_plan(
    state: SymbolState,
    direction: str,
    current_price: float,
    target_price: float,
    target_pct: float = 0.0,
    target_usd: float = 0.0,
) -> dict:
    print_price = state.big_order_price
    if print_price <= 0 or current_price <= 0 or direction not in {"long", "short"}:
        return {
            "print_price": 0.0,
            "initial_reaction": "Waiting",
            "trap_type": "Waiting",
            "adverse_move_pct": 0.0,
            "fvg_low": 0.0,
            "fvg_high": 0.0,
            "nearest_delta": "Mixed",
            "delta_flip": "Waiting",
            "absorption": "No",
            "entry_quality": "Wait",
            "best_entry_low": 0.0,
            "best_entry_high": 0.0,
            "invalidation_price": 0.0,
            "no_chase": False,
            "execution_note": "Wait for confirmation",
            "impulse_confirmation": "Waiting",
            "impulse_move_pct": 0.0,
            "impulse_required_pct": impulse_required_pct(target_pct, target_usd, current_price),
            "impulse_candles": 0,
            "impulse_efficiency": 0.0,
            "impulse_wick_high": 0.0,
            "impulse_wick_low": 0.0,
            "impulse_stop_price": 0.0,
            "impulse_confirmed_at": 0,
            "impulse_note": "Wait for a strong same-direction 1-2 candle push after the print",
        }

    impulse = impulse_confirmation(state, direction, target_pct, target_usd, current_price)
    after_print = [(ts, price) for ts, price in state.prices.values if ts >= state.big_order_time_ms]
    prices = [price for _, price in after_print] or [current_price]
    low = min(prices)
    high = max(prices)
    latest = prices[-1]
    recent_from = max(state.big_order_time_ms, now_ms() - 15_000)
    buy_delta = state.buy_quote_volume.sum_since(recent_from)
    sell_delta = state.sell_quote_volume.sum_since(recent_from)

    if buy_delta > sell_delta * 1.15:
        nearest_delta = "Green"
    elif sell_delta > buy_delta * 1.15:
        nearest_delta = "Red"
    else:
        nearest_delta = "Mixed"

    if direction == "long":
        adverse_pct = max(0.0, (print_price - low) / print_price * 100)
        favorable_pct = max(0.0, (high - print_price) / print_price * 100)
        reclaimed = latest >= print_price
        wanted_delta = nearest_delta == "Green"
        invalidation = low if adverse_pct > 0 else print_price * 0.994
        entry_low = low if adverse_pct >= 0.12 else print_price * 0.998
        entry_high = print_price * 1.002 if adverse_pct >= 0.12 else latest
        no_chase = target_price > 0 and latest > print_price and (latest - print_price) > (target_price - print_price) * 0.45
    else:
        adverse_pct = max(0.0, (high - print_price) / print_price * 100)
        favorable_pct = max(0.0, (print_price - low) / print_price * 100)
        reclaimed = latest <= print_price
        wanted_delta = nearest_delta == "Red"
        invalidation = high if adverse_pct > 0 else print_price * 1.006
        entry_low = print_price * 0.998 if adverse_pct >= 0.12 else latest
        entry_high = high if adverse_pct >= 0.12 else print_price * 1.002
        no_chase = target_price > 0 and latest < print_price and (print_price - latest) > (print_price - target_price) * 0.45

    if adverse_pct >= 0.35:
        trap_type = "Sweep + FVG"
    elif adverse_pct >= 0.12:
        trap_type = "FVG Retest"
    elif favorable_pct >= 0.12:
        trap_type = "Instant Momentum"
    else:
        trap_type = "Wait"

    if trap_type in {"Sweep + FVG", "FVG Retest"}:
        initial_reaction = "Opposite sweep"
    elif trap_type == "Instant Momentum":
        initial_reaction = "Same direction"
    else:
        initial_reaction = "Waiting"

    delta_flip = "Confirmed" if wanted_delta and reclaimed else "Waiting"
    absorption = "Yes" if adverse_pct >= 0.12 and reclaimed and wanted_delta else "Watching" if adverse_pct >= 0.12 else "No"

    if impulse["impulse_confirmation"] != "Confirmed":
        entry_quality = "Wait"
        note = impulse["impulse_note"]
    elif delta_flip == "Confirmed" and absorption == "Yes":
        entry_quality = "A+"
        note = "Sweep reclaimed with delta flip and impulse confirmation"
    elif delta_flip == "Confirmed":
        entry_quality = "A"
        note = "Delta and 1-2 candle impulse confirm filled-volume direction"
    elif trap_type in {"Sweep + FVG", "FVG Retest"}:
        entry_quality = "Wait"
        note = "Wait for reclaim of print price with matching delta"
    elif trap_type == "Instant Momentum" and wanted_delta:
        entry_quality = "A"
        note = "Momentum continuation with matching delta"
    else:
        entry_quality = "Wait"
        note = "Wait for delta flip or clean retest"

    fvg_low = min(entry_low, entry_high)
    fvg_high = max(entry_low, entry_high)
    return {
        "print_price": print_price,
        "initial_reaction": initial_reaction,
        "trap_type": trap_type,
        "adverse_move_pct": adverse_pct,
        "fvg_low": fvg_low,
        "fvg_high": fvg_high,
        "nearest_delta": nearest_delta,
        "delta_flip": delta_flip,
        "absorption": absorption,
        "entry_quality": entry_quality,
        "best_entry_low": fvg_low,
        "best_entry_high": fvg_high,
        "invalidation_price": invalidation,
        "no_chase": no_chase,
        "execution_note": note,
        **impulse,
    }


class ExpansionScorer:
    def __init__(
        self,
        large_order_min_quote: float = 5_000,
        large_order_avg_multiple: float = 5.0,
        large_order_recent_seconds: int = 600,
        large_order_min_volume_share_pct: float = 3.0,
        natr_min_5m_14: float = 0.35,
        target_move_pct: float = 10.0,
        metal_targets_usd: dict[str, float] | None = None,
        manipulation_sensitivity: float = 60.0,
        retracement_percentage: float = 40.0,
        liquidity_sensitivity: float = 55.0,
        volume_shock_multiplier: float = 1.0,
        market_cap_filter: float = 1500.0,
    ) -> None:
        self.large_order_min_quote = large_order_min_quote
        self.large_order_avg_multiple = large_order_avg_multiple
        self.large_order_recent_seconds = large_order_recent_seconds
        self.large_order_min_volume_share_pct = large_order_min_volume_share_pct
        self.natr_min_5m_14 = natr_min_5m_14
        self.target_move_pct = clamp(target_move_pct, 1.0, 30.0)
        self.manipulation_sensitivity = clamp(manipulation_sensitivity, 1.0, 100.0)
        self.retracement_percentage = clamp(retracement_percentage, 30.0, 50.0)
        self.liquidity_sensitivity_setting = clamp(liquidity_sensitivity, 1.0, 100.0)
        self.volume_shock_multiplier = max(0.5, min(3.0, float(volume_shock_multiplier)))
        self.market_cap_filter_millions = max(0.0, min(10_000.0, float(market_cap_filter)))
        self.metal_targets_usd = {
            "PAXGUSDT": 10.0,
            "XAGUSDT": 10.0,
            **(metal_targets_usd or {}),
        }
        self.metal_targets_usd = {symbol: clamp_metal_target(value) for symbol, value in self.metal_targets_usd.items()}

    def adaptive_requirements(
        self,
        current_volume: float,
        natr: float,
        price: float = 0.0,
        symbol: str = "",
        market_cap_usd: float = 0.0,
    ) -> tuple[float, float, float, float, float]:
        if symbol in self.metal_targets_usd:
            target_usd = metal_target_move_usd(symbol, natr, price, self.metal_targets_usd)
            target_pct = (target_usd / price * 100) if price > 0 and target_usd > 0 else 0.0
            if target_usd >= 1:
                scale = max(0.5, min(3.0, target_usd / 10.0))
                quote = max(15_000.0 * scale, min(150_000.0 * scale, current_volume * 0.08 * scale))
                multiple = max(2.5, self.large_order_avg_multiple * 0.55 * scale)
                share = max(4.0, 6.0 * min(scale, 2.0))
            else:
                quote = float("inf")
                multiple = float("inf")
                share = float("inf")
            if quote != float("inf"):
                quote *= self.volume_shock_multiplier
                multiple *= max(0.85, min(1.25, self.volume_shock_multiplier ** 0.35))
                share *= max(0.85, min(1.30, self.volume_shock_multiplier ** 0.45))
            return quote, multiple, share, target_pct, target_usd

        target_pct = target_move_pct(natr, self.target_move_pct)
        target_usd = 0.0
        cap_factor = market_cap_factor(market_cap_usd, price, natr)
        if target_pct >= 20:
            scale = target_pct / 20.0
            quote = max(self.large_order_min_quote, min(75_000.0 * scale, current_volume * 0.05 * scale))
            multiple = max(4.0, self.large_order_avg_multiple * 0.8 * scale)
            share = max(5.0, self.large_order_min_volume_share_pct * scale)
        elif target_pct >= 10:
            scale = target_pct / 10.0
            quote = max(self.large_order_min_quote * 1.5 * scale, min(100_000.0 * scale, current_volume * 0.08 * scale))
            multiple = max(6.0 * scale, self.large_order_avg_multiple)
            share = max(8.0 * scale, self.large_order_min_volume_share_pct * 1.8)
        elif target_pct >= 1:
            scale = target_pct / 10.0
            quote = max(self.large_order_min_quote * max(0.35, scale), min(60_000.0, current_volume * max(0.025, 0.08 * scale)))
            multiple = max(3.0, self.large_order_avg_multiple * max(0.6, scale))
            share = max(self.large_order_min_volume_share_pct, 8.0 * max(0.45, scale))
        else:
            quote = float("inf")
            multiple = float("inf")
            share = float("inf")
        if quote != float("inf"):
            quote *= cap_factor
            quote = max(quote, price_impact_quote_floor(price, target_pct, target_usd, market_cap_usd))
            quote *= self.volume_shock_multiplier
            multiple *= max(0.82, min(1.35, cap_factor ** 0.45))
            multiple *= max(0.85, min(1.25, self.volume_shock_multiplier ** 0.35))
            share *= max(0.85, min(1.45, cap_factor))
            share *= max(0.85, min(1.30, self.volume_shock_multiplier ** 0.45))
        return quote, multiple, share, target_pct, target_usd

    def advanced_orderflow_model(
        self,
        state: SymbolState,
        direction: str,
        has_recent_big_order: bool,
        last_order_quote: float,
        required_quote: float,
        required_multiple: float,
        required_share: float,
        order_volume_share_pct: float,
        relative_volume: float,
        buy_ratio: float,
        sell_ratio: float,
        effective_natr: float,
        price_velocity_bps: float,
        expansion_efficiency: float,
        displacement_strength: float,
        liquidity_sensitivity: float,
        cap_sensitivity: float,
        spread_bps: float,
        plan: dict,
        target_pct: float,
        target_usd: float,
    ) -> dict:
        base = {
            "impact_score": 0.0,
            "manipulation_probability": 0.0,
            "distribution_strength": 0.0,
            "retracement_quality": 0.0,
            "continuation_probability": 0.0,
            "expected_move_pct": 0.0,
            "retracement_low": 0.0,
            "retracement_high": 0.0,
            "entry_confirmation_price": 0.0,
            "manipulation_phase": "Waiting",
            "distribution_phase": "Waiting",
            "retracement_phase": "Waiting",
        }
        if not has_recent_big_order or direction not in {"long", "short"} or state.big_order_price <= 0:
            return base

        quote_score = clamp((last_order_quote / max(required_quote, 1.0)) * 55)
        multiple_score = clamp((state.big_order_multiple / max(required_multiple, 0.1)) * 60)
        share_score = clamp((order_volume_share_pct / max(required_share, 0.1)) * 70)
        rel_volume_score = clamp(relative_volume * 13)
        volatility_score = clamp(effective_natr * 42)
        thin_book_score = clamp((liquidity_sensitivity / max(self.liquidity_sensitivity_setting, 1.0)) * 70)
        impact_score = clamp(
            quote_score * 0.24
            + multiple_score * 0.19
            + share_score * 0.18
            + rel_volume_score * 0.15
            + thin_book_score * 0.10
            + cap_sensitivity * 0.08
            + volatility_score * 0.06
        )

        post_print_points = [(ts, price) for ts, price in state.prices.values if ts >= state.big_order_time_ms]
        candles = price_candles_after_print(post_print_points, state.big_order_time_ms)
        wick_to_body_ratio = 0.0
        candle_velocity_score = clamp(abs(price_velocity_bps) / 2.0)
        if candles:
            first = candles[0]
            body = abs(first["close"] - first["open"])
            upper_wick = max(0.0, first["high"] - max(first["open"], first["close"]))
            lower_wick = max(0.0, min(first["open"], first["close"]) - first["low"])
            wick_to_body_ratio = (upper_wick + lower_wick) / max(body, first["open"] * 0.0001)
            candle_duration = max((first["end"] - first["start"]) / 1000, 1.0)
            if first["open"] > 0:
                candle_range_bps = ((first["high"] - first["low"]) / first["open"]) * 10_000
                candle_velocity_score = max(candle_velocity_score, clamp(candle_range_bps / max(candle_duration, 1.0) * 14))

        dominant_ratio = max(buy_ratio, sell_ratio)
        delta_imbalance_score = clamp((dominant_ratio - 0.5) * 250)
        adverse_score = clamp(plan.get("adverse_move_pct", 0.0) / max(plan.get("impulse_required_pct", 0.1), 0.1) * 80)
        wick_score = clamp(wick_to_body_ratio * 28)
        spread_score = clamp(spread_bps * 10)
        manipulation_raw = (
            rel_volume_score * 0.19
            + volatility_score * 0.14
            + wick_score * 0.16
            + delta_imbalance_score * 0.14
            + spread_score * 0.12
            + candle_velocity_score * 0.13
            + adverse_score * 0.12
        )
        sensitivity_adjustment = 1.0 + ((self.manipulation_sensitivity - 50.0) / 180.0)
        manipulation_probability = clamp(manipulation_raw * sensitivity_adjustment)

        wanted_delta = (direction == "long" and buy_ratio >= sell_ratio * 1.08) or (direction == "short" and sell_ratio >= buy_ratio * 1.08)
        impulse_score = 100.0 if plan.get("impulse_confirmation") == "Confirmed" else clamp(plan.get("impulse_move_pct", 0.0) / max(plan.get("impulse_required_pct", 0.1), 0.1) * 70)
        distribution_strength = clamp(
            impulse_score * 0.30
            + delta_imbalance_score * 0.20
            + expansion_efficiency * 0.18
            + displacement_strength * 0.14
            + impact_score * 0.12
            + (20 if wanted_delta else 0)
            - (22 if plan.get("no_chase") else 0)
        )

        prices = [price for _, price in post_print_points] or [state.price]
        print_price = state.big_order_price
        latest = prices[-1] if prices else state.price
        retracement_low = 0.0
        retracement_high = 0.0
        retracement_depth = 0.0
        if direction == "long":
            impulse_extreme = max(prices)
            impulse_size = impulse_extreme - print_price
            if impulse_size > 0:
                retracement_depth = clamp((impulse_extreme - latest) / impulse_size * 100)
                retracement_low = impulse_extreme - impulse_size * 0.50
                retracement_high = impulse_extreme - impulse_size * 0.30
                entry_confirmation_price = max(latest, retracement_high)
            else:
                entry_confirmation_price = print_price
            counter_delta_weak = sell_ratio <= buy_ratio * 0.92
        else:
            impulse_extreme = min(prices)
            impulse_size = print_price - impulse_extreme
            if impulse_size > 0:
                retracement_depth = clamp((latest - impulse_extreme) / impulse_size * 100)
                retracement_low = impulse_extreme + impulse_size * 0.30
                retracement_high = impulse_extreme + impulse_size * 0.50
                entry_confirmation_price = min(latest, retracement_low)
            else:
                entry_confirmation_price = print_price
            counter_delta_weak = buy_ratio <= sell_ratio * 0.92

        ideal_retrace = self.retracement_percentage
        zone_distance = abs(retracement_depth - ideal_retrace)
        zone_score = clamp(100 - zone_distance * 5)
        counter_delta_score = 85 if counter_delta_weak else 45 if dominant_ratio < 0.58 else 20
        slower_candle_score = clamp(100 - candle_velocity_score * 0.70)
        lower_volume_score = clamp(100 - max(relative_volume - 1.0, 0.0) * 12)
        retracement_quality = clamp(
            zone_score * 0.42
            + counter_delta_score * 0.24
            + slower_candle_score * 0.18
            + lower_volume_score * 0.16
        ) if plan.get("impulse_confirmation") == "Confirmed" and retracement_depth > 0 else 0.0

        manipulation_complete_score = 78 if plan.get("absorption") == "Yes" or plan.get("delta_flip") == "Confirmed" else 45 if plan.get("trap_type") in {"Instant Momentum", "Wait"} else 35
        continuation_probability = clamp(
            impact_score * 0.22
            + distribution_strength * 0.32
            + retracement_quality * 0.24
            + manipulation_complete_score * 0.14
            + clamp(max(target_pct, 1.0) * 2.2) * 0.08
            - (18 if plan.get("no_chase") else 0)
        )
        if target_usd > 0 and state.price > 0:
            selected_target_pct = target_usd / state.price * 100
        else:
            selected_target_pct = target_pct
        if has_recent_big_order and selected_target_pct > 0:
            quote_ratio = last_order_quote / max(required_quote, 1.0)
            multiple_ratio = state.big_order_multiple / max(required_multiple, 0.1)
            share_ratio = order_volume_share_pct / max(required_share, 0.1)
            pressure_ratio = max(0.0, min(quote_ratio, multiple_ratio, share_ratio))
            expected_move_pct = clamp(selected_target_pct * pressure_ratio, 0.0, 30.0)
        else:
            expected_move_pct = 0.0

        manipulation_phase = "Complete" if manipulation_complete_score >= 70 else "Active" if manipulation_probability >= self.manipulation_sensitivity else "Low"
        distribution_phase = "Confirmed" if distribution_strength >= 70 else "Building" if distribution_strength >= 45 else "Waiting"
        retracement_phase = "Quality" if retracement_quality >= 70 else "Watching" if retracement_quality > 0 else "Waiting"
        return {
            "impact_score": impact_score,
            "manipulation_probability": manipulation_probability,
            "distribution_strength": distribution_strength,
            "retracement_quality": retracement_quality,
            "continuation_probability": continuation_probability,
            "expected_move_pct": expected_move_pct,
            "retracement_low": min(retracement_low, retracement_high),
            "retracement_high": max(retracement_low, retracement_high),
            "entry_confirmation_price": entry_confirmation_price,
            "manipulation_phase": manipulation_phase,
            "distribution_phase": distribution_phase,
            "retracement_phase": retracement_phase,
        }

    def build_snapshot(self, state: SymbolState) -> MetricSnapshot:
        ts = now_ms()
        one_min_ago = ts - 60_000
        five_min_ago = ts - 300_000
        fifteen_min_ago = ts - 900_000

        current_volume = state.quote_volume.sum_since(one_min_ago)
        baseline = state.one_minute_volume_history.mean()
        relative_volume = current_volume / baseline if baseline > 0 else (1.0 if current_volume > 0 else 0.0)

        buy_vol = state.buy_quote_volume.sum_since(one_min_ago)
        sell_vol = state.sell_quote_volume.sum_since(one_min_ago)
        total_flow = buy_vol + sell_vol
        buy_ratio = buy_vol / total_flow if total_flow else 0.0
        sell_ratio = sell_vol / total_flow if total_flow else 0.0

        big_order_age_seconds = (ts - state.big_order_time_ms) / 1000 if state.big_order_time_ms else 9999.0
        last_order_quote = 0.0
        if state.big_order_time_ms:
            for trade_time, quote_size in reversed(state.big_order_quote.values):
                if trade_time == state.big_order_time_ms:
                    last_order_quote = quote_size
                    break
        order_volume_share_pct = (last_order_quote / current_volume * 100) if current_volume > 0 else 0.0
        five_range_for_natr = state.prices.min_max_since(five_min_ago)
        rolling_natr = 0.0
        if five_range_for_natr and state.price > 0:
            rolling_natr = ((five_range_for_natr[1] - five_range_for_natr[0]) / state.price) * 100
        effective_natr = max(state.natr_5m_14, rolling_natr)

        required_quote, required_multiple, required_share, move_target_pct, move_target_usd = self.adaptive_requirements(
            current_volume,
            effective_natr,
            state.price,
            state.symbol,
            state.market_cap_usd,
        )
        has_recent_big_order = (
            state.big_order_time_ms > 0
            and big_order_age_seconds <= self.large_order_recent_seconds
            and (move_target_pct >= 1 or move_target_usd >= 10)
            and last_order_quote >= required_quote
            and state.big_order_multiple >= required_multiple
            and order_volume_share_pct >= required_share
        )
        big_order_score = 0.0
        if has_recent_big_order:
            notional_score = clamp((last_order_quote / required_quote) * 35)
            multiple_score = clamp((state.big_order_multiple / required_multiple) * 65)
            share_score = clamp((order_volume_share_pct / required_share) * 70)
            recency_score = clamp(100 - (big_order_age_seconds / self.large_order_recent_seconds) * 40)
            big_order_score = clamp(notional_score * 0.25 + multiple_score * 0.35 + share_score * 0.25 + recency_score * 0.15)

        price_pair_60 = state.prices.first_last_since(one_min_ago)
        price_velocity_bps = 0.0
        if price_pair_60 and price_pair_60[0] > 0:
            price_velocity_bps = ((price_pair_60[1] - price_pair_60[0]) / price_pair_60[0]) * 10_000

        price_range = state.prices.min_max_since(one_min_ago)
        expansion_efficiency = 0.0
        displacement_strength = 0.0
        if price_range and price_pair_60 and price_range[1] > price_range[0]:
            net_move = abs(price_pair_60[1] - price_pair_60[0])
            full_range = price_range[1] - price_range[0]
            expansion_efficiency = clamp((net_move / full_range) * 100)
            displacement_strength = clamp(abs(price_velocity_bps) / 2.0)

        oi_pair = state.open_interest.first_last_since(five_min_ago)
        oi_change_pct = 0.0
        if oi_pair and oi_pair[0] > 0:
            oi_change_pct = ((oi_pair[1] - oi_pair[0]) / oi_pair[0]) * 100

        fifteen_range = state.prices.min_max_since(fifteen_min_ago)
        five_range = state.prices.min_max_since(five_min_ago)
        compression_score = 0.0
        compression = False
        if fifteen_range and five_range and state.price > 0:
            fifteen_bps = ((fifteen_range[1] - fifteen_range[0]) / state.price) * 10_000
            five_bps = ((five_range[1] - five_range[0]) / state.price) * 10_000
            if fifteen_bps > 0:
                compression_score = clamp((1 - min(five_bps / fifteen_bps, 1.0)) * 100)
            compression = compression_score >= 45 and fifteen_bps <= 120

        spread = state.spreads_bps.mean()
        avg_depth = state.top_book_depth.mean()
        depth_penalty = 100.0 if avg_depth <= 0 else clamp(150_000 / avg_depth * 100)
        spread_score = clamp(spread * 12)
        liquidity_sensitivity = clamp((depth_penalty * 0.7) + (spread_score * 0.3))
        liquidity_label = "High" if liquidity_sensitivity >= 70 else "Elevated" if liquidity_sensitivity >= 45 else "Normal"
        cap_factor = market_cap_factor(state.market_cap_usd, state.price, effective_natr)
        cap_sensitivity = market_cap_sensitivity_score(cap_factor)

        if has_recent_big_order:
            direction = "long" if state.big_order_side == "buy" else "short"
        else:
            direction = "neutral"
        target_price, stop_loss_price, risk_pct, reward_pct = trade_levels(
            state.price,
            direction,
            move_target_pct,
            order_volume_share_pct,
            effective_natr,
        )
        plan = (
            execution_plan(state, direction, state.price, target_price, move_target_pct, move_target_usd)
            if has_recent_big_order
            else execution_plan(state, "neutral", state.price, target_price, move_target_pct, move_target_usd)
        )
        if has_recent_big_order and plan["impulse_confirmation"] == "Confirmed" and plan["impulse_stop_price"] > 0:
            wick_buffer_pct = max(0.03, min(0.25, effective_natr * 0.08))
            if direction == "long":
                stop_loss_price = plan["impulse_stop_price"] * (1 - wick_buffer_pct / 100)
                risk_pct = max(0.01, (state.price - stop_loss_price) / state.price * 100) if state.price > 0 else risk_pct
            elif direction == "short":
                stop_loss_price = plan["impulse_stop_price"] * (1 + wick_buffer_pct / 100)
                risk_pct = max(0.01, (stop_loss_price - state.price) / state.price * 100) if state.price > 0 else risk_pct
        target_1_price, target_2_price, target_3_price = partial_target_prices(state.price, direction, reward_pct)
        dominant_flow_ratio = max(buy_ratio, sell_ratio)
        taker_score = clamp((dominant_flow_ratio - 0.5) * 240)
        rel_volume_score = clamp(relative_volume * 12)
        oi_score = clamp(max(oi_change_pct, 0) * 8)
        compression_component = compression_score if compression else compression_score * 0.35
        natr_score = clamp(effective_natr * 55)
        advanced = self.advanced_orderflow_model(
            state,
            direction,
            has_recent_big_order,
            last_order_quote,
            required_quote,
            required_multiple,
            required_share,
            order_volume_share_pct,
            relative_volume,
            buy_ratio,
            sell_ratio,
            effective_natr,
            price_velocity_bps,
            expansion_efficiency,
            displacement_strength,
            liquidity_sensitivity,
            cap_sensitivity,
            spread,
            plan,
            move_target_pct,
            move_target_usd,
        )

        score = clamp(
            rel_volume_score * 0.22
            + big_order_score * 0.28
            + taker_score * 0.13
            + natr_score * 0.13
            + displacement_strength * 0.08
            + expansion_efficiency * 0.08
            + oi_score * 0.05
            + liquidity_sensitivity * 0.06
            + cap_sensitivity * 0.03
            + compression_component * 0.02
            + advanced["impact_score"] * 0.08
            + advanced["distribution_strength"] * 0.07
            + advanced["continuation_probability"] * 0.08
            + advanced["retracement_quality"] * 0.04
            - (12 if advanced["manipulation_probability"] >= 80 and advanced["manipulation_phase"] != "Complete" else 0)
        )

        reasons: list[str] = []
        if relative_volume >= 5:
            reasons.append(f"Relative volume {relative_volume:.1f}x")
        if dominant_flow_ratio >= 0.68:
            reasons.append("Aggressive taker flow imbalance")
        market_type = "perp" if state.exchange.endswith("_perp") else "spot"
        margin_pressure_score = 0.0
        margin_pressure_label = "Normal"
        if has_recent_big_order:
            margin_pressure_score = clamp(big_order_score * 0.58 + cap_sensitivity * 0.18 + rel_volume_score * 0.12 + natr_score * 0.12)
            if margin_pressure_score >= 85:
                margin_pressure_label = "Extreme"
            elif margin_pressure_score >= 70:
                margin_pressure_label = "High"
            elif margin_pressure_score >= 50:
                margin_pressure_label = "Elevated"

        if has_recent_big_order:
            venue_label = "perp" if market_type == "perp" else "spot"
            reasons.append(
                f"Large {venue_label} taker {state.big_order_side} print ${last_order_quote:,.0f} "
                f"({state.big_order_multiple:.1f}x avg, {order_volume_share_pct:.1f}% of 1m volume)"
            )
            if move_target_usd > 0:
                reasons.append(f"Gold ${move_target_usd:.0f} absolute move criteria passed")
            else:
                reasons.append(f"Adaptive {move_target_pct:.0f}% target criteria passed")
        if effective_natr >= 1.0:
            reasons.append(f"Fast 5m volatility {effective_natr:.2f}%")
        if compression:
            reasons.append("Compression breakout conditions")
        if liquidity_sensitivity >= 70:
            reasons.append("Thin top-of-book liquidity")
        if cap_sensitivity >= 70:
            reasons.append("Small-cap sensitivity")
        if has_recent_big_order and plan["impulse_confirmation"] == "Confirmed":
            reasons.append(
                f"Same-direction impulse {plan['impulse_move_pct']:.2f}% in {plan['impulse_candles']} candle(s)"
            )
        if advanced["impact_score"] >= 70:
            reasons.append(f"Impact score {advanced['impact_score']:.0f}/100 from adaptive print size, liquidity, cap, and volatility")
        if advanced["manipulation_probability"] >= 65:
            reasons.append(f"Manipulation probability {advanced['manipulation_probability']:.0f}/100")
        if advanced["distribution_strength"] >= 65:
            reasons.append(f"Distribution strength {advanced['distribution_strength']:.0f}/100")
        if advanced["retracement_quality"] >= 65:
            reasons.append(f"Retracement quality {advanced['retracement_quality']:.0f}/100")
        if expansion_efficiency >= 70:
            reasons.append("High price efficiency")

        return MetricSnapshot(
            symbol=state.symbol,
            exchange=state.exchange,
            market_type=market_type,
            price=state.price,
            relative_volume=round(relative_volume, 2),
            taker_buy_ratio=round(buy_ratio, 3),
            taker_sell_ratio=round(sell_ratio, 3),
            aggressive_buy_flow=label_flow(buy_ratio, relative_volume),
            aggressive_sell_flow=label_flow(sell_ratio, relative_volume),
            last_big_order_side=state.big_order_side if has_recent_big_order else "none",
            last_big_order_quote=round(last_order_quote if has_recent_big_order else 0.0, 2),
            last_big_order_age_seconds=round(big_order_age_seconds if has_recent_big_order else 0.0, 1),
            last_big_order_multiple=round(state.big_order_multiple if has_recent_big_order else 0.0, 2),
            last_big_order_volume_share_pct=round(order_volume_share_pct if has_recent_big_order else 0.0, 2),
            required_order_quote=round(required_quote if required_quote != float("inf") else 0.0, 2),
            required_order_multiple=round(required_multiple if required_multiple != float("inf") else 0.0, 2),
            required_volume_share_pct=round(required_share if required_share != float("inf") else 0.0, 2),
            big_order_score=round(big_order_score, 2),
            margin_pressure_usd=round(last_order_quote if market_type == "perp" and has_recent_big_order else 0.0, 2),
            margin_pressure_score=round(margin_pressure_score if market_type == "perp" else 0.0, 2),
            margin_pressure_label=margin_pressure_label if market_type == "perp" else "Normal",
            natr_5m_14=round(effective_natr, 3),
            volatility_label=volatility_label(effective_natr),
            target_move_pct=round(move_target_pct, 1),
            target_move_usd=round(move_target_usd, 2),
            price_velocity_bps=round(price_velocity_bps, 2),
            expansion_efficiency=round(expansion_efficiency, 2),
            displacement_strength=round(displacement_strength, 2),
            oi_change_pct=round(oi_change_pct, 2),
            compression=compression,
            compression_score=round(compression_score, 2),
            spread_bps=round(spread, 3),
            liquidity_depth_usdt=round(avg_depth, 2),
            liquidity_sensitivity=round(liquidity_sensitivity, 2),
            liquidity_label=liquidity_label,
            market_cap_usd=round(state.market_cap_usd, 2),
            market_cap_tier=market_cap_tier(state.market_cap_usd),
            market_cap_sensitivity=round(cap_sensitivity, 2),
            impact_score=round(advanced["impact_score"], 2),
            manipulation_probability=round(advanced["manipulation_probability"], 2),
            distribution_strength=round(advanced["distribution_strength"], 2),
            retracement_quality=round(advanced["retracement_quality"], 2),
            continuation_probability=round(advanced["continuation_probability"], 2),
            expected_move_pct=round(advanced["expected_move_pct"], 2),
            retracement_low=round(advanced["retracement_low"], 10) if has_recent_big_order else 0.0,
            retracement_high=round(advanced["retracement_high"], 10) if has_recent_big_order else 0.0,
            entry_confirmation_price=round(advanced["entry_confirmation_price"], 10) if has_recent_big_order else 0.0,
            manipulation_phase=advanced["manipulation_phase"],
            distribution_phase=advanced["distribution_phase"],
            retracement_phase=advanced["retracement_phase"],
            expected_move=expected_move(score, move_target_pct, move_target_usd),
            entry_price=round(state.price, 10) if has_recent_big_order else 0.0,
            target_1_price=round(target_1_price, 10) if has_recent_big_order else 0.0,
            target_2_price=round(target_2_price, 10) if has_recent_big_order else 0.0,
            target_3_price=round(target_3_price, 10) if has_recent_big_order else 0.0,
            target_price=round(target_price, 10) if has_recent_big_order else 0.0,
            stop_loss_price=round(stop_loss_price, 10) if has_recent_big_order else 0.0,
            print_price=round(plan["print_price"], 10) if has_recent_big_order else 0.0,
            initial_reaction=plan["initial_reaction"],
            trap_type=plan["trap_type"],
            adverse_move_pct=round(plan["adverse_move_pct"], 3) if has_recent_big_order else 0.0,
            fvg_low=round(plan["fvg_low"], 10) if has_recent_big_order else 0.0,
            fvg_high=round(plan["fvg_high"], 10) if has_recent_big_order else 0.0,
            nearest_delta=plan["nearest_delta"],
            delta_flip=plan["delta_flip"],
            absorption=plan["absorption"],
            entry_quality=plan["entry_quality"],
            best_entry_low=round(plan["best_entry_low"], 10) if has_recent_big_order else 0.0,
            best_entry_high=round(plan["best_entry_high"], 10) if has_recent_big_order else 0.0,
            invalidation_price=round(plan["invalidation_price"], 10) if has_recent_big_order else 0.0,
            no_chase=plan["no_chase"],
            execution_note=plan["execution_note"],
            impulse_confirmation=plan["impulse_confirmation"],
            impulse_move_pct=round(plan["impulse_move_pct"], 3) if has_recent_big_order else 0.0,
            impulse_required_pct=round(plan["impulse_required_pct"], 3),
            impulse_candles=int(plan["impulse_candles"]) if has_recent_big_order else 0,
            impulse_efficiency=round(plan["impulse_efficiency"], 2) if has_recent_big_order else 0.0,
            impulse_wick_high=round(plan["impulse_wick_high"], 10) if has_recent_big_order else 0.0,
            impulse_wick_low=round(plan["impulse_wick_low"], 10) if has_recent_big_order else 0.0,
            impulse_stop_price=round(plan["impulse_stop_price"], 10) if has_recent_big_order else 0.0,
            impulse_confirmed_at=int(plan["impulse_confirmed_at"]) if has_recent_big_order else 0,
            impulse_note=plan["impulse_note"],
            risk_pct=round(risk_pct, 2) if has_recent_big_order else 0.0,
            reward_pct=round(reward_pct, 2) if has_recent_big_order else 0.0,
            ignition_probability=round(score, 2),
            probability_label=probability_label(score),
            direction=direction,
            reasons=reasons,
            updated_at=ts,
        )
