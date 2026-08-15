from dataclasses import dataclass
import requests


@dataclass(frozen=True)
class WeatherSnapshot:
    ok: bool
    outdoor_temp_f: float | None
    humidity: float | None
    dewpoint_f: float | None
    solar_wm2: float | None
    uv: float | None
    fc_high_f: float | None
    fc_low_f: float | None
    conditions: str | None
    aqi: float | None
    alert_count: int | None
    rain_today_in: float | None = None


_NULL = WeatherSnapshot(False, None, None, None, None, None, None, None, None, None, None)


def _num(v):
    """Coerce a JSON value to float, or None if absent/non-numeric. A feed that
    reports numbers as strings ("72") is tolerated; a bool or anything else
    becomes None (True must not read as 1.0)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(d: dict) -> WeatherSnapshot:
    """Turn a weather-feed JSON body into a snapshot. `ok` is NOT a constant:
    a reachable feed can still return 200 with an empty body, an error wrapper
    ({"error": ...}), or valid JSON after an upstream field rename — none of
    which carry a usable reading. Marking those healthy stored weather_ok=True
    with every field null, so a real outage read as a "cool spell" (0
    cooling-degree-days), silently corrupted the rainfall series, and blanked
    dew-point/AQI advice with no recorded reason. The snapshot counts as OK
    only if at least one core current-condition field parses to a real number."""
    if not isinstance(d, dict):
        return _NULL
    temp = _num(d.get("temp"))
    humidity = _num(d.get("humidity"))
    dewpoint = _num(d.get("dewPoint"))
    # Tradeoff: the "is this feed alive" signal is the current-condition trio.
    # A feed carrying ONLY forecast/rain/AQI (no temp/humidity/dewpoint) is
    # treated as down — acceptable because every supported feed reports current
    # conditions, and it's the only shape that reliably separates a real reading
    # from an empty/error/renamed body. Broadening the gate to rain/forecast
    # would let a temp-field rename slip back through as false-healthy.
    if temp is None and humidity is None and dewpoint is None:
        return _NULL
    alert = d.get("alertCount")
    return WeatherSnapshot(
        ok=True,
        outdoor_temp_f=temp,
        humidity=humidity,
        dewpoint_f=dewpoint,
        solar_wm2=_num(d.get("radiation")),
        uv=_num(d.get("uvIndex")),
        fc_high_f=_num(d.get("fcHigh")),
        fc_low_f=_num(d.get("fcLow")),
        conditions=d.get("conditions"),
        aqi=_num(d.get("aqi")),
        alert_count=int(alert) if isinstance(alert, (int, float)) and not isinstance(alert, bool) else None,
        # The station's own rain gauge: cumulative inches since local
        # midnight. The moisture case's rainfall series is built from this.
        rain_today_in=_num(d.get("rainToday")))


def fetch(url: str, fallback: str | None = None, timeout: int = 5) -> WeatherSnapshot:
    """Fetch the primary feed, then the fallback, returning the first snapshot
    that parses to a usable reading. A reachable-but-broken feed (HTTP error,
    non-JSON body, or a 200 that `parse` rejects as empty/wrong-shape) falls
    through to the fallback and then to _NULL — never a false-healthy snapshot.
    Only network/JSON errors are swallowed; a bug in `parse` still surfaces."""
    for u in [url, fallback]:
        if not u:
            continue
        try:
            r = requests.get(u, timeout=timeout)
        except requests.RequestException:
            continue
        if not r.ok:
            continue
        try:
            snap = parse(r.json())
        except ValueError:   # body wasn't JSON (includes JSONDecodeError)
            continue
        if snap.ok:
            return snap
    return _NULL
