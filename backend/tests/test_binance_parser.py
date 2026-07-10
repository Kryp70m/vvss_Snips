from app.marketdata.binance import BinanceFuturesClient
from app.models.events import BookTickerEvent, KlineEvent, TakerSide, TradeEvent


def test_parse_trade_infers_taker_buy_when_buyer_is_not_maker():
    client = BinanceFuturesClient("wss://example.test/stream", "https://example.test")
    event = client.parse_ws_message(
        '{"stream":"dogeusdt@aggTrade","data":{"e":"aggTrade","E":1000,"s":"DOGEUSDT","p":"0.1200","q":"1000","T":999,"m":false}}'
    )

    assert isinstance(event, TradeEvent)
    assert event.symbol == "DOGEUSDT"
    assert event.taker_side == TakerSide.BUY
    assert event.quote_quantity == 120.0


def test_parse_trade_infers_taker_sell_when_buyer_is_maker():
    client = BinanceFuturesClient("wss://example.test/stream", "https://example.test")
    event = client.parse_ws_message(
        '{"stream":"dogeusdt@trade","data":{"e":"trade","E":1000,"s":"DOGEUSDT","p":"0.1200","q":"1000","T":999,"m":true}}'
    )

    assert isinstance(event, TradeEvent)
    assert event.taker_side == TakerSide.SELL
    assert event.aggregate is False


def test_parse_book_ticker():
    client = BinanceFuturesClient("wss://example.test/stream", "https://example.test")
    event = client.parse_ws_message(
        '{"stream":"dogeusdt@bookTicker","data":{"e":"bookTicker","E":1000,"s":"DOGEUSDT","b":"0.1199","B":"5000","a":"0.1201","A":"4000"}}'
    )

    assert isinstance(event, BookTickerEvent)
    assert event.bid_price == 0.1199
    assert event.ask_quantity == 4000


def test_parse_five_minute_kline():
    client = BinanceFuturesClient("wss://example.test/stream", "https://example.test")
    event = client.parse_ws_message(
        '{"stream":"wifusdt@kline_5m","data":{"e":"kline","E":1000,"s":"WIFUSDT","k":{"t":900,"T":1200,"i":"5m","h":"1.25","l":"1.20","c":"1.23","x":false}}}'
    )

    assert isinstance(event, KlineEvent)
    assert event.symbol == "WIFUSDT"
    assert event.interval == "5m"
    assert event.high == 1.25
