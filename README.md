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

### Health: `/health` and `/health/full`

`/health` is the container healthcheck: 503 only when the database is
unreachable, with data ages as information, so an outage elsewhere never
marks `web` unhealthy.

`/health/full` answers "does it actually work?" and is what a deploy should
gate on. It always returns 200 with a report:

- `poller`: the heartbeat's age, and the commit and start time the poller
  writes into it. When the image has a deploy record, the poller must report
  the same commit and build as `web` (`status: other_commit` or `other_build`
  is the old container still ticking; the build time tells two deploys of
  one commit apart).
- `sources`: `thermostat`, `rooms` (Ecowitt) and `weather`, each with the
  newest reading's `data_ts`. `ok` needs it recent AND written after the
  running poller started, so a reading the previous container wrote never
  counts. Each room sensor that has ever reported is judged on its own
  (`stale_items`), and a Daikin outage since the poller started reads
  `upstream_down`.
- `alerts`: the alert loop in this process evaluated recently.
- `settings`: the Daikin credentials, and `ALERT_WEBHOOK_URL` when alerts go
  to a webhook.
- `config`: the sha256 of the `config.json` baked into the image, and whether
  it is the one the deploy recorded (`matches_deploy`).
- `deploy`: `src/house_climate/build_info.json` as a deploy wrote it
  (`engine_commit`, `overlay_commit`, `config_sha256`, `built_at`), or null.
  Never commit that file; it is git-ignored.
- `status` is `ok` only when nothing above failed; `problems` names each
  failure. `notes` (the backup heartbeat) is information only.

```bash
curl -s http://<your-server>:8090/health/full | jq '{status, problems}'
```

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
  **Holidays** (optional, off by default): many tariffs price a short list of
  holidays like a weekend. Add `tou.holidays` and those days use your
  weekend bands:

  ```json
  "holidays": {
    "rules": ["new_years_day", "memorial_day", "independence_day",
              "labor_day", "thanksgiving_day", "christmas_day"],
    "observed": "saturday_to_friday_sunday_to_monday",
    "dates": ["2026-12-24"]
  }
  ```

  `rules` are computed for every year. Available: `new_years_day`,
  `martin_luther_king_day`, `presidents_day`, `memorial_day`, `juneteenth`,
  `independence_day`, `labor_day`, `columbus_day`, `veterans_day`,
  `thanksgiving_day`, `day_after_thanksgiving`, `christmas_day`. `observed`
  (required when `rules` is set) says what happens when a fixed-date holiday
  lands on a weekend: `none`, `sunday_to_monday`, or
  `saturday_to_friday_sunday_to_monday`. `dates` adds one-off days. Copy the
  list and the weekend rule from your utility's tariff sheet; they differ
  between utilities.
- **`system_kw`** — what your AC actually draws when cooling. The naive
  estimate is `tons × 1.2 kW/ton`; inverter systems draw meaningfully less,
  so check a real bill or an energy monitor if you have one. **`heat_kw`** is
  used while heating — for gas furnaces that's just the blower (~0.5 kW).
- **`latitude`/`longitude`** — your rough location, for sun/weather math.
- **`filter_reminder_hours`** / **`filter_reminder_months`** — when the
  filter is due: after that many blower hours, after that many calendar
  months since the last logged change, or whichever comes first if you set
  both. Blower hours suit thin 1" filters; a 4-5" media cabinet is usually
  rated in months (often 6-12). Set the one you do not use to `null`.
- **`alerts`** — thresholds for humidity, setpoint drift, short-cycling,
  offline, peak-hour surges, AQI, and the crawl-space probe. The crawl gets
  three tiers: sustained RH over `crawl_mold_pct` (mold watch, 75%), sustained
  RH over `crawl_saturated_pct` (near-saturation escalation, 90% — suppresses
  the mold alert so it doesn't double-notify), and sustained air-to-dew-point
  spread under `crawl_condensation_spread_f` (3°F — liquid water on joists and
  ducts, independent of the RH number). Those crawl checks only run on recent
  probe data: if the probe goes quiet for `crawl_offline_minutes` (default
  45) you get a `crawl_sensor_offline` alert instead of alerts built on old
  readings.
- **Where alerts go** (`alerts.channel`):
  - `"ntfy"` with your own `ntfy_topic` on [ntfy.sh](https://ntfy.sh) (free
    push to your phone, no app account).
  - `"webhook"` POSTs each alert as JSON,
    `{"key": ..., "severity": ..., "title": ..., "message": ...}`, to the URL
    in the `ALERT_WEBHOOK_URL` environment variable (put it in `.env`, never
    in `config.json`: a webhook URL is a secret). Use it to hand alerts to,
    for example, a Home Assistant webhook automation. A non-2xx reply counts
    as not delivered and is retried next cycle. If the channel is `"webhook"`
    and the variable is missing, the web service refuses to start and says
    why. Note that Home Assistant answers a webhook call with success even
    when no automation uses that webhook ID, so check once by hand that an
    alert actually reaches you.
  - `"noop"` just logs them.
  - `alerts.push_suppress` is a list of alert keys (for example
    `["air_quality"]`) that are still checked and still shown on the wall but
    never pushed, for alerts another system already sends you. Unknown keys
    are rejected at startup.
  - A pushed alert is not re-sent within `cooldown_minutes`, and that holds
    across restarts and deploys. The cooldown only quiets a condition that is
    still going:
    - once an alert has been checked and found clear for
      `rearm_after_clear_minutes` (default 60), its next occurrence pushes at
      once. An alert that is merely unchecked because its data went stale (a
      thermostat or weather-feed outage) has not cleared and stays quiet;
    - a condition that gets WORSE pushes through the cooldown: a colder
      freeze band (frost, freeze, hard freeze at 20°F and below, which is
      critical) or more active NWS alerts. Getting better (warming, an NWS
      alert expiring) does not push.
  - With `"webhook"`, a `{"key": "heartbeat", ...}` POST goes out every
    `relay_heartbeat_hours` (default 24; `0` turns it off). It is not an
    alert: have the receiver record it silently and warn you when it stops,
    since that is the only way to know the push path still works end to end.
    **If you already relay webhook alerts to a phone, make the receiver skip
    `key == "heartbeat"` before upgrading**, or you will get a daily
    "heartbeat" push.

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
temp, solar gain, AQI). Because every snapshot is kept, outdoor temperature,
humidity, and dew point are queryable over time (`/api/outdoor`, ranges
`24h`/`7d`/`30d`) with a per-field coverage figure that flags feed gaps — and
the crawl-space view compares the crawl against that same outdoor dew point
to decide whether venting would dry the crawl or wet it. We feed it from a
self-hosted almanac dashboard built on
[**WeatherFlow_PiConsole**](https://github.com/peted-davis/WeatherFlow_PiConsole)
with a small Open-Meteo adapter behind it. It's optional — everything else
works without a weather source.

Rainfall comes from the feed's `rainToday` (inches since local midnight). If
the feed also sends `rainSource`, only `"gauge"` counts as a real gauge
reading; `"partial"` or `"model"` (a forecast estimate) is kept only as a
placeholder until Open-Meteo's total for that day replaces it. A feed with no
`rainSource` is treated as a gauge. If the thermostat is unreachable, the
poller still stores that tick's weather, with the thermostat fields left
empty.

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

Each nightly dump carries a row-count file, and the first dump of every month
is also kept in `monthly/` for two years, so damage you don't notice for weeks
can still be undone.

**Verify the restore, don't assume it.** A backup you've never restored is a
guess. Install `house-climate-backup-verify.timer` too: once a week it runs
`house-climate-backup.sh --verify-dump latest`, which restores your newest real
dump into a throwaway container and checks every table and the newest reading. `house-climate-backup.sh --restore-selftest` does a real dump → restore
into a throwaway database (using the pre/post-restore wrappers) → verify → drop,
so you find a broken restore path on your schedule, not during an outage. CI
runs this on every push/PR. **Store dumps off-box:** point `HC_BACKUP_DIR` at a
NAS or second disk (the default `/var/backups/house-climate` shares the disk
with the DB volume, so one disk failure loses both), set `HC_REQUIRE_MOUNTPOINT`
so a missing mount fails loud, and **encrypt** off-box dumps — they encode your
household's occupancy patterns.

See `docs/backup-and-deploy.md` for the backup model and the pre-deploy gate
(`backup/require-fresh-backup.sh`) that refuses to deploy without a fresh,
verified snapshot.

## Tests

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python3 -m pytest tests -q
```

DB-backed tests skip unless `TEST_DB_DSN` points at a running Postgres (the
compose `db` service on `postgresql://climate:climate@localhost:5433/climate`
works). The JS helper tests need Node ≥ 20.

## Versioning

One SemVer in `VERSION` drives `house_climate.__version__`, the `GET
/api/version` readout, the footer version line, and the asset cache-busts.
Every code PR adds a `## [Unreleased]` bullet in `CHANGELOG.md` (enforced by a
CI job + a local pre-commit hook); `python scripts/release.py {major|minor|patch}`
cuts a release and `git push --follow-tags` publishes a GitHub Release from the
changelog. See [docs/releasing.md](docs/releasing.md).

## License

MIT.
