"""Pure-function tests for analytics/moisture.py — the statistics the
moisture case's conclusions rest on. No database needed."""
import math
from datetime import date, datetime, timezone, timedelta

import pytest
from house_climate.analytics import moisture


def test_t_crit_95_rounds_dof_down_conservatively():
    # A dof between anchors uses the LOWER anchor's (larger) crit -> wider CI,
    # so 'noise' isn't flipped to 'real' too easily. dof=13 must use the df=12
    # value (2.18), NOT round up to df=15's 2.13.
    assert moisture._t_crit_95(13) == 2.18
    assert moisture._t_crit_95(9) == 2.26            # exact anchor
    assert moisture._t_crit_95(8) == 2.26            # below first anchor -> most conservative
    assert moisture._t_crit_95(10 ** 10) == 1.96     # top anchor
    assert moisture._t_crit_95(13) > moisture._t_crit_95(15)  # down-rounding is conservative


def test_r_crit_bonf6_rounds_dof_down_conservatively():
    # n=13 -> dof=11 -> rounds down to anchor 10 (t=3.17), not up to 15 (t=2.94).
    expected = 3.17 / math.sqrt(11 + 3.17 ** 2)
    assert moisture._r_crit_bonf6(13) == pytest.approx(expected, rel=1e-9)
    assert moisture._r_crit_bonf6(2) == 1.0          # too few points -> impossible bar


def _hourly(now, hours, fn):
    """[{bucket, dp}] for the last `hours` hours, oldest first."""
    return [{"bucket": now - timedelta(hours=hours - i), "dp": fn(i)}
            for i in range(hours)]


# ---------------------------------------------------------------- pearson

def test_pearson_perfect_and_inverse():
    assert abs(moisture.pearson([(i, 2 * i + 1) for i in range(10)]) - 1.0) < 1e-9
    assert abs(moisture.pearson([(i, -i) for i in range(10)]) + 1.0) < 1e-9


def test_pearson_undefined_cases():
    assert moisture.pearson([(1, 2), (2, 3)]) is None            # n < 3
    assert moisture.pearson([(1, 5), (2, 5), (3, 5)]) is None    # zero variance
    assert moisture.pearson([(1, None), (None, 2), (3, 4)]) is None


# ---------------------------------------------------- source attribution

def test_attribution_coupled_when_crawl_tracks_outdoor():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    outdoor = _hourly(now, 200, lambda i: 50 + 10 * math.sin(i / 12))
    crawl = _hourly(now, 200, lambda i: 55 + 8 * math.sin(i / 12))  # follows
    w = moisture.attribution_window(crawl, outdoor, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w["ready"] is True
    assert w["r"] > 0.9
    verdict = moisture.attribution_verdict(w, {"ready": False})
    assert verdict[0] == "ventilation"


def test_attribution_decoupled_means_soil():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    outdoor = _hourly(now, 200, lambda i: 50 + 10 * math.sin(i / 12))
    # crawl varies on its own rhythm, uncorrelated with outdoor
    crawl = _hourly(now, 200, lambda i: 60 + 3 * math.sin(i / 5 + 2))
    w = moisture.attribution_window(crawl, outdoor, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w["ready"] is True
    verdict = moisture.attribution_verdict(w, {"ready": False})
    assert verdict[0] in ("soil", "mixed")


def test_attribution_gates_on_hours_and_flat_outdoor():
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    few = _hourly(now, 10, lambda i: 50 + i)
    w = moisture.attribution_window(few, few, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w["ready"] is False and w["reason"] == "collecting"
    # plenty of hours but outdoor dp doesn't move -> not attributable
    outdoor_flat = _hourly(now, 200, lambda i: 55.0 + 0.01 * (i % 3))
    crawl = _hourly(now, 200, lambda i: 55 + 5 * math.sin(i / 9))
    w2 = moisture.attribution_window(crawl, outdoor_flat, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w2["ready"] is False and w2["reason"] == "outdoor_flat"
    assert moisture.attribution_verdict(w, w2) is None


# ----------------------------------------------------------- rain lags

def _daily(start, n, dp_fn):
    return [{"day": start + timedelta(days=i), "dp_mean": dp_fn(i), "rh_mean": 60}
            for i in range(n)]


def test_rain_lag_finds_two_day_lag():
    start = date(2026, 7, 1)
    rain = {start + timedelta(days=i): (0.8 if i % 7 == 0 else 0.0) for i in range(30)}
    # crawl dp spikes exactly 2 days after each rain day
    crawl = _daily(start, 30, lambda i: 62.0 if (i - 2) % 7 == 0 and i >= 2 else 52.0)
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] is True
    assert r["best"]["lag"] == 2
    assert r["best"]["r"] > 0.8
    assert r["verdict"][0] == "rain_driven"


def test_rain_lag_no_response_is_evidence_against_drainage():
    start = date(2026, 7, 1)
    rain = {start + timedelta(days=i): (0.6 if i in (3, 10, 17, 24) else 0.0)
            for i in range(30)}
    crawl = _daily(start, 30, lambda i: 55.0 + 2 * math.sin(i / 3.0))  # ignores rain
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] is True
    assert r["verdict"][0] in ("no_response", "weak")


def test_rain_lag_gates_without_wet_days():
    start = date(2026, 7, 1)
    rain = {start + timedelta(days=i): 0.0 for i in range(30)}
    crawl = _daily(start, 30, lambda i: 55.0 + i * 0.1)
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] is False
    assert r["reason"] == "no_rain_yet"
    assert "verdict" not in r


def test_rain_lag_gates_on_few_days():
    start = date(2026, 7, 1)
    rain = {start + timedelta(days=i): 0.5 for i in range(5)}
    crawl = _daily(start, 5, lambda i: 55.0 + i)
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] is False and r["reason"] == "collecting"


# ------------------------------------------------------ threshold rollups

def test_threshold_rollups_weekly_monthly():
    days = [{"day": date(2026, 8, 10) + timedelta(days=i),   # Mon..Wed same ISO week
             "h60": 5.0, "h70": 2.0, "h80": 0.0, "obs_h": 24.0} for i in range(3)]
    t = moisture.threshold_rollups(days)
    assert len(t["weeks"]) == 1
    wk = t["weeks"][0]
    assert wk["h60"] == 15.0 and wk["h70"] == 6.0 and wk["obs_h"] == 72.0
    assert t["months"][0]["period"] == "2026-08"


# ------------------------------------------------- intervention baselines

def _stats_days(start, n, rh, dp, h60=0.0, h70=0.0):
    return [{"day": start + timedelta(days=i), "rh_mean": rh(i), "dp_mean": dp(i),
             "h60": h60, "h70": h70} for i in range(n)]


def test_intervention_real_improvement_detected():
    d0 = date(2026, 8, 1)
    # 20 baseline days at ~75% RH, 20 post days at ~55%: unmistakable
    daily = (_stats_days(d0 - timedelta(days=20), 20,
                         lambda i: 75 + (i % 3) - 1, lambda i: 62 + (i % 2), h60=10)
             + _stats_days(d0, 20, lambda i: 55 + (i % 3) - 1, lambda i: 50 + (i % 2)))
    out = moisture.intervention_report(daily, [{"id": 1, "marked_on": d0,
                                                "label": "Vapor barrier", "note": None}])
    assert len(out) == 1
    iv = out[0]
    assert iv["overall"] == "real_change"
    rh = iv["metrics"]["rh_mean"]
    assert rh["verdict"] == "real"
    assert rh["diff"] < -15
    assert rh["baseline_n"] == 20 and rh["post_n"] == 20


def test_intervention_collecting_with_short_post():
    d0 = date(2026, 8, 10)
    daily = (_stats_days(d0 - timedelta(days=20), 20, lambda i: 70, lambda i: 60)
             + _stats_days(d0, 3, lambda i: 55, lambda i: 50))
    out = moisture.intervention_report(daily, [{"id": 1, "marked_on": d0,
                                                "label": "Sealing", "note": None}])
    assert out[0]["overall"] == "collecting"
    assert out[0]["metrics"]["rh_mean"]["verdict"] == "collecting"


def test_intervention_noise_not_called_real():
    d0 = date(2026, 8, 1)
    # same distribution both sides — difference is pure noise
    daily = (_stats_days(d0 - timedelta(days=15), 15, lambda i: 70 + (i % 5) - 2, lambda i: 60)
             + _stats_days(d0, 15, lambda i: 70 + ((i + 2) % 5) - 2, lambda i: 60))
    out = moisture.intervention_report(daily, [{"id": 1, "marked_on": d0,
                                                "label": "Fan test", "note": None}])
    assert out[0]["metrics"]["rh_mean"]["verdict"] == "noise"
    assert out[0]["overall"] == "no_change_detected"


def test_phased_interventions_split_baselines():
    d1, d2 = date(2026, 6, 1), date(2026, 7, 15)
    daily = (_stats_days(d1 - timedelta(days=20), 20, lambda i: 80, lambda i: 65)
             + _stats_days(d1, 44, lambda i: 70, lambda i: 60)
             + _stats_days(d2, 20, lambda i: 55, lambda i: 50))
    ivs = [{"id": 1, "marked_on": d1, "label": "Phase 1", "note": None},
           {"id": 2, "marked_on": d2, "label": "Phase 2", "note": None}]
    out = moisture.intervention_report(daily, ivs)
    # phase 1's post window must STOP at phase 2's marker
    assert out[0]["post_days"] == 44
    # phase 2's baseline must start at phase 1's marker (capped at 60d)
    assert out[1]["baseline_days"] == 44
    assert out[1]["metrics"]["rh_mean"]["baseline_mean"] == 70.0


# ------------------------------------------------------ winter projection

def _proj_days(n, temp_span):
    """Synthetic days where crawl dp = 10 + 0.3*T + 0.5*DP exactly. Outdoor
    dp varies independently of T (the sin term) so the predictors are not
    collinear — as in reality, where dew point and temperature decouple."""
    start = date(2026, 3, 1)
    out_days, crawl = [], []
    for i in range(n):
        t = 40 + temp_span * (i / max(n - 1, 1))
        dp = t - 12 + 4 * math.sin(i / 2.5)
        out_days.append({"day": start + timedelta(days=i), "temp_mean": t, "dp_mean": dp})
        crawl.append({"day": start + timedelta(days=i),
                      "dp_mean": 10 + 0.3 * t + 0.5 * dp})
    return crawl, out_days


def test_projection_gates_on_days_and_span():
    crawl, out_days = _proj_days(20, 40)
    p = moisture.winter_projection(crawl, out_days)
    assert p["ready"] is False and p["reason"] == "collecting"
    crawl, out_days = _proj_days(60, 10)   # plenty of days, summer-only span
    p = moisture.winter_projection(crawl, out_days)
    assert p["ready"] is False and p["reason"] == "narrow_temp_range"


def test_projection_recovers_linear_model():
    crawl, out_days = _proj_days(60, 40)
    p = moisture.winter_projection(crawl, out_days)
    assert p["ready"] is True
    expected = 10 + 0.3 * moisture.WINTER_TEMP_F + 0.5 * moisture.WINTER_DP_F
    assert abs(p["predicted_dp_f"] - expected) < 0.5
    assert p["ci95_f"] < 2.0   # exact linear data -> tight interval


# ------------------------------------------------------ condensation

def test_condensation_summary_duct_proxy():
    days = [{"day": date(2026, 8, 10), "cond_h": 2.0, "obs_h": 24.0, "dp_mean": 60.0},
            {"day": date(2026, 8, 11), "cond_h": 0.0, "obs_h": 24.0, "dp_mean": 50.0}]
    outdoor = [{"day": date(2026, 8, 10), "cooling_h": 5.0},
               {"day": date(2026, 8, 11), "cooling_h": 6.0}]
    c = moisture.condensation_summary(days, outdoor, days)
    by_day = {d["day"]: d for d in c["days"]}
    # dp 60 > assumed 57F duct -> cooling hours count as duct risk
    assert by_day["2026-08-10"]["duct_hours"] == 5.0
    # dp 50 < 57 -> no duct sweat even though AC ran longer
    assert by_day["2026-08-11"]["duct_hours"] == 0.0
    assert c["hours_7d"] == 2.0


# ------------------------------------------------ audit regression tests

def test_attribution_negative_r_is_still_coupling():
    """A strong NEGATIVE correlation is strong coupling (phase-shifted),
    not 'soil' — classification must use |r|."""
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    outdoor = _hourly(now, 200, lambda i: 50 + 10 * math.sin(i / 12))
    crawl = _hourly(now, 200, lambda i: 55 - 8 * math.sin(i / 12))  # inverse
    w = moisture.attribution_window(crawl, outdoor, now, 7, moisture.ATTR_MIN_HOURS_7D)
    assert w["ready"] and w["r"] < -0.9
    verdict = moisture.attribution_verdict(w, {"ready": False})
    assert verdict[0] == "ventilation"


def test_rain_wet_days_counted_in_window_only():
    """Backfilled rain from before the sensor existed must not satisfy the
    wet-day contrast gate."""
    start = date(2026, 8, 11)
    # 5 wet days in July (pre-sensor), bone-dry within the crawl window
    rain = {date(2026, 7, 1) + timedelta(days=i): 0.8 for i in range(5)}
    rain.update({start + timedelta(days=i): 0.0 for i in range(15)})
    crawl = _daily(start, 15, lambda i: 55.0 + 2 * math.sin(i))
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] is False
    assert r["reason"] == "no_rain_yet"
    assert r["wet_days"] == 0


def test_rain_best_lag_requires_its_own_day_count():
    """A lag whose own pair count is below the gate must not be selectable
    even when lag 0 has plenty of pairs."""
    start = date(2026, 8, 1)
    # rain data exists ONLY for the crawl window itself -> lag k has n-k pairs
    rain = {start + timedelta(days=i): (0.5 if i % 4 == 0 else 0.0) for i in range(12)}
    crawl = _daily(start, 12, lambda i: 55.0 + (3.0 if (i - 5) % 4 == 0 else 0.0))
    r = moisture.rain_lag_correlation(rain, crawl)
    for l in r["lags"]:
        if l["n"] < moisture.RAIN_MIN_DAYS and r.get("best"):
            assert r["best"]["lag"] != l["lag"]


def test_rain_driven_requires_selection_corrected_significance():
    """The 'buy drainage' verdict must clear the Bonferroni-corrected bar,
    which at small n is far above the 0.5 label threshold."""
    assert moisture._r_crit_bonf6(10) > 0.7
    assert moisture._r_crit_bonf6(30) > 0.45
    # the strong-lag fixture from above still passes at n~28
    start = date(2026, 7, 1)
    rain = {start + timedelta(days=i): (0.8 if i % 7 == 0 else 0.0) for i in range(30)}
    crawl = _daily(start, 30, lambda i: 62.0 if (i - 2) % 7 == 0 and i >= 2 else 52.0)
    r = moisture.rain_lag_correlation(rain, crawl)
    assert r["ready"] and r["verdict"][0] == "rain_driven"


def test_metric_compare_ci_and_verdict_agree():
    """ONE bar: verdict is 'real' exactly when |diff| > displayed ci95 —
    never an interval excluding zero labeled 'noise'."""
    import random
    rng = random.Random(7)
    for _ in range(200):
        base = [50 + rng.gauss(0, 3) for _ in range(rng.randint(10, 30))]
        post = [50 + rng.gauss(rng.uniform(-3, 3), 3) for _ in range(rng.randint(10, 30))]
        m = moisture._metric_compare(base, post)
        if m["ci95"] is not None and m["diff"] is not None:
            # rounding tolerance: compare on the rounded values shown to users
            if m["verdict"] == "real":
                assert abs(m["diff"]) >= m["ci95"] - 0.11
            elif m["verdict"] == "noise":
                assert abs(m["diff"]) <= m["ci95"] + 0.11


def test_intervention_seasonal_confound_downgrades_verdict():
    """Autumn's natural dew-point drop must not certify a September fan as a
    'real change' — when outdoor dp moved with the crawl, the verdict is
    'confounded'."""
    d0 = date(2026, 9, 15)
    daily = (_stats_days(d0 - timedelta(days=20), 20, lambda i: 70, lambda i: 62 + (i % 3))
             + _stats_days(d0, 20, lambda i: 60, lambda i: 50 + (i % 3)))
    # outdoor dew point fell by ~12F across the same boundary (season change)
    outdoor = ([{"day": d0 - timedelta(days=20) + timedelta(days=i), "dp_mean": 60.0}
                for i in range(20)]
               + [{"day": d0 + timedelta(days=i), "dp_mean": 48.0} for i in range(20)])
    out = moisture.intervention_report(
        daily, [{"id": 1, "marked_on": d0, "label": "Fan", "note": None}],
        outdoor_days=outdoor)
    assert out[0]["metrics"]["dp_mean"]["verdict"] == "confounded"
    assert out[0]["outdoor_dp_shift"] == -12.0
    assert out[0]["overall"] in ("confounded", "real_change")  # rh may still be real


def test_intervention_real_when_outdoor_stable():
    """Same crawl improvement with FLAT outdoor conditions stays 'real'."""
    d0 = date(2026, 9, 15)
    daily = (_stats_days(d0 - timedelta(days=20), 20, lambda i: 70, lambda i: 62 + (i % 3))
             + _stats_days(d0, 20, lambda i: 60, lambda i: 50 + (i % 3)))
    outdoor = [{"day": d0 - timedelta(days=20) + timedelta(days=i), "dp_mean": 55.0}
               for i in range(40)]
    out = moisture.intervention_report(
        daily, [{"id": 1, "marked_on": d0, "label": "Barrier", "note": None}],
        outdoor_days=outdoor)
    assert out[0]["metrics"]["dp_mean"]["verdict"] == "real"
    assert out[0]["overall"] == "real_change"


def test_condensation_7d_means_calendar_days():
    """After an outage, 'last 7 days' must not silently reach back 10+
    calendar days of rows."""
    mk = lambda day, h: {"day": day, "cond_h": h, "obs_h": 24.0, "dp_mean": 50.0}
    days = ([mk(date(2026, 8, 1) + timedelta(days=i), 5.0) for i in range(4)]     # old
            + [mk(date(2026, 8, 20) + timedelta(days=i), 1.0) for i in range(3)])  # recent
    c = moisture.condensation_summary(days, [], days)
    # only the Aug 20-22 rows fall inside [max-6, max]
    assert c["hours_7d"] == 3.0


# -------------------------------------------- crawl-to-floor AH gap

def _ah_hourly(now, hours, fn):
    """[{bucket, ah, temp}] for the last `hours` hours, oldest first."""
    return [{"bucket": now - timedelta(hours=hours - i), "ah": fn(i), "temp": 60.0}
            for i in range(hours)]


def _ah_daily(start, n, fn):
    return [{"day": start + timedelta(days=i), "ah_mean": fn(i)} for i in range(n)]


def test_ah_gap_hourly_pairs_on_shared_buckets():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    crawl = _ah_hourly(now, 5, lambda i: 12.0)
    floor = _ah_hourly(now, 5, lambda i: 9.5)
    out = moisture.ah_gap_hourly(crawl, floor)
    assert len(out) == 5
    assert all(abs(r["gap"] - 2.5) < 1e-9 for r in out)


def test_ah_gap_hourly_drops_unpaired_and_null_hours():
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    crawl = _ah_hourly(now, 5, lambda i: 12.0)
    crawl[1]["ah"] = None                      # crawl missing this hour
    floor = _ah_hourly(now, 5, lambda i: 9.0)
    del floor[3]                               # floor never reported this hour
    floor[0]["ah"] = None                      # floor null -> not a zero gap
    out = moisture.ah_gap_hourly(crawl, floor)
    assert len(out) == 2
    assert all(r["gap"] == 3.0 for r in out)


def test_ah_gap_daily_joins_on_day():
    start = date(2026, 8, 1)
    crawl = _ah_daily(start, 4, lambda i: 13.0)
    floor = _ah_daily(start, 4, lambda i: 10.0)
    del floor[2]
    floor[0]["ah_mean"] = None
    out = moisture.ah_gap_daily(crawl, floor)
    assert [r["day"] for r in out] == [start + timedelta(days=1), start + timedelta(days=3)]
    assert all(abs(r["gap"] - 3.0) < 1e-9 for r in out)


def _gap_days(start, n, fn):
    return [{"day": start + timedelta(days=i), "crawl": 12.0, "floor": 12.0 - fn(i),
             "gap": fn(i)} for i in range(n)]


def test_gap_intervention_detects_real_narrowing():
    d0 = date(2026, 7, 1)
    # 20 baseline days at a 3.0 gap, 20 post days at 1.0 — a big, clean change.
    gaps = (_gap_days(d0 - timedelta(days=20), 20, lambda i: 3.0 + (i % 2) * 0.05)
            + _gap_days(d0, 20, lambda i: 1.0 + (i % 2) * 0.05))
    out = moisture.gap_intervention_report(
        gaps, [{"id": 1, "marked_on": d0, "label": "Vapor barrier"}])
    m = out[0]["metric"]
    assert m["verdict"] == "real"
    assert m["diff"] == pytest.approx(-2.0, abs=0.05)
    assert out[0]["baseline_days"] == 20 and out[0]["post_days"] == 20


def test_gap_intervention_reports_no_direction_verdict():
    """The report must NOT label a sign good or bad — a narrowing gap and a
    widening gap are both consistent with a successful intervention depending
    on whether it targeted ground vapour or air movement."""
    d0 = date(2026, 7, 1)
    gaps = (_gap_days(d0 - timedelta(days=15), 15, lambda i: 1.0)
            + _gap_days(d0, 15, lambda i: 3.0))
    out = moisture.gap_intervention_report(
        gaps, [{"id": 1, "marked_on": d0, "label": "Air sealing"}])
    assert out[0]["metric"]["diff"] > 0
    assert "verdict" not in out[0]           # no overall directional call
    assert set(out[0]) == {"id", "marked_on", "label", "baseline_days",
                           "post_days", "outdoor_ah_shift", "metric"}


def test_gap_intervention_collecting_when_window_too_short():
    d0 = date(2026, 7, 1)
    gaps = (_gap_days(d0 - timedelta(days=20), 20, lambda i: 3.0)
            + _gap_days(d0, 4, lambda i: 1.0))
    out = moisture.gap_intervention_report(
        gaps, [{"id": 1, "marked_on": d0, "label": "Barrier"}])
    assert out[0]["metric"]["verdict"] == "collecting"


def test_gap_intervention_seasonal_confound_downgrades():
    """Outdoor AH falling by as much as the gap changed means autumn, not a
    barrier — the verdict must drop from 'real' to 'confounded'."""
    d0 = date(2026, 9, 1)
    gaps = (_gap_days(d0 - timedelta(days=20), 20, lambda i: 3.0 + (i % 2) * 0.05)
            + _gap_days(d0, 20, lambda i: 1.0 + (i % 2) * 0.05))
    outdoor = ([{"day": d0 - timedelta(days=20 - i), "ah_mean": 14.0} for i in range(20)]
               + [{"day": d0 + timedelta(days=i), "ah_mean": 11.0} for i in range(20)])
    out = moisture.gap_intervention_report(
        gaps, [{"id": 1, "marked_on": d0, "label": "Barrier"}], outdoor_daily=outdoor)
    assert out[0]["outdoor_ah_shift"] == pytest.approx(-3.0)
    assert out[0]["metric"]["verdict"] == "confounded"
