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


def parse(d: dict) -> WeatherSnapshot:
    return WeatherSnapshot(
        ok=True,
        outdoor_temp_f=d.get("temp"),
        humidity=d.get("humidity"),
        dewpoint_f=d.get("dewPoint"),
        solar_wm2=d.get("radiation"),
        uv=d.get("uvIndex"),
        fc_high_f=d.get("fcHigh"),
        fc_low_f=d.get("fcLow"),
        conditions=d.get("conditions"),
        aqi=d.get("aqi"),
        alert_count=d.get("alertCount"),
        # The station's own rain gauge: cumulative inches since local
        # midnight. The moisture case's rainfall series is built from this.
        rain_today_in=d.get("rainToday"))


def fetch(url: str, fallback: str | None = None, timeout: int = 5) -> WeatherSnapshot:
    for u in [url, fallback]:
        if not u:
            continue
        try:
            r = requests.get(u, timeout=timeout)
            if r.ok:
                return parse(r.json())
        except Exception:
            continue
    return _NULL
