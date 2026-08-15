from datetime import datetime, timezone, timedelta
from house_climate.analytics import thermal

BASE = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def _idle(ts, indoor, outdoor):
    return {"ts": ts, "indoor_temp_f": indoor, "daikin_outdoor_temp_f": outdoor,
            "equipment_status": "idle"}


def test_coasting_recovers_known_tau():
    # Generate idle drift from the exact RC model with tau=10h; the fit must
    # recover it. Each step: dT = k*(T_out - T_in)*dt, so drift == k*gap.
    k = 0.1                      # 1/tau -> tau = 10h
    tout, tin = 90.0, 70.0
    dt_h = 0.05                  # 3-minute polls
    rows = []
    for i in range(60):
        rows.append(_idle(BASE + timedelta(minutes=3 * i), tin, tout))
        tin += k * (tout - tin) * dt_h
    res = thermal.coasting_constant(rows)
    assert res["ready"] is True
    assert abs(res["tau_hours"] - 10.0) < 0.5
    assert res["r2"] >= 0.99


def test_coasting_collecting_when_thin():
    # Only a couple of readings and a sub-15-min run -> not enough to fit.
    rows = [_idle(BASE + timedelta(minutes=3 * i), 72.0, 90.0) for i in range(3)]
    res = thermal.coasting_constant(rows)
    assert res["ready"] is False
    assert res["reason"] in ("collecting", "no_signal")


def _idle_wx(ts, indoor, outdoor):
    # No Daikin outdoor sensor; only the weather feed -- the common case for a
    # home without a Daikin outdoor unit. _outdoor() must fall back to it.
    return {"ts": ts, "indoor_temp_f": indoor, "wx_outdoor_temp_f": outdoor,
            "equipment_status": "idle"}


def test_coasting_uses_wx_outdoor_when_daikin_absent():
    k = 0.1
    tout, tin = 90.0, 70.0
    dt_h = 0.05
    rows = []
    for i in range(60):
        rows.append(_idle_wx(BASE + timedelta(minutes=3 * i), tin, tout))
        tin += k * (tout - tin) * dt_h
    res = thermal.coasting_constant(rows)
    assert res["ready"] is True
    assert abs(res["tau_hours"] - 10.0) < 0.5


def test_coasting_no_signal_when_indoor_barely_drifts():
    # A big indoor/outdoor gap but the indoor temp never moves -> slope ~0 ->
    # no_signal, not a bogus enormous tau.
    tout, tin = 95.0, 70.0
    rows = [_idle(BASE + timedelta(minutes=3 * i), tin, tout) for i in range(60)]
    res = thermal.coasting_constant(rows)
    assert res["ready"] is False
    assert res["reason"] == "no_signal"


def _r(ts, status, outdoor):
    return {"ts": ts, "equipment_status": status,
            "daikin_outdoor_temp_f": outdoor, "indoor_temp_f": 74.0}


def test_load_curve_refuses_implausible_balance_point():
    # A clean linear fit whose x-intercept (balance point) is ~30 deg-F, below
    # the physically plausible 50-80 range: must hold at "learning" (noisy),
    # never publish a nonsense balance point.
    rows = []
    t = BASE
    for tout in [60, 64, 68, 72, 76, 80, 84, 88]:
        ncool = round(0.006 * (tout - 30) * 100)
        for i in range(100):
            rows.append(_r(t, "cooling" if i < ncool else "idle", tout))
            t += timedelta(minutes=3)
    res = thermal.cooling_load_curve(rows)
    assert res["ready"] is False
    assert res["reason"] == "noisy"


def test_load_curve_recovers_balance_and_slope():
    # Cooling fraction rises ~3%/degF above a 65degF balance point.
    rows = []
    t = BASE
    for tout in [62, 66, 70, 74, 78, 82, 86, 90]:
        ncool = round(max(0.0, 0.03 * (tout - 65)) * 20)
        for i in range(20):
            rows.append(_r(t, "cooling" if i < ncool else "idle", tout))
            t += timedelta(minutes=3)
    res = thermal.cooling_load_curve(rows)
    assert res["ready"] is True
    assert abs(res["balance_point_f"] - 65) < 4
    assert abs(res["slope_pct_per_f"] - 3.0) < 1.2


def test_load_curve_collecting_without_spread():
    # All one outdoor temperature -> can't fit a load-vs-temp line.
    rows = [_r(BASE + timedelta(minutes=3 * i), "idle", 75) for i in range(30)]
    res = thermal.cooling_load_curve(rows)
    assert res["ready"] is False
