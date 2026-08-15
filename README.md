# house-climate

Self-hosted **thermostat analytics and home climate dashboard**. Polls your
thermostat every few minutes, stores every reading forever in TimescaleDB, and
turns that history into things the thermostat itself will never tell you:
runtime and cycling analysis, a time-of-use electricity cost estimate against
your utility's real rates, weather-correlated efficiency metrics, indoor air
quality, per-room temperatures from cheap wireless sensors, humidity and
moisture analytics, filter-life tracking, and push alerts when something's off.

Runs on any always-on Linux box with Docker. No cloud accounts beyond your
thermostat vendor's own API, no telemetry, LAN-only.

![house-climate dashboard](docs/dashboard.png)

*The wall dashboard, shown with synthetic sample data.*

**First-class thermostat support: Daikin One+** (via Daikin's official Open
API). The poller is a thin adapter (`src/house_climate/daikin.py`, ~one file):
anything that can report indoor temp/humidity, setpoints, mode, and equipment
status can implement the same surface — the storage, analytics, dashboard,
and alerting underneath are thermostat-agnostic.

## Why this exists

Most smart thermostats store little or no history and report no energy use —
you get a pretty app and no data. This stack *creates* the history by polling,
then does the analysis your utility bill wishes it could: what did the 5pm–9pm
peak window actually cost you? Is the system short-cycling? How does runtime
track outdoor temperature? Did closing the blinds help?

## Architecture & trust model

```
poller ──► Daikin Open API        (cloud, outbound-only, every 3 min)
   │   ──► weather wx.json        (optional local weather service)
   │   ──► Ecowitt gateway        (optional local sensors, LAN pull)
   ▼
TimescaleDB (every reading, forever; continuous aggregates)
   ▲
web ──► dashboard + JSON API + alerts on :8090   (LAN-only, no auth)
```

- **No auth, LAN-only — on purpose.** Bind to a LAN interface via
  `CLIMATE_BIND` in `.env` and never port-forward it.
- **Secrets live in `.env` only** (git-ignored; `.env.example` is the
  template). `config.json` is non-secret tuning.
- **Fails soft.** No weather source? No sensors? The dashboard renders with
  what it has.

## Quick start

```bash
git clone https://github.com/drench44/house-climate.git && cd house-climate
cp config.example.json config.json   # edit: rates, sizing, location (below)
cp .env.example .env                 # edit: Daikin credentials (below)
docker compose up -d --build
curl -s http://<your-server>:8090/health
```

By default the dashboard binds to loopback (`127.0.0.1`) — reachable only from
the server itself. To serve the wall display to your LAN, set `CLIMATE_BIND` in
`.env` to the server's LAN IP. `config.json` is copied into the image at build
time, so re-run `docker compose up -d --build` after editing it.

Dashboard: `http://<your-server>:8090/` — a wall-friendly night design with a
compact `square.html` view for small kiosk screens, plus `moisture.html` for
the humidity/moisture deep-dive.

## The three Daikin credentials

All from the Daikin **SkyportHome** phone app, on the account tied to the
thermostat:

1. **Integrator API key** — enable the developer menu in SkyportHome, request
   developer/API access; this issues the `apiKey`.
2. **Integrator token** — SkyportCare → Home Integration → Get Integration
   Token.
3. **Account email** — the SkyportHome account email. **Case-sensitive.**

Put them in `.env`. The cloud read is outbound-only — no ports opened, no
firewall changes, works even if the thermostat sits on an isolated IoT VLAN.

## config.json: make the numbers yours

- **`tou`** — your utility's time-of-use rate table. The example ships a
  generic 3-tier weekday shape (`peak` / `midpeak` / `offpeak` + weekend
  off-peak); replace the windows and `rate` values with your utility's
  published schedule. Flat-rate plans: one band, `00:00`–`00:00`, every day.
  Seasonal plans: split the `seasons` months and add per-season bands.
- **`system_kw`** — what your AC actually draws when cooling. The naive
  estimate is `tons × 1.2 kW/ton`; inverter systems draw meaningfully less,
  so check a real bill or an energy monitor if you have one. **`heat_kw`** is
  used while heating — for gas furnaces that's just the blower (~0.5 kW).
- **`latitude`/`longitude`** — your rough location, for sun/weather math.
- **`filter_reminder_hours`** — blower-hours between filter changes.
- **`alerts`** — thresholds for humidity, setpoint drift, short-cycling,
  offline, peak-hour surges, and AQI. Set `channel` to `"ntfy"` with your
  own topic on [ntfy.sh](https://ntfy.sh) (free push to your phone, no app
  account), or leave `"noop"`.

## Optional: per-room sensors (the hardware we run)

Any **Ecowitt** gateway + sensors give you per-room temperature/humidity for
very little money, fully local:

- **Ecowitt GW1100** Wi-Fi gateway (~$30) — the poller pulls it over the LAN;
  you can firewall it from the internet entirely and it keeps working.
- **Ecowitt WN31/WH31** channel sensors (~$10–15 each) — one per room; a
  crawl-space or attic sensor is the sleeper hit for moisture analytics.

Set `ecowitt.enabled: true`, point `gateway_url` at the gateway's IP, and map
channel numbers to room names. Everything sensor-driven on the dashboard
(rooms panel, humidity/moisture analytics, sensor-vs-thermostat deltas)
lights up automatically.

## Optional: weather

Point `weather_url` at anything serving a `wx.json` snapshot (outdoor temp,
solar, AQI, and friends — see `tests/fixtures/wx.json` for the full shape).
With one configured, the poller snapshots outdoor conditions alongside every
reading, unlocking the weather-correlated analytics (runtime vs. outdoor
temp, solar gain, AQI). We feed it from a self-hosted almanac dashboard
built on
[**WeatherFlow_PiConsole**](https://github.com/peted-davis/WeatherFlow_PiConsole)
with a small Open-Meteo adapter behind it. It's optional — everything else
works without a weather source.

## Peak-cost guidance strip

The main dashboard (not the compact `square.html` kiosk view) shows a live
strip above the cost rail telling you whether power is on-peak, off-peak, or
about to flip, with a countdown when a peak window is approaching. It's
driven entirely by your `tou.bands` table: the engine sorts your table's
distinct `rate` values and classifies whichever band is active right now as
`off`/`mid`/`peak` — or `flat` if every band shares one rate. There's no
hardcoded assumption about band names, hours, or tier count, so it works
whether your utility publishes two tiers, five, or one flat rate.

## Outdoor AQI

The weather feed's own `wx.json` can carry a `wx_aqi` field (see *Optional:
weather* above), but outdoor AQI often lags or goes stale on a slow-polling
feed. The engine also accepts a live push:

```
POST /api/ha/air
{ "outdoor_aqi": <0-1000 or null>, "<room>": <pm2.5>, ... }
```

`outdoor_aqi` is an optional top-level field on the same endpoint that
carries per-room indoor PM2.5 (e.g. from smart purifiers); post whichever
fields you have. Our reference setup is Home Assistant's built-in AirNow
integration (`sensor.airnow_air_quality_index`) pushed here by an automation, but any AQI
sensor or script that can `POST` a number works the same way.

The dashboard prefers a fresh pushed value over `wx_aqi` — fresh meaning
received within the last 30 minutes; past that it falls back to the weather
feed automatically. `alerts.aqi_unhealthy` in `config.json` (101 by
default — the EPA's "Unhealthy for Sensitive Groups" cutoff) is served on
`/api/humidity` and read by the dashboard itself, so once the effective AQI
crosses it, the small AQI indicator escalates into a full smoke banner —
change the config value and the on-screen threshold moves with it, no code
edit required.

## Companion projects

- [**family-hub**](https://github.com/drench44/family-hub) — a family wall
  display (chores, calendars, cameras) that embeds this dashboard as a live
  panel.

## Backup

TimescaleDB holds your entire climate history. `backup/` has a nightly
`pg_dump` script + systemd units (fail-loud, atomic, keeps a rolling set):
edit the paths in the `.service` file, install, and
`systemctl enable --now house-climate-backup.timer`.

A failed backup exits non-zero but nothing watches that on its own — so also
install `house-climate-backup-failure.service` (wired via `OnFailure=`) and
point `HC_NTFY_URL` at your ntfy topic, so a silently-stopped backup pushes an
alert instead of just sitting in the journal. The restore procedure (Timescale
needs its pre/post-restore wrappers) is documented at the top of
`backup/house-climate-backup.sh` — read it *before* you need it.

## Tests

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python3 -m pytest tests -q
```

DB-backed tests skip unless `TEST_DB_DSN` points at a running Postgres (the
compose `db` service on `postgresql://climate:climate@localhost:5433/climate`
works). The JS helper tests need Node ≥ 20.

## License

MIT.
