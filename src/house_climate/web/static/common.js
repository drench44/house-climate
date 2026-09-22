'use strict';

/* House Climate — helpers shared by the dashboard (app.js) and the moisture
   case page (moisture.js). Loaded as a classic script BEFORE either; its
   top-level declarations are visible to the scripts that follow. */

const DEG = '°';           // for SVG text nodes (HTML strings use &deg;)
const GAP_MS = 15 * 60 * 1000;  // break a line across gaps longer than this

async function j(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.json();
}

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

function fmtTemp(v, decimals) {
  if (v == null) return '—';
  return v.toFixed(decimals == null ? 1 : decimals);
}
function fmtPct(v) { return v == null ? '—' : Math.round(v); }

function fmtAge(ageS) {
  if (ageS == null) return '—';
  if (ageS < 90) return `${Math.round(ageS)}s`;
  if (ageS < 5400) return `${Math.round(ageS / 60)}m`;
  return `${Math.round(ageS / 3600)}h`;
}

function backupBadge(status) {
  // Pure: /api/backup payload -> header-badge descriptor. Hidden when the
  // backup is healthy, still 'unknown' (no heartbeat recorded yet), or the
  // payload is missing/errored; amber only once a KNOWN backup has gone stale.
  if (!status || !status.known || !status.stale) return { show: false };
  const age = fmtAge(status.age_s);
  return {
    show: true,
    level: 'warn',
    text: `⚠ Backup stale (${age})`,
    title: `No successful backup in ${age} (warns past ${fmtAge(status.threshold_s)})`,
  };
}

function fmtHour(h) {
  const ap = h < 12 || h === 24 ? 'am' : 'pm';
  let hh = h % 12;
  if (hh === 0) hh = 12;
  return `${hh}${ap}`;
}

function cap(s) { return s ? s.charAt(0).toUpperCase() + s.slice(1) : s; }

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

/* SVG element factory into a given parent. */
const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(parent, name, attrs, text) {
  const e = document.createElementNS(SVGNS, name);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (text != null) e.textContent = text;
  parent.appendChild(e);
  return e;
}

/* Generic gap-aware polyline over time-stamped points. `points` is an array
   of {ms, v}; nulls already filtered. Returns an SVG path string with a new
   subpath (M) wherever the time gap exceeds gapMs (default GAP_MS — bucketed
   series pass their own threshold since buckets sit further apart). */
function timePath(points, xOf, yOf, gapMs) {
  const g = gapMs || GAP_MS;
  let d = '';
  let prevMs = null;
  points.forEach((p) => {
    const cmd = (prevMs == null || p.ms - prevMs > g) ? 'M' : 'L';
    d += `${cmd}${xOf(p.ms).toFixed(1)} ${yOf(p.v).toFixed(1)} `;
    prevMs = p.ms;
  });
  return d.trim();
}

/* Hover math shared by every chart: map a pointer x (already in SVG units)
   to a time inside the window, and find the point nearest a time. */
function hoverTargetMs(svgX, geom) {
  return geom.winStart + clamp(
    (svgX - geom.padL) / (geom.W - geom.padL - geom.padR), 0, 1,
  ) * (geom.winEnd - geom.winStart);
}

function nearestByMs(pts, targetMs) {
  let best = null, bestD = Infinity;
  pts.forEach((p) => {
    const d = Math.abs(p.ms - targetMs);
    if (d < bestD) { bestD = d; best = p; }
  });
  return best;
}

/* ---------------------------------------------------------------------- */
/* range coloring (red / yellow / green): the value is the status.        */
/* Shared by the dashboard (app.js) and the square tile (square.js) so    */
/* the same reading can never be green on one screen and red on another.  */
/* ---------------------------------------------------------------------- */

function tempClass(v, heat, cool) {
  if (v == null || heat == null || cool == null) return '';
  if (v >= heat && v <= cool) return 'v-ok';
  const dist = v < heat ? heat - v : v - cool;
  return dist <= 2 ? 'v-watch' : 'v-out';
}
function crawlTempClass(v) {
  if (v == null) return '';
  if (v >= 50 && v <= 80) return 'v-ok';
  if ((v >= 40 && v < 50) || (v > 80 && v <= 90)) return 'v-watch';
  return 'v-out';
}
function rhClass(v) {
  if (v == null) return '';
  if (v >= 30 && v <= 60) return 'v-ok';
  if ((v >= 25 && v < 30) || (v > 60 && v <= 65)) return 'v-watch';
  return 'v-out';
}
function crawlRhClass(v) {
  if (v == null) return '';
  if (v < 65) return 'v-ok';
  if (v <= 75) return 'v-watch';
  return 'v-out';
}

/* What the equipment is doing right now, from a /api/now payload. */
function equipmentState(n) {
  const eq = n.equipment_status;
  if (eq === 'cooling' || eq === 'overcool') return 'cooling';
  if (eq === 'heating') return 'heating';
  if (eq === 'fan') return 'fan';
  if (n.mode === 'off') return 'off';
  return 'idle';
}

/* Current TOU band label for the square tile, derived from the server's
   cost-summary TIER fields (tier_now / rate_now / next_change_at), NOT a
   hardcoded schedule — the old bandNow() baked in the EXAMPLE 17-21 peak, so
   any operator whose real hours differed saw a wrong band on the kiosk while
   the dashboard (which reads these same fields via peakStripHtml) was right.
   Returns null when there's no tier yet, so the caller keeps the last label
   instead of blanking. `until` is a pre-formatted " until 9pm" or ''. */
function bandTierLabel(cost) {
  if (!cost || !cost.tier_now) return null;
  const names = { peak: 'on-peak', mid: 'mid-peak', off: 'off-peak', flat: 'flat rate' };
  const clss = { peak: 'sq-peak', mid: 'sq-mid', off: 'sq-off', flat: 'sq-off' };
  let until = '';
  if (cost.next_change_at) {
    const at = new Date(cost.next_change_at);
    until = ` until ${at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`;
  }
  return { name: names[cost.tier_now] || cost.tier_now,
           cls: clss[cost.tier_now] || 'sq-off',
           until };
}

/* ---------------------------------------------------------------------- */
/* wall-state rules shared by app.js and square.js (tested in              */
/* tests/js/wall_state.test.mjs)                                           */
/* ---------------------------------------------------------------------- */

/* Range colors for one Ecowitt room row. A STALE room is never colored: a
   green "fine" on a reading from hours ago is a claim nobody measured. The
   dashboard already cleared it; the kiosk only dimmed it. */
function roomValueClasses(rm, heat, cool) {
  if (!rm || rm.stale) return { t: '', h: '' };
  const isCrawl = rm.channel === 'outdoor' || /crawl/i.test(rm.name || '');
  return {
    t: isCrawl ? crawlTempClass(rm.temp_f) : tempClass(rm.temp_f, heat, cool),
    h: isCrawl ? crawlRhClass(rm.humidity) : rhClass(rm.humidity),
  };
}

/* The cost rail's state: 'waiting' (no payload yet / fetch failed),
   'unavailable' (the server said it cannot price today, e.g. a TOU gap) or
   'ok'. The rail used to read cost.today.dollars unguarded, throw on an
   unavailable payload, and leave the previous numbers frozen on screen. */
function costRailState(cost) {
  if (!cost) return 'waiting';
  if (cost.available === false || !cost.today || cost.today.dollars == null) return 'unavailable';
  return 'ok';
}

function costUnavailableHtml(cost) {
  const why = cost && cost.reason === 'tou_gap'
    ? 'the rate table does not cover this time'
    : 'the server could not price today';
  return `<span class="micro">Cost</span><p class="loading">Cost unavailable &mdash; ${escapeHtml(why)}.</p>`;
}

/* The kiosk's price-band label. A fresh payload wins. When the fetch fails,
   the last label is kept ONLY until the band it names ends (its own
   next_change_at): "on-peak until 9pm" must not outlast 9pm. Returns
   { label: bandTierLabel()|null, last: the cost payload to remember }. */
function kioskBand(cost, lastGood, nowMs) {
  const fresh = cost && cost.available !== false ? bandTierLabel(cost) : null;
  if (fresh) return { label: fresh, last: cost };
  if (lastGood && lastGood.next_change_at) {
    const end = Date.parse(lastGood.next_change_at);
    if (Number.isFinite(end) && nowMs < end) return { label: bandTierLabel(lastGood), last: lastGood };
  }
  return { label: null, last: null };
}

const TIER_LABELS = { peak: 'on-peak', mid: 'mid-peak', off: 'off-peak', flat: 'flat rate' };
const TIER_CLASSES = { peak: 'b-peak', mid: 'b-mid', off: 'b-off', flat: 'b-off' };

/* Today's cost split, one row per configured band (cost.bands: today's
   season, {name, tier, rate}), highest rate first. Built from whatever the
   TOU config calls its bands: the old rail summed only 'peak', 'midpeak' and
   'offpeak', so any other naming showed $0 everywhere. A row is labelled by
   its tier unless two bands share a tier, then by its own name. A band that
   ran today but is missing from the list (a season edge) still shows. */
function bandSplitRows(cost) {
  const by = (cost && cost.today && cost.today.by_band) || {};
  const bands = (cost && Array.isArray(cost.bands)) ? cost.bands.slice() : [];
  const seen = new Set(bands.map((b) => b.name));
  Object.keys(by).forEach((name) => {
    if (!seen.has(name)) bands.push({ name, tier: null, rate: -1 });
  });
  bands.sort((a, b) => (b.rate || 0) - (a.rate || 0));
  const tierCount = {};
  bands.forEach((b) => { if (b.tier) tierCount[b.tier] = (tierCount[b.tier] || 0) + 1; });
  const dollars = (name) => (by[name] && by[name].dollars) || 0;
  const total = bands.reduce((acc, b) => acc + dollars(b.name), 0);
  return bands.map((b) => ({
    name: b.name,
    label: b.tier && tierCount[b.tier] === 1 ? TIER_LABELS[b.tier] : b.name,
    cls: TIER_CLASSES[b.tier] || 'b-off',
    dollars: dollars(b.name),
    pct: total > 0 ? (dollars(b.name) / total) * 100 : 0,
  }));
}

/* "17:00" -> "5pm", "16:30" -> "4:30pm" (config times are 24h HH:MM). */
function fmtClockHHMM(hhmm) {
  const [h, m] = String(hhmm).split(':').map(Number);
  const ap = h < 12 ? 'am' : 'pm';
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return m ? `${h12}:${String(m).padStart(2, '0')}${ap}` : `${h12}${ap}`;
}

function fmtWindow(w) {
  const s = fmtClockHHMM(w.start), e = fmtClockHHMM(w.end);
  const sAp = s.slice(-2), eAp = e.slice(-2);
  return `${sAp === eAp ? s.slice(0, -2) : s}&ndash;${e}`;
}

/* Tomorrow's peak line for the rail, from the forecast's own has_peak and
   peak_windows (both derived from the TOU config for tomorrow's date), never
   a hardcoded "5-9pm" or a band name. */
function forecastPeakText(fc) {
  if (!fc || fc.has_peak === false) return 'and no on-peak window tomorrow';
  const wins = Array.isArray(fc.peak_windows) ? fc.peak_windows : [];
  const when = wins.length ? wins.map(fmtWindow).join(' and ') : '';
  const inWin = when ? `the ${when} peak window` : 'the peak window';
  if (fc.predicted_peak_dollars == null) {
    return `peak cost for ${inWin} unknown (not enough history on peak days yet)`;
  }
  return `<b>$${fc.predicted_peak_dollars.toFixed(2)}</b> of it in ${inWin} if nothing shifts`;
}

/* The kiosk's one-word version. */
function forecastPeakWord(fc) {
  if (!fc || !fc.has_peak) return 'no peak';
  return fc.predicted_peak_dollars == null ? 'peak cost unknown' : 'in peak';
}

/* The wall's alert strip. null means the /api/anomalies fetch FAILED, which
   must not read as "nothing is wrong": say the alerts are unavailable. */
function alertsStripHtml(list) {
  if (list == null) {
    return '<div class="alert"><span class="sev">warning</span>' +
      '<span>Alerts unavailable: could not reach the server.</span></div>';
  }
  if (!Array.isArray(list) || list.length === 0) return '';
  return list.map((a) => {
    const sev = (a.severity || 'warning').toLowerCase();
    const crit = sev === 'critical' || sev === 'crit';
    return `<div class="alert${crit ? ' crit' : ''}"><span class="sev">${escapeHtml(crit ? 'critical' : 'warning')}</span>` +
      `<span>${escapeHtml(a.message || a.key || 'Alert')}</span></div>`;
  }).join('');
}

/* One line for an unavailable forecast, by reason; '' when there is none to
   explain (no history yet is not worth a line). */
function forecastUnavailableText(fc) {
  if (!fc || fc.available !== false) return '';
  if (fc.reason === 'feed_unreachable') return 'Tomorrow: forecast unavailable (weather feed unreachable).';
  if (fc.reason === 'no_tomorrow_forecast') return 'Tomorrow: forecast unavailable (no forecast for tomorrow yet).';
  return '';
}

/* The humidity panel's stale line: '' while fresh. */
function humidityStaleNote(h) {
  if (!h || !h.stale) return '';
  return `Stale &middot; last thermostat reading ${fmtAge(h.age_s)} old`;
}

/* Generic peak-cost guidance strip. All copy derives from rate TIERS
   (off/mid/peak/flat), never band names or fixed hours, so any utility
   works — including a one-rate (flat) utility with no peak concept at all. */
function peakStripHtml(cost, precool) {
  if (!cost || !cost.tier_now) return '';
  const rate = (r) => (r == null ? '' : `$${r.toFixed(2)}/kWh`);
  const at = cost.next_change_at ? new Date(cost.next_change_at) : null;
  const atTxt = at ? at.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }) : '';
  const coasting = precool && precool.ha_precool && precool.ha_precool.enabled;

  let cls, head, sub;
  if (cost.tier_now === 'peak') {
    cls = 'is-peak'; head = 'ON-PEAK';
    sub = `${rate(cost.rate_now)}${atTxt ? ` · until ${atTxt}` : ''} · ${coasting ? 'coasting — ' : ''}hold off on the dryer &amp; oven`;
  } else if (cost.next_tier === 'peak' && cost.minutes_to_change != null) {
    cls = 'is-warn'; head = `PEAK IN <span id="peak-countdown">${cost.minutes_to_change}</span> MIN`;
    sub = `run big loads now — beats ${rate(cost.next_rate)} at ${atTxt}`;
  } else if (cost.tier_now === 'flat') {
    /* One-rate utility: no peak/off framing at all, just the rate. */
    cls = 'is-flat'; head = 'FLAT RATE';
    sub = rate(cost.rate_now);
  } else if (cost.tier_now === 'mid') {
    /* An active mid tier that isn't bordering peak is NOT cheap power —
       keep it visually distinct from off-peak so it never reads as a deal. */
    cls = 'is-mid'; head = 'MID-PEAK';
    sub = rate(cost.rate_now);
  } else {
    /* tier_now === 'off' */
    cls = 'is-off'; head = 'OFF-PEAK';
    sub = (cost.next_tier === 'peak' && atTxt)
      ? `${rate(cost.rate_now)} · peak at ${atTxt}`
      : `${rate(cost.rate_now)} · cheap power`;
  }
  return `<div id="peak-strip" class="peak-strip ${cls}"><b>${head}</b><span>${sub}</span></div>`;
}

/* Is the AQI on screen a real monitor reading, or the weather feed's MODEL?
   `resolve_outdoor_aqi` silently falls back from the pushed monitor value to
   the feed's own `wx_aqi` once the monitor has been quiet for 30 minutes, and
   the two are not interchangeable: on 2026-08-31 the modeled source read 113
   "Unhealthy" while the regulatory monitor said 85 "Moderate". Every surface
   that prints the number has to say which one it is, or an outage turns a
   guess into a confident claim. Anything that is not an explicit monitor
   source counts as an estimate -- an unknown provenance is not a known-good
   one. */
function aqiIsEstimate(source) {
  return source !== 'airnow';
}

/* The one place the marker's wording lives, so the dashboard chip, the kiosk
   chip and any future surface cannot drift apart on it. */
function aqiEstimateSuffix(source) {
  return aqiIsEstimate(source) ? ' est.' : '';
}

/* Both AQI chips live here, beside the predicate they depend on, for the same
   reason peakStripHtml and smokeBannerHtml were moved: a builder that returns
   a string from app.js/square.js cannot be executed by the test suite (those
   files run top-level DOM code), so it could only ever be grep-tested -- and a
   grep that looks at an intermediate `label` variable does NOT see whether the
   return statement uses it. A reviewer proved exactly that against the first
   version of this fix: deleting `${label}` from aqiChip's return reintroduced
   the original bug with the whole suite green. Real functions, real tests. */

/* Full chip for the dashboard. */
function aqiChipHtml(aqi, category, source) {
  if (aqi == null) return '';
  const n = Math.round(aqi);
  // The `est.` is VISIBLE, not tooltip-only. This chip lives on a wall kiosk
  // that nobody hovers, so a provenance caveat hidden in `title` is a caveat
  // nobody ever reads -- which is how a modeled 113 passed for a monitor
  // reading of 85.
  const label = `${category ? `${n} · ${category}` : `${n}`}${aqiEstimateSuffix(source)}`;
  // The tooltip must not out-claim the label: for an unknown source the visible
  // chip hedges, so the hover text cannot name a specific feed as fact.
  const sourceText = source === 'airnow' ? 'from AirNow'
    : source === 'weather' ? 'from the weather feed (estimate)'
    : 'from an unknown source — treat it as an estimate';
  const tip = `Outdoor Air Quality Index (US AQI ${n}${category ? `, ${category}` : ''}) `
    + `${sourceText} — a unitless 0–500 scale. 0–50 good, 51–100 `
    + 'moderate, 101+ unhealthy. Above 100, keep windows shut regardless of humidity.';
  return `<span class="aqi num ${aqiChipClass(aqi)}" title="${escapeHtml(tip)}">`
    + `Outdoor AQI ${escapeHtml(label)}</span>`;
}

/* Compact chip for the square kiosk tile. */
function aqiChipCompactHtml(hum) {
  if (!hum || !hum.available || hum.outdoor_aqi == null) return '';
  const n = Math.round(hum.outdoor_aqi);
  const cat = hum.aqi_category ? ` ${escapeHtml(hum.aqi_category.toLowerCase())}` : '';
  // This chip never carried provenance at all -- the kiosk's most-glanced
  // surface was the one most able to pass a model off as a measurement.
  return `<span class="aqi num ${aqiChipClass(n)}">`
    + `AQI ${n}${cat}${aqiEstimateSuffix(hum.aqi_source)}</span>`;
}

/* Smoke-banner decision — decoupled from `rooms`/Ecowitt on purpose: a
   rooms/Ecowitt outage must never suppress an outdoor-smoke warning, so
   this reads ONLY from the `humidity` object (never `rooms`). Mirrors the
   peakStripHtml extraction — pure function so it's testable without a DOM.
   `fallbackThreshold` is used only when humidity.aqi_unhealthy is missing
   (the caller's AQI_UNHEALTHY constant). */
function smokeBannerHtml(humidity, fallbackThreshold) {
  const aqiVal = humidity ? humidity.outdoor_aqi : null;
  if (aqiVal == null) return '';
  const threshold = (humidity && humidity.aqi_unhealthy != null) ? humidity.aqi_unhealthy : fallbackThreshold;
  if (aqiVal < threshold) return '';
  const aqiCat = humidity ? humidity.aqi_category : null;
  // Marked, never SUPPRESSED. A modeled AQI is weak evidence of smoke, but
  // during a monitor outage it is the only evidence there is, and the cost of
  // hiding a real smoke event beats the cost of an over-cautious banner.
  const est = aqiIsEstimate(humidity ? humidity.aqi_source : null)
    ? ' (estimate, not a monitor)' : '';
  return `<div class="smoke-banner">Smoky outside — AQI ${Math.round(aqiVal)}` +
    `${aqiCat ? `, ${escapeHtml(aqiCat)}` : ''}${est}. ` +
    `Windows closed; purifiers should be running.</div>`;
}

/* AQI chip color band (US AQI): <=50 green, <=100 neutral, <=150 amber, else red */
function aqiChipClass(aqi) {
  if (aqi <= 50) return 'aqi-ok';
  if (aqi <= 100) return 'aqi-neutral';
  if (aqi <= 150) return 'aqi-warn';
  return 'aqi-crit';
}

/* Indoor PM2.5 chip for a room, matched by name against the purifier data
   HA pushes. The reading is taken AT the purifier (which cleans its own
   vicinity first), so it's an indication, not a certified number. */
function pmChip(rm, air) {
  if (!air || !air.available || !Array.isArray(air.rooms)) return '';
  const entry = air.rooms.find((a) => a.room === (rm.name || '').toLowerCase());
  if (!entry) return '';
  const th = air.thresholds || { elevated: 12, bad: 35 };
  if (entry.stale) {
    return `<span class="pm num pm-stale" title="PM2.5 from the ${escapeHtml(entry.room)} Levoit purifier via Home Assistant — stale, last push ${fmtAge(entry.age_s)} ago">PM —</span>`;
  }
  const cls = entry.pm25 > th.bad ? 'pm-bad' : entry.pm25 > th.elevated ? 'pm-warn' : 'pm-ok';
  const tip = `PM2.5 ${entry.pm25.toFixed(0)} µg/m³ — measured at the ${escapeHtml(entry.room)} Levoit purifier (via Home Assistant, ${fmtAge(entry.age_s)} ago). Under ${th.elevated} good · over ${th.bad} bad.`;
  return `<span class="pm num ${cls}" title="${tip}">PM ${Math.round(entry.pm25)}</span>`;
}

/* Nice 5-multiple gridlines strictly inside [lo, hi], at most `maxN`.
   Thinning halves uniformly (every 2nd level) so surviving gridlines stay
   EVENLY spaced — deleting from one end left one lonely line at the bottom
   and a bunched cluster at the top, breaking the visual value scale. */
function gridLevels(lo, hi, maxN) {
  let out = [];
  const first = Math.ceil(lo / 5) * 5;
  for (let v = first; v < hi; v += 5) out.push(v);
  while (out.length > maxN) out = out.filter((_, i) => i % 2 === 0);
  return out;
}
