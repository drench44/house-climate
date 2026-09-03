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


def _r(status, rh):
    return {"equipment_status": status, "indoor_humidity": rh}


def test_avg_rh_by_state_groups_cooling_and_idle():
    readings = [
        _r("cooling", 40), _r("cooling", 42), _r("overcool", 44),
        _r("idle", 50), _r("idle", 52),
        _r("heating", 60),  # excluded from both groups
        _r("fan", 45),      # excluded from both groups
    ]
    res = humidity.avg_rh_by_state(readings)
    assert res["cooling_n"] == 3
    assert res["idle_n"] == 2
    assert abs(res["cooling"] - 42.0) < 1e-9
    assert abs(res["idle"] - 51.0) < 1e-9


def test_avg_rh_by_state_empty_groups_are_none():
    res = humidity.avg_rh_by_state([_r("heating", 60), _r("fan", 45)])
    assert res == {"cooling": None, "idle": None, "cooling_n": 0, "idle_n": 0}


def test_avg_rh_by_state_empty_input():
    res = humidity.avg_rh_by_state([])
    assert res == {"cooling": None, "idle": None, "cooling_n": 0, "idle_n": 0}


def test_avg_rh_by_state_skips_missing_rh():
    readings = [_r("cooling", None), _r("cooling", 40)]
    res = humidity.avg_rh_by_state(readings)
    assert res["cooling_n"] == 1
    assert res["cooling"] == 40


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
