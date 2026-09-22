"""Local-network client for an Ecowitt gateway (GW1100B).

Polls the gateway's built-in HTTP API (/get_livedata_info) on the LAN — no
cloud, no WS View app. The gateway is deliberately blocked from the internet
(Firewalla), so this is the only path the sensor data takes. WH31 room sensors
appear under `ch_aisle`, keyed by their channel switch (1-8).
"""
import re
import requests


def _num(v):
    """Parse a value like '78.6', '40%', or '0' to float; None if unparseable.
    Ecowitt returns everything as strings, humidity with a trailing '%'."""
    if v is None:
        return None
    s = str(v).strip().rstrip("%").strip()
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _temp_f(v, unit):
    """Parse a temperature and return it in Fahrenheit. The gateway tags each
    temperature with its display unit; a gateway switched to metric reports
    Celsius, which stored as-is would silently read as a cold snap. A missing
    unit is treated as F (the gateway default, and older payloads without the
    tag). Any other unit raises, so poll_ecowitt records a poll_error instead
    of storing numbers in an unknown scale."""
    t = _num(v)
    u = str(unit).strip().upper().lstrip("\u00b0") if unit is not None else "F"
    if u in ("F", "\u2109"):
        return t
    if u in ("C", "\u2103"):
        return None if t is None else t * 9 / 5 + 32
    raise ValueError(f"unsupported Ecowitt temperature unit {unit!r}")


def fetch_livedata(gateway_url, timeout=6) -> dict:
    r = requests.get(f"{gateway_url.rstrip('/')}/get_livedata_info", timeout=timeout)
    r.raise_for_status()
    return r.json()


def fetch_sensors_info(gateway_url, timeout=6) -> list:
    """The gateway's per-sensor detail (battery flag + signal), paginated. Best
    effort: returns [] on any failure so it never blocks the main livedata poll."""
    out = []
    for page in (1, 2):
        try:
            r = requests.get(f"{gateway_url.rstrip('/')}/get_sensors_info",
                             params={"page": page}, timeout=timeout)
            r.raise_for_status()
            d = r.json()
            if isinstance(d, list):
                out += d
        except Exception:
            break
    return out


def signal_by_sensor_id(sensors_info: list) -> dict:
    """Map our sensor_id -> radio signal (0-4 bars) for the registered WH31
    channels and the outdoor WH32. Sensors not actively reporting (signal '--')
    are skipped so a phantom registration doesn't override a live one."""
    out = {}
    for s in sensors_info:
        if str(s.get("id")) in ("None", "FFFFFFFF", "00000000"):
            continue
        sig = _num(s.get("signal"))
        if sig is None:
            continue
        img = str(s.get("img", "")).lower()
        if img == "wh31":
            m = re.search(r"CH(\d+)", str(s.get("name", "")))
            if m:
                out[f"ecowitt_ch{m.group(1)}"] = int(sig)
        elif img in ("wh26", "wh32"):
            out["ecowitt_outdoor"] = int(sig)
    return out


# The single outdoor T&H sensor (WH32) reports in `common_list`, keyed by
# Ecowitt field id: 0x02 = outdoor temp, 0x07 = outdoor humidity, 0x03 =
# outdoor dew point. The GW1100 puts the sensor's battery flag on the 0x03
# entry, not on 0x02/0x07, so all three are this sensor's own entries.
_OUTDOOR_TEMP_ID = "0x02"
_OUTDOOR_HUM_ID = "0x07"
_OUTDOOR_DEWPOINT_ID = "0x03"
_OUTDOOR_IDS = (_OUTDOOR_TEMP_ID, _OUTDOOR_HUM_ID, _OUTDOOR_DEWPOINT_ID)


def parse_outdoor(data: dict, name: str):
    """Extract the gateway's single outdoor T&H sensor (WH32), if one is
    reporting. Returns a reading dict or None. Battery is a low/ok flag."""
    temp = hum = None
    battery_low = False
    for c in data.get("common_list", []):
        cid = str(c.get("id"))
        if cid not in _OUTDOOR_IDS:
            continue   # battery must come from THIS sensor's entries only —
                       # any other common_list accessory reporting battery>=1
                       # would flag the crawl probe low forever
        if cid == _OUTDOOR_TEMP_ID:
            temp = _temp_f(c.get("val"), c.get("unit"))
        elif cid == _OUTDOOR_HUM_ID:
            hum = _num(c.get("val"))
        batt = _num(c.get("battery"))
        if batt is not None and batt >= 1:
            battery_low = True
    if temp is None and hum is None:
        return None
    return {"sensor_id": "ecowitt_outdoor", "name": name,
            "temp_f": temp, "humidity": hum, "battery_low": battery_low}


def parse_channels(data: dict, channels: dict) -> list[dict]:
    """Extract readings for the configured channels from a livedata payload.

    `channels` maps channel-number string -> room name, e.g. {"8": "Upstairs"}.
    Only channels present in BOTH the config and the payload are returned, so a
    not-yet-installed sensor simply doesn't appear. Battery is "0" (ok) / "1"
    (low) for WH31s.
    """
    out = []
    for s in data.get("ch_aisle", []):
        ch = str(s.get("channel"))
        if ch not in channels:
            continue
        batt = _num(s.get("battery"))
        out.append({
            "channel": ch,
            "sensor_id": f"ecowitt_ch{ch}",
            "name": channels[ch],
            "temp_f": _temp_f(s.get("temp"), s.get("unit")),
            "humidity": _num(s.get("humidity")),
            "battery_low": batt is not None and batt >= 1,
        })
    return out
