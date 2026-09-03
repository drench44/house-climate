# Crawl-to-floor absolute humidity: gap, coupling, and the transport test

Date: 2026-09-03
Status: design, awaiting review
Scope: `analytics/humidity.py`, `analytics/moisture.py`, new
`analytics/coupling.py`, `db.py`, `web/api.py`, `web/static/moisture.*`,
`web/static/index.html` + `app.js`

## The question

Does crawl-space air actually reach the upper floor of the house?

The crawl is the wettest air in the building. If it is being transported
upward — stack effect through the floor and rim joist, or a duct chase — then
crawl moisture is an upstairs problem, and drying the crawl is worth money. If
it is not, the crawl is a contained problem and upstairs humidity has some
other source. The dashboard should answer this from measurement, not
assumption, and should refuse to answer until the data can carry the answer.

A secondary question follows the first: after a crawl intervention (a ground
vapour barrier, air sealing, encapsulation), did the upper floor measurably
benefit?

## Why absolute humidity

All three metrics below work in absolute humidity (AH, g/m³) rather than
relative humidity or dew point.

- **RH is not comparable between rooms.** A 55 °F crawl at 85 % RH and a 72 °F
  hallway at 50 % RH hold nearly the same water. Comparing their RH numbers
  compares thermometers, not moisture.
- **Dew point is comparable but is a vapour *pressure*.** What moves between
  floors is a mass of water carried by a volume of air, which is what AH
  measures directly. It also makes the transport arithmetic literal: if a
  cubic metre of crawl air arrives upstairs, it brings its g/m³ with it.

AH is nonlinear in temperature, so it is computed **per reading** and averaged
afterwards. Computing it from a bucket's mean temperature and mean dew point
would be wrong. This is why the conversion lives in SQL (`db._ah_sql`) beside
the existing `_MAGNUS_SQL` precedent, not in a post-processing step over
rollups.

## Metrics

### M1 — Gap (headline)

`gap = crawl AH − floor AH`, in g/m³, for every non-crawl channel.

Surfaces: a 7-day hourly series and a 60-day daily series per floor, the
current value, and a before/after comparison across each intervention marker.

**The gap is deliberately given no directional verdict.** A ground vapour
barrier lowers crawl AH and narrows the gap. Air sealing decouples the floor
and widens it. Both are successes. More importantly, the gap moves the same
way whether transport is total or zero — so it tests "did the barrier dry the
crawl", not "does crawl air reach upstairs". It is the headline because it is
the most legible number on the page, not because it is the strongest evidence.

### M2 — Excess over outdoor

`crawl_excess = crawl AH − outdoor AH` and `floor_excess = floor AH − outdoor
AH`, daily means.

Outdoor AH swings hard across a season and drags every sensor with it. The raw
gap therefore drifts for reasons that have nothing to do with the house.
Excess-over-outdoor is the season-adjusted read: crawl excess measures barrier
efficacy, floor excess measures the benefit that actually reached the living
space.

### M3 — Coupling β (the mechanism test)

The regression slope of upstairs AH on crawl AH, controlling for outdoor AH
and time of day. β is the **transport gain**: the fraction of a crawl moisture
excursion that shows up on the floor above.

β is reported instead of a correlation coefficient on purpose. A partial
correlation is algebraically the same test, but `r` scales with how much the
crawl swings, so it falls when a barrier reduces the crawl's independent
variance even if transport is completely unchanged. β does not: it is the
physically invariant quantity, and it is the one that supports a prediction.

### M4 — Transport prediction test (the proof)

A ground vapour barrier changes the moisture **source**, not the air
**pathway**. Stack flow is set by indoor-to-outdoor temperature difference,
leakage area and wind — a barrier on the soil touches none of them. So β
should *not* change after one, and "β dropped" must not be presented as
success.

The direction-unambiguous proof is a prediction:

```
predicted Δfloor_excess = β̂ × Δcrawl_excess
```

Measure β and the crawl excess drop, predict the floor excess drop, then check
it against what was observed. If the observed change lands inside the
predicted interval, the transport mechanism is confirmed *and quantified*.
That is a materially stronger claim than any single correlation, and it is the
number worth putting in front of a contractor.

### M5 — Downstairs consistency check

Crawl air rising by stack effect must pass the lower floor before reaching the
upper one. So `β_downstairs ≥ β_upstairs`, and the best-fit lag downstairs
should be no longer than upstairs. When the upper floor couples *harder* than
the lower one, the path is not floor stack — it is ducts or a chase, and the
page says so. This check is free once every channel is fitted.

## The β estimator

### Inputs

Hourly means of: crawl AH and RH, each floor's AH, outdoor AH, indoor and
outdoor temperature, and cooling/blower duty fraction from `equipment_status`.

### Preprocessing

1. **Drop saturated crawl hours** — exclude any hour where crawl RH ≥ 95 %.
   Capacitive RH sensors go nonlinear near saturation and a vented crawl sits
   there often; AH derived from a pinned sensor is not a measurement.
2. **Remove the slow trend** — subtract a centred 168-hour (7-day) moving mean
   from each series, leaving anomalies. Seven days, not 24 hours: the signal
   lives at the 6-hour-to-several-day timescale (rain wets soil, the crawl runs
   high for days, the house follows with a lag of hours). A 24-hour high-pass
   would delete exactly the band of interest. Hours without a full centred
   window are dropped rather than fitted with a partial one.
3. **Do not first-difference.** Hour-to-hour AH changes are the same order as
   sensor noise, so differencing whitens toward noise and destroys the
   multi-day structure where the effect lives.

### Model

```
floor_anom[t] = β·crawl_anom[t−k] + γ·outdoor_anom[t] + Σ hour-of-day dummies + ε[t]
```

Hour-of-day dummies (23 of them) absorb the shared diurnal cycle whatever its
source. Regressing on outdoor alone would not: the floor's daily rhythm is
partly occupancy and AC cycling, which is periodic without being
outdoor-driven and can align with the crawl by coincidence.

Lag `k` is selected from 0–6 hours by maximum HAC t-statistic, with a
Bonferroni correction at α = 0.05/7 — the same max-of-lags discipline the
existing rainfall correlation already applies. A full distributed lag would be
cleaner in principle but is unstable on seven collinear hourly terms across a
few months of data.

### Inference

- **Newey-West (HAC) standard errors**, Bartlett kernel, fixed 48-hour
  bandwidth. The bandwidth is set from building physics — house time constants
  are under a day or two — rather than fitted, so it is deterministic and
  cheap to recompute on every page load.
- **Effective sample size**, reported on the page:

  ```
  n_eff = n · (1 − ρx·ρe) / (1 + ρx·ρe)
  ```

  where ρx is the lag-1 autocorrelation of the crawl anomaly and ρe that of
  the residuals. At a realistic ρ ≈ 0.85, a 30-day window of 720 hours yields
  an n_eff in the tens, not the hundreds. Showing n_eff rather than n is the
  honest presentation, and it is what the t critical value uses.

### Readiness gates

β is not displayed unless every gate passes. Each failure has its own
plain-language reason on the page.

| Gate | Bar | Why |
|---|---|---|
| G1 window | ≥ 21 days (30 preferred) | The 7-day window is dropped entirely — at realistic autocorrelation its n_eff is around 14 and it could never pass honestly. 7 days remains only for the gap time series. |
| G2 n_eff | ≥ 30 | The autocorrelation-corrected count, not the raw hour count. |
| G3 coverage | ≥ 85 % of hours present in all three series, no single gap > 24 h | A window stitched across an outage is not a window. |
| G4 identifiability | SD of the crawl anomaly, after removing outdoor and hour effects, ≥ 0.3 g/m³ | Roughly twice sensor noise. Below it, β divides by noise. |
| G5 no straddling | The window must not cross an intervention marker | Pre and post are fitted separately. |
| G6 sign | β significantly negative → report "inconsistent", not a number | Negative transport is not physical; it means the model is picking up something else. |

G4 is expected to start failing *after* a successful barrier, once the crawl
stops swinging independently of outdoor. The page must present that as the
good result it is — "the crawl now tracks outdoor air too closely to measure
coupling" — not as an error.

### The confounder that matters most

**A vented crawl is a better local outdoor sensor than a weather station miles
away.** Because the station carries measurement error as a proxy for true
local outdoor conditions, the crawl can "explain" upstairs AH simply by
carrying better local-outdoor information — with no transport at all. This is
the single largest spurious-β risk and it cannot be argued away.

The discriminator is a **stack signature test**, fitted as a separate
diagnostic so the headline β stays simple:

```
floor_anom[t] = β·crawl_anom[t−k] + δ·(crawl_anom[t−k] × ΔT[t]) + γ·outdoor_anom[t] + hour dummies + ε[t]
```

where ΔT = indoor − outdoor temperature. Genuine stack transport strengthens
as ΔT rises; a local-outdoor-proxy artefact does not. A significantly positive
δ is affirmative evidence of the physical mechanism. This also predicts that β
is weak in a cooling season (indoor cooler, flow pushes down and out) and
strengthens through autumn — which, on a few months of data, could otherwise
be mistaken for an intervention effect.

Blower duty enters as a covariate for the same reason: return-side duct leaks
in the crawl pull crawl air in whenever the fan runs, regardless of ΔT, and
that is duct leakage rather than stack transport.

### Confounders documented but not modelled

Listed on the page's methodology note so the reader can weigh them:

- **Sensor placement is a proxy.** An upper-floor hallway sensor measures
  hallway air; a closed bedroom door decouples the room from it. A hallway at
  the top of a stairwell sits directly in the stack path and is well mixed, so
  it is the highest-signal placement available — which makes its result an
  **upper bound** on what any single room behind a door experiences.
- **Occupancy moisture** (showers, cooking, breathing) adds floor humidity
  uncorrelated with the crawl. Hour-of-day dummies absorb the scheduled part;
  a long dryer or bath-fan run remains an outlier.
- **Depressurisation events** — bath fans, dryer, range hood — pull crawl air
  up regardless of season.
- **Open windows** make a floor track outdoor directly and depress β.
- **Sensor calibration.** A ±3 % RH bias shifts AH by roughly 0.5 g/m³. That
  moves gap *levels* without biasing β, but a gain error does bias β. A
  one-time 24-hour co-location of all sensors, with stored offsets, is the fix;
  it is out of scope here and noted as a known limitation.

## Before/after comparisons

Gap and excess comparisons across an intervention marker reuse the existing
Welch-t machinery in `moisture._metric_compare`, with three changes:

1. **Autocorrelation-scaled degrees of freedom.** Daily means are still
   correlated day to day (ρ ≈ 0.4–0.6). Each side's df is scaled by
   `(1 − ρd)/(1 + ρd)` from that side's own daily lag-1 autocorrelation before
   the CI is formed. Without this the interval is too narrow and noise reads
   as real.
2. **≥ 14 days per side**, up from the existing 10.
3. **The install week is excluded** — an open hatch, disturbed soil and people
   crawling around produce a transient that is not the steady-state result.

The existing seasonal-confound downgrade (outdoor shift ≥ 50 % of the observed
change, same sign → verdict becomes `confounded`) applies unchanged.

## Code layout

A new module rather than more growth in `moisture.py`, which is already past
600 lines and would otherwise carry two unrelated statistical subsystems.

**`analytics/coupling.py`** — pure functions, no DB, no config:

- `hourly_anomalies(series, window_h=168)` — centred moving-mean detrend
- `hour_of_day_dummies(buckets)`
- `ols(X, y)` — normal equations with a singularity guard, reusing the
  existing `_solve3` approach generalised to n columns
- `newey_west_se(X, resid, bandwidth=48)`
- `effective_n(x_anom, resid)`
- `coupling_window(crawl, floor, outdoor, hours, ...)` → `{beta, ci95, lag,
  n, n_eff, t, ready, reason}`
- `stack_signature(...)` → `{delta, ci95, ready, reason}`
- `prediction_test(beta, d_crawl_excess, d_floor_excess)` → `{predicted,
  observed, ci95, verdict}`
- `consistency_check(betas_by_floor)` → floor-order sanity verdict

**`analytics/moisture.py`** — gap only: `ah_gap_hourly`, `ah_gap_daily`,
`ah_excess_daily`, `gap_intervention_report`.

**`db.py`** — `_ah_sql()` fragment; `ah_mean` on `sensor_daily_stats` and
`outdoor_daily`; `ah` on `outdoor_series`; `sensor_hourly_ah()` extended to
return RH for the saturation filter; new `indoor_hourly()` for temperature and
blower duty.

**`web/api.py`** — `_indoor_sensors(cfg)` returns every non-crawl channel.
`build_moisture` gains `ah_gaps` (per floor: now, series, daily, excess,
interventions) and `coupling` (per floor: beta block, stack signature,
prediction test, plus the cross-floor consistency verdict). `build_crawl`
gains a compact `ah_gap` summary — current gap per floor and the coupling
verdict when ready — so the dashboard does not have to fetch the full moisture
payload on its poll cycle.

## Surfaces

### Moisture evidence page (`/moisture.html`)

A new panel after the existing dew-point delta:

- Current gap per floor as tiles, g/m³.
- A 30-day daily multi-line chart, one line per floor, with intervention
  markers drawn in.
- The excess-over-outdoor pair as a second, season-adjusted line pair.
- The coupling readout: β with its CI, the chosen lag, n_eff, the stack
  signature verdict, and the downstairs consistency check — or, when a gate
  fails, that gate's plain-language reason and what is still needed.
- The prediction test, once an intervention has enough post data.
- A methodology note carrying the proxy-placement caveat and the unmodelled
  confounders.

### Main dashboard (`/index.html`)

A compact strip beneath the existing crawl panel: one tile per floor showing
the current gap and its 7-day direction, plus a single-line coupling verdict
once it is ready, linking through to the full panel on the moisture page. It
reads from the `ah_gap` summary already on `/api/crawl`, so it adds no new
request to the dashboard's poll cycle.

## Testing

- **AH math** — known psychrometric reference values; the equal-moisture
  cross-temperature case; agreement between the RH path and the dew-point path
  (the SQL rollups use the latter, live tiles the former, and they must not
  disagree about the same reading). *Written and green.*
- **Gap** — hourly pairing drops unpaired and null hours; daily join; the
  before/after real-change, collecting and seasonal-confound cases; an
  explicit test that no directional verdict is emitted. *Written and green
  against the first cut.* The three before/after changes above
  (autocorrelation-scaled df, the 14-day bar, install-week exclusion) are not
  yet implemented and need their own tests — currently the comparison still
  uses the inherited 10-day bar and unscaled df.
- **β estimator on synthetic data** — construct a floor series as a known β
  times a lagged crawl series plus outdoor plus a diurnal cycle plus noise,
  and assert the fit recovers β within its CI and finds the right lag.
- **Null case** — independent series must either fail a gate or return a CI
  containing zero. This is the test that matters most; it is what stops the
  page from inventing a mechanism.
- **Autocorrelation** — on a strongly autocorrelated input, assert
  `n_eff << n` and that the naive-n version would have wrongly passed.
- **Gates** — one test per gate, each asserting the specific refusal reason.
- **Confound** — a series where the crawl is a pure outdoor proxy with no
  transport must not produce a significant δ in the stack signature test.
- **DB** — the new SQL columns against real Postgres in CI, including the
  per-reading-then-average property (a day mixing temperatures must not equal
  AH computed from that day's means). These skip silently on a bare local
  pytest run, so they are verified on the PR.

## Delivery

Two PRs, because the first is already built and independently useful.

**PR 1 — AH and the gap.** The AH math, the SQL fragment and rollups, the
non-crawl channel enumeration, gap and excess analytics, the moisture-page gap
panel, and the dashboard strip. Ships a real answer to "how much wetter is the
crawl than each floor, and how is that changing".

**PR 2 — Coupling and the transport test.** `analytics/coupling.py`, the gates,
the stack signature, the prediction test, the consistency check, and the
coupling readout on both surfaces.

Both go through the three review agents per `CLAUDE.md`, both add a
`CHANGELOG.md` entry under `## [Unreleased]`.

## Known limitations

- Sensor calibration offsets are not measured; a gain error biases β.
- Wind is not measured, and it drives stack flow alongside ΔT.
- The outdoor feed is a station reading, not an on-site sensor. An on-site
  outdoor temperature and humidity sensor would materially strengthen every
  metric here by removing the errors-in-variables problem in M3, and is the
  single highest-value hardware addition for this analysis.
