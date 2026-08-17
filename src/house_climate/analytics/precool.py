"""Retrospective pre-cool effectiveness.

Does shifting cooling out of the on-peak TOU window actually cut peak-window
cooling? Each day the peak window applies to is classified as **pre-cool** (the
coast raised the cool setpoint by >= 2 deg-F right around the window's start) or
**normal**, then peak-window cooling is compared between the two groups,
normalized by how hot it was during peak.

The peak window (start/end + whether it's weekday-only) is passed in by the
caller, derived from the configured TOU table (`TouTable.peak_window`) — this
module hardcodes no hours, so a utility whose peak is 16:00-20:00 (or a weekend
peak) is analyzed over the right window.

This needs a control: some pre-cool-OFF days. With pre-cool always on there is
nothing to compare against, so it reports {"ready": False} and the UI tells the
user to run a few days with pre-cool disabled.
"""
from datetime import time
from zoneinfo import ZoneInfo

# Retained as the default window so a caller that doesn't pass one keeps the
# example schedule's behavior; the config-derived window overrides these.
PEAK_START = time(17, 0)
PEAK_END = time(21, 0)
_COOL = {"cooling", "overcool"}


def _out(r):
    v = r.get("daikin_outdoor_temp_f")
    return v if v is not None else r.get("wx_outdoor_temp_f")


def _mins(t):
    return t.hour * 60 + t.minute


def effectiveness(readings, tz_name, *, min_days_each=2, base_f=75.0,
                  min_peak_minutes=60.0, peak_start=PEAK_START, peak_end=PEAK_END,
                  peak_weekday_only=True):
    tz = ZoneInfo(tz_name)
    ps, pe = _mins(peak_start), _mins(peak_end)
    if pe <= ps:                              # wrap-aware (peak windows are midday, but be safe)
        pe += 24 * 60
    rows = sorted(readings, key=lambda r: r["ts"])
    days = {}
    for a, b in zip(rows, rows[1:]):
        la = a["ts"].astimezone(tz)
        if peak_weekday_only and la.weekday() >= 5:   # peak applies to weekdays only
            continue
        d = days.setdefault(la.date(), {"cool_min": 0.0, "out_sum": 0.0,
                                        "out_min": 0.0, "min_sum": 0.0,
                                        "sp_before": None, "sp_after": None})
        sp = a.get("cool_setpoint_f")
        if sp is not None:
            m = _mins(la.time())
            if ps - 15 <= m < ps:            # just before the coast (window start)
                d["sp_before"] = sp
            elif ps <= m <= ps + 15:          # just after
                d["sp_after"] = sp
        dt = (b["ts"] - a["ts"]).total_seconds() / 60.0
        # Window membership by interval MIDPOINT: attributing by the start
        # timestamp smears up to one poll interval across the window edges
        # every single day.
        lm = (a["ts"] + (b["ts"] - a["ts"]) / 2).astimezone(tz)
        lm_m = _mins(lm.time())
        lm_m_wrapped = lm_m + 24 * 60 if pe > 24 * 60 and lm_m < ps else lm_m
        if ps <= lm_m_wrapped < pe and 0 < dt <= 30:
            d["min_sum"] += dt
            o = _out(a)
            if o is not None:
                d["out_sum"] += o * dt
                d["out_min"] += dt
            if a.get("equipment_status") in _COOL:
                d["cool_min"] += dt

    groups = {"precool": [], "normal": []}
    for v in days.values():
        if v["min_sum"] < min_peak_minutes:    # need most of the peak window covered
            continue
        # avg over only the minutes that HAD an outdoor reading — dividing by
        # all minutes would drag the average toward zero during feed outages.
        avg_out = v["out_sum"] / v["out_min"] if v["out_min"] else None
        # A day whose setpoint can't be observed around 17:00 (poller gap) is
        # UNCLASSIFIABLE — skipping it beats defaulting it into "normal",
        # where a genuine pre-cool day would poison the control group.
        if v["sp_before"] is None or v["sp_after"] is None:
            continue
        raised = v["sp_after"] - v["sp_before"] >= 2.0
        groups["precool" if raised else "normal"].append(
            {"cool_min": v["cool_min"], "out_f": avg_out})

    def summarize(lst):
        if not lst:
            return None
        n = len(lst)
        cm = sum(x["cool_min"] for x in lst) / n
        outs = [x["out_f"] for x in lst if x["out_f"] is not None]
        avg_out = sum(outs) / len(outs) if outs else None
        norm = cm / (avg_out - base_f) if (avg_out is not None and avg_out > base_f) else None
        return {"days": n, "avg_peak_cool_min": round(cm, 1),
                "avg_peak_out_f": round(avg_out, 1) if avg_out is not None else None,
                "cool_min_per_deg": round(norm, 2) if norm is not None else None}

    pc = summarize(groups["precool"])
    nm = summarize(groups["normal"])
    if not pc or not nm or pc["days"] < min_days_each or nm["days"] < min_days_each:
        return {"ready": False, "reason": "collecting",
                "precool_days": pc["days"] if pc else 0,
                "normal_days": nm["days"] if nm else 0}

    saved = None
    if pc["cool_min_per_deg"] is not None and nm["cool_min_per_deg"] is not None \
            and pc["avg_peak_out_f"] is not None:
        heat = max(0.0, pc["avg_peak_out_f"] - base_f)
        saved = round(max(0.0, (nm["cool_min_per_deg"] - pc["cool_min_per_deg"]) * heat))
    return {"ready": True, "precool": pc, "normal": nm,
            "peak_min_saved_per_day": saved}
