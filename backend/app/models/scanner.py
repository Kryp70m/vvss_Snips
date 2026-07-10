from pydantic import BaseModel, Field


class MetricSnapshot(BaseModel):
    symbol: str
    exchange: str = "binance"
    market_type: str = "spot"
    price: float = 0.0
    relative_volume: float = 0.0
    taker_buy_ratio: float = 0.0
    taker_sell_ratio: float = 0.0
    aggressive_buy_flow: str = "Quiet"
    aggressive_sell_flow: str = "Quiet"
    last_big_order_side: str = "none"
    last_big_order_quote: float = 0.0
    last_big_order_age_seconds: float = 0.0
    last_big_order_multiple: float = 0.0
    last_big_order_volume_share_pct: float = 0.0
    required_order_quote: float = 0.0
    required_order_multiple: float = 0.0
    required_volume_share_pct: float = 0.0
    big_order_score: float = 0.0
    margin_pressure_usd: float = 0.0
    margin_pressure_score: float = 0.0
    margin_pressure_label: str = "Normal"
    natr_5m_14: float = 0.0
    volatility_label: str = "Cold"
    target_move_pct: float = 0.0
    target_move_usd: float = 0.0
    price_velocity_bps: float = 0.0
    expansion_efficiency: float = 0.0
    displacement_strength: float = 0.0
    oi_change_pct: float = 0.0
    compression: bool = False
    compression_score: float = 0.0
    spread_bps: float = 0.0
    liquidity_depth_usdt: float = 0.0
    liquidity_sensitivity: float = 0.0
    liquidity_label: str = "Normal"
    market_cap_usd: float = 0.0
    market_cap_tier: str = "Unknown"
    market_cap_sensitivity: float = 0.0
    impact_score: float = 0.0
    manipulation_probability: float = 0.0
    distribution_strength: float = 0.0
    retracement_quality: float = 0.0
    continuation_probability: float = 0.0
    expected_move_pct: float = 0.0
    move_category: str = "Watching"
    retracement_low: float = 0.0
    retracement_high: float = 0.0
    entry_confirmation_price: float = 0.0
    manipulation_phase: str = "Waiting"
    distribution_phase: str = "Waiting"
    retracement_phase: str = "Waiting"
    expected_move: str = "0.5-1%"
    entry_price: float = 0.0
    target_1_price: float = 0.0
    target_2_price: float = 0.0
    target_3_price: float = 0.0
    target_price: float = 0.0
    stop_loss_price: float = 0.0
    print_price: float = 0.0
    initial_reaction: str = "Waiting"
    trap_type: str = "Waiting"
    adverse_move_pct: float = 0.0
    fvg_low: float = 0.0
    fvg_high: float = 0.0
    nearest_delta: str = "Mixed"
    delta_flip: str = "Waiting"
    absorption: str = "No"
    entry_quality: str = "Wait"
    best_entry_low: float = 0.0
    best_entry_high: float = 0.0
    invalidation_price: float = 0.0
    no_chase: bool = False
    execution_note: str = "Wait for confirmation"
    impulse_confirmation: str = "Waiting"
    impulse_move_pct: float = 0.0
    impulse_required_pct: float = 0.0
    impulse_candles: int = 0
    impulse_efficiency: float = 0.0
    impulse_wick_high: float = 0.0
    impulse_wick_low: float = 0.0
    impulse_stop_price: float = 0.0
    impulse_confirmed_at: int = 0
    impulse_note: str = "Wait for impulse confirmation"
    risk_pct: float = 0.0
    reward_pct: float = 0.0
    ignition_probability: float = 0.0
    probability_label: str = "Low"
    direction: str = "neutral"
    reasons: list[str] = Field(default_factory=list)
    updated_at: int = 0


class AlertEvent(BaseModel):
    symbol: str
    exchange: str = "binance"
    direction: str
    score: float
    label: str
    expected_move: str
    move_category: str = "Watching"
    snapshot: MetricSnapshot
    created_at: int
    entry_price: float = 0.0
    target_price: float = 0.0
    stop_loss_price: float = 0.0
    last_price: float = 0.0
    status: str = "open"
    status_at: int = 0


class LiquidationEvent(BaseModel):
    symbol: str
    side: str
    price: float
    quantity: float
    notional: float
    event_time: int
    timeframe_hint: str = "live"
    hunt_side: str = "neutral"
    reversal_bias: str = "watch"
    severity: str = "large"


class WhaleTradeEvent(BaseModel):
    source: str = "hyperliquid"
    symbol: str
    side: str
    price: float
    quantity: float = 0.0
    notional: float
    event_time: int
    trader: str = ""
    venue: str = ""
    bias: str = "watch"
    severity: str = "large"
