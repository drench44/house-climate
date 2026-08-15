import logging
import time
from dataclasses import dataclass
import requests

log = logging.getLogger("house_climate.daikin")

DEFAULT_BASE = "https://integrator-api.daikinskyport.com"   # Daikin One Open API host

# Daikin enums, confirmed against the official Open API docs + a live ONETOUCH device.
# equipmentStatus: 1 cool, 2 overcool-for-dehum, 3 heat, 4 fan, 5 idle.
_EQUIP = {1: "cooling", 2: "overcool", 3: "heating", 4: "fan", 5: "idle"}
# mode: 0 off, 1 heat, 2 cool, 3 auto, 4 emergency heat.
_MODE = {0: "off", 1: "heat", 2: "cool", 3: "auto", 4: "emheat"}

# A Daikin firmware update could introduce an equipmentStatus/mode value not in
# the confirmed sets above. Mapping it to "unknown" is safe (it just isn't in
# runtime._COOL/_HEAT/_FAN or cost._RUNNING), but that silently counts a
# genuinely-running system as idle, deflating runtime and cost with no signal.
# Warn once per unseen value so an API drift shows up in the logs instead.
_seen_unknown = set()


class DaikinError(Exception): ...
class RateLimited(DaikinError): ...


def _map_enum(table: dict, raw, label: str) -> str:
    if raw in table:
        return table[raw]
    if raw is not None and (label, raw) not in _seen_unknown:
        _seen_unknown.add((label, raw))
        log.warning("unmapped Daikin %s value %r -> treated as 'unknown' "
                    "(counted as idle in runtime/cost); update _EQUIP/_MODE if "
                    "Daikin added a code", label, raw)
    return "unknown"


def _c_to_f(c):
    return None if c is None else c * 9 / 5 + 32


def _humidity(v):
    """Humidity carries no unit conversion, so unlike the temps it would
    otherwise store a raw string on type-drift. Coerce here: a non-numeric,
    non-null value raises (-> read_device wraps it as DaikinError -> recorded
    poll_error) instead of a string silently entering the DB."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"non-numeric humidity: {v!r}")
    return float(v)   # str/int/float coerce; other types raise TypeError


@dataclass(frozen=True)
class DeviceState:
    indoor_temp_f: float | None
    indoor_humidity: float | None
    heat_setpoint_f: float | None
    cool_setpoint_f: float | None
    equipment_status: str | None
    mode: str | None
    outdoor_temp_f: float | None
    outdoor_humidity: float | None


def _parse_device(d: dict) -> DeviceState:
    return DeviceState(
        indoor_temp_f=_c_to_f(d.get("tempIndoor")),
        indoor_humidity=_humidity(d.get("humIndoor")),
        heat_setpoint_f=_c_to_f(d.get("heatSetpoint")),
        cool_setpoint_f=_c_to_f(d.get("coolSetpoint")),
        equipment_status=_map_enum(_EQUIP, d.get("equipmentStatus"), "equipmentStatus"),
        mode=_map_enum(_MODE, d.get("mode"), "mode"),
        outdoor_temp_f=_c_to_f(d.get("tempOutdoor")),
        outdoor_humidity=_humidity(d.get("humOutdoor")))


class DaikinClient:
    def __init__(self, api_key, token, email, base_url=DEFAULT_BASE, timeout=15):
        self._api_key = api_key
        self._token = token
        self._email = email
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._access = None
        self._exp = 0.0

    def access_token(self) -> str:
        if self._access and time.time() < self._exp - 30:
            return self._access
        r = requests.post(f"{self._base}/v1/token",
                          headers={"x-api-key": self._api_key,
                                   "Content-Type": "application/json"},
                          json={"email": self._email, "integratorToken": self._token},
                          timeout=self._timeout)
        self._raise(r)
        try:
            body = r.json()
            self._access = body["accessToken"]
            expires_in = int(body.get("accessTokenExpiresIn", 3600))
        except (ValueError, KeyError, TypeError) as e:
            # A 200 whose body isn't the expected token shape (Daikin renamed a
            # field, returned HTML, etc.) is an upstream contract break, not a
            # bug here. Raise DaikinError so poll_once records it in poll_errors
            # instead of a bare KeyError escaping to the generic loop handler
            # (which logs but writes nothing, hiding the cause behind a silent
            # data gap).
            raise DaikinError(f"unexpected token response shape: {e}") from e
        self._exp = time.time() + expires_in
        return self._access

    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token()}",
                "x-api-key": self._api_key}

    def list_devices(self) -> list[dict]:
        r = requests.get(f"{self._base}/v1/devices/", headers=self._headers(),
                         timeout=self._timeout)
        self._raise(r)
        # Response is a list of locations, each with a nested "devices" array.
        try:
            return [{"id": d["id"], "name": d.get("name"), "model": d.get("model")}
                    for loc in r.json() for d in loc.get("devices", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise DaikinError(f"unexpected devices response shape: {e}") from e

    def read_device(self, device_id: str) -> DeviceState:
        r = requests.get(f"{self._base}/v1/devices/{device_id}",
                         headers=self._headers(), timeout=self._timeout)
        self._raise(r)
        try:
            return _parse_device(r.json())
        except (ValueError, TypeError, AttributeError) as e:
            raise DaikinError(f"unexpected device response shape: {e}") from e

    @staticmethod
    def _raise(r):
        if r.status_code == 429:
            raise RateLimited("Daikin rate limit (429)")
        if not r.ok:
            raise DaikinError(f"Daikin HTTP {r.status_code}: {r.text[:200]}")
