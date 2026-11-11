import pytest

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


# The real GW1100 payload shape (values changed): the outdoor sensor's battery
# flag rides on the 0x03 (dew point) entry, not on 0x02/0x07, and the gateway
# also lists derived entries ("3" feels-like, "5" VPD) with no battery at all.
LIVE_OUT_REAL = {
    "common_list": [
        {"id": "0x02", "val": "63.0", "unit": "F"},
        {"id": "0x07", "val": "80%"},
        {"id": "3", "val": "63.0", "unit": "F"},
        {"id": "5", "val": "0.392 kPa"},
        {"id": "0x03", "val": "56.7", "unit": "F", "battery": "1"},
    ],
    "ch_aisle": [],
}


def test_parse_outdoor_reads_battery_from_the_dew_point_entry():
    o = ecowitt.parse_outdoor(LIVE_OUT_REAL, "Crawl Space")
    assert o["temp_f"] == 63.0
    assert o["humidity"] == 80.0
    assert o["battery_low"] is True


def test_parse_outdoor_battery_ok_on_real_shape():
    data = {"common_list": [dict(c) for c in LIVE_OUT_REAL["common_list"]]}
    data["common_list"][-1]["battery"] = "0"
    assert ecowitt.parse_outdoor(data, "Crawl Space")["battery_low"] is False


def test_parse_outdoor_ignores_battery_on_unrelated_entries():
    # Only the outdoor sensor's own entries may flag it low.
    data = {"common_list": [{"id": "0x02", "val": "63.0", "unit": "F"},
                            {"id": "0x07", "val": "80%"},
                            {"id": "0x19", "val": "5.0", "battery": "1"}]}
    assert ecowitt.parse_outdoor(data, "Crawl Space")["battery_low"] is False


# A gateway switched to metric reports Celsius with unit "C". Storing that as
# Fahrenheit would silently corrupt every temperature and dew point.
def test_parse_channels_converts_celsius():
    data = {"ch_aisle": [{"channel": "8", "battery": "0", "temp": "25.0",
                          "unit": "C", "humidity": "40%"}]}
    r = ecowitt.parse_channels(data, {"8": "Upstairs"})[0]
    assert r["temp_f"] == pytest.approx(77.0)
    assert r["humidity"] == 40.0


def test_parse_outdoor_converts_celsius():
    data = {"common_list": [{"id": "0x02", "val": "17.2", "unit": "C"},
                            {"id": "0x07", "val": "80%"}]}
    assert ecowitt.parse_outdoor(data, "Crawl")["temp_f"] == pytest.approx(62.96)


def test_parse_unknown_temperature_unit_fails_loudly():
    data = {"ch_aisle": [{"channel": "8", "temp": "300.1", "unit": "K",
                          "humidity": "40%"}]}
    with pytest.raises(ValueError):
        ecowitt.parse_channels(data, {"8": "Upstairs"})
    with pytest.raises(ValueError):
        ecowitt.parse_outdoor({"common_list": [{"id": "0x02", "val": "300", "unit": "K"}]}, "C")


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


@pytest.mark.parametrize("unit", ["C", "c", " °C ", "℃"])
def test_parse_channels_accepts_celsius_spellings(unit):
    data = {"ch_aisle": [{"channel": "8", "temp": "0", "unit": unit, "humidity": "40%"}]}
    assert ecowitt.parse_channels(data, {"8": "Up"})[0]["temp_f"] == pytest.approx(32.0)


# sensors_info as a GW1100B on firmware 2.4.5 reports it, with rssi.
SENSORS_INFO_RSSI = [
    {"img": "wh26", "name": "Temp & Humidity", "id": "BD", "batt": "0", "rssi": "-46", "signal": "4"},
    {"img": "wh31", "name": "Temp & Humidity CH1", "id": "EB", "batt": "9", "rssi": "--", "signal": "--"},
    {"img": "wh31", "name": "Temp & Humidity CH7", "id": "AF", "batt": "0", "rssi": "-55", "signal": "4"},
]


def test_rssi_by_sensor_id_maps_dbm_and_skips_phantoms():
    m = ecowitt.rssi_by_sensor_id(SENSORS_INFO_RSSI)
    assert m == {"ecowitt_outdoor": -46, "ecowitt_ch7": -55}   # CH1 '--' skipped


def test_rssi_by_sensor_id_empty_when_gateway_does_not_report_it():
    # SENSORS_INFO has no rssi key at all (older firmware): nothing, no error.
    assert ecowitt.rssi_by_sensor_id(SENSORS_INFO) == {}
    assert ecowitt.signal_by_sensor_id(SENSORS_INFO)["ecowitt_ch7"] == 4   # bars unaffected


def test_parse_gateway_reads_room_and_both_pressures():
    data = {"wh25": [{"intemp": "72.1", "unit": "F", "inhumi": "50%",
                      "abs": "29.67 inHg", "rel": "29.71 inHg"}]}
    g = ecowitt.parse_gateway(data)
    assert g == {"sensor_id": "ecowitt_gateway", "temp_f": 72.1, "humidity": 50.0,
                 "abs_pressure_inhg": pytest.approx(29.67),
                 "rel_pressure_inhg": pytest.approx(29.71)}


@pytest.mark.parametrize("raw,inhg", [
    ("1004.7 hPa", 29.669), ("1004.7hpa", 29.669), ("753.6 mmHg", 29.669),
    ("100.47 kPa", 29.669), (" 29.67 inHg ", 29.67),
])
def test_parse_gateway_converts_pressure_units(raw, inhg):
    g = ecowitt.parse_gateway({"wh25": [{"abs": raw}]})
    assert g["abs_pressure_inhg"] == pytest.approx(inhg, abs=0.005)
    assert g["rel_pressure_inhg"] is None and g["temp_f"] is None


@pytest.mark.parametrize("raw", ["29.67", "1004.7 furlongs", "-- inHg", "inHg",
                                 "0.00 inHg", "2967 inHg", "nan inHg"])
def test_parse_gateway_unknown_pressure_fails_loudly(raw):
    # A bare number or an unknown unit must not be stored on a guessed scale.
    with pytest.raises(ValueError):
        ecowitt.parse_gateway({"wh25": [{"intemp": "72.1", "unit": "F", "abs": raw}]})


def test_parse_gateway_without_pressure_keeps_room_reading():
    # LIVE's wh25 block has no abs/rel at all: temp and humidity still parse.
    g = ecowitt.parse_gateway(LIVE)
    assert (g["temp_f"], g["humidity"]) == (77.5, 41.0)
    assert g["abs_pressure_inhg"] is None and g["rel_pressure_inhg"] is None


@pytest.mark.parametrize("data", [{}, {"wh25": []}, {"wh25": [{}]},
                                  {"wh25": [{"intemp": "", "inhumi": "", "abs": " "}]}])
def test_parse_gateway_none_when_absent_or_empty(data):
    assert ecowitt.parse_gateway(data) is None


@pytest.mark.parametrize("blank", ["--", " -- ", "", None])
def test_parse_gateway_no_reading_mark_is_none_not_an_error(blank):
    # A barometer showing '--' must not cost the room's temp and humidity.
    g = ecowitt.parse_gateway({"wh25": [{"intemp": "72.1", "unit": "F", "abs": blank}]})
    assert g["temp_f"] == 72.1 and g["abs_pressure_inhg"] is None


def test_parse_gateway_converts_celsius_and_rejects_unknown_unit():
    g = ecowitt.parse_gateway({"wh25": [{"intemp": "22.5", "unit": "C", "inhumi": "40%"}]})
    assert g["temp_f"] == pytest.approx(72.5)
    with pytest.raises(ValueError):
        ecowitt.parse_gateway({"wh25": [{"intemp": "295.6", "unit": "K"}]})


def test_parse_gateway_missing_unit_reads_as_fahrenheit():
    assert ecowitt.parse_gateway({"wh25": [{"intemp": "72.1"}]})["temp_f"] == 72.1


def test_rssi_without_signal_and_non_finite_values():
    info = [
        {"img": "wh31", "name": "Temp & Humidity CH7", "id": "AF", "rssi": "-50", "signal": "--"},
        {"img": "wh31", "name": "Temp & Humidity CH8", "id": "EB", "rssi": "nan", "signal": "inf"},
        {"img": "wh26", "name": "Temp & Humidity", "id": "BD", "rssi": "-67.9", "signal": "4"},
    ]
    assert ecowitt.rssi_by_sensor_id(info) == {"ecowitt_ch7": -50, "ecowitt_outdoor": -68}
    assert ecowitt.signal_by_sensor_id(info) == {"ecowitt_outdoor": 4}
