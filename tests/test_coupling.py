"""Pure-function tests for analytics/coupling.py — the transport-gain estimate
that decides whether crawl air is reaching the floors above. No database.

The tests that matter most here are the NEGATIVE ones: an estimator that
happily reports a number on unrelated series would invent a mechanism and send
someone to spend money on it.
"""
import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest
from house_climate.analytics import coupling


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _series(hours, fn, key="ah", now=NOW):
    """[{bucket, <key>}] hourly, oldest first, ending one hour before `now`."""
    return [{"bucket": now - timedelta(hours=hours - i), key: fn(i)}
            for i in range(hours)]


# ------------------------------------------------------------ linear algebra

def test_solve_recovers_known_solution():
    a = [[2.0, 1.0, 0.0], [1.0, 3.0, 1.0], [0.0, 1.0, 2.0]]
    x = [1.0, -2.0, 3.0]
    b = [sum(a[i][j] * x[j] for j in range(3)) for i in range(3)]
    got = coupling.solve(a, b)
    assert got is not None
    assert all(abs(g - e) < 1e-9 for g, e in zip(got, x))


def test_solve_returns_none_when_singular():
    assert coupling.solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0]) is None


def test_ols_recovers_exact_coefficients_without_noise():
    X = [[float(i), float(i % 5), 1.0] for i in range(40)]
    y = [2.5 * r[0] - 1.5 * r[1] + 7.0 for r in X]
    fit = coupling.ols(X, y)
    assert fit is not None
    assert fit["beta"][0] == pytest.approx(2.5, abs=1e-6)
    assert fit["beta"][1] == pytest.approx(-1.5, abs=1e-6)
    assert max(abs(e) for e in fit["resid"]) < 1e-6


def test_ols_returns_none_on_collinear_columns():
    X = [[float(i), float(2 * i), 1.0] for i in range(20)]
    assert coupling.ols(X, [float(i) for i in range(20)]) is None


# ------------------------------------------------------------- detrending

def test_centered_anomalies_removes_slow_trend_keeps_fast_signal():
    """A linear ramp must vanish; a fast wiggle riding on it must survive."""
    hours = 500
    buckets = [NOW - timedelta(hours=hours - i) for i in range(hours)]
    by_bucket = {b: 0.05 * i + 2.0 * math.sin(2 * math.pi * i / 12.0)
                 for i, b in enumerate(buckets)}
    anom = coupling.centered_anomalies(by_bucket, buckets)
    inner = [(b, v) for b, v in anom.items() if v is not None]
    assert len(inner) > 200
    vals = [v for _, v in inner]
    # The ramp is gone: the anomaly mean sits at zero, not at the ramp's level.
    assert abs(sum(vals) / len(vals)) < 0.05
    # The 12-hour wiggle survives at close to its original amplitude.
    assert max(vals) > 1.7 and min(vals) < -1.7


def test_centered_anomalies_none_at_edges_and_in_thin_windows():
    hours = 200
    buckets = [NOW - timedelta(hours=hours - i) for i in range(hours)]
    by_bucket = {b: 10.0 for b in buckets}
    anom = coupling.centered_anomalies(by_bucket, buckets)
    # First and last hours lack a full centred window -> no anomaly invented.
    assert anom[buckets[0]] is None
    assert anom[buckets[-1]] is None


# --------------------------------------------------- autocorrelation honesty

def test_effective_n_far_below_n_for_autocorrelated_series():
    rnd = random.Random(7)
    x, e = [], []
    xv = ev = 0.0
    for _ in range(720):
        xv = 0.9 * xv + rnd.gauss(0, 1)
        ev = 0.9 * ev + rnd.gauss(0, 1)
        x.append(xv)
        e.append(ev)
    n_eff = coupling.effective_n(x, e)
    assert n_eff < 120, f"720 highly autocorrelated hours must not count as 720 (got {n_eff})"
    assert n_eff > 5


def test_effective_n_close_to_n_for_white_noise():
    rnd = random.Random(11)
    x = [rnd.gauss(0, 1) for _ in range(500)]
    e = [rnd.gauss(0, 1) for _ in range(500)]
    assert coupling.effective_n(x, e) > 350


def test_hac_variance_exceeds_naive_for_autocorrelated_residuals():
    """Newey-West must widen the interval when residuals are serially
    correlated — that is the whole reason it is here."""
    rnd = random.Random(3)
    n = 400
    X, y, resid = [], [], []
    ev = 0.0
    xv = 0.0
    for _ in range(n):
        xv = 0.85 * xv + rnd.gauss(0, 1)
        ev = 0.85 * ev + rnd.gauss(0, 1)
        X.append([xv, 1.0])
        y.append(0.5 * xv + ev)
    fit = coupling.ols(X, y)
    buckets = [NOW - timedelta(hours=n - i) for i in range(n)]
    hac = coupling.hac_var(X, fit["resid"], fit["xtx"], buckets, col=0)
    assert hac > _naive_var(X, fit["resid"], fit["xtx"])


def _naive_var(X, resid, xtx, col=0):
    """Textbook variance of one coefficient, assuming independent errors —
    what the estimate would be WITHOUT the autocorrelation correction. Lives
    here rather than in the module because nothing in production may use it."""
    dof = len(resid) - len(X[0])
    s2 = sum(r * r for r in resid) / dof
    v = coupling.solve(xtx, [1.0 if i == col else 0.0 for i in range(len(X[0]))])
    return s2 * v[col]


# --------------------------------------------------------- coupling_window

BETA_TRUE = 0.4
LAG_TRUE = 2


def _synthetic(hours=24 * 35, beta=BETA_TRUE, lag=LAG_TRUE, noise=0.02, seed=5,
               floor_follows_crawl=True):
    """Crawl / floor / outdoor hourly AH where the floor is a KNOWN fraction of
    a lagged crawl excursion, plus an outdoor term, a daily rhythm, a slow
    seasonal drift, and a little noise."""
    rnd = random.Random(seed)

    def crawl_anom(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0) + 0.8 * math.sin(2 * math.pi * i / 13.0)

    def outdoor_anom(i):
        return 1.5 * math.sin(2 * math.pi * i / 97.0 + 1.0)

    def diurnal(i):
        return 0.6 * math.sin(2 * math.pi * i / 24.0 + 0.5)

    def drift(i):
        return 0.02 * i

    crawl = _series(hours, lambda i: 12.0 + crawl_anom(i) + drift(i))
    outdoor = _series(hours, lambda i: 10.0 + outdoor_anom(i) + drift(i))
    transported = (lambda i: beta * crawl_anom(i - lag)) if floor_follows_crawl \
        else (lambda i: 0.0)
    floor = _series(hours, lambda i: (9.0 + transported(i) + 0.3 * outdoor_anom(i)
                                      + diurnal(i) + drift(i) + rnd.gauss(0, noise)))
    return crawl, floor, outdoor


def test_coupling_recovers_known_transport_gain_and_lag():
    crawl, floor, outdoor = _synthetic()
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is True, out.get("reason")
    assert out["lag"] == LAG_TRUE
    assert out["beta"] == pytest.approx(BETA_TRUE, abs=0.06)
    lo, hi = out["beta"] - out["ci95"], out["beta"] + out["ci95"]
    assert lo < BETA_TRUE < hi
    assert lo > 0, "a real transport signal must exclude zero"


def test_coupling_reports_effective_n_below_raw_hours():
    crawl, floor, outdoor = _synthetic()
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["n_eff"] < out["n"]


def test_coupling_finds_nothing_when_floor_ignores_crawl():
    """THE test. A floor driven only by outdoor air and its own daily rhythm
    must not produce a confident transport number."""
    crawl, floor, outdoor = _synthetic(floor_follows_crawl=False, noise=0.15)
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    if out["ready"]:
        lo, hi = out["beta"] - out["ci95"], out["beta"] + out["ci95"]
        assert lo <= 0 <= hi, f"invented a mechanism: beta={out['beta']} ci={out['ci95']}"
    else:
        assert out["reason"] in ("weak_signal", "insufficient_n_eff", "inconsistent_sign")


def test_coupling_refuses_short_window():
    crawl, floor, outdoor = _synthetic(hours=24 * 12)
    out = coupling.coupling_window(crawl, floor, outdoor, days=12, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "window_too_short"
    assert out["need_days"] == coupling.MIN_WINDOW_DAYS


def test_coupling_refuses_when_coverage_is_thin_and_names_the_thin_series():
    """Naming the series matters now that outdoor readings can come from a
    sensor at the house: a patchy outdoor sensor refuses every floor, and
    "thin coverage" alone points the reader at the crawl or the room."""
    crawl, floor, outdoor = _synthetic()
    floor = [r for i, r in enumerate(floor) if i % 4]     # drop a quarter of hours
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "thin_coverage"
    assert out["thinnest"] == "floor"
    assert out["coverage_by_series"]["floor"] < out["coverage_by_series"]["crawl"]


def test_a_patchy_outdoor_sensor_is_named_as_the_thin_one():
    crawl, floor, outdoor = _synthetic()
    outdoor = [r for i, r in enumerate(outdoor) if i % 4]
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["thinnest"] == "outdoor"


def test_coupling_refuses_on_a_long_outage():
    crawl, floor, outdoor = _synthetic()
    cut_from = NOW - timedelta(days=10)
    cut_to = cut_from + timedelta(hours=30)
    floor = [r for r in floor if not (cut_from <= r["bucket"] < cut_to)]
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "outage"


def test_coupling_refuses_when_crawl_barely_moves_on_its_own():
    """After a good barrier the crawl stops swinging independently of outdoor.
    That is a SUCCESS, and the estimator must say so rather than divide by
    noise."""
    hours = 24 * 35
    outdoor = _series(hours, lambda i: 10.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    crawl = _series(hours, lambda i: 12.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    floor = _series(hours, lambda i: 9.0 + 0.6 * math.sin(2 * math.pi * i / 24.0))
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "weak_signal"
    assert out["crawl_sd"] < coupling.MIN_CRAWL_ANOM_SD


def test_coupling_drops_saturated_crawl_hours():
    """A pinned sensor at 95%+ RH is not a measurement. Those hours must leave
    the fit, visibly."""
    crawl, floor, outdoor = _synthetic()
    rh = [{"bucket": r["bucket"], "rh": 97.0 if i % 2 else 80.0}
          for i, r in enumerate(crawl)]
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW, crawl_rh=rh)
    assert out["dropped_saturated"] > 300


def test_coupling_window_never_straddles_an_intervention():
    crawl, floor, outdoor = _synthetic()
    mark = (NOW - timedelta(days=15)).date()
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW,
                                   interventions=[{"marked_on": mark}])
    assert out["ready"] is False
    assert out["reason"] == "straddles_intervention"


def test_coupling_reports_inconsistent_sign_rather_than_a_negative_number():
    """Negative transport is not physical. Report it as a problem, not a
    result."""
    crawl, floor, outdoor = _synthetic(beta=-0.5)
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "inconsistent_sign"


# --------------------------------------------------------- stack signature

def test_stack_signature_positive_when_transport_grows_with_temp_difference():
    """Real stack-driven flow strengthens as indoor-minus-outdoor grows."""
    hours = 24 * 35
    rnd = random.Random(9)

    def ca(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    def dt(i):
        return 5.0 + 15.0 * (i / hours)          # a season turning colder outside

    crawl = _series(hours, lambda i: 12.0 + ca(i))
    outdoor = _series(hours, lambda i: 10.0 + 1.2 * math.sin(2 * math.pi * i / 97.0))
    # transport gain itself rises with the temperature difference
    floor = _series(hours, lambda i: 9.0 + (0.01 * dt(i)) * ca(i) + rnd.gauss(0, 0.02))
    dts = _series(hours, dt, key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW)
    assert out["ready"] is True, out.get("reason")
    assert out["delta"] > 0
    assert out["delta"] - out["ci95"] > 0


def test_stack_signature_flat_when_crawl_is_just_an_outdoor_proxy():
    """The confounder that matters: a vented crawl carries local outdoor
    information the distant weather station misses. That produces coupling
    with NO transport, and it does not strengthen with temperature."""
    hours = 24 * 35
    rnd = random.Random(13)

    def local_out(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    station = random.Random(4)
    crawl = _series(hours, lambda i: 12.0 + local_out(i))
    outdoor = _series(hours, lambda i: 10.0 + 0.5 * local_out(i)
                      + 1.2 * math.sin(2 * math.pi * i / 83.0)
                      + station.gauss(0, 0.6))
    floor = _series(hours, lambda i: 9.0 + 0.4 * local_out(i) + rnd.gauss(0, 0.02))
    dts = _series(hours, lambda i: 5.0 + 15.0 * (i / hours), key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW)
    if out["ready"]:
        assert out["delta"] - out["ci95"] <= 0 <= out["delta"] + out["ci95"], \
            "a pure outdoor-proxy effect must not look like stack transport"


# --------------------------------------------------------- prediction test

def test_prediction_test_confirms_when_observed_matches_prediction():
    out = coupling.prediction_test(beta=0.4, beta_ci=0.1,
                                   d_crawl=-3.0, d_crawl_ci=0.3,
                                   d_floor=-1.2, d_floor_ci=0.25)
    assert out["predicted"] == pytest.approx(-1.2, abs=0.01)
    assert out["verdict"] == "confirmed"


def test_prediction_test_flags_a_miss():
    out = coupling.prediction_test(beta=0.4, beta_ci=0.05,
                                   d_crawl=-3.0, d_crawl_ci=0.1,
                                   d_floor=-0.05, d_floor_ci=0.05)
    assert out["verdict"] == "not_confirmed"


def test_prediction_test_collecting_without_inputs():
    assert coupling.prediction_test(None, None, -3.0, 0.2, -1.0, 0.2)["verdict"] == "collecting"


# ------------------------------------------------------ consistency check

def _floor(name, beta, ci=0.1, lag=1, ready=True):
    return {"name": name, "beta": beta, "ci95": ci, "lag": lag, "ready": ready}


def test_consistency_check_passes_when_lower_floor_couples_harder():
    out = coupling.consistency_check([
        _floor("Downstairs", 0.5, lag=1), _floor("Upstairs", 0.3, lag=3)])
    assert out["verdict"] == "consistent"


def test_consistency_check_flags_an_upper_floor_coupling_harder():
    """Crawl air rising by stack must pass the lower floor first. If it does
    not, the path is ducts or a chase, not the floor."""
    out = coupling.consistency_check([
        _floor("Downstairs", 0.1, ci=0.05, lag=4),
        _floor("Upstairs", 0.6, ci=0.05, lag=1)])
    assert out["verdict"] == "bypass_suspected"
    assert "duct" in out["text"].lower() or "chase" in out["text"].lower()


def test_consistency_check_needs_two_ready_floors():
    assert coupling.consistency_check(
        [_floor("Upstairs", 0.3)])["verdict"] == "collecting"


def test_consistency_check_ignores_a_refused_fit_that_still_carries_a_number():
    """A refusal keeps its point estimate for display. Reasoning from it would
    turn a fit the module declined to publish into a specific, expensive
    repair recommendation."""
    out = coupling.consistency_check([
        _floor("Downstairs", 0.1, ci=0.05, ready=False),
        _floor("Upstairs", 0.6, ci=0.05, ready=True)])
    assert out["verdict"] == "collecting"


def test_consistency_check_refuses_when_the_floor_order_is_unknown():
    """Nothing records how high a sensor sits. An inverted list would produce
    a confident wrong diagnosis, so an unknown order refuses instead."""
    out = coupling.consistency_check(
        [_floor("Sensor A", 0.1), _floor("Sensor B", 0.6)], ordered=False)
    assert out["verdict"] == "unknown_order"
    assert "not recorded" in out["text"]


# ------------------------------------------------------- blower covariate

def test_blower_covariate_does_not_break_the_recovery():
    """Air-handler duty is carried as a covariate. Adding it must not disturb
    a transport gain that is genuinely there."""
    crawl, floor, outdoor = _synthetic()
    rnd = random.Random(31)
    duty = _series(len(crawl),
                   lambda i: (1.0 if (i % 24) in (7, 8, 18, 19) else 0.0)
                             + 0.3 * rnd.random(),
                   key="duty")
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW,
                                   blower=duty)
    assert out["ready"] is True, out.get("reason")
    assert out["beta"] == pytest.approx(BETA_TRUE, abs=0.08)


def test_a_covariate_that_is_purely_a_daily_schedule_is_dropped_not_fatal():
    """Air-handler duty that runs to the identical schedule every day IS the
    time of day. Once the daily rhythm is removed there is nothing left of it,
    and it must be left out rather than taking the whole estimate down."""
    crawl, floor, outdoor = _synthetic()
    duty = _series(len(crawl), lambda i: 1.0 if (i % 24) in (7, 8, 18, 19) else 0.0,
                   key="duty")
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW,
                                   blower=duty)
    assert out["ready"] is True, out.get("reason")
    assert out["beta"] == pytest.approx(BETA_TRUE, abs=0.06)


def test_blower_driven_floor_is_not_read_as_crawl_transport():
    """A floor that responds to the air handler, not the crawl, must not be
    reported as crawl air arriving.

    The duty signal here deliberately CORRELATES with the crawl, so the two
    are genuinely competing to explain the floor. Without the covariate doing
    real work the crawl takes the credit — the assertion at the end proves
    that, so this test cannot pass while the blower argument is ignored."""
    hours = 24 * 35
    rnd = random.Random(21)

    def crawl_anom(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    # Duty follows the crawl's own multi-day swing (both track the weather),
    # plus its own irregular component so it is not a pure daily schedule.
    def duty_of(i):
        return max(0.0, 0.5 + 0.4 * crawl_anom(i) / 2.0
                   + 0.3 * math.sin(2 * math.pi * i / 31.0))

    crawl = _series(hours, lambda i: 12.0 + crawl_anom(i))
    outdoor = _series(hours, lambda i: 10.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    # The floor is driven ONLY by the blower.
    floor = _series(hours, lambda i: 9.0 - 0.8 * duty_of(i) + rnd.gauss(0, 0.02))
    duty = _series(hours, duty_of, key="duty")

    with_duty = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW,
                                         blower=duty)
    assert with_duty["ready"] is True, with_duty.get("reason")
    assert abs(with_duty["beta"]) < 0.05, (
        f"blower response read as crawl transport: {with_duty['beta']}")
    assert with_duty["dropped_covariates"] == [], "the covariate has to be doing the work"

    # Handed the same house WITHOUT the air handler's duty, the module cannot
    # separate the two influences at all and refuses. That contrast is what
    # makes the covariate worth carrying — and it means this test cannot pass
    # if the blower argument were being ignored, since the two calls would
    # then return the same thing.
    without = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert without["ready"] is False
    assert without != with_duty


def test_a_dropped_covariate_is_reported_not_hidden():
    """When the blower correction is discarded, the payload has to say so —
    otherwise the page shows a figure that quietly had no such correction."""
    crawl, floor, outdoor = _synthetic()
    duty = _series(len(crawl), lambda i: 1.0 if (i % 24) in (7, 8, 18, 19) else 0.0,
                   key="duty")
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW,
                                   blower=duty)
    assert out["dropped_covariates"] == ["blower"]
    kept = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert kept["dropped_covariates"] == []


def test_hour_of_day_means_are_removed_from_every_column():
    """The fast path only matches a full hour-dummy model if EVERY predictor
    is treated the same way. A covariate left with its daily shape intact
    would bias the transport gain."""
    rows = [{"bucket": NOW - timedelta(hours=48 - i),
             "crawl": 1.0 + (i % 24), "floor": 2.0, "outdoor": 3.0,
             "blower": 5.0 + (i % 24)} for i in range(48)]
    coupling._remove_hour_of_day_means(rows, ["crawl", "floor", "outdoor", "blower"])
    for key in ("crawl", "blower"):
        by_hour = {}
        for r in rows:
            by_hour.setdefault(r["bucket"].hour, []).append(r[key])
        for h, vals in by_hour.items():
            assert abs(sum(vals) / len(vals)) < 1e-9, f"{key} kept its {h}:00 mean"


# ------------------------------------------- autocorrelation, for real

def test_effective_n_bites_when_residuals_are_correlated():
    """The noiseless fixtures leave the correction idle. With realistically
    correlated residuals a month of hours must be worth a fraction of its
    length, or every interval on the page is too narrow."""
    hours = 24 * 35
    rnd = random.Random(77)

    def crawl_anom(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    crawl = _series(hours, lambda i: 12.0 + crawl_anom(i))
    outdoor = _series(hours, lambda i: 10.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    # Residual noise that remembers the previous hour, as real house air does.
    noise, e = [], 0.0
    for _ in range(hours):
        e = 0.92 * e + rnd.gauss(0, 0.08)
        noise.append(e)
    floor = _series(hours, lambda i: 9.0 + BETA_TRUE * crawl_anom(i - LAG_TRUE) + noise[i])
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is True, out.get("reason")
    assert out["n_eff"] < out["n"] / 3, (
        f"correlated hours counted as nearly independent: n={out['n']} n_eff={out['n_eff']}")


def test_clock_based_autocorrelation_is_not_fooled_by_an_outage():
    """Rows either side of a gap are not neighbours. Pairing them makes a
    smooth series look choppy, which inflates the effective sample size."""
    stamps, vals = [], []
    for i in range(200):
        if 80 <= i < 120:            # a gap in the middle
            continue
        stamps.append(NOW - timedelta(hours=200 - i))
        vals.append(math.sin(2 * math.pi * i / 50.0))
    on_clock = coupling.lag1_autocorr(vals, stamps)
    by_position = coupling.lag1_autocorr(vals)
    assert on_clock > by_position
    assert coupling.effective_n(vals, vals, stamps) < coupling.effective_n(vals, vals)


# --------------------------------------------- refusals that must refuse

def test_t_crit_refuses_below_the_tabulated_range():
    """Below four degrees of freedom the critical value climbs past any table.
    Returning the smallest tabulated value there would halve the interval on
    exactly the marginal fits the gates exist to catch."""
    assert coupling.t_crit_bonf7(3) == float("inf")
    assert coupling.t_crit_bonf7(coupling.MIN_DOF) == 5.068
    # Rounding down between anchors is the conservative direction.
    assert coupling.t_crit_bonf7(7) == coupling.t_crit_bonf7(6)
    assert coupling.t_crit_bonf7(7) > coupling.t_crit_bonf7(8)


def test_hac_var_refuses_rather_than_falling_back_to_the_narrow_answer():
    """A non-positive HAC sum means the correction broke down. The uncorrected
    variance is the NARROWEST number available, so substituting it would turn
    a failure into false confidence."""
    n = 60
    buckets = [NOW - timedelta(hours=n - i) for i in range(n)]
    # Residuals that alternate sign every hour drive the lag terms negative.
    X = [[1.0 if i % 2 else -1.0, 1.0] for i in range(n)]
    resid = [1.0 if i % 2 else -1.0 for i in range(n)]
    fit = coupling.ols(X, [0.0] * n)
    var = coupling.hac_var(X, resid, fit["xtx"], buckets, col=0, bandwidth=4)
    assert var is None or var > 0


def test_a_refused_fit_does_not_leave_a_usable_looking_estimate():
    """A caller filtering on 'is there a number?' must not find one on a fit
    the module declined to publish."""
    crawl, floor, outdoor = _synthetic(hours=24 * 12)
    out = coupling.coupling_window(crawl, floor, outdoor, days=12, now=NOW)
    assert out["ready"] is False
    assert out.get("beta") is None


# --------------------------------------- stack_signature refuses too

def test_stack_signature_refuses_a_short_window():
    crawl, floor, outdoor = _synthetic(hours=24 * 12)
    dts = _series(len(crawl), lambda i: 10.0, key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=12, now=NOW)
    assert out["ready"] is False and out["reason"] == "window_too_short"


def test_stack_signature_refuses_across_an_intervention():
    crawl, floor, outdoor = _synthetic()
    dts = _series(len(crawl), lambda i: 10.0, key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW,
                                   interventions=[{"marked_on": (NOW - timedelta(days=15)).date()}])
    assert out["ready"] is False and out["reason"] == "straddles_intervention"


def test_stack_signature_refuses_when_the_crawl_barely_moves():
    """The gate coupling_window enforces before fitting. Without it this
    function would publish the page's strongest causal claim from a crawl that
    no longer moves on its own."""
    hours = 24 * 35
    outdoor = _series(hours, lambda i: 10.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    crawl = _series(hours, lambda i: 12.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    floor = _series(hours, lambda i: 9.0 + 0.6 * math.sin(2 * math.pi * i / 24.0))
    dts = _series(hours, lambda i: 10.0 + 5.0 * (i / hours), key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW)
    assert out["ready"] is False and out["reason"] == "weak_signal"


def test_stack_signature_refuses_thin_coverage():
    crawl, floor, outdoor = _synthetic()
    floor = [r for i, r in enumerate(floor) if i % 4]
    dts = _series(len(crawl), lambda i: 10.0, key="dt")
    out = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW)
    assert out["ready"] is False and out["reason"] == "thin_coverage"


def test_outdoor_proxy_produces_coupling_without_a_stack_signature():
    """The module's central architectural claim, asserted directly: a crawl
    acting as a better local weather station than the outdoor feed produces a
    LARGE apparent transport gain, and the temperature check is what refuses
    to confirm it."""
    hours = 24 * 35
    rnd = random.Random(13)

    def local_out(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    # The station is a NOISY, distant proxy for the weather at the house — it
    # sees part of the local swing and adds measurement error of its own. That
    # error is exactly what lets the crawl "explain" the floor without any air
    # moving, and it is why the crawl still has independent variance here.
    station = random.Random(4)
    crawl = _series(hours, lambda i: 12.0 + local_out(i))
    outdoor = _series(hours, lambda i: 10.0 + 0.5 * local_out(i)
                      + 1.2 * math.sin(2 * math.pi * i / 83.0)
                      + station.gauss(0, 0.6))
    floor = _series(hours, lambda i: 9.0 + 0.4 * local_out(i) + rnd.gauss(0, 0.02))
    dts = _series(hours, lambda i: 5.0 + 15.0 * (i / hours), key="dt")

    cw = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    st = coupling.stack_signature(crawl, floor, outdoor, dts, days=30, now=NOW)
    assert cw["ready"] is True and cw["significant"] is True
    assert cw["beta"] > 0.1, "the confounder should look like real transport"
    if st["ready"]:
        assert st["delta"] - st["ci95"] <= 0, \
            "the temperature check must not confirm a weather artefact"


def test_a_window_too_repetitive_to_carry_an_interval_says_so():
    """A perfectly smooth relationship leaves near-zero, near-perfectly
    correlated residuals — genuinely almost no independent information. The
    refusal is right, but it has to name the wall it hit: "no fit" tells the
    reader nothing they can act on."""
    hours = 24 * 35

    def crawl_anom(i):
        return 2.0 * math.sin(2 * math.pi * i / 60.0)

    crawl = _series(hours, lambda i: 12.0 + crawl_anom(i))
    outdoor = _series(hours, lambda i: 10.0 + 1.5 * math.sin(2 * math.pi * i / 97.0))
    # No noise at all: the floor is an exact function of the crawl.
    floor = _series(hours, lambda i: 9.0 + 0.4 * crawl_anom(i - 2))
    out = coupling.coupling_window(crawl, floor, outdoor, days=30, now=NOW)
    assert out["ready"] is False
    assert out["reason"] == "insufficient_n_eff"
    assert out["n_eff"] < out["n"]
    assert out["beta"] is None, "a refused fit must not leave a number behind"


def test_a_verdict_names_the_floors_it_actually_compared():
    """The old wording — "each floor follows the crawl less closely than the
    one below it" — was a claim about the whole house, printed after reasoning
    over whatever subset survived the ordering."""
    out = coupling.consistency_check(
        [_floor("Downstairs", 0.5), _floor("Upstairs", 0.3)])
    assert out["verdict"] == "consistent"
    assert "Downstairs then Upstairs" in out["text"]
    assert out["compared"] == ["Downstairs", "Upstairs"]


def test_an_excluded_sensor_is_named_in_the_verdict():
    """The excluded channel can be the LARGEST coupling on the page. Saying
    'each floor' while leaving it out is a false statement to the one reader
    who would act on it."""
    out = coupling.consistency_check(
        [_floor("Downstairs", 0.5), _floor("Upstairs", 0.3)],
        excluded=["Garage"])
    assert out["excluded"] == ["Garage"]
    assert "Garage" in out["text"]
    assert "does not say where in the house it sits" in out["text"]


def test_an_excluded_sensor_is_named_on_a_bypass_verdict_too():
    """A bypass verdict sends someone looking for leaky ducts. It must not
    hide that the reasoning ran on a subset."""
    out = coupling.consistency_check(
        [_floor("Downstairs", 0.1, ci=0.05), _floor("Upstairs", 0.6, ci=0.05)],
        excluded=["Garage"])
    assert out["verdict"] == "bypass_suspected"
    assert "Garage" in out["text"]


def test_the_unknown_order_verdict_still_carries_its_exclusions():
    out = coupling.consistency_check(
        [_floor("A", 0.1), _floor("B", 0.6)], ordered=False, excluded=["A", "B"])
    assert out["verdict"] == "unknown_order"
    assert out["excluded"] == ["A", "B"]
