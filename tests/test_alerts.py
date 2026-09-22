from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import types

import pytest

from house_climate.config import load_config
from house_climate.web import alerts

from conftest import CFG_PATH

CFG = load_config(CFG_PATH)

_BASE = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _row(minute, status="cooling", hum=48, indoor=72, cool_sp=72):
    # NOTE: built via timedelta offset, not datetime(..., minute=minute, ...) --
    # the brief's sample test used the latter, which raises ValueError once
    # `minute` reaches 60 (e.g. range(0, 63, 3)). See task-11-report.md.
    return {"ts": _BASE + timedelta(minutes=minute),
            "equipment_status": status, "indoor_humidity": hum,
            "indoor_temp_f": indoor, "cool_setpoint_f": cool_sp, "heat_setpoint_f": 68}


def test_humidity_high_fires():
    rows = [_row(m, hum=70) for m in range(0, 63, 3)]   # >60% for >60 min
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "humidity_high" for a in out)


def test_humidity_high_does_not_fire_when_brief():
    # only 6 minutes above threshold, well short of the 60-minute sustain window
    rows = [_row(m, hum=48) for m in range(0, 30, 3)]
    rows += [_row(m, hum=70) for m in range(30, 36, 3)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "humidity_high" for a in out)


def test_humidity_high_does_not_fire_if_recovered_by_latest_row():
    # high for a full sustained window, but the very latest row has recovered
    # below threshold -- must NOT fire even though span >= sustained_minutes
    rows = [_row(m, hum=70) for m in range(0, 60, 3)]
    rows += [_row(60, hum=48)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "humidity_high" for a in out)


def test_humidity_high_does_not_fire_on_non_contiguous_spikes():
    # a spike at the very start, back to normal for a long stretch, then a
    # short trailing run of high readings ending at the latest row. The
    # first-match-to-last-match span is >= 60 min, but the CONTIGUOUS
    # trailing run is only 6 min -- must NOT fire.
    rows = [_row(0, hum=70)]
    rows += [_row(m, hum=48) for m in range(3, 60, 3)]
    rows += [_row(m, hum=70) for m in (60, 63, 66)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "humidity_high" for a in out)


def test_short_cycle_alert_fires():
    # many tiny cool cycles within window
    rows = []
    for base in range(0, 60, 6):
        rows += [_row(base, "cooling"), _row(base + 3, "idle")]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "short_cycling" for a in out)


def test_short_cycle_alert_does_not_fire_on_normal_runtime():
    # single long cool cycle, no short-cycling
    rows = [_row(0, "cooling"), _row(30, "cooling"), _row(60, "idle")]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "short_cycling" for a in out)


def test_offline_fires_on_missed_polls():
    out = alerts.evaluate([], CFG, poll_errors_recent=5)
    assert any(a.key == "offline" for a in out)


def test_offline_fires_on_empty_rows_even_without_poll_errors():
    out = alerts.evaluate([], CFG, poll_errors_recent=0)
    assert any(a.key == "offline" for a in out)


def test_offline_short_circuits_other_alerts():
    out = alerts.evaluate([], CFG, poll_errors_recent=5)
    assert [a.key for a in out] == ["offline"]


def test_setpoint_drift_fires_on_sustained_overshoot():
    # indoor temp 3+ degrees above cool setpoint for the full sustained window
    rows = [_row(m, status="cooling", indoor=75, cool_sp=72) for m in range(0, 48, 3)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "setpoint_drift" for a in out)


def test_no_alerts_on_healthy_rows():
    rows = [_row(m) for m in range(0, 30, 3)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert out == []


def test_offline_fires_when_readings_stale():
    # newest reading is older than (poll_interval_s * offline_missed_polls),
    # i.e. the poller has gone silent even though no poll_errors accrued and
    # the readings are still inside the 3h lookback window.
    stale_after = CFG.poll_interval_s * CFG.alerts["offline_missed_polls"]
    rows = [_row(0)]   # single healthy row, nothing else would fire
    now_stale = rows[-1]["ts"] + timedelta(seconds=stale_after + 60)
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=now_stale)
    assert any(a.key == "offline" for a in out)


def test_offline_does_not_fire_on_fresh_healthy_reading():
    # non-vacuous companion to test_offline_fires_when_readings_stale: the
    # exact same fixture, evaluated as fresh (now = latest row's ts), must
    # NOT report offline -- proves the alert above is due to staleness, not
    # some other property of the fixture.
    rows = [_row(0)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "offline" for a in out)


def test_air_quality_alert_fires_on_unhealthy_aqi():
    rows = [_row(0)]
    rows[-1]["wx_aqi"] = CFG.alerts["aqi_unhealthy"]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "air_quality" for a in out)


def test_air_quality_alert_does_not_fire_below_threshold():
    # non-vacuous companion: same fixture shape, AQI just under the threshold
    rows = [_row(0)]
    rows[-1]["wx_aqi"] = CFG.alerts["aqi_unhealthy"] - 1
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "air_quality" for a in out)


def test_weather_alert_fires_on_active_nws_alert():
    rows = [_row(0)]
    rows[-1]["wx_alert_count"] = 2
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "weather_alert" for a in out)


def test_weather_alert_does_not_fire_with_no_active_alerts():
    rows = [_row(0)]
    rows[-1]["wx_alert_count"] = 0
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "weather_alert" for a in out)


def test_air_quality_and_weather_alert_absent_on_clean_data():
    # Neither key set at all (missing from the row dict, as older rows in the
    # DB before this feature shipped would be) -- must not raise or fire.
    rows = [_row(0)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key in ("air_quality", "weather_alert") for a in out)


def test_drift_suppressed_during_active_recovery():
    # Cool setpoint is 67 and indoor is falling toward it (73 -> 70): a normal
    # post-setpoint-change pulldown, not a fault -> no drift alert.
    rows = [_row(m, indoor=round(73 - m * 0.05, 2), cool_sp=67) for m in range(0, 63, 3)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert not any(al.key == "setpoint_drift" for al in out)


def test_drift_fires_when_stalled_above_setpoint():
    # Drifted 3F above setpoint, steady (not falling), no recent setpoint change
    # -> the system isn't keeping up -> drift alert fires.
    rows = [_row(m, indoor=70, cool_sp=67) for m in range(0, 63, 3)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert any(al.key == "setpoint_drift" for al in out)


def test_poll_errors_alone_do_not_fire_offline_when_data_fresh():
    """Ecowitt-gateway failures land in poll_errors too; with FRESH thermostat
    rows an error count must not fire the critical offline alert (which also
    early-returns and masks every real alert)."""
    rows = [_row(m, hum=48) for m in range(0, 30, 3)]
    out = alerts.evaluate(rows, CFG, poll_errors_recent=7, now=rows[-1]["ts"])
    assert not any(a.key == "offline" for a in out)


def test_sustained_broken_by_observation_gap():
    """Two above-threshold samples three hours apart are two moments, not
    three sustained hours — a poller outage inside the run breaks it."""
    rows = [_row(0, hum=70), _row(180, hum=70)]   # 3h hole between samples
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "humidity_high" for a in out)


# --- _sustained: a null field is "not observed" (skip), not "cleared" -------

def test_sustained_survives_intermittent_null_reading():
    # A cloud API drops one null indoor_humidity reading mid-run. The run must
    # NOT reset — the condition is still genuinely sustained. (Old code coerced
    # null -> 0 -> predicate false -> broke the run and the alert never fired.)
    rows = [_row(m, hum=70) for m in range(0, 63, 3)]
    rows[5]["indoor_humidity"] = None   # a single dropped reading in the middle
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "humidity_high" for a in out)


def test_sustained_false_when_latest_field_is_null():
    # The latest reading has no humidity at all -> can't assert the condition
    # holds right now -> must not fire.
    rows = [_row(m, hum=70) for m in range(0, 63, 3)]
    rows[-1]["indoor_humidity"] = None
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "humidity_high" for a in out)


# --- Freeze / frost ---------------------------------------------------------

def test_freeze_alert_fires_when_outdoor_at_or_below_threshold():
    rows = [_row(0)]
    rows[-1]["wx_outdoor_temp_f"] = 30
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "freeze" for a in out)


def test_freeze_alert_does_not_fire_when_mild():
    rows = [_row(0)]
    rows[-1]["wx_outdoor_temp_f"] = 55
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert not any(a.key == "freeze" for a in out)


def test_freeze_uses_daikin_outdoor_when_station_missing():
    rows = [_row(0)]
    rows[-1]["wx_outdoor_temp_f"] = None
    rows[-1]["daikin_outdoor_temp_f"] = 28
    out = alerts.evaluate(rows, CFG, poll_errors_recent=0, now=rows[-1]["ts"])
    assert any(a.key == "freeze" for a in out)


# --- Crawl-space mold risk (sustained) --------------------------------------

def _crawl(minute, hum):
    return {"ts": _BASE + timedelta(minutes=minute), "humidity": hum}


def _fresh_row(ts):
    # a healthy thermostat row stamped AT `ts`, so the offline check (which
    # compares now to the latest reading) stays fresh and doesn't short-circuit.
    return {**_row(0), "ts": ts}


def test_crawl_mold_fires_on_sustained_high_rh():
    crawl = [_crawl(m, 80) for m in range(0, 200, 5)]   # >75% for >180 min
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_mold" for a in out)


def test_crawl_mold_does_not_fire_when_brief():
    crawl = [_crawl(m, 80) for m in range(0, 30, 5)]    # only 30 min
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_mold" for a in out)


def test_crawl_mold_skipped_when_no_crawl_rows():
    out = alerts.evaluate([_row(0)], CFG, 0, now=_BASE, crawl_rows=None)
    assert not any(a.key == "crawl_mold" for a in out)


def _crawl_full(minute, hum, temp=60.0, dp=None):
    # A crawl row shaped like db.sensor_readings_range: ts, humidity, temp_f,
    # dewpoint_f. `dp=None` mirrors a row where dew point was not computed.
    return {"ts": _BASE + timedelta(minutes=minute), "humidity": hum,
            "temp_f": temp, "dewpoint_f": dp}


# --- Escalated RH tier: sustained >=90% is a distinct, worse regime ---------

def test_crawl_saturated_fires_above_90():
    crawl = [_crawl(m, 95) for m in range(0, 200, 5)]   # >90% for >180 min
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_saturated" for a in out)


def test_crawl_saturated_suppresses_mold():
    # At >=90% the saturated tier fires and the 75% mold alert is suppressed --
    # one damp crawl must not buzz the phone twice for the same condition.
    crawl = [_crawl(m, 95) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_saturated" for a in out)
    assert not any(a.key == "crawl_mold" for a in out)


def test_crawl_mold_fires_but_not_saturated_between_75_and_90():
    crawl = [_crawl(m, 80) for m in range(0, 200, 5)]   # 75<RH<90
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_mold" for a in out)
    assert not any(a.key == "crawl_saturated" for a in out)


def test_crawl_saturated_disabled_when_misconfigured_below_mold():
    # Misconfig: saturated bar transposed BELOW the mold bar. The escalation
    # must NOT fire (it would mislabel a moldy crawl as "near saturation") and
    # must NOT suppress the accurate mold alert.
    import dataclasses
    cfg = dataclasses.replace(CFG, alerts={**CFG.alerts,
                                           "crawl_saturated_pct": 70,
                                           "crawl_mold_pct": 75})
    crawl = [_crawl(m, 80) for m in range(0, 200, 5)]   # moldy, not saturated
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], cfg, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_saturated" for a in out)
    assert any(a.key == "crawl_mold" for a in out)


def test_crawl_saturated_disabled_when_equal_to_mold_bar():
    # The guard is strict `sat_pct > mold_pct`. With the bars EQUAL there is no
    # sensible escalation band, so saturated must stay disabled and mold fire —
    # pins the boundary against a silent flip to `>=` (which would fire
    # "near saturation" at the 75% mold level).
    import dataclasses
    cfg = dataclasses.replace(CFG, alerts={**CFG.alerts,
                                           "crawl_saturated_pct": 75,
                                           "crawl_mold_pct": 75})
    crawl = [_crawl(m, 95) for m in range(0, 200, 5)]   # well above both bars
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], cfg, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_saturated" for a in out)
    assert any(a.key == "crawl_mold" for a in out)


def test_crawl_mold_fires_at_exactly_75():
    # `_mold` is `>= mold_pct` (75); exactly the bar must fire. Pins the
    # inclusive lower boundary against a silent flip to `> mold_pct`, mirroring
    # the saturated inclusive-boundary test at 90.
    crawl = [_crawl(m, 75) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_mold" for a in out)


def test_crawl_saturated_boundary_at_exactly_90():
    # sat_pct default is 90 and the test is `>= sat_pct` -- exactly 90 must
    # escalate (and suppress mold). Pins the inclusive boundary against a
    # silent flip to `> sat_pct`.
    crawl = [_crawl(m, 90) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_saturated" for a in out)
    assert not any(a.key == "crawl_mold" for a in out)


# --- Condensation risk: sustained small air-to-dew-point spread -------------

def test_crawl_condensation_fires_on_sustained_small_spread():
    # temp 55, dew 54 -> spread 1F < 3F, sustained past the window.
    crawl = [_crawl_full(m, 96, temp=55.0, dp=54.0) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_does_not_fire_on_wide_spread():
    # temp 60, dew 50 -> spread 10F, comfortably above the 3F bar.
    crawl = [_crawl_full(m, 70, temp=60.0, dp=50.0) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_skipped_when_dewpoint_missing():
    # dew point null on every row -> not OBSERVED, so the alert can't fire
    # (must not treat missing data as either safe or condensing).
    crawl = [_crawl_full(m, 96, temp=55.0, dp=None) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_does_not_fire_when_brief():
    crawl = [_crawl_full(m, 96, temp=55.0, dp=54.0) for m in range(0, 30, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_skipped_when_temp_missing():
    # temp_f null (dew point present) is also NOT OBSERVED -- guards against a
    # regression that drops the temp check and reads dewpoint alone.
    crawl = [_crawl_full(m, 96, temp=None, dp=54.0) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_survives_interior_null_dewpoint():
    # A genuinely condensing run with one interior row missing dew point: the
    # None is SKIPPED, not a break, so the run still spans the window and fires.
    crawl = [_crawl_full(m, 96, temp=55.0, dp=54.0) for m in range(0, 200, 5)]
    crawl[10]["dewpoint_f"] = None   # a single dropped reading mid-run
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_boundary_at_exactly_3f_spread():
    # spread == 3.0 must NOT fire (predicate is strict `< cond_spread`).
    crawl = [_crawl_full(m, 90, temp=58.0, dp=55.0) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert not any(a.key == "crawl_condensation" for a in out)


def test_crawl_condensation_and_saturated_are_independent():
    # A cold, near-saturated crawl trips BOTH tiers: condensation is a separate
    # `if`, not folded into the RH `elif`. Locks that independence in.
    crawl = [_crawl_full(m, 97, temp=50.0, dp=49.0) for m in range(0, 200, 5)]
    now = crawl[-1]["ts"]
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    keys = {a.key for a in out}
    assert "crawl_saturated" in keys
    assert "crawl_condensation" in keys


def test_alert_context_window_covers_longest_crawl_sustained(monkeypatch):
    """The crawl fetch window must span the LONGEST crawl sustained-window, not
    just mold's -- a condensation window longer than mold's would otherwise be
    starved of rows the same way the original mold fetch-window bug starved it."""
    import types
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", 0.0)
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: ("ecowitt_ch1", "crawl"))
    monkeypatch.setattr(alerts.api, "filter_status", lambda *a, **k: {"due": False})
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (None, None))
    captured = {}

    def fake_range(conn, sensor_id, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(alerts.db, "sensor_readings_range", fake_range)
    cfg = types.SimpleNamespace(alerts={"crawl_mold_sustained_minutes": 180,
                                        "crawl_condensation_sustained_minutes": 600})
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=3)
    alerts._alert_context(None, "dev", cfg, since, rows=[])
    window_min = (now - captured["since"]).total_seconds() / 60.0
    assert window_min >= 600 * 1.5, \
        f"crawl fetch window {window_min:.0f}min must span the 600min condensation window"


def test_alert_context_window_covers_mold_when_it_is_longer(monkeypatch):
    """The mirror of the above: when MOLD is the longer window, the fetch must
    still span it. Without asserting both directions, dropping mold from the
    max() (span=cond_min) would slip through and starve the mold fetch."""
    import types
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", 0.0)
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: ("ecowitt_ch1", "crawl"))
    monkeypatch.setattr(alerts.api, "filter_status", lambda *a, **k: {"due": False})
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (None, None))
    captured = {}

    def fake_range(conn, sensor_id, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(alerts.db, "sensor_readings_range", fake_range)
    cfg = types.SimpleNamespace(alerts={"crawl_mold_sustained_minutes": 600,
                                        "crawl_condensation_sustained_minutes": 180})
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=3)
    alerts._alert_context(None, "dev", cfg, since, rows=[])
    window_min = (now - captured["since"]).total_seconds() / 60.0
    assert window_min >= 600 * 1.5, \
        f"crawl fetch window {window_min:.0f}min must span the 600min mold window"


def test_alert_context_fetches_crawl_over_window_larger_than_mold(monkeypatch):
    """REGRESSION (fable): _alert_context fetched crawl over the short-cycle
    `since` window (which can be ~= crawl_mold_sustained_minutes), so _sustained
    could never find a run spanning mold_min and the mold alert never fired.
    This asserts the crawl fetch window is comfortably larger than mold_min --
    the REAL path (the evaluate() mold tests above use an injected series and
    can't catch a too-short fetch window)."""
    monkeypatch.setattr(alerts, "_filter_due_cache", False)   # avoid cache pollution
    monkeypatch.setattr(alerts, "_filter_due_at", 0.0)
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: ("ecowitt_ch1", "crawl"))
    monkeypatch.setattr(alerts.api, "filter_status", lambda *a, **k: {"due": False})
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (None, None))
    captured = {}

    def fake_range(conn, sensor_id, since):
        captured["since"] = since
        return []

    monkeypatch.setattr(alerts.db, "sensor_readings_range", fake_range)

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=3)   # the buggy case: short-cycle window ~= mold_min (180)
    alerts._alert_context(None, "dev", CFG, since, rows=[])

    mold_min = CFG.alerts.get("crawl_mold_sustained_minutes", 180)
    window_min = (now - captured["since"]).total_seconds() / 60.0
    assert window_min >= mold_min * 1.5, \
        f"crawl fetch window {window_min:.0f}min must be >> mold_min {mold_min}min so a run can span it"


def test_mold_alert_end_to_end_through_db(conn, monkeypatch):
    """DB-backed REAL path: >3h of crawl readings above the mold threshold in
    the DB -> _alert_context fetches them over the correct window -> evaluate
    fires the mold alert. Catches the fetch-window bug end to end (skips locally
    without Timescale via the conn fixture; runs in CI)."""
    from house_climate import db as fdb
    sid = "ecowitt_ch_crawltest"
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: (sid, "crawl"))
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", 0.0)
    now = datetime.now(timezone.utc)
    # Readings from 240min..5min ago (latest is 5min stale, a realistic probe
    # cadence). This is deliberately NOT boundary-aligned to `now`: under the
    # BUGGY short window (since = now-180) the fetched span is only 175min < 180
    # and mold must NOT fire; only the fixed 2*mold_min window fetches the full
    # 235min so it DOES fire -- so this test actually distinguishes the fix.
    for off in range(5, 241, 5):
        fdb.insert_sensor_reading(conn, sid, now - timedelta(minutes=off),
                                  temp_f=68.0, humidity=80.0)
    since = now - timedelta(hours=3)
    crawl_rows, _due, _aqi, _src = alerts._alert_context(conn, "dev", CFG, since, rows=[])
    assert crawl_rows and len(crawl_rows) >= 30
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl_rows)
    assert any(a.key == "crawl_mold" for a in out)


# --- Filter due -------------------------------------------------------------

def test_filter_due_alert_fires_when_due():
    out = alerts.evaluate([_row(0)], CFG, 0, now=_BASE, filter_due=True)
    assert any(a.key == "filter_due" for a in out)


def test_filter_due_alert_absent_when_not_due():
    out = alerts.evaluate([_row(0)], CFG, 0, now=_BASE, filter_due=False)
    assert not any(a.key == "filter_due" for a in out)


# --- AirNow-preferred AQI ---------------------------------------------------

def test_air_quality_fires_from_airnow_when_wx_aqi_absent():
    # The weather feed has NO wx_aqi, but AirNow (resolved by the caller) shows
    # unhealthy air. The alert must fire off the AirNow value.
    rows = [_row(0)]   # no wx_aqi key
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"],
                          outdoor_aqi=CFG.alerts["aqi_unhealthy"] + 20)
    assert any(a.key == "air_quality" for a in out)


def test_air_quality_does_not_fire_when_airnow_below_threshold():
    rows = [_row(0)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"],
                          outdoor_aqi=CFG.alerts["aqi_unhealthy"] - 1)
    assert not any(a.key == "air_quality" for a in out)


def _aq_message(out):
    return next(a.message for a in out if a.key == "air_quality")


def test_air_quality_push_names_a_modeled_aqi_as_an_estimate():
    """resolve_outdoor_aqi silently swaps the monitor reading for the weather
    feed's MODEL after 30 quiet minutes, and the two disagree in the direction
    that matters: on 2026-08-31 the model read 113 "Unhealthy" against a
    monitor's 85 "Moderate". A push that reads identically either way turns a
    monitor outage into a confident false claim on someone's phone."""
    rows = [_row(0)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"],
                          outdoor_aqi=CFG.alerts["aqi_unhealthy"] + 20,
                          aqi_source="weather")
    assert "estimated from the weather feed" in _aq_message(out)


def test_air_quality_push_from_a_real_monitor_carries_no_caveat():
    """The non-vacuous companion: hedging every push would train the reader to
    ignore the hedge, so a real monitor reading must stay unqualified."""
    rows = [_row(0)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"],
                          outdoor_aqi=CFG.alerts["aqi_unhealthy"] + 20,
                          aqi_source="airnow")
    msg = _aq_message(out)
    assert "estimated" not in msg, msg


def test_air_quality_push_treats_unknown_provenance_as_modeled():
    """The dangerous default. `_alert_context` leaves aqi_source None when the
    resolver raised, and callers predating this argument omit it entirely --
    both cases still print a number. Unknown provenance must fail toward "we
    are not sure", never toward an unearned claim of a real reading."""
    rows = [_row(0)]
    rows[-1]["wx_aqi"] = CFG.alerts["aqi_unhealthy"] + 5
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])   # no aqi_source
    assert "estimated from the weather feed" in _aq_message(out)


def _ctx(monkeypatch, resolver):
    """Drive _alert_context with the crawl/filter work stubbed out, so the
    assertions are about the AQI plumbing and nothing else. Mirrors the
    existing _alert_context tests above, which already run without a DB."""
    import types
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", float("inf"))
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: (None, None))
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", resolver)
    cfg = types.SimpleNamespace(alerts={})
    return alerts._alert_context(None, "dev", cfg,
                                 datetime.now(timezone.utc) - timedelta(hours=3),
                                 rows=[{"wx_aqi": 40}])


def test_alert_context_carries_the_provenance_out_with_the_value(monkeypatch):
    """BEHAVIOURAL, not a grep. The first version of this test string-matched
    the return statement; a reviewer inserted `aqi_source = None` right after
    the resolver call and the ENTIRE suite stayed green while every push --
    real monitor readings included -- got hedged as an estimate."""
    _crawl, _due, aqi, source = _ctx(monkeypatch, lambda *a, **k: (150, "airnow"))
    assert (aqi, source) == (150, "airnow")


def test_alert_context_reports_no_provenance_when_the_resolver_raises(monkeypatch):
    """The swallow must not invent a provenance. None flows on to evaluate(),
    which treats unknown as modeled -- the safe direction."""
    def boom(*a, **k):
        raise RuntimeError("kv table is on fire")
    _crawl, _due, aqi, source = _ctx(monkeypatch, boom)
    assert (aqi, source) == (None, None)


def test_alert_context_does_not_blame_the_resolver_for_a_bad_rows_shape(monkeypatch):
    """The `rows[-1]` read sits OUTSIDE the try on purpose: reporting a row-shape
    change as "AQI resolution failed" sends the next debugger to the kv table
    instead of to the caller that changed the rows."""
    import types
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", float("inf"))
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: (None, None))
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (1, "airnow"))
    with pytest.raises(AttributeError):
        alerts._alert_context(None, "dev", types.SimpleNamespace(alerts={}),
                              datetime.now(timezone.utc), rows=["not-a-dict"])


def test_alert_loop_passes_the_provenance_to_evaluate():
    """No cheap behavioural substitute: alert_loop owns a live DB connection and
    an infinite loop. Grep is the honest tool here, and it is the LAST link in
    the chain -- everything either side of it is behaviour-tested above."""
    import inspect
    assert "aqi_source=aqi_source" in inspect.getsource(alerts.evaluate_current), \
        "the shared evaluator resolves the provenance but never passes it to evaluate()"
    # ...and the push loop goes through that shared evaluator, not its own copy
    assert "evaluate_current(" in inspect.getsource(alerts.alert_loop)


def test_a_provenance_flip_is_not_swallowed_by_the_cooldown():
    """The cooldown used to key on `Alert.key` alone. Timeline: the monitor
    reads 105 and an UNQUALIFIED push lands on the phone; the monitor then goes
    quiet, the model says 113, and the corrected message carrying "estimated
    from the weather feed" is dropped as a duplicate. The reader is left
    holding the unhedged claim -- the exact failure the caveat exists to
    prevent."""
    sent = []
    sink = types.SimpleNamespace(send=sent.append)
    last_sent, cooldown = {}, timedelta(hours=1)
    now = datetime.now(timezone.utc)

    measured = alerts.Alert("air_quality", "warning", "AQI 105", variant="airnow")
    modeled = alerts.Alert("air_quality", "warning", "AQI 113, estimated",
                           variant="estimate")
    alerts._dispatch(sink, [measured], last_sent, cooldown, now)
    alerts._dispatch(sink, [modeled], last_sent, cooldown, now + timedelta(minutes=1))
    assert [a.message for a in sent] == ["AQI 105", "AQI 113, estimated"]

    # ...and the cooldown still works WITHIN one provenance, or the flip fix
    # would just have turned the alert into a per-tick buzzer.
    alerts._dispatch(sink, [modeled], last_sent, cooldown, now + timedelta(minutes=2))
    assert len(sent) == 2


def test_a_modeled_number_cannot_be_relabelled_as_a_measurement():
    """`evaluate` falls back to the reading's own wx_aqi when the resolver gave
    it nothing -- and that value IS the model, whatever `aqi_source` claims.
    Provenance has to follow the branch that produced the number."""
    rows = [_row(0)]
    rows[-1]["wx_aqi"] = CFG.alerts["aqi_unhealthy"] + 5
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"],
                          outdoor_aqi=None, aqi_source="airnow")
    assert "estimated from the weather feed" in _aq_message(out)


# --- NtfySink: a non-2xx response must RAISE (the CRITICAL fix) --------------

class _FakeResp:
    def __init__(self, status): self.status_code = status
    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


def test_ntfy_send_raises_on_non_2xx(monkeypatch):
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: _FakeResp(404))
    with pytest.raises(Exception):
        alerts.NtfySink("topic").send(alerts.Alert("freeze", "warning", "x"))


def test_ntfy_send_ok_on_2xx(monkeypatch):
    monkeypatch.setattr(alerts.requests, "post", lambda *a, **k: _FakeResp(200))
    alerts.NtfySink("topic").send(alerts.Alert("freeze", "warning", "x"))   # no raise


# --- Dispatch resilience: a failed send doesn't suppress or block -----------

class _Sink:
    def __init__(self, fail_keys=()):
        self.fail_keys = set(fail_keys)
        self.sent = []
    def send(self, alert):
        if alert.key in self.fail_keys:
            raise RuntimeError("send failed")
        self.sent.append(alert.key)


def test_dispatch_failed_send_not_marked_and_does_not_block_others():
    sink = _Sink(fail_keys={"a"})
    last_sent = {}
    now = _BASE
    a, b = alerts.Alert("a", "warning", "x"), alerts.Alert("b", "warning", "y")
    alerts._dispatch(sink, [a, b], last_sent, timedelta(hours=1), now)
    assert sink.sent == ["b"]                  # b still attempted despite a failing
    assert a.dedupe_key not in last_sent       # a left unsent -> retries next cycle
    assert b.dedupe_key in last_sent


def test_dispatch_respects_cooldown():
    sink = _Sink()
    now = _BASE
    al = alerts.Alert("a", "warning", "x")
    # Keyed on `dedupe_key` (key + variant), not the bare key -- see
    # test_a_provenance_flip_is_not_swallowed_by_the_cooldown for why.
    last_sent = {al.dedupe_key: now}
    alerts._dispatch(sink, [al], last_sent, timedelta(hours=1),
                     now + timedelta(minutes=5))
    assert sink.sent == []             # within cooldown -> not resent


# --- peak-hour surge (issue #3: peak_surge_ratio was read by nothing) ---

_LA = ZoneInfo("America/Los_Angeles")


def _row_at(now_local, status="cooling"):
    """A fresh single reading stamped at now_local (UTC), with the given
    equipment status — for exercising the on-peak surge check."""
    return {**_row(0), "ts": now_local.astimezone(timezone.utc), "equipment_status": status}


def test_peak_surge_fires_when_cooling_on_peak():
    # Mon 2026-08-10 18:00 PDT is on-peak ($0.40 = 4.4x the $0.09 off-peak).
    now = datetime(2026, 8, 10, 18, 0, tzinfo=_LA)
    out = alerts.evaluate([_row_at(now)], CFG, poll_errors_recent=0,
                          now=now.astimezone(timezone.utc))
    assert any(a.key == "peak_surge" for a in out)


def test_peak_surge_quiet_off_peak():
    # 10:00 PDT is mid-peak, not the top tier -> no surge even while cooling.
    now = datetime(2026, 8, 10, 10, 0, tzinfo=_LA)
    out = alerts.evaluate([_row_at(now)], CFG, poll_errors_recent=0,
                          now=now.astimezone(timezone.utc))
    assert not any(a.key == "peak_surge" for a in out)


def test_peak_surge_quiet_when_idle_on_peak():
    now = datetime(2026, 8, 10, 18, 0, tzinfo=_LA)
    out = alerts.evaluate([_row_at(now, status="idle")], CFG, poll_errors_recent=0,
                          now=now.astimezone(timezone.utc))
    assert not any(a.key == "peak_surge" for a in out)
# --- equipment-status drift (issue #4) ---

def test_equipment_unknown_alert_fires_when_status_drifts():
    # Half the recent readings carry an unrecognized ("unknown") status.
    rows = [_row(m, status=("unknown" if (m // 3) % 2 == 0 else "cooling"))
            for m in range(0, 60, 3)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert any(a.key == "equipment_unknown" for a in out)


def test_equipment_unknown_quiet_when_status_known():
    rows = [_row(m, status="cooling") for m in range(0, 60, 3)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert not any(a.key == "equipment_unknown" for a in out)
# --- weather-feed staleness (issue #5) ---

def _wx_row(minute, weather_ok):
    return {**_row(minute), "weather_ok": weather_ok}


def test_weather_feed_stale_fires_when_feed_down():
    rows = [_wx_row(m, False) for m in range(0, 45, 3)]   # >30 min of feed down
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert any(a.key == "weather_feed_stale" for a in out)


def test_weather_feed_stale_quiet_when_feed_ok():
    rows = [_wx_row(m, True) for m in range(0, 45, 3)]
    out = alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])
    assert not any(a.key == "weather_feed_stale" for a in out)


# --- A stale THERMOSTAT must not silence the crawl (a separate sensor) -------

def _stale_thermostat_now():
    stale_after = CFG.poll_interval_s * CFG.alerts["offline_missed_polls"]
    return _BASE + timedelta(seconds=stale_after + 3600)


def test_stale_thermostat_still_evaluates_crawl_alerts():
    """A Daikin cloud outage used to early-return out of evaluate(), taking the
    Ecowitt crawl alerts down with it. The crawl probe is still reporting, so
    its mold alert must still fire next to the offline alert."""
    now = _stale_thermostat_now()
    crawl = [{"ts": now - timedelta(minutes=m), "humidity": 80}
             for m in range(0, 200, 5)][::-1]
    out = alerts.evaluate([_row(0)], CFG, 0, now=now, crawl_rows=crawl,
                          filter_due=True)
    keys = {a.key for a in out}
    assert "offline" in keys
    assert "crawl_mold" in keys
    assert "filter_due" in keys


def test_stale_thermostat_skips_the_checks_built_on_its_rows():
    """Everything read off the thermostat rows (including the weather fields
    the poller writes into the same row) is old news once the row is stale:
    no humidity/drift/freeze/NWS alert from a reading that is an hour old."""
    rows = [_row(m, hum=70, indoor=80) for m in range(0, 90, 3)]
    rows[-1] = {**rows[-1], "wx_outdoor_temp_f": 20, "wx_alert_count": 2,
                "wx_aqi": 180}
    fresh = {a.key for a in alerts.evaluate(rows, CFG, 0, now=rows[-1]["ts"])}
    # non-vacuous: the same rows, evaluated fresh, DO fire all of these
    assert {"humidity_high", "freeze", "weather_alert", "air_quality"} <= fresh
    stale_after = CFG.poll_interval_s * CFG.alerts["offline_missed_polls"]
    now = rows[-1]["ts"] + timedelta(seconds=stale_after + 60)
    out = alerts.evaluate(rows, CFG, 0, now=now)
    assert [a.key for a in out] == ["offline"]


def test_stale_thermostat_still_reports_a_fresh_monitor_aqi():
    """The monitor AQI comes from Home Assistant, not the thermostat row, so it
    stays valid through a thermostat outage. A modeled value that came off the
    stale row does not."""
    now = _stale_thermostat_now()
    out = alerts.evaluate([_row(0)], CFG, 0, now=now, outdoor_aqi=180,
                          aqi_source="airnow")
    assert "air_quality" in {a.key for a in out}
    out = alerts.evaluate([_row(0)], CFG, 0, now=now, outdoor_aqi=180,
                          aqi_source="weather")
    assert "air_quality" not in {a.key for a in out}


def test_no_thermostat_rows_at_all_still_evaluates_crawl():
    now = _BASE + timedelta(hours=4)
    crawl = [{"ts": now - timedelta(minutes=m), "humidity": 80}
             for m in range(0, 200, 5)][::-1]
    keys = {a.key for a in alerts.evaluate([], CFG, 0, now=now, crawl_rows=crawl)}
    assert {"offline", "crawl_mold"} <= keys


# --- Crawl-sensor freshness ---------------------------------------------------

def _crawl_ending(end, hum=80, span_min=200):
    return [{"ts": end - timedelta(minutes=m), "humidity": hum,
             "temp_f": 60.0, "dewpoint_f": 58.5}
            for m in range(0, span_min, 5)][::-1]


def test_dead_crawl_probe_does_not_keep_the_mold_alert_alive():
    """A probe that died at 80% RH used to keep the mold alert firing on its
    last readings for hours. Old data is not a current condition."""
    now = _BASE + timedelta(hours=6)
    crawl = _crawl_ending(now - timedelta(hours=2))     # probe silent for 2h
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    keys = {a.key for a in out}
    assert not keys & {"crawl_mold", "crawl_saturated", "crawl_condensation"}
    assert "crawl_sensor_offline" in keys


def test_crawl_sensor_offline_when_configured_but_no_rows_in_window():
    now = _BASE
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=[])
    assert "crawl_sensor_offline" in {a.key for a in out}


def test_crawl_sensor_offline_quiet_when_not_configured():
    now = _BASE
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=None)
    assert "crawl_sensor_offline" not in {a.key for a in out}


def test_crawl_sensor_offline_quiet_when_fresh():
    now = _BASE + timedelta(hours=6)
    crawl = _crawl_ending(now - timedelta(minutes=5), hum=50)
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert "crawl_sensor_offline" not in {a.key for a in out}


def test_crawl_offline_threshold_default_is_45_minutes():
    now = _BASE + timedelta(hours=6)
    fresh = _crawl_ending(now - timedelta(minutes=44), hum=50)
    old = _crawl_ending(now - timedelta(minutes=46), hum=50)
    assert "crawl_sensor_offline" not in {
        a.key for a in alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=fresh)}
    assert "crawl_sensor_offline" in {
        a.key for a in alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=old)}


def test_crawl_offline_threshold_is_configurable():
    cfg = types.SimpleNamespace(**{**CFG.__dict__,
                                   "alerts": {**CFG.alerts, "crawl_offline_minutes": 120}})
    now = _BASE + timedelta(hours=6)
    crawl = _crawl_ending(now - timedelta(minutes=90), hum=50)
    out = alerts.evaluate([_fresh_row(now)], cfg, 0, now=now, crawl_rows=crawl)
    assert "crawl_sensor_offline" not in {a.key for a in out}


def test_crawl_offline_message_says_how_long():
    now = _BASE + timedelta(hours=6)
    crawl = _crawl_ending(now - timedelta(hours=2), hum=50)
    msg = next(a.message for a in alerts.evaluate([_fresh_row(now)], CFG, 0, now=now,
                                                   crawl_rows=crawl)
               if a.key == "crawl_sensor_offline")
    assert "2h" in msg or "120" in msg


# --- One context for the wall strip AND the push loop ------------------------

def test_evaluate_current_passes_every_piece_of_context(monkeypatch):
    """/api/anomalies used to call evaluate() with no crawl rows, no filter flag
    and no resolved AQI, so the wall could never show those alerts. Both paths
    now go through evaluate_current; this pins that it forwards everything."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(alerts.db, "recent_readings", lambda *a, **k: [_fresh_row(now)])
    monkeypatch.setattr(alerts, "_poll_errors_recent", lambda conn: 0)
    crawl = [{"ts": now - timedelta(minutes=m), "humidity": 95}
             for m in range(0, 200, 5)][::-1]
    monkeypatch.setattr(alerts, "_alert_context",
                        lambda *a, **k: (crawl, True, 180, "airnow"))
    keys = {a.key for a in alerts.evaluate_current(None, "dev", CFG)}
    assert {"crawl_saturated", "filter_due", "air_quality"} <= keys



def test_crawl_fetch_failure_raises_an_alert_not_silence(monkeypatch):
    """A crawl probe that IS configured but whose rows could not be fetched used
    to look exactly like 'no crawl probe': every crawl alert vanished quietly."""
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", float("inf"))
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: ("ecowitt_ch1", "crawl"))
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (None, None))

    def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(alerts.db, "sensor_readings_range", boom)
    now = datetime.now(timezone.utc)
    crawl, *_ = alerts._alert_context(None, "dev", CFG, now - timedelta(hours=3), rows=[])
    assert crawl is alerts.CRAWL_FETCH_FAILED
    out = alerts.evaluate([_fresh_row(now)], CFG, 0, now=now, crawl_rows=crawl)
    assert "crawl_sensor_offline" in {a.key for a in out}


def test_crawl_fetch_window_covers_a_long_offline_threshold(monkeypatch):
    monkeypatch.setattr(alerts, "_filter_due_cache", False)
    monkeypatch.setattr(alerts, "_filter_due_at", float("inf"))
    monkeypatch.setattr(alerts.api, "_crawl_sensor_id", lambda cfg: ("ecowitt_ch1", "crawl"))
    monkeypatch.setattr(alerts.api, "resolve_outdoor_aqi", lambda *a, **k: (None, None))
    got = {}
    monkeypatch.setattr(alerts.db, "sensor_readings_range",
                        lambda c, sid, since: got.setdefault("since", since) and [])
    cfg = types.SimpleNamespace(alerts={"crawl_offline_minutes": 900})
    now = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
    alerts._alert_context(None, "dev", cfg, now, rows=[], now=now)
    assert (now - got["since"]) >= timedelta(minutes=900)


def test_evaluate_current_survives_a_failed_poll_error_count(monkeypatch):
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(alerts.db, "recent_readings", lambda *a, **k: [_fresh_row(now)])
    monkeypatch.setattr(alerts, "_alert_context", lambda *a, **k: (None, True, None, None))

    class BadConn:
        def execute(self, *a, **k):
            raise RuntimeError("poll_errors unreadable")
    assert "filter_due" in {a.key for a in alerts.evaluate_current(BadConn(), "dev", CFG)}
