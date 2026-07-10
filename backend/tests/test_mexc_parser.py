from app.marketdata.mexc import MexcSpotClient
from app.models.events import TakerSide


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(field_number: int, value: int) -> bytes:
    return _varint((field_number << 3) | 0) + _varint(value)


def _field_string(field_number: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _varint((field_number << 3) | 2) + _varint(len(raw)) + raw


def _field_message(field_number: int, value: bytes) -> bytes:
    return _varint((field_number << 3) | 2) + _varint(len(value)) + value


def test_parse_mexc_public_aggre_deals_protobuf_trade():
    client = MexcSpotClient("wss://example.test/ws")
    trade_time = 1_736_409_765_051
    deal = (
        _field_string(1, "0.1234")
        + _field_string(2, "10000")
        + _field_varint(3, 1)
        + _field_varint(4, trade_time)
    )
    public_deals = _field_message(1, deal) + _field_string(2, "spot@public.aggre.deals.v3.api.pb@100ms")
    wrapper = (
        _field_string(1, "spot@public.aggre.deals.v3.api.pb@100ms@TESTUSDT")
        + _field_message(314, public_deals)
        + _field_string(3, "TESTUSDT")
        + _field_varint(6, trade_time + 1)
    )

    events = client.parse_ws_message(wrapper)

    assert len(events) == 1
    assert events[0].exchange == "mexc"
    assert events[0].symbol == "TESTUSDT"
    assert events[0].taker_side == TakerSide.BUY
    assert events[0].price == 0.1234
    assert events[0].quantity == 10000
    assert events[0].quote_quantity == 1234


def test_mexc_clean_symbols_normalizes_usdt_pairs():
    client = MexcSpotClient("wss://example.test/ws")

    assert client.clean_symbols(["pepe", "MEXC:mxusdt", "wif/usdt", "pepeusdt"], 10) == [
        "PEPEUSDT",
        "MXUSDT",
        "WIFUSDT",
    ]
