import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Mapping


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


@dataclass(frozen=True)
class TouTable:
    summer_months: frozenset
    bands: tuple

    def _season(self, month: int) -> str:
        return "summer" if month in self.summer_months else "winter"

    def band_for(self, dt_local: datetime) -> tuple[str, float]:
        season = self._season(dt_local.month)
        weekend = dt_local.weekday() >= 5
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


def load_config(path: str) -> Config:
    with open(path) as f:
        d = json.load(f)
    tou = d["tou"]
    bands = tuple(
        TouBand(b["name"], b["season"], b.get("days", "all"),
                _parse_hhmm(b["start"]), _parse_hhmm(b["end"]), float(b["rate"]))
        for b in tou["bands"])
    table = TouTable(frozenset(tou["seasons"]["summer"]["months"]), bands)
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
