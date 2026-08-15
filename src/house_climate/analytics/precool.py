"""Retrospective pre-cool effectiveness.

Does shifting cooling out of the on-peak TOU window (17:00-21:00) actually cut
peak-window cooling? Each weekday is classified as **pre-cool** (the coast
raised the cool setpoint by >= 2 deg-F right around 17:00) or **normal**, then
peak-window cooling is compared between the two groups, normalized by how hot
it was during peak.

This needs a control: some pre-cool-OFF weekdays. With pre-cool always on there
is nothing to compare against, so it reports {"ready": False} and the UI tells
the user to run a few days with pre-cool disabled.
"""
from zoneinfo import ZoneInfo

PEAK_START_H = 17
PEAK_END_H = 21
_COOL = {"cooling", "overcool"}


def _out(r):
    v = r.get("daikin_outdoor_temp_f")
    return v if v is not None else r.get("wx_outdoor_temp_f")


def effectiveness(readings, tz_name, *, min_days_each=2, base_f=75.0,
                  min_peak_minutes=60.0):
    tz = ZoneInfo(tz_name)
    rows = sorted(readings, key=lambda r: r["ts"])
    days = {}
    for a, b in zip(rows, rows[1:]):
        la = a["ts"].astimezone(tz)
        if la.weekday() >= 5:                 # weekdays only (the example TOU peak is weekday)
            continue
        d = days.setdefault(la.date(), {"cool_min": 0.0, "out_sum": 0.0,
                                        "out_min": 0.0, "min_sum": 0.0,
                                        "sp_before": None, "sp_after": None})
        sp = a.get("cool_setpoint_f")
        if sp is not None:
            if la.hour == 16 and la.minute >= 45:      # just before the 17:00 coast
                d["sp_before"] = sp
            elif la.hour == 17 and la.minute <= 15:     # just after
                d["sp_after"] = sp
        dt = (b["ts"] - a["ts"]).total_seconds() / 60.0
        # Window membership by interval MIDPOINT: attributing by the start
        # timestamp smears up to one poll interval across the 17:00/21:00
        # edges every single day.
        lm = (a["ts"] + (b["ts"] - a["ts"]) / 2).astimezone(tz)
        if PEAK_START_H <= lm.hour < PEAK_END_H and 0 < dt <= 30:
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
        norm = cm / (avg_out - base_f) if (avg_out and avg_out > base_f) else None
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
