from house_climate.analytics import humidity


def test_dew_point_known_value():
    # 77°F / 50% RH ≈ 56.7°F dew point (Magnus formula, standard reference value).
    dp = humidity.dew_point_f(77, 50)
    assert dp is not None
    assert abs(dp - 56.7) < 0.5


def test_dew_point_none_when_temp_missing():
    assert humidity.dew_point_f(None, 50) is None


def test_dew_point_none_when_rh_missing():
    assert humidity.dew_point_f(77, None) is None


def test_dew_point_none_when_rh_zero_or_negative():
    assert humidity.dew_point_f(77, 0) is None
    assert humidity.dew_point_f(77, -5) is None


def test_dew_point_monotonic_with_rh():
    # Higher RH at the same temp must mean a higher (closer to temp) dew point.
    lo = humidity.dew_point_f(77, 30)
    hi = humidity.dew_point_f(77, 70)
    assert hi > lo


TZ = "America/Los_Angeles"


def _r(status, rh, hour=12, minute=0, day=10):
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    local = datetime(2026, 8, day, hour, minute, tzinfo=ZoneInfo(TZ))
    return {"ts": local.astimezone(timezone.utc), "equipment_status": status,
            "indoor_humidity": rh}


def test_same_hour_groups_cooling_and_idle():
    readings = [
        _r("cooling", 40), _r("cooling", 42), _r("overcool", 44),
        _r("idle", 50), _r("idle", 52), _r("idle", 54),
        _r("heating", 60),  # excluded from both groups
        _r("fan", 45),      # excluded from both groups
    ]
    res = humidity.rh_by_state_same_hour(readings, TZ)
    assert res["cooling_n"] == 3 and res["idle_n"] == 3
    assert res["hours_matched"] == 1
    assert abs(res["cooling"] - 42.0) < 1e-9
    assert abs(res["idle"] - 52.0) < 1e-9


def test_same_hour_needs_a_real_baseline_on_both_sides():
    """One idle reading is not a baseline for a busy cooling hour."""
    readings = [_r("cooling", 40, 15, m) for m in range(0, 60, 5)] + [_r("idle", 70, 15, 59)]
    res = humidity.rh_by_state_same_hour(readings, TZ)
    assert res["hours_matched"] == 0 and res["cooling"] is None


def test_same_hour_weights_hours_by_cooling_readings():
    """Hours are weighted by how much cooling they hold: a mostly-cooling
    afternoon counts for more than an hour with a few cooling readings."""
    readings = ([_r("cooling", 40, 15, m) for m in range(0, 54, 6)]      # 9 cooling
                + [_r("idle", 50, 15, m) for m in (55, 57, 59)]
                + [_r("cooling", 60, 9, m) for m in (0, 5, 10)]           # 3 cooling
                + [_r("idle", 62, 9, m) for m in (20, 30, 40)])
    res = humidity.rh_by_state_same_hour(readings, TZ)
    assert abs(res["cooling"] - (9 * 40 + 3 * 60) / 12) < 1e-9
    assert abs(res["idle"] - (9 * 50 + 3 * 62) / 12) < 1e-9


def test_same_hour_empty_and_unmatched_are_none():
    for readings in ([], [_r("heating", 60), _r("fan", 45)],
                     [_r("cooling", 40, hour=15), _r("idle", 60, hour=3)]):
        res = humidity.rh_by_state_same_hour(readings, TZ)
        assert res["cooling"] is None and res["idle"] is None
        assert res["hours_matched"] == 0


def test_same_hour_skips_missing_rh():
    res = humidity.rh_by_state_same_hour(
        [_r("cooling", None)] + [_r("cooling", 40, 12, m) for m in (1, 2, 3)]
        + [_r("idle", 50, 12, m) for m in (4, 5, 6)], TZ)
    assert res["cooling_n"] == 3
    assert res["cooling"] == 40


def test_same_hour_does_not_credit_the_daily_cycle_to_the_ac():
    """The AC runs on hot afternoons, when indoor RH is low anyway; nights are
    idle and damp. A plain average of cooling vs idle readings called that a
    12-point drop 'from cooling'. Within the same hours the real difference
    here is 1 point, and that is what must be reported."""
    readings = []
    for day in range(10, 17):
        for hour in range(14, 19):              # afternoons: mostly cooling
            for m in (0, 15, 30):
                readings.append(_r("cooling", 45, hour, m, day))
            readings.append(_r("idle", 46, hour, 45, day))
        for hour in (0, 1, 2, 3, 4, 5):         # nights: idle and damp
            for m in (0, 20, 40):
                readings.append(_r("idle", 60, hour, m, day))
    res = humidity.rh_by_state_same_hour(readings, TZ)
    assert res["hours_matched"] == 5
    assert abs((res["idle"] - res["cooling"]) - 1.0) < 1e-9


def test_window_advice_open_when_outside_much_drier_and_mild():
    # Outdoor dew point well below indoor, and outdoor temp inside the mild
    # window. If the comparison direction were inverted this would wrongly
    # report keep_closed (outdoor 55 vs indoor+2=62 is not >=, so an inverted
    # implementation would NOT also produce "open" by accident).
    res = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65)
    assert res["action"] == "open"
    assert "reason" in res and res["reason"]


def test_window_advice_keep_closed_when_outside_more_humid():
    # Outdoor dew point well above indoor. If the comparison direction were
    # inverted, this case (outdoor 65 vs indoor-3=57, 65 <= 57 is False) would
    # NOT produce "open" either, so the two tests together pin the direction.
    res = humidity.window_advice(indoor_dp=60, outdoor_dp=65, outdoor_temp_f=65)
    assert res["action"] == "keep_closed"
    assert "reason" in res and res["reason"]


def test_window_advice_neutral_when_close():
    res = humidity.window_advice(indoor_dp=60, outdoor_dp=59, outdoor_temp_f=65)
    assert res["action"] == "neutral"


def test_window_advice_neutral_when_outdoor_temp_out_of_mild_range():
    # Outside is drier by enough, but it's 95°F out -- don't suggest opening
    # windows into a heat wave even though the dew-point math looks good.
    res = humidity.window_advice(indoor_dp=60, outdoor_dp=50, outdoor_temp_f=95)
    assert res["action"] == "neutral"


def test_window_advice_neutral_when_missing_data():
    assert humidity.window_advice(None, 55, 65)["action"] == "neutral"
    assert humidity.window_advice(60, None, 65)["action"] == "neutral"
    assert humidity.window_advice(60, 55, None)["action"] == "neutral"


def test_aqi_category_bands():
    assert humidity.aqi_category(0) == "Good"
    assert humidity.aqi_category(50) == "Good"
    assert humidity.aqi_category(51) == "Moderate"
    assert humidity.aqi_category(100) == "Moderate"
    assert humidity.aqi_category(101) == "Unhealthy for Sensitive"
    assert humidity.aqi_category(150) == "Unhealthy for Sensitive"
    assert humidity.aqi_category(151) == "Unhealthy"
    assert humidity.aqi_category(200) == "Unhealthy"
    assert humidity.aqi_category(201) == "Very Unhealthy"
    assert humidity.aqi_category(300) == "Very Unhealthy"
    assert humidity.aqi_category(301) == "Hazardous"
    assert humidity.aqi_category(500) == "Hazardous"


def test_aqi_category_none_when_missing():
    assert humidity.aqi_category(None) is None


def test_window_advice_aqi_override_beats_open_case():
    # Non-vacuous: the SAME dew-point/temp inputs that produced "open" in
    # test_window_advice_open_when_outside_much_drier_and_mild must flip to
    # keep_closed once outdoor AQI crosses the unhealthy threshold -- smoke
    # beats moisture.
    good_aqi = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65, outdoor_aqi=30)
    assert good_aqi["action"] == "open"

    smoky = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65,
                                    outdoor_aqi=humidity.AQI_UNHEALTHY)
    assert smoky["action"] == "keep_closed"
    assert "AQI" in smoky["reason"]
    assert str(humidity.AQI_UNHEALTHY) in smoky["reason"]


def test_window_advice_aqi_none_keeps_prior_behavior():
    with_none = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65, outdoor_aqi=None)
    without_param = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65)
    assert with_none == without_param == {"action": "open",
                                           "reason": "Outside air is drier — opening windows would lower indoor moisture."}


# --- absolute humidity -------------------------------------------------------

def test_absolute_humidity_known_value():
    # 68°F (20°C) / 50% RH ≈ 8.6 g/m³ — the standard psychrometric reference.
    ah = humidity.absolute_humidity_gm3(68, 50)
    assert ah is not None
    assert abs(ah - 8.6) < 0.15


def test_absolute_humidity_saturated_matches_saturation_density():
    # At 100% RH and 86°F (30°C), saturation vapour density is ≈ 30.4 g/m³.
    ah = humidity.absolute_humidity_gm3(86, 100)
    assert abs(ah - 30.4) < 0.5


def test_absolute_humidity_none_when_inputs_missing():
    assert humidity.absolute_humidity_gm3(None, 50) is None
    assert humidity.absolute_humidity_gm3(68, None) is None


def test_absolute_humidity_none_when_rh_non_positive():
    assert humidity.absolute_humidity_gm3(68, 0) is None
    assert humidity.absolute_humidity_gm3(68, -5) is None


def test_absolute_humidity_monotonic_with_rh():
    assert humidity.absolute_humidity_gm3(68, 70) > humidity.absolute_humidity_gm3(68, 30)


def test_absolute_humidity_equal_moisture_at_different_temps():
    """The whole point of AH over RH: two rooms holding the SAME water vapour
    at different temperatures must report (nearly) the same absolute humidity,
    even though their relative humidities differ a lot."""
    # A 50°F crawl at 90% RH and a 70°F room share a dew point near 47.5°F.
    dp = humidity.dew_point_f(50, 90)
    # RH the 70°F room needs to sit at that same dew point.
    warm_rh = 100.0 * (
        humidity.saturation_vapor_pressure_hpa(dp)
        / humidity.saturation_vapor_pressure_hpa(70))
    cold_ah = humidity.absolute_humidity_gm3(50, 90)
    warm_ah = humidity.absolute_humidity_gm3(70, warm_rh)
    # Same vapour pressure, but AH is per unit VOLUME, so the warmer (less
    # dense) air holds slightly less per m³ — a few percent, not a factor.
    assert abs(cold_ah - warm_ah) / cold_ah < 0.05


def test_absolute_humidity_from_dew_point_matches_direct():
    """The SQL rollups compute AH from stored temp + dewpoint; the Python path
    computes it from temp + RH. They must agree, or daily means and live tiles
    would disagree on the same reading."""
    direct = humidity.absolute_humidity_gm3(72, 55)
    via_dp = humidity.absolute_humidity_from_dew_point_gm3(72, humidity.dew_point_f(72, 55))
    assert abs(direct - via_dp) < 0.01


def test_absolute_humidity_from_dew_point_none_when_missing():
    assert humidity.absolute_humidity_from_dew_point_gm3(None, 50) is None
    assert humidity.absolute_humidity_from_dew_point_gm3(72, None) is None


# --- the windows verdict must say what KIND of AQI it acted on ---------------
# resolve_outdoor_aqi silently swaps a real monitor reading for the weather
# feed's MODEL after 30 quiet minutes, and this verdict is the most declarative
# thing on the page: it prints the number in a full sentence AND gives an
# order. It sits inches below a chip that now says "est."; leaving it
# unqualified was the surface the first pass of this fix missed entirely.

def _aqi_reason(source):
    res = humidity.window_advice(indoor_dp=60, outdoor_dp=55, outdoor_temp_f=65,
                                 outdoor_aqi=humidity.AQI_UNHEALTHY + 12,
                                 aqi_source=source)
    assert res["action"] == "keep_closed", res
    return res["reason"]


def test_window_advice_marks_a_modeled_aqi_as_estimated():
    assert "estimated" in _aqi_reason("weather")


def test_window_advice_does_not_hedge_a_real_monitor_reading():
    """Non-vacuous companion: hedging every verdict would train the reader to
    ignore the hedge."""
    reason = _aqi_reason("airnow")
    assert "estimated" not in reason
    assert str(humidity.AQI_UNHEALTHY + 12) in reason


def test_window_advice_treats_unknown_provenance_as_estimated():
    """The dangerous default, and the one that actually fires: every caller
    predating the argument omits it. Unknown provenance must fail toward "we
    are not sure", never toward an unearned claim of a real reading."""
    assert "estimated" in _aqi_reason(None)


def test_window_advice_aqi_override_still_beats_dew_point():
    """The mark must not change the DECISION -- this fixture is a textbook
    "open the windows" dew-point case, and unhealthy air still overrides it."""
    dry_and_mild = humidity.window_advice(indoor_dp=60, outdoor_dp=55,
                                          outdoor_temp_f=65)
    assert dry_and_mild["action"] == "open"
    assert _aqi_reason("weather")   # same inputs + bad air -> keep_closed
