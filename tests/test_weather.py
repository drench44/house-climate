import json
from pathlib import Path
from house_climate import weather

FIX = json.loads((Path(__file__).parent / "fixtures" / "wx.json").read_text())

def test_parse_maps_fields():
    s = weather.parse(FIX)
    assert s.ok is True
    assert s.outdoor_temp_f == FIX["temp"]
    assert s.solar_wm2 == FIX["radiation"]
    assert s.fc_high_f == FIX["fcHigh"]
    assert s.alert_count == FIX["alertCount"]

def test_parse_missing_is_null_not_crash():
    s = weather.parse({})
    assert s.ok is True          # parse of a dict is ok; fetch decides staleness
    assert s.outdoor_temp_f is None

def test_fetch_unreachable_url_returns_null():
    """fetch() never raises; returns _NULL on network error."""
    s = weather.fetch("http://127.0.0.1:1/")
    assert s == weather._NULL
    assert s.ok is False

def test_fetch_fallback_both_unreachable_returns_null():
    """fetch() tries fallback if primary fails, returns _NULL if both unreachable."""
    s = weather.fetch("http://127.0.0.1:1/", "http://127.0.0.1:2/")
    assert s == weather._NULL
    assert s.ok is False
