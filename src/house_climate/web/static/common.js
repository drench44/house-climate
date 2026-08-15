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
  return `<div class="smoke-banner">Smoky outside — AQI ${Math.round(aqiVal)}` +
    `${aqiCat ? `, ${escapeHtml(aqiCat)}` : ''}. ` +
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
