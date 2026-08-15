from datetime import datetime, timezone, date
import pytest
from house_climate.config import load_config
from house_climate.analytics import correlation

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)
TZ = "America/Los_Angeles"

def test_cdd_positive_when_hot():
    rows = [{"ts": datetime(2026, 8, 10, h, tzinfo=timezone.utc),
             "wx_outdoor_temp_f": 85.0} for h in range(0, 24)]
    cdd = correlation.cooling_degree_days(rows, base_f=65.0, tz=TZ)
    assert cdd > 0

def test_predict_uses_mean_when_sparse():
    history = [{"day_high": 88.0, "cool_minutes": 100.0, "peak_cool_minutes": 40.0},
               {"day_high": 92.0, "cool_minutes": 300.0, "peak_cool_minutes": 120.0}]
    out = correlation.predict_peak_cost(95.0, history, CFG.tou, CFG.system_kw, TZ)
    assert out["predicted_cool_minutes"] == 200.0     # falls back to mean (100+300)/2
    assert out["predicted_peak_cool_minutes"] == 80.0
    assert out["basis"] == "historical mean"
    assert out["predicted_peak_dollars"] >= 0

def test_predict_uses_linear_fit_when_sufficient_history():
    history = [{"day_high": 70.0, "cool_minutes": 60.0, "peak_cool_minutes": 20.0},
               {"day_high": 85.0, "cool_minutes": 180.0, "peak_cool_minutes": 60.0},
               {"day_high": 100.0, "cool_minutes": 300.0, "peak_cool_minutes": 110.0}]
    pred_low = correlation.predict_peak_cost(70.0, history, CFG.tou, CFG.system_kw, TZ)
    pred_high = correlation.predict_peak_cost(100.0, history, CFG.tou, CFG.system_kw, TZ)
    assert pred_low["basis"] == "linear fit"
    assert pred_high["basis"] == "linear fit"
    assert pred_high["predicted_cool_minutes"] > pred_low["predicted_cool_minutes"]

_HIST = [{"day_high": 70.0, "cool_minutes": 60.0, "peak_cool_minutes": 20.0},
         {"day_high": 85.0, "cool_minutes": 180.0, "peak_cool_minutes": 60.0},
         {"day_high": 100.0, "cool_minutes": 300.0, "peak_cool_minutes": 110.0}]


def test_predict_peak_cost_not_seasonal():
    # The example TOU schedule is not seasonal: the on-peak rate applies
    # year-round, so a January WEEKDAY and an August WEEKDAY price identically.
    august = correlation.predict_peak_cost(95.0, _HIST, CFG.tou, CFG.system_kw, TZ, target_date=date(2026, 8, 14))  # Friday
    january = correlation.predict_peak_cost(95.0, _HIST, CFG.tou, CFG.system_kw, TZ, target_date=date(2026, 1, 15))  # Thursday
    assert august["predicted_cool_minutes"] == january["predicted_cool_minutes"]
    assert august["predicted_peak_dollars"] == pytest.approx(january["predicted_peak_dollars"])
    assert august["peak_band"] == "peak"


def test_predict_prices_peak_window_minutes_not_whole_day():
    # The dollars must come from PEAK-WINDOW minutes at the peak rate — the
    # old code priced the whole day's cooling at $0.43 (a ~3x overstatement).
    out = correlation.predict_peak_cost(100.0, _HIST, CFG.tou, CFG.system_kw, TZ, target_date=date(2026, 8, 14))
    peak_rate = next(b.rate for b in CFG.tou.bands if b.name == "peak")
    expected = out["predicted_peak_cool_minutes"] / 60.0 * CFG.system_kw * peak_rate
    assert out["predicted_peak_dollars"] == pytest.approx(expected)
    assert out["predicted_peak_cool_minutes"] < out["predicted_cool_minutes"]
    assert out["predicted_peak_cool_minutes"] <= 240.0


def test_predict_weekend_uses_weekend_rate():
    # Saturday 5-9pm has NO peak band — pricing it at the weekday peak rate
    # overstated the figure ~4.8x with these rates.
    sat = correlation.predict_peak_cost(95.0, _HIST, CFG.tou, CFG.system_kw, TZ, target_date=date(2026, 8, 15))  # Saturday
    fri = correlation.predict_peak_cost(95.0, _HIST, CFG.tou, CFG.system_kw, TZ, target_date=date(2026, 8, 14))
    assert sat["peak_band"] != "peak"
    assert sat["predicted_peak_dollars"] < fri["predicted_peak_dollars"] / 3


def test_cdd_time_weighted_across_gaps():
    # Afternoon-only survivors must not read as an all-day scorcher: the day
    # below is 85F for 2h observed and has a 22h hole; time-weighting keeps
    # its contribution at (85-65)=20 CDD from those hours, same as before,
    # but a day with BOTH cool morning (10h @ 60F) and hot afternoon (2h @
    # 85F) must weight the 60F hours, not average readings 1:1.
    rows = ([{"ts": datetime(2026, 8, 10, h, tzinfo=timezone.utc), "wx_outdoor_temp_f": 60.0}
             for h in range(0, 10)]
            + [{"ts": datetime(2026, 8, 10, 20, tzinfo=timezone.utc), "wx_outdoor_temp_f": 85.0},
               {"ts": datetime(2026, 8, 10, 20, 3, tzinfo=timezone.utc), "wx_outdoor_temp_f": 85.0}])
    cdd = correlation.cooling_degree_days(rows, base_f=65.0, tz=TZ)
    # 9h of 60F (10-min-capped intervals... hourly rows -> capped at 600s each)
    # matter is: weighted mean stays near 60, so CDD ~ 0 for that day
    assert cdd < 5
