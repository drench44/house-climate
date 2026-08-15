from house_climate import ecowitt

# Shape of a real GW1100B /get_livedata_info response (trimmed).
LIVE = {
    "wh25": [{"intemp": "77.5", "unit": "F", "inhumi": "41%"}],
    "ch_aisle": [
        {"channel": "8", "name": "", "battery": "0", "temp": "78.6", "unit": "F", "humidity": "40%"},
        {"channel": "7", "name": "", "battery": "1", "temp": "72.1", "unit": "F", "humidity": "55%"},
    ],
}


def test_parse_channels_maps_names_units_and_battery():
    rows = ecowitt.parse_channels(LIVE, {"8": "Upstairs", "7": "Downstairs", "6": "Crawl Space"})
    by = {r["channel"]: r for r in rows}
    assert by["8"]["name"] == "Upstairs"
    assert by["8"]["sensor_id"] == "ecowitt_ch8"
    assert by["8"]["temp_f"] == 78.6
    assert by["8"]["humidity"] == 40.0          # trailing % stripped
    assert by["8"]["battery_low"] is False       # "0" = ok
    assert by["7"]["battery_low"] is True        # "1" = low
    assert "6" not in by                          # configured but not reporting yet


def test_parse_channels_ignores_unconfigured_channels():
    rows = ecowitt.parse_channels(LIVE, {"8": "Upstairs"})
    assert [r["channel"] for r in rows] == ["8"]


def test_parse_channels_handles_missing_and_bad_values():
    data = {"ch_aisle": [{"channel": "8", "temp": None, "humidity": "--", "battery": ""}]}
    r = ecowitt.parse_channels(data, {"8": "Upstairs"})[0]
    assert r["temp_f"] is None and r["humidity"] is None
    assert r["battery_low"] is False             # unparseable battery is not "low"


# Outdoor T&H sensor (WH32) in common_list — used as the crawlspace probe.
LIVE_OUT = {
    "common_list": [
        {"id": "0x02", "val": "94.6", "unit": "F"},
        {"id": "0x07", "val": "28%"},
        {"id": "0x03", "val": "56.5", "unit": "F", "battery": "0"},
    ],
    "ch_aisle": [],
}


def test_parse_outdoor_maps_temp_humidity_battery():
    o = ecowitt.parse_outdoor(LIVE_OUT, "Crawl Space")
    assert o["sensor_id"] == "ecowitt_outdoor"
    assert o["name"] == "Crawl Space"
    assert o["temp_f"] == 94.6
    assert o["humidity"] == 28.0
    assert o["battery_low"] is False


def test_parse_outdoor_none_when_absent():
    assert ecowitt.parse_outdoor({"common_list": []}, "Crawl Space") is None


SENSORS_INFO = [
    {"img": "wh31", "name": "Temp & Humidity CH7", "id": "AF", "batt": "0", "signal": "4"},
    {"img": "wh31", "name": "Temp & Humidity CH8", "id": "EB", "batt": "0", "signal": "3"},
    {"img": "wh26", "name": "Temp & Humidity", "id": "BD", "batt": "0", "signal": "4"},
    {"img": "wh31", "name": "Temp & Humidity CH1", "id": "EB", "batt": "9", "signal": "--"},
    {"img": "wh40", "name": "Rain", "id": "FFFFFFFF", "batt": "9", "signal": "--"},
]


def test_signal_by_sensor_id_maps_channels_and_outdoor():
    m = ecowitt.signal_by_sensor_id(SENSORS_INFO)
    assert m["ecowitt_ch7"] == 4
    assert m["ecowitt_ch8"] == 3
    assert m["ecowitt_outdoor"] == 4          # wh26/wh32 -> outdoor slot
    assert "ecowitt_ch1" not in m             # signal '--' (phantom) skipped
