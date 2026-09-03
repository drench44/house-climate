import math

# Magnus formula constants (Alduchov & Eskridge, 1996 — the "AERK" coefficients).
_MAGNUS_A = 17.62
_MAGNUS_B = 243.12  # deg C

# Equipment states that count toward the "cooling" bucket in avg_rh_by_state.
_COOLING_STATES = {"cooling", "overcool"}
_IDLE_STATE = "idle"

# window_advice thresholds, in degrees F of dew point separation, plus the
# outdoor-temp band where opening windows is comfortable at all (below 50 is
# cold, above 74 defeats the point of running AC).
WINDOW_OPEN_DP_MARGIN_F = 3.0
WINDOW_CLOSE_DP_MARGIN_F = 2.0
WINDOW_OPEN_TEMP_MIN_F = 50.0
WINDOW_OPEN_TEMP_MAX_F = 74.0

# avg_rh_by_state / ac_effect: minimum samples in each bucket before the
# cooling-vs-idle comparison is considered meaningful.
AC_EFFECT_MIN_SAMPLES = 5

# US AQI (Open-Meteo outdoor reading) at/above which smoke-season window
# advice overrides the dew-point comparison entirely -- keeping windows shut
# against unhealthy air matters more than moisture exchange.
AQI_UNHEALTHY = 101

# US AQI category bands: (inclusive upper bound, label). Anything above the
# last bound's upper bound is "Hazardous".
_AQI_BANDS = [
    (50, "Good"),
    (100, "Moderate"),
    (150, "Unhealthy for Sensitive"),
    (200, "Unhealthy"),
    (300, "Very Unhealthy"),
]
_AQI_HAZARDOUS = "Hazardous"


def aqi_category(aqi):
    """US-AQI category label for an outdoor AQI value. None if aqi is None."""
    if aqi is None:
        return None
    for upper, label in _AQI_BANDS:
        if aqi <= upper:
            return label
    return _AQI_HAZARDOUS


def dew_point_f(temp_f, rh):
    """Dew point in °F via the Magnus formula. None if inputs are missing or
    rh is non-positive (ln(rh/100) is undefined at rh<=0)."""
    if temp_f is None or rh is None or rh <= 0:
        return None
    temp_c = (temp_f - 32.0) * 5.0 / 9.0
    gamma = math.log(rh / 100.0) + (_MAGNUS_A * temp_c) / (_MAGNUS_B + temp_c)
    dp_c = _MAGNUS_B * gamma / (_MAGNUS_A - gamma)
    return dp_c * 9.0 / 5.0 + 32.0


# Absolute humidity: g of water vapour per m^3 of air. Derived from the same
# Magnus saturation curve as dew_point_f so the two never disagree about the
# same reading. 216.7 = 100 * M_water / R_universal (18.016 / 8.3145), the
# factor that turns vapour pressure in hPa over absolute temperature in K into
# g/m^3 via the ideal gas law.
_VAPOR_DENSITY_K = 216.7
_SAT_VP_HPA_0C = 6.112
_ABS_ZERO_C = -273.15


def saturation_vapor_pressure_hpa(temp_f):
    """Saturation vapour pressure in hPa at a given temperature (Magnus/AERK).
    Evaluated at the dew point it gives the ACTUAL vapour pressure. None when
    temp_f is missing, or at/below absolute zero where the curve is undefined."""
    if temp_f is None:
        return None
    temp_c = (temp_f - 32.0) * 5.0 / 9.0
    if temp_c <= _ABS_ZERO_C:
        return None
    return _SAT_VP_HPA_0C * math.exp(_MAGNUS_A * temp_c / (_MAGNUS_B + temp_c))


def absolute_humidity_gm3(temp_f, rh):
    """Absolute humidity in g/m^3 from air temperature and relative humidity.

    Unlike RH this is a real moisture CONTENT, so it is comparable between a
    55F crawl space and a 72F hallway — which is exactly what the crawl-to-
    floor gap needs. None if inputs are missing or rh is non-positive."""
    if temp_f is None or rh is None or rh <= 0:
        return None
    es = saturation_vapor_pressure_hpa(temp_f)
    if es is None:
        return None
    return _vapor_density(es * rh / 100.0, temp_f)


def absolute_humidity_from_dew_point_gm3(temp_f, dew_point_f_val):
    """Absolute humidity from air temperature and dew point — the path the SQL
    rollups take, since dewpoint_f is stored first-class while RH is averaged
    per bucket. Agrees with absolute_humidity_gm3 on the same reading."""
    if temp_f is None or dew_point_f_val is None:
        return None
    e = saturation_vapor_pressure_hpa(dew_point_f_val)
    if e is None:
        return None
    return _vapor_density(e, temp_f)


def _vapor_density(vapor_pressure_hpa, temp_f):
    """Ideal-gas conversion of vapour pressure (hPa) at an air temperature to
    vapour density (g/m^3). The AIR temperature sets the density, not the dew
    point — that is why the two arguments are separate."""
    temp_c = (temp_f - 32.0) * 5.0 / 9.0
    if temp_c <= _ABS_ZERO_C:
        return None
    return _VAPOR_DENSITY_K * vapor_pressure_hpa / (temp_c - _ABS_ZERO_C)


def avg_rh_by_state(readings):
    """Mean indoor_humidity while the equipment is actively cooling
    (cooling/overcool) vs idle. Readings in any other state (heating, fan)
    are excluded from both buckets. None for an empty bucket."""
    cooling_vals = []
    idle_vals = []
    for r in readings:
        rh = r.get("indoor_humidity")
        if rh is None:
            continue
        status = r.get("equipment_status")
        if status in _COOLING_STATES:
            cooling_vals.append(rh)
        elif status == _IDLE_STATE:
            idle_vals.append(rh)
    return {
        "cooling": (sum(cooling_vals) / len(cooling_vals)) if cooling_vals else None,
        "idle": (sum(idle_vals) / len(idle_vals)) if idle_vals else None,
        "cooling_n": len(cooling_vals),
        "idle_n": len(idle_vals),
    }


def window_advice(indoor_dp, outdoor_dp, outdoor_temp_f, outdoor_aqi=None):
    """Whether opening windows would help, based on dew point (not RH) —
    dew point is the moisture measure that doesn't move with temperature.

    Outdoor AQI, when given, is checked FIRST and overrides the dew-point
    comparison entirely once air quality is unhealthy: smoke beats moisture."""
    if outdoor_aqi is not None and outdoor_aqi >= AQI_UNHEALTHY:
        return {"action": "keep_closed",
                "reason": f"Outdoor air is poor (AQI {int(round(outdoor_aqi))}, {aqi_category(outdoor_aqi)}) "
                          "— keep windows shut and run purifiers."}
    have_both_dp = indoor_dp is not None and outdoor_dp is not None
    if (have_both_dp
            and outdoor_dp <= indoor_dp - WINDOW_OPEN_DP_MARGIN_F
            and outdoor_temp_f is not None
            and WINDOW_OPEN_TEMP_MIN_F <= outdoor_temp_f <= WINDOW_OPEN_TEMP_MAX_F):
        return {"action": "open",
                "reason": "Outside air is drier — opening windows would lower indoor moisture."}
    if have_both_dp and outdoor_dp >= indoor_dp + WINDOW_CLOSE_DP_MARGIN_F:
        return {"action": "keep_closed",
                "reason": "Outside is more humid — opening windows would add moisture."}
    return {"action": "neutral",
            "reason": "Little to gain from opening windows right now."}
