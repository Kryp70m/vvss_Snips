from app.marketdata.perp import BinancePerpClient, BybitPerpClient, MexcPerpClient
from app.models.events import BookTickerEvent, TakerSide, TradeEvent


def test_binance_perp_parse_agg_trade_uses_perp_exchange():
    client = BinancePerpClient("wss://example.test/stream")

    event = client.parse_ws_message(
        '{"stream":"dogeusdt@aggTrade","data":{"e":"aggTrade","E":1000,"s":"DOGEUSDT","p":"0.12","q":"1000","T":999,"m":false}}'
    )

    assert isinstance(event, TradeEvent)
    assert event.exchange == "binance_perp"
    assert event.symbol == "DOGEUSDT"
    assert event.taker_side == TakerSide.BUY
    assert event.quote_quantity == 120


def test_binance_perp_parse_book_ticker_uses_perp_exchange():
    client = BinancePerpClient("wss://example.test/stream")

    event = client.parse_ws_message(
        '{"stream":"dogeusdt@bookTicker","data":{"e":"bookTicker","E":1000,"s":"DOGEUSDT","b":"0.1199","B":"5000","a":"0.1201","A":"4000"}}'
    )

    assert isinstance(event, BookTickerEvent)
    assert event.exchange == "binance_perp"


def test_bybit_perp_parse_public_trade():
    client = BybitPerpClient("wss://example.test/v5/public/linear")

    events = client.parse_ws_message(
        '{"topic":"publicTrade.WIFUSDT","type":"snapshot","ts":1000,"data":[{"T":999,"s":"WIFUSDT","S":"Sell","v":"2500","p":"1.25"}]}'
    )

    assert len(events) == 1
    assert events[0].exchange == "bybit_perp"
    assert events[0].taker_side == TakerSide.SELL
    assert events[0].quote_quantity == 3125


def test_mexc_perp_parse_push_deal():
    client = MexcPerpClient("wss://example.test/edge")

    events = client.parse_ws_message(
        '{"channel":"push.deal","symbol":"PEPE_USDT","ts":1000,"data":{"p":0.000012,"v":100000000,"T":1,"t":999}}'
    )

    assert len(events) == 1
    assert events[0].exchange == "mexc_perp"
    assert events[0].symbol == "PEPEUSDT"
    assert events[0].taker_side == TakerSide.BUY
    assert events[0].quote_quantity == 1200
