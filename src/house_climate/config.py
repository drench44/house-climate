import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Alert keys the alert daemon reads with a hard subscript (not .get): a config
# missing any one of these made the alert thread throw and die silently, so no
# alert ever fired. Validated at load so a bad config fails LOUD at boot.
_REQUIRED_ALERT_KEYS = (
    "cooldown_minutes", "offline_missed_polls", "humidity_high_pct",
    "humidity_sustained_minutes", "setpoint_drift_f", "setpoint_drift_minutes",
    "short_cycles_threshold", "short_cycles_window_hours",
)

# Every alert key the engine can emit (web/alerts.py). alerts.push_suppress is
# validated against this so a typo fails at boot instead of silently letting
# the push through; tests/test_alert_delivery.py keeps it in sync with the
# engine.
ALERT_KEYS = (
    "offline", "humidity_high", "setpoint_drift", "short_cycling", "freeze",
    "crawl_saturated", "crawl_mold", "crawl_condensation", "crawl_sensor_offline",
    "filter_due", "air_quality", "weather_alert", "peak_surge",
    "equipment_unknown", "weather_feed_stale",
)

# Push channels make_sink understands. Anything else used to fall through to
# the no-op sink, so a misspelled channel meant alerts silently went nowhere.
ALERT_CHANNELS = ("noop", "ntfy", "webhook")


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class TouBand:
    name: str
    season: str
    days: str      # "all" | "weekday" | "weekend"
    start: time
    end: time
    rate: float

    def covers(self, t: time) -> bool:
        if self.start == self.end:
            return True   # all-day band (e.g. weekend 00:00-00:00)
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end   # wraps midnight


# ---- TOU holidays -----------------------------------------------------------
# Many time-of-use tariffs price a short list of holidays like a weekend. The
# list and the rule for a holiday that lands on a weekend differ by utility, so
# both are configuration (tou.holidays), never hardcoded. Each named rule is
# computed per year. Default: no holidays, which is how every config written
# before this existed behaves.
def _nth_weekday(year, month, weekday, n):
    """The n-th `weekday` (Mon=0) of a month."""
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    """The last `weekday` (Mon=0) of a month."""
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


# name -> (fixed_date, fn(year) -> date). Only FIXED-date holidays can land on a
# weekend, so only they are subject to the weekend-observance rule; the rest
# are defined as a weekday.
HOLIDAY_RULES = {
    "new_years_day": (True, lambda y: date(y, 1, 1)),
    "martin_luther_king_day": (False, lambda y: _nth_weekday(y, 1, 0, 3)),
    "presidents_day": (False, lambda y: _nth_weekday(y, 2, 0, 3)),
    "memorial_day": (False, lambda y: _last_weekday(y, 5, 0)),
    "juneteenth": (True, lambda y: date(y, 6, 19)),
    "independence_day": (True, lambda y: date(y, 7, 4)),
    "labor_day": (False, lambda y: _nth_weekday(y, 9, 0, 1)),
    "columbus_day": (False, lambda y: _nth_weekday(y, 10, 0, 2)),
    "veterans_day": (True, lambda y: date(y, 11, 11)),
    "thanksgiving_day": (False, lambda y: _nth_weekday(y, 11, 3, 4)),
    "day_after_thanksgiving": (False, lambda y: _nth_weekday(y, 11, 3, 4) + timedelta(days=1)),
    "christmas_day": (True, lambda y: date(y, 12, 25)),
}

# How a fixed-date holiday that falls on a weekend is observed:
#   "none"                          - it is not moved (it is already a weekend)
#   "sunday_to_monday"              - Sunday moves to the Monday after
#   "saturday_to_friday_sunday_to_monday"
#                                   - Saturday moves to the Friday before and
#                                     Sunday to the Monday after (the US federal
#                                     rule, and the one several utilities use)
HOLIDAY_OBSERVANCE = ("none", "sunday_to_monday", "saturday_to_friday_sunday_to_monday")


@lru_cache(maxsize=256)
def _holidays_in_year(rules, observed, year):
    """Every holiday date that falls in `year`, observance applied. A holiday
    of the NEXT year can be observed in this one (New Year's Day on a Saturday
    is observed on Friday 31 December), so both years' rules are evaluated."""
    out = set()
    for y in (year, year + 1):
        for name in rules:
            fixed, fn = HOLIDAY_RULES[name]
            d = fn(y)
            if fixed and d.weekday() == 5 and observed == "saturday_to_friday_sunday_to_monday":
                d -= timedelta(days=1)
            elif fixed and d.weekday() == 6 and observed != "none":
                d += timedelta(days=1)
            if d.year == year:
                out.add(d)
    return frozenset(out)


@dataclass(frozen=True)
class TouTable:
    summer_months: frozenset
    bands: tuple
    # Named HOLIDAY_RULES, their weekend observance, and any extra explicit
    # dates. A holiday is priced with the weekend bands.
    holiday_rules: tuple = ()
    holiday_observed: str = "none"
    holiday_dates: frozenset = frozenset()

    def season(self, month: int) -> str:
        return "summer" if month in self.summer_months else "winter"

    def is_holiday(self, d: date) -> bool:
        """True when local date `d` is a configured TOU holiday (after any
        weekend observance), and so priced like a weekend."""
        if d in self.holiday_dates:
            return True
        if not self.holiday_rules:
            return False
        return d in _holidays_in_year(self.holiday_rules, self.holiday_observed, d.year)

    def band_for(self, dt_local: datetime) -> tuple[str, float]:
        season = self.season(dt_local.month)
        weekend = dt_local.weekday() >= 5 or self.is_holiday(dt_local.date())
        t = dt_local.time()
        for b in self.bands:
            if b.season != season:
                continue
            if b.days == "weekday" and weekend:
                continue
            if b.days == "weekend" and not weekend:
                continue
            if b.covers(t):
                return b.name, b.rate
        raise ValueError(f"no TOU band covers {dt_local}")

    def next_transition(self, dt_local: datetime):
        """The next TOU band change at/after dt_local.

        Returns (next_band_name, boundary_datetime) — the band that begins at
        the boundary and its tz-aware datetime — or (None, None) if no change
        occurs within the scanned window (today plus the 8 days after it —
        day_offset 0..8 inclusive, so 9 calendar days total; e.g. a single
        flat all-day band never transitions). Generic: it scans this table's
        own band start/end times, so any utility works.
        """
        try:
            cur, _ = self.band_for(dt_local)
        except ValueError:
            cur = None
        # A transition can only happen at some band's start or end. Enumerate
        # those wall-clock boundaries across the next 8 local days (covers the
        # weekend gap and any weekly pattern), then take the first that lands
        # in a different band than now.
        candidates = set()
        for day_offset in range(0, 9):
            day = (dt_local + timedelta(days=day_offset)).date()
            for b in self.bands:
                for tm in (b.start, b.end):
                    cand = datetime.combine(day, tm, tzinfo=dt_local.tzinfo)
                    if cand > dt_local:
                        candidates.add(cand)
        for cand in sorted(candidates):
            try:
                band, _ = self.band_for(cand)
            except ValueError:
                continue
            if band != cur:
                return band, cand
        return None, None

    def peak_rate(self, season: str):
        """The highest rate among bands applicable to `season`, or None when
        that season has fewer than two distinct rates (a flat season has no
        meaningful 'peak')."""
        rates = {b.rate for b in self.bands if b.season == season}
        if len(rates) < 2:
            return None
        return max(rates)

    def is_peak(self, dt_local: datetime) -> bool:
        """True iff dt_local falls in an on-peak band — the highest-rate tier
        for its season. Fully generic: weekday/weekend, seasonal, and any tier
        count are handled by band_for; a flat (single-rate) season is never
        peak. This is the one on-peak-membership test the timed cost analytics
        (forecast, pre-cool) share, so none of them hardcode 17:00-21:00."""
        top = self.peak_rate(self.season(dt_local.month))
        if top is None:
            return False
        try:
            _, rate = self.band_for(dt_local)
        except ValueError:
            return False
        return rate >= top

    def day_has_peak(self, day: date, tzinfo) -> bool:
        """True iff any quarter-hour of local calendar `day` is on-peak. A
        weekend under a weekday-only peak, or a flat season, has none -- and
        must price and fit as having no peak exposure at all."""
        return any(self.is_peak(datetime.combine(day, time(q // 4, (q % 4) * 15),
                                                 tzinfo=tzinfo))
                   for q in range(96))

    def peak_windows(self, dt_local: datetime):
        """On-peak windows for dt_local's season as a list of (start, end,
        weekday_only), one per contiguous run of top-rate bands, sorted by start.
        Adjacent same-rate bands merge; a two-humped peak (e.g. a morning AND an
        evening peak at the same top rate — a solar-duck tariff) yields two
        windows rather than one collapsed envelope. Empty when the season is
        flat / has no peak. Derived from the rate table so any utility's shape
        works, not just the example's single weekday 17:00-21:00 window."""
        season = self.season(dt_local.month)
        top = self.peak_rate(season)
        if top is None:
            return []
        # The timed analytics operate on weekday/all-days windows; prefer those,
        # but fall back to whatever peak bands exist (e.g. a weekend-only peak).
        windowed = [b for b in self.bands
                    if b.season == season and b.rate == top and b.days in ("weekday", "all")]
        if not windowed:
            windowed = [b for b in self.bands if b.season == season and b.rate == top]
        if not windowed:
            return []
        merged = []
        for b in sorted(windowed, key=lambda b: (b.start, b.end)):
            if merged and b.start <= merged[-1][1]:          # contiguous/overlapping
                s, e, wd = merged[-1]
                merged[-1] = (s, max(e, b.end), wd and b.days == "weekday")
            else:
                merged.append((b.start, b.end, b.days == "weekday"))
        return merged

    def peak_window(self, dt_local: datetime):
        """The primary (earliest) on-peak window as (start, end, weekday_only),
        or None. For a multi-humped peak this is the first hump; single-window
        consumers (the retrospective pre-cool analysis) use it, while
        predict_peak_cost uses peak_windows() to price every hump."""
        wins = self.peak_windows(dt_local)
        return wins[0] if wins else None


@dataclass(frozen=True)
class Config:
    poll_interval_s: int
    timezone: str
    system_kw: float
    heat_kw: float
    short_cycle_minutes: int
    weather_url: str
    weather_url_fallback: str
    web_port: int
    tou: TouTable
    alerts: dict
    filter_reminder_hours: float
    setpoint_tolerance_f: float
    ecowitt: dict | None
    latitude: float | None
    longitude: float | None


@dataclass(frozen=True)
class Secrets:
    api_key: str
    integrator_token: str
    email: str
    db_dsn: str


def _validate_config(d: dict, table: "TouTable") -> None:
    """Fail LOUD at load for the misconfigurations that used to fail silently or
    per-request at runtime: a missing alert key (killed the alert thread), an
    unknown timezone (500'd every panel), or a TOU table with an uncovered
    minute (500'd cost/forecast). Raises ValueError with a specific reason."""
    alerts = d.get("alerts")
    if not isinstance(alerts, dict):
        raise ValueError("config 'alerts' must be an object")
    missing = [k for k in _REQUIRED_ALERT_KEYS if k not in alerts]
    if missing:
        raise ValueError(f"config 'alerts' is missing required keys: {', '.join(missing)}")
    # Every runtime, cost and coverage figure credits a reading with at most
    # 10 minutes (analytics.cost.MAX_GAP_S) and treats anything longer as
    # unobserved. A slower poller would leave every day "incomplete" and the
    # cost average, forecast and pre-cool analysis would wait forever.
    poll = d.get("poll_interval_s")
    if not isinstance(poll, (int, float)) or isinstance(poll, bool) or not 0 < poll <= 600:
        raise ValueError(f"config 'poll_interval_s' is {poll!r}; it must be between 1 and "
                         "600 seconds (readings further apart than 10 minutes are "
                         "treated as unobserved time)")
    channel = alerts.get("channel", "noop")
    if channel not in ALERT_CHANNELS:
        raise ValueError(f"config 'alerts.channel' must be one of "
                         f"{', '.join(ALERT_CHANNELS)}; got {channel!r}")
    if channel == "ntfy" and not (isinstance(alerts.get("ntfy_topic"), str)
                                  and alerts["ntfy_topic"].strip()):
        raise ValueError("config 'alerts.ntfy_topic' must be a non-empty string "
                         "when alerts.channel is 'ntfy'")
    suppress = alerts.get("push_suppress", [])
    if not isinstance(suppress, list) or not all(isinstance(k, str) for k in suppress):
        raise ValueError("config 'alerts.push_suppress' must be a list of alert keys")
    unknown = [k for k in suppress if k not in ALERT_KEYS]
    if unknown:
        raise ValueError(f"config 'alerts.push_suppress' has unknown alert keys: "
                         f"{', '.join(unknown)} (known: {', '.join(ALERT_KEYS)})")
    if "crawl_offline_minutes" in alerts:
        v = alerts["crawl_offline_minutes"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError("config 'alerts.crawl_offline_minutes' must be a "
                             f"positive number of minutes; got {v!r}")
    try:
        ZoneInfo(d["timezone"])
    except (ZoneInfoNotFoundError, ValueError, KeyError) as e:
        raise ValueError(f"config 'timezone' is invalid: {d.get('timezone')!r} ({e})")
    # TOU must cover every wall-clock minute of every month, weekday and
    # weekend, that can actually occur — so band_for never raises at runtime.
    # 2027 has a Wednesday and a Saturday in every month; probing both day-types
    # every 15 minutes (catching :15/:45 boundaries, not just :00/:30) exercises
    # weekday/weekend and season selection.
    for m in range(1, 13):
        for target_wd in (2, 5):          # a Wednesday and a Saturday
            day = 1
            while date(2027, m, day).weekday() != target_wd:
                day += 1
            for q in range(96):           # 96 quarter-hours in a day
                probe = datetime(2027, m, day, (q * 15) // 60, (q * 15) % 60)
                try:
                    table.band_for(probe)
                except ValueError:
                    raise ValueError(
                        f"TOU table has no band covering {probe:%A} {probe:%H:%M} "
                        f"in month {m}; every minute of every day must be covered")


def _parse_holidays(h) -> tuple:
    """tou.holidays -> (rules, observed, dates). Absent means no holidays.
    Raises ValueError naming the bad entry, so a typo fails at boot instead of
    silently billing a holiday at peak.

        "holidays": {
          "rules": ["new_years_day", "memorial_day", "independence_day",
                    "labor_day", "thanksgiving_day", "christmas_day"],
          "observed": "saturday_to_friday_sunday_to_monday",
          "dates": ["2026-12-24"]
        }
    """
    if h is None:
        return (), "none", frozenset()
    if not isinstance(h, dict):
        raise ValueError("config 'tou.holidays' must be an object")
    unknown_keys = set(h) - {"rules", "observed", "dates"}
    if unknown_keys:
        raise ValueError(f"config 'tou.holidays' has unknown keys: {', '.join(sorted(unknown_keys))}")
    rules = h.get("rules", [])
    if not isinstance(rules, list) or not all(isinstance(r, str) for r in rules):
        raise ValueError("config 'tou.holidays.rules' must be a list of rule names")
    bad = [r for r in rules if r not in HOLIDAY_RULES]
    if bad:
        raise ValueError(f"config 'tou.holidays.rules' has unknown rules: {', '.join(bad)} "
                         f"(known: {', '.join(HOLIDAY_RULES)})")
    # Required whenever rules are listed: whether a Saturday Independence Day
    # moves to Friday is a property of the tariff, and guessing it either way
    # misprices a whole weekday.
    observed = h.get("observed")
    if rules and observed is None:
        raise ValueError("config 'tou.holidays.observed' is required when rules are listed "
                         f"(one of: {', '.join(HOLIDAY_OBSERVANCE)})")
    observed = observed or "none"
    if observed not in HOLIDAY_OBSERVANCE:
        raise ValueError(f"config 'tou.holidays.observed' is {observed!r}; "
                         f"must be one of: {', '.join(HOLIDAY_OBSERVANCE)}")
    raw_dates = h.get("dates", [])
    if not isinstance(raw_dates, list):
        raise ValueError("config 'tou.holidays.dates' must be a list of YYYY-MM-DD dates")
    dates = set()
    for s in raw_dates:
        try:
            dates.add(date.fromisoformat(s))
        except (TypeError, ValueError):
            raise ValueError(f"config 'tou.holidays.dates' has an invalid date: {s!r} "
                             "(expected YYYY-MM-DD)")
    return tuple(dict.fromkeys(rules)), observed, frozenset(dates)


def load_config(path: str) -> Config:
    with open(path) as f:
        d = json.load(f)
    tou = d["tou"]
    bands = tuple(
        TouBand(b["name"], b["season"], b.get("days", "all"),
                _parse_hhmm(b["start"]), _parse_hhmm(b["end"]), float(b["rate"]))
        for b in tou["bands"])
    rules, observed, dates = _parse_holidays(tou.get("holidays"))
    table = TouTable(frozenset(tou["seasons"]["summer"]["months"]), bands,
                     holiday_rules=rules, holiday_observed=observed,
                     holiday_dates=dates)
    _validate_config(d, table)
    return Config(
        poll_interval_s=int(d["poll_interval_s"]),
        timezone=d["timezone"],
        system_kw=float(d["system_kw"]),
        heat_kw=float(d.get("heat_kw", d["system_kw"])),
        short_cycle_minutes=int(d["short_cycle_minutes"]),
        weather_url=d["weather_url"],
        weather_url_fallback=d["weather_url_fallback"],
        web_port=int(d["web_port"]),
        tou=table,
        alerts=d["alerts"],
        filter_reminder_hours=float(d.get("filter_reminder_hours", 300.0)),
        setpoint_tolerance_f=float(d.get("setpoint_tolerance_f", 1.0)),
        ecowitt=d.get("ecowitt"),
        # For the Open-Meteo rainfall backfill (days before the station gauge
        # was being captured). Absent -> backfill silently skipped.
        latitude=float(d["latitude"]) if d.get("latitude") is not None else None,
        longitude=float(d["longitude"]) if d.get("longitude") is not None else None)


def load_secrets(env: Mapping) -> Secrets:
    return Secrets(
        api_key=env["DAIKIN_API_KEY"],
        integrator_token=env["DAIKIN_INTEGRATOR_TOKEN"],
        email=env["DAIKIN_EMAIL"],
        db_dsn=env["DB_DSN"])
