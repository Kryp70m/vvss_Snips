from app.models.events import BookTickerEvent, OpenInterestEvent, TakerSide, TradeEvent
from app.services.scoring import ExpansionScorer, execution_plan, impulse_required_pct, partial_target_prices
from app.services.symbol_state import SymbolState, now_ms


def test_high_priority_like_conditions_score_above_threshold():
    state = SymbolState("DOGEUSDT")
    ts = now_ms()

    for minute in range(20, 5, -1):
        state.one_minute_volume_history.add(ts - minute * 60_000, 30_000)

    for index in range(38):
        trade_ts = ts - 55_000 + index * 1_000
        price = 0.1000 + index * 0.00005
        state.apply_trade(
            TradeEvent(
                symbol="DOGEUSDT",
                event_time=trade_ts,
                trade_time=trade_ts,
                price=price,
                quantity=20_000,
                quote_quantity=price * 20_000,
                taker_side=TakerSide.BUY,
                aggregate=True,
            )
        )
    for offset, quantity in [(39_000, 700_000), (40_000, 750_000)]:
        price = 0.102
        state.apply_trade(
            TradeEvent(
                symbol="DOGEUSDT",
                event_time=ts - 55_000 + offset,
                trade_time=ts - 55_000 + offset,
                price=price,
                quantity=quantity,
                quote_quantity=price * quantity,
                taker_side=TakerSide.BUY,
                aggregate=True,
            )
        )

    for index in range(6):
        book_ts = ts - 5_000 + index * 500
        state.apply_book_ticker(
            BookTickerEvent(
                symbol="DOGEUSDT",
                event_time=book_ts,
                bid_price=0.1018,
                bid_quantity=100_000,
                ask_price=0.1019,
                ask_quantity=90_000,
            )
        )

    from app.models.events import KlineEvent

    for index in range(15):
        open_time = ts - (15 - index) * 300_000
        close = 0.100 + index * 0.0008
        state.apply_kline(
            KlineEvent(
                symbol="DOGEUSDT",
                event_time=open_time + 299_000,
                open_time=open_time,
                close_time=open_time + 299_999,
                interval="5m",
                high=close * 1.012,
                low=close * 0.988,
                close=close,
                closed=True,
            )
        )

    state.apply_open_interest(OpenInterestEvent("DOGEUSDT", ts - 250_000, 10_000_000))
    state.apply_open_interest(OpenInterestEvent("DOGEUSDT", ts, 11_100_000))

    snapshot = ExpansionScorer().build_snapshot(state)

    assert snapshot.relative_volume > 5
    assert snapshot.aggressive_buy_flow == "Extreme"
    assert snapshot.last_big_order_quote > 5_000
    assert snapshot.last_big_order_multiple > 5
    assert snapshot.last_big_order_volume_share_pct >= 3
    assert snapshot.natr_5m_14 > 0
    assert snapshot.oi_change_pct > 10
    assert snapshot.ignition_probability >= 70
    assert snapshot.direction == "long"
    assert snapshot.impact_score > 0
    assert snapshot.distribution_strength > 0
    assert snapshot.continuation_probability > 0


def test_metal_target_settings_are_per_symbol():
    scorer = ExpansionScorer(metal_targets_usd={"PAXGUSDT": 50, "XAGUSDT": 5})

    paxg_requirements = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=0.6,
        price=4500,
        symbol="PAXGUSDT",
    )
    xag_requirements = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=5.0,
        price=50,
        symbol="XAGUSDT",
    )

    assert paxg_requirements[4] == 50
    assert round(paxg_requirements[3], 2) == 1.11
    assert xag_requirements[4] == 5
    assert xag_requirements[3] == 10


def test_market_cap_changes_required_spot_order_size():
    scorer = ExpansionScorer(target_move_pct=10)

    low_cap = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=2.0,
        price=0.10,
        symbol="TESTUSDT",
        market_cap_usd=20_000_000,
    )
    large_cap = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=2.0,
        price=0.10,
        symbol="TESTUSDT",
        market_cap_usd=2_000_000_000,
    )

    assert low_cap[0] < large_cap[0]
    assert low_cap[1] < large_cap[1]
    assert low_cap[2] < large_cap[2]


def test_high_price_coin_requires_large_enough_print():
    scorer = ExpansionScorer(target_move_pct=10)

    cheap_coin = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=2.0,
        price=0.10,
        symbol="CHEAPUSDT",
    )
    high_price_coin = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=2.0,
        price=18.0,
        symbol="PRICEYUSDT",
    )

    assert high_price_coin[0] >= 45_000
    assert cheap_coin[0] < high_price_coin[0]


def test_qualified_print_estimated_move_respects_selected_target():
    scorer = ExpansionScorer(target_move_pct=7)
    state = SymbolState("TARGETUSDT")
    state.price = 0.10
    state.big_order_price = 0.10
    state.big_order_multiple = 1
    required_quote, required_multiple, required_share, *_ = scorer.adaptive_requirements(
        current_volume=1_000_000,
        natr=2.0,
        price=0.10,
        symbol="TARGETUSDT",
        market_cap_usd=20_000_000,
    )
    state.big_order_multiple = required_multiple * 1.25
    model = scorer.advanced_orderflow_model(
        state=state,
        direction="long",
        has_recent_big_order=True,
        last_order_quote=required_quote * 1.25,
        required_quote=required_quote,
        required_multiple=required_multiple,
        required_share=required_share,
        order_volume_share_pct=required_share * 1.25,
        relative_volume=5,
        buy_ratio=0.72,
        sell_ratio=0.28,
        effective_natr=2.0,
        price_velocity_bps=80,
        expansion_efficiency=70,
        displacement_strength=70,
        liquidity_sensitivity=75,
        cap_sensitivity=80,
        spread_bps=4,
        plan={"impulse_confirmation": "Waiting", "impulse_move_pct": 0, "impulse_required_pct": 0.7},
        target_pct=7,
        target_usd=0,
    )

    assert model["expected_move_pct"] >= 7


def test_entry_plan_confirms_fast_same_direction_impulse():
    state = SymbolState("TESTUSDT")
    ts = now_ms()
    state.big_order_time_ms = ts
    state.big_order_price = 0.10
    for offset, price in [(0, 0.10), (15_000, 0.1004), (35_000, 0.1012)]:
        state.prices.add(ts + offset, price)

    plan = execution_plan(state, "long", 0.1012, 0.11, target_pct=10)

    assert plan["impulse_confirmation"] == "Confirmed"
    assert plan["impulse_candles"] == 1
    assert plan["impulse_move_pct"] >= 1
    assert plan["impulse_stop_price"] > 0


def test_entry_plan_rejects_fragmented_small_candle_move():
    state = SymbolState("TESTUSDT")
    ts = now_ms()
    state.big_order_time_ms = ts
    state.big_order_price = 0.10
    for index, price in enumerate([0.10, 0.1003, 0.1006, 0.1009, 0.1012]):
        state.prices.add(ts + index * 60_000, price)

    plan = execution_plan(state, "long", 0.1012, 0.11, target_pct=10)

    assert plan["impulse_confirmation"] == "Waiting"
    assert "fragmented" in plan["impulse_note"].lower()


def test_impulse_confirmation_requires_ten_percent_of_selected_target():
    assert impulse_required_pct(10, 0, 1) == 1
    assert impulse_required_pct(20, 0, 1) == 2
    assert impulse_required_pct(5, 0, 1) == 0.5


def test_partial_targets_are_30_60_100_percent_of_total_move():
    targets = partial_target_prices(100, "long", 10)

    assert tuple(round(target, 6) for target in targets) == (103, 106, 110)
