'use strict';

/* House Climate — "the house is the interface."
   Fetches the /api/* endpoints and renders a living cross-section of the
   house that breathes with equipment state and the real weather feed.
   No external libraries; inline SVG + CSS animation only. LAN-only, must
   work with the internet down. */

/* Shared helpers (j, clamp, fmt*, escapeHtml, svgEl, timePath, gridLevels,
   DEG, GAP_MS) come from common.js, loaded before this file. */

const REFRESH_MS = 60000;

/* Outdoor AQI at/above this escalates the modest chip into a prominent
   .smoke-banner. The real threshold comes from /api/humidity's aqi_unhealthy
   field (mirrors the server's alerts.aqi_unhealthy in config.json); this is
   only the fallback used if that field is ever missing. */
const AQI_UNHEALTHY = 101;

const EQ_LABELS = {
  cooling: 'Cooling', overcool: 'Cooling (boost)', heating: 'Heating',
  fan: 'Fan only', idle: 'Idle', off: 'Off', unknown: 'Unknown',
};
const MODE_LABELS = {
  off: 'off', cool: 'cool', heat: 'heat', auto: 'auto',
  emheat: 'emergency heat', unknown: '',
};

/* ---------------------------------------------------------------------- */
/* range coloring (red / yellow / green): the value is the status.        */
/* tempClass / rhClass / crawl* live in common.js (shared with square.js) */
/* ---------------------------------------------------------------------- */

function dewClass(v) {
  if (v == null) return '';
  if (v <= 60) return 'v-ok';
  if (v <= 65) return 'v-watch';
  return 'v-out';
}
function holdClass(v) {
  if (v == null) return '';
  if (v >= 70) return 'v-ok';
  if (v >= 50) return 'v-watch';
  return 'v-out';
}
function filterClass(pct) {
  if (pct == null) return '';
  if (pct < 80) return 'v-ok';
  if (pct < 100) return 'v-watch';
  return 'v-out';
}

/* ---------------------------------------------------------------------- */
/* equipment + weather state (drive the ambience via root data-* attrs)   */
/* ---------------------------------------------------------------------- */

const root = document.getElementById('root');
/* equipmentState() comes from common.js (shared with square.js) */

/* Sky from the real weather feed: conditions string first, then solar for
   day-vs-night when it's clear. */
function weatherState(n) {
  const cond = (n.wx_conditions || '').toLowerCase();
  if (/rain|shower|drizzle|storm|thunder/.test(cond)) return 'rain';
  if (/snow|sleet|flurr|ice/.test(cond)) return 'snow';
  if (/cloud|overcast|fog|haze|mist/.test(cond)) return 'clouds';
  const solar = n.wx_solar_wm2;
  if (solar == null || solar < 10) return 'night';
  return 'day';
}

/* ---------------------------------------------------------------------- */
/* one-time ambience: particles spawned once, animated purely in CSS      */
/* ---------------------------------------------------------------------- */

function spawnAmbience() {
  const rand = (a, b) => a + Math.random() * (b - a);

  const stars = document.getElementById('stars');
  for (let i = 0; i < 26; i++) {
    const s = document.createElement('i');
    s.style.left = `${rand(2, 98)}%`;
    s.style.top = `${rand(2, 38)}%`;
    s.style.setProperty('--tw', `${rand(6, 15).toFixed(1)}s`);
    s.style.setProperty('--twd', `${rand(0, 8).toFixed(1)}s`);
    s.style.setProperty('--twmax', rand(0.25, 0.55).toFixed(2));
    stars.appendChild(s);
  }

  const clouds = document.getElementById('clouds');
  for (let i = 0; i < 4; i++) {
    const c = document.createElement('i');
    c.style.top = `${rand(3, 25)}%`;
    c.style.left = `${rand(-10, 60)}%`;
    c.style.transform = `scale(${rand(0.7, 1.6).toFixed(2)})`;
    c.style.setProperty('--cd', `${rand(60, 110).toFixed(0)}s`);
    c.style.setProperty('--cdd', `${(-rand(0, 60)).toFixed(0)}s`);
    clouds.appendChild(c);
  }

  const rain = document.getElementById('rain');
  for (let i = 0; i < 34; i++) {
    const d = document.createElement('i');
    d.style.left = `${rand(0, 100)}%`;
    d.style.setProperty('--rd', `${rand(0.9, 1.6).toFixed(2)}s`);
    d.style.setProperty('--rdd', `${rand(0, 2).toFixed(2)}s`);
    rain.appendChild(d);
  }

  const snow = document.getElementById('snow');
  for (let i = 0; i < 24; i++) {
    const f = document.createElement('i');
    f.style.left = `${rand(0, 100)}%`;
    f.style.setProperty('--sd', `${rand(6, 10).toFixed(1)}s`);
    f.style.setProperty('--sdd', `${rand(0, 6).toFixed(1)}s`);
    f.style.setProperty('--sx', `${rand(-14, 18).toFixed(0)}px`);
    snow.appendChild(f);
  }

  // streamlines: full-width sine paths; light "puffs" (stroke dashes) travel
  // ALONG the curve so the motion itself undulates. 4 streams at different
  // heights, amplitudes, phases and speeds; each also breathes vertically.
  const flow = document.getElementById('airflow');
  for (let i = 0; i < 4; i++) {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('class', 'aw-stream');
    s.setAttribute('viewBox', '0 0 100 100');
    s.setAttribute('preserveAspectRatio', 'none');
    s.setAttribute('aria-hidden', 'true');
    s.style.setProperty('--dur', `${rand(4.5, 8).toFixed(2)}s`);
    s.style.setProperty('--delay', `${rand(0, 3).toFixed(2)}s`);
    s.style.setProperty('--bob', `${rand(6, 10).toFixed(1)}s`);
    s.style.setProperty('--bobamp', `${rand(3, 6).toFixed(0)}px`);

    // 3-period sine across the width: alternate control points, smooth T-chain
    const base = 16 + i * 22 + rand(-4, 4);         // spread streams over the height
    const amp = rand(3.5, 7.5) * (Math.random() < 0.5 ? 1 : -1);
    const step = 100 / 6;
    let d = `M0 ${base.toFixed(1)} Q${(step / 2).toFixed(1)} ${(base - amp).toFixed(1)}, ${step.toFixed(1)} ${base.toFixed(1)}`;
    for (let k = 2; k <= 6; k++) d += ` T${(step * k).toFixed(1)} ${base.toFixed(1)}`;

    const path = document.createElementNS(SVGNS, 'path');
    path.setAttribute('d', d);
    path.setAttribute('pathLength', '100');
    s.appendChild(path);
    flow.appendChild(s);
  }
}

/* ---------------------------------------------------------------------- */
/* clock                                                                  */
/* ---------------------------------------------------------------------- */

function tickClock() {
  const d = new Date();
  const t = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit' });
  const day = d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
  document.getElementById('clock').textContent = `${t} · ${day}`;
  document.getElementById('foot-updated').textContent = `updated ${t}`;
}

/* ---------------------------------------------------------------------- */
/* the scene (hero)                                                       */
/* ---------------------------------------------------------------------- */

/* aqiChipClass() comes from common.js (shared with square.js) */
function aqiChip(aqi, category, source) {
  if (aqi == null) return '';
  const n = Math.round(aqi);
  const label = category ? `${n} · ${category}` : `${n}`;
  const sourceText = source === 'airnow' ? 'from AirNow'
    : source === 'weather' ? 'from the weather feed (estimate)'
    : 'from the weather feed';
  const tip = `Outdoor Air Quality Index (US AQI ${n}${category ? `, ${category}` : ''}) `
    + `${sourceText} — a unitless 0–500 scale. 0–50 good, 51–100 `
    + 'moderate, 101+ unhealthy. Above 100, keep windows shut regardless of humidity.';
  return `<span class="aqi num ${aqiChipClass(aqi)}" title="${escapeHtml(tip)}">Outdoor AQI ${escapeHtml(label)}</span>`;
}

function sceneEmpty() {
  root.setAttribute('data-state', 'idle');
  document.getElementById('scene').classList.remove('is-stale');
  document.getElementById('scene-outdoor').innerHTML =
    `<div class="scene-empty"><span class="dot" aria-hidden="true"></span>` +
    `<p>No fresh reading from the thermostat.</p>` +
    `<p class="sub">Check the Daikin link and the poller service on the box.</p></div>`;
  document.getElementById('scene-rooms').innerHTML = '';
  document.getElementById('scene-crawl').innerHTML = '';
  document.getElementById('scene-stat').innerHTML = '';
}

/* pmChip() comes from common.js (shared with square.js) */

function roomRow(rm, heat, cool, air) {
  const isCrawl = rm.channel === 'outdoor' || /crawl/i.test(rm.name || '');
  const kind = rm.channel === 'outdoor'
    ? 'WH32 in the crawl space (wired to the gateway’s outdoor slot)'
    : `WH31 · channel ${rm.channel}`;
  const batt = rm.battery_low ? 'battery LOW' : 'battery ok';
  const nameTip = `Ecowitt ${kind} · ${batt}`;
  const pm = pmChip(rm, air);

  if (!rm.present) {
    /* No Ecowitt sensor yet — but the purifier's PM2.5 may already exist
       (the Garage row is useful today for exactly this reason). */
    return `<div class="floor absent">
      <div class="room"><span class="micro">${escapeHtml(rm.name)}</span></div>
      <div class="vals">${pm}<span class="waiting">sensor on the way</span></div>
    </div>`;
  }

  const stale = !!rm.stale;
  const tCls = stale ? '' : (isCrawl ? crawlTempClass(rm.temp_f) : tempClass(rm.temp_f, heat, cool));
  const hCls = stale ? '' : (isCrawl ? crawlRhClass(rm.humidity) : rhClass(rm.humidity));

  const lvl = rm.signal == null ? null : clamp(Math.round(rm.signal), 0, 4);
  let sig = '';
  if (lvl != null) {
    let bars = '';
    for (let i = 1; i <= 4; i++) bars += `<i${i > lvl ? ' class="off"' : ''}></i>`;
    sig = `<span class="sig${lvl <= 2 ? ' weak' : ''}" title="Signal ${lvl} of 4 · ${batt}" aria-label="signal ${lvl} of 4">${bars}</span>`;
  }
  const battChip = rm.battery_low ? '<span class="batt-chip" title="Sensor battery low">batt</span>' : '';
  const age = `<span class="age num${stale ? ' old' : ''}">${fmtAge(rm.age_s)}</span>`;

  return `<div class="floor${stale ? ' stale' : ''}">
    <div class="room"><span class="micro" title="${escapeHtml(nameTip)}">${escapeHtml(rm.name)}</span></div>
    <div class="vals">
      <span class="t num ${tCls}">${fmtTemp(rm.temp_f, 0)}<span class="u">${DEG}</span></span>
      <span class="rh num ${hCls}">${fmtPct(rm.humidity)}%</span>
      ${pm}${sig}${battChip}${age}
    </div>
  </div>`;
}

function renderScene(n, rooms, air, humidity) {
  const scene = document.getElementById('scene');
  const hasData = n.indoor_temp_f != null;   // key can exist with a null value
  if (!hasData) { sceneEmpty(); return; }

  root.setAttribute('data-state', equipmentState(n));
  root.setAttribute('data-weather', weatherState(n));
  scene.classList.toggle('is-stale', !!n.stale);

  const heat = n.heat_setpoint_f, cool = n.cool_setpoint_f, indoor = n.indoor_temp_f;
  const solar = n.wx_solar_wm2 != null ? clamp(n.wx_solar_wm2 / 1000, 0, 1) : 0;

  /* outdoor strip — AQI comes from /api/humidity, which is independent of
     /api/rooms (indoor Ecowitt). A rooms/Ecowitt failure must never suppress
     the smoke banner: read AQI off `humidity`'s own availability instead of
     `rooms.available`. smokeBannerHtml() (common.js) enforces this by
     reading only from `humidity` — see its tests for the decoupled case. */
  const aqiVal = humidity ? humidity.outdoor_aqi : null;
  const aqiCat = humidity ? humidity.aqi_category : null;
  const aqiSource = humidity ? humidity.aqi_source : null;
  const smoke = smokeBannerHtml(humidity, AQI_UNHEALTHY);
  const aqi = aqiChip(aqiVal, aqiCat, aqiSource);
  document.getElementById('scene-outdoor').innerHTML =
    smoke +
    `<span class="sun" style="--solar:${solar.toFixed(2)}" aria-hidden="true"></span>` +
    `<span class="line num">Outside <b>${fmtTemp(n.wx_outdoor_temp_f, 0)}${DEG}</b> &nbsp;·&nbsp; ` +
    `solar <b>${fmtPct(n.wx_solar_wm2)}</b> W/m²</span>` +
    (aqi || '');

  /* house rows (indoor sensors) + crawl (below the ground line) */
  const list = rooms && rooms.available && Array.isArray(rooms.rooms) ? rooms.rooms : [];
  const crawlRooms = list.filter((r) => r.channel === 'outdoor' || /crawl/i.test(r.name || ''));
  const indoorRooms = list.filter((r) => !(r.channel === 'outdoor' || /crawl/i.test(r.name || '')));
  document.getElementById('scene-rooms').innerHTML =
    indoorRooms.map((r) => roomRow(r, heat, cool, air)).join('');
  document.getElementById('scene-crawl').innerHTML =
    crawlRooms.map((r) => roomRow(r, heat, cool, air)).join('');

  /* thermostat column: big temp, equipment pill, comfort band, humidity */
  const eq = equipmentState(n);
  const modeLabel = MODE_LABELS[n.mode] != null ? MODE_LABELS[n.mode] : (n.mode || '');
  const eqText = eq === 'cooling' ? 'Cooling'
    : eq === 'heating' ? 'Heating'
    : eq === 'fan' ? 'Fan only'
    : eq === 'off' ? 'Off'
    : `Idle${modeLabel ? ` · ${modeLabel} mode` : ''}`;

  let lo = Math.min(heat != null ? heat - 5 : indoor - 8, indoor - 3);
  let hi = Math.max(cool != null ? cool + 6 : indoor + 8, indoor + 3);
  if (hi - lo < 10) hi = lo + 10;
  const pos = (v) => (clamp(v, lo, hi) - lo) / (hi - lo) * 100;

  const notchH = heat != null ? `<span class="notch h" style="left:${pos(heat).toFixed(1)}%"></span>` : '';
  const notchC = cool != null ? `<span class="notch c" style="left:${pos(cool).toFixed(1)}%"></span>` : '';
  const labels = `<div class="labels">` +
    (heat != null ? `<span class="h num">heat at <b>${fmtTemp(heat, 0)}${DEG}</b></span>` : '<span></span>') +
    (cool != null ? `<span class="c num">cool at <b>${fmtTemp(cool, 0)}${DEG}</b></span>` : '<span></span>') +
    `</div>`;

  document.getElementById('scene-stat').innerHTML = `
    <div>
      <span class="micro">Main floor · thermostat</span>
      <div class="big-temp">
        <span class="v num ${tempClass(indoor, heat, cool)}">${fmtTemp(indoor, 1)}</span>
        <span class="u">${DEG}F</span>
      </div>
    </div>
    <div class="eq-pill">
      <span class="d" aria-hidden="true"></span>${escapeHtml(eqText)}
    </div>
    <div class="band" role="img" aria-label="Indoor ${fmtTemp(indoor, 0)} degrees${heat != null && cool != null ? `, between heat ${Math.round(heat)} and cool ${Math.round(cool)}` : ''}">
      <div class="track">
        ${notchH}${notchC}
        <span class="marker" style="left:${pos(indoor).toFixed(1)}%"></span>
      </div>
      ${labels}
    </div>
    <p class="rh-line num">Indoor humidity <b class="${rhClass(n.indoor_humidity)}">${fmtPct(n.indoor_humidity)}%</b> RH <span class="rh-src">· at the thermostat</span></p>
    ${n.stale ? `<p class="stale-banner">Stale · last reading ${fmtAge(n.age_s)} old</p>` : ''}
  `;
}

/* ---------------------------------------------------------------------- */
/* cost rail + live ticker (ported: rebaselines each server refresh)      */
/* ---------------------------------------------------------------------- */

const COST_TICK_MS = 1000;
const COST_ACCRUAL_CAP_SEC = 330; // 180s poll interval + buffer
let costTickerHandle = null;
let costTickerState = null; // { baseToday, asOfMs, liveRatePerHr, running }

function tickCost() {
  const st = costTickerState;
  if (!st) return;
  const todayEl = document.getElementById('cost-today');
  const chipEl = document.getElementById('chip-today');
  if (!todayEl) return; // rail re-rendered mid-tick

  const show = st.running && st.liveRatePerHr > 0 && st.asOfMs != null;
  let accrued = 0;
  if (show) {
    const elapsed = clamp((Date.now() - st.asOfMs) / 1000, 0, COST_ACCRUAL_CAP_SEC);
    accrued = (st.liveRatePerHr * elapsed) / 3600;
  }
  const total = st.baseToday + accrued;
  // Extra precision while accruing so the number visibly climbs each second.
  todayEl.textContent = `$${total.toFixed(show ? 4 : 2)}`;
  todayEl.classList.toggle('is-ticking', show);
  if (chipEl) chipEl.textContent = `$${total.toFixed(2)} today`;

  const cd = document.getElementById('peak-countdown');
  if (cd && st.nextChangeMs) {
    const mins = Math.max(0, Math.round((st.nextChangeMs - Date.now()) / 60000));
    cd.textContent = mins;
  }
}

/* peakStripHtml() comes from common.js (shared with square.js) */

function renderRail(cost, forecast, precool) {
  const el = document.getElementById('rail');
  if (!cost) {
    el.innerHTML = `<p class="loading">Cost — waiting for data.</p>`;
    costTickerState = null;
    return;
  }

  /* Legend swatch rate: derived from config (cost.peak_rate = the highest
     TOU band rate) instead of hardcoded, so it can't disagree with the
     peak strip below it for a non-default rate config. Flat-rate/no-peak
     configs have no distinct peak band, so fall back to the bare label. */
  const legendRateEl = document.getElementById('legend-peak-rate');
  if (legendRateEl) {
    legendRateEl.textContent = (cost.peak_rate != null)
      ? `on-peak $${cost.peak_rate.toFixed(2)}/kWh`
      : 'on-peak';
  }

  const now = new Date();
  const todayLabel = now.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });

  /* Honest month label: if tracking began after the 1st, say "since <date>" —
     a young dataset must not masquerade as a full month of August. */
  let monthLabel = `${now.toLocaleDateString('en-US', { month: 'long' })} so far`;
  let monthTip = '';
  if (cost.data_since) {
    const since = new Date(cost.data_since);
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1);
    if (since > monthStart) {
      const sinceLabel = since.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
      monthLabel = `Since ${sinceLabel}`;
      monthTip = ` title="Tracking began ${sinceLabel} — not the full month."`;
    }
  }

  const stripHtml = peakStripHtml(cost, precool);
  const rate = cost.live_rate_per_hr || 0;
  const showAccrual = !!cost.running && rate > 0;
  const accrual = showAccrual
    ? `<span class="accrual num" id="accrual">▲ accruing $${rate.toFixed(2)}/hr</span>`
    : `<span class="accrual num" id="accrual" hidden></span>`;

  /* Band split for TODAY, so the breakdown sums to the headline above it. */
  const by = (cost.today && cost.today.by_band) || {};
  const bandDollars = (k) => (by[k] && by[k].dollars) || 0;
  const monthTotal = cost.month_to_date ? cost.month_to_date.dollars : 0;
  const total3 = bandDollars('peak') + bandDollars('midpeak') + bandDollars('offpeak');
  const width = (k) => (total3 > 0 ? (bandDollars(k) / total3) * 100 : 0);
  const bandRow = (cls, name, k) =>
    `<div class="bandrow ${cls}"><span class="dot"></span><span class="nm">${name}</span>` +
    `<span class="bar"><i style="width:${width(k).toFixed(0)}%"></i></span>` +
    `<span class="amt num">$${bandDollars(k).toFixed(2)}</span></div>`;

  let proj = `<div class="proj-row"><span>${escapeHtml(monthLabel)}</span><b class="num"${monthTip}>$${monthTotal.toFixed(2)}</b></div>`;
  if ((cost.complete_days || 0) > 0) {
    const avg = cost.avg_per_day != null ? `$${cost.avg_per_day.toFixed(2)}` : '—';
    const pm = cost.projected_month != null ? `$${cost.projected_month.toFixed(2)}` : '—';
    proj += `<div class="proj-row"><span>avg/day <b>${avg}</b></span><span>projected month <b>${pm}</b></span></div>`;
  }

  let fc = '';
  if (forecast && forecast.available) {
    const hrs = forecast.predicted_cool_minutes != null
      ? (Math.round((forecast.predicted_cool_minutes / 60) * 2) / 2)
      : null;
    const basis = (forecast.days_of_history != null && forecast.days_of_history < 7)
      ? `<p class="fc-basis">based on ${forecast.days_of_history} day${forecast.days_of_history === 1 ? '' : 's'} of history</p>`
      : '';
    /* predicted_peak_dollars covers ONLY the 5-9pm window's predicted
       cooling at that window's real rate (off-peak on weekends). */
    const peakTxt = forecast.peak_band === 'peak'
      ? `<b>$${(forecast.predicted_peak_dollars || 0).toFixed(2)}</b> of it in the 5&ndash;9pm peak window if nothing shifts`
      : `<b>$${(forecast.predicted_peak_dollars || 0).toFixed(2)}</b> during 5&ndash;9pm (weekend &mdash; off-peak rate)`;
    fc = `<hr><span class="micro">Tomorrow</span>
      <p class="fc-line num">High <b>${fmtTemp(forecast.fc_high_f, 0)}${DEG}</b> — expect about ` +
      `<b>${hrs != null ? hrs : '—'}h</b> of cooling, ${peakTxt}.</p>${basis}`;
    // Advisory pre-cool tip — house-climate does not know if HA's automation
    // is on, so never claim it will run; state the conditional saving only.
    if (precool && precool.relevant) {
      fc += `<p class="fc-tip num">Pre-cooling tomorrow would shift ~${precool.shiftable_kwh} kWh ` +
        `to cheaper hours — <b>~$${(precool.savings || 0).toFixed(2)} saved</b>.</p>`;
    }
  }

  el.innerHTML = `
    ${stripHtml}
    <span class="micro">Today · ${escapeHtml(todayLabel)}</span>
    <div class="today-amt num" id="cost-today">$${cost.today.dollars.toFixed(2)}</div>
    ${accrual}
    <div class="bands">
      ${bandRow('b-peak', 'on-peak', 'peak')}
      ${bandRow('b-mid', 'mid-peak', 'midpeak')}
      ${bandRow('b-off', 'off-peak', 'offpeak')}
    </div>
    <p class="fc-basis" title="Runtime × ${cost.assumed_kw != null ? cost.assumed_kw : '?'} kW (configured system draw) × the example TOU schedule's rates. HVAC energy only — not your whole electric bill.">HVAC only · estimated at ${cost.assumed_kw != null ? cost.assumed_kw : '?'} kW while cooling</p>
    <hr>
    ${proj}
    ${fc}
  `;

  costTickerState = {
    baseToday: cost.today.dollars,
    asOfMs: cost.as_of ? new Date(cost.as_of).getTime() : null,
    liveRatePerHr: rate,
    running: !!cost.running,
    nextChangeMs: cost.next_change_at ? new Date(cost.next_change_at).getTime() : null,
  };
  if (costTickerHandle) clearInterval(costTickerHandle);
  tickCost();
  costTickerHandle = setInterval(tickCost, COST_TICK_MS);
}

/* ---------------------------------------------------------------------- */
/* humidity panel (full width — the headline metric)                      */
/* ---------------------------------------------------------------------- */

function renderHumidity(h) {
  const el = document.getElementById('humidity');
  if (!h || !h.available) {
    el.innerHTML = `<div class="panel-head"><span class="micro" style="color:var(--dim)">Humidity</span></div>` +
      `<p class="loading">Humidity — waiting for data.</p>`;
    return;
  }

  const d = h.dew_point_delta;
  let compare = '';
  if (d != null) {
    if (Math.abs(d) < 2) compare = ' — house is about the same humidity as outside.';
    else if (d <= -2) compare = ' — house is drier than outside.';
    else compare = ' — house is damper than outside.';
  }

  const w = h.window || { action: 'neutral', reason: 'Little to gain from opening windows right now.' };
  const winCls = w.action === 'open' ? 'open' : w.action === 'keep_closed' ? 'closed' : '';
  const ac = h.ac_effect
    ? `Cooling pulls RH from <b>${Math.round(h.ac_effect.idle)}%</b> to <b>${Math.round(h.ac_effect.cooling)}%</b> — a <b>${h.ac_effect.drop.toFixed(1)}</b>-pt drop.`
    : '';

  el.innerHTML = `
    <div class="panel-head">
      <span class="micro" style="color:var(--dim)">Humidity</span>
      <span class="micro right">indoor vs outdoor dew point · 24h</span>
    </div>
    <div class="hum-top">
      <div class="hum-big"><span class="v num ${rhClass(h.indoor_rh)}">${fmtPct(h.indoor_rh)}</span><span class="u">% RH indoor</span></div>
      <p class="hum-dew num">Indoor dew point <b class="${dewClass(h.indoor_dp)}">${fmtTemp(h.indoor_dp, 0)}${DEG}</b> · outside <b>${fmtTemp(h.outdoor_dp, 0)}${DEG}</b>${compare}</p>
    </div>
    <svg class="hum-spark" id="hum-spark" viewBox="0 0 960 78" role="img" aria-label="Indoor and outdoor dew point over the last 24 hours"></svg>
    <div class="hum-foot">
      <span class="hum-window ${winCls}">${escapeHtml(w.reason)}</span>
      ${ac ? `<span class="hum-ac num">${ac}</span>` : ''}
    </div>
  `;

  drawDewSparkline(h.trend);
}

function drawDewSparkline(trend) {
  const hs = document.getElementById('hum-spark');
  if (!hs) return;
  const W = 960, H = 78, padL = 34, padR = 12, top = 8, bot = 66;
  if (!Array.isArray(trend) || trend.length < 2) {
    svgEl(hs, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'Dew-point history — collecting.');
    return;
  }

  const pts = trend.map((t) => ({ ms: new Date(t.ts).getTime(), i: t.indoor_dp, o: t.outdoor_dp }));
  const inP = pts.filter((p) => p.i != null).map((p) => ({ ms: p.ms, v: p.i }));
  const outP = pts.filter((p) => p.o != null).map((p) => ({ ms: p.ms, v: p.o }));
  const vals = inP.concat(outP).map((p) => p.v);
  if (!vals.length) return;

  const start = pts[0].ms, end = pts[pts.length - 1].ms || start + 1;
  let lo = Math.floor(Math.min(...vals) - 1), hi = Math.ceil(Math.max(...vals) + 1);
  if (hi - lo < 4) hi = lo + 4;
  const xOf = (ms) => padL + clamp((ms - start) / (end - start || 1), 0, 1) * (W - padL - padR);
  const yOf = (v) => bot - (v - lo) / (hi - lo) * (bot - top);

  const defs = document.createElementNS(SVGNS, 'defs');
  defs.innerHTML = '<linearGradient id="hs-grad" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="rgba(91,201,240,0.12)"/>' +
    '<stop offset="1" stop-color="rgba(91,201,240,0)"/></linearGradient>';
  hs.appendChild(defs);

  gridLevels(lo, hi, 3).forEach((v) => {
    svgEl(hs, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(hs, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v}${DEG}`);
  });

  if (inP.length >= 2) {
    const line = timePath(inP, xOf, yOf);
    const first = inP[0], last = inP[inP.length - 1];
    svgEl(hs, 'path', { class: 'hs-fill', d: `${line} L${xOf(last.ms).toFixed(1)} ${bot} L${xOf(first.ms).toFixed(1)} ${bot} Z` });
  }
  if (outP.length >= 2) svgEl(hs, 'path', { class: 'hs-outdoor', d: timePath(outP, xOf, yOf) });
  if (inP.length >= 2) {
    svgEl(hs, 'path', { class: 'hs-indoor', d: timePath(inP, xOf, yOf) });
    const last = inP[inP.length - 1];
    svgEl(hs, 'circle', { cx: xOf(last.ms), cy: yOf(last.v), r: 3.5, fill: 'var(--cool)' });
  }

  /* direct labels at the line ends — which line is which, without a legend.
     Nudged apart when the lines end close together. */
  const ends = [];
  if (inP.length >= 2) ends.push({ y: yOf(inP[inP.length - 1].v), text: 'indoor', fill: 'var(--cool)' });
  if (outP.length >= 2) ends.push({ y: yOf(outP[outP.length - 1].v), text: 'outside', fill: 'rgba(245, 164, 92, 0.8)' });
  if (ends.length === 2 && Math.abs(ends[0].y - ends[1].y) < 12) {
    const mid = (ends[0].y + ends[1].y) / 2;
    const firstBelow = ends[0].y >= ends[1].y;
    ends[0].y = mid + (firstBelow ? 6 : -6);
    ends[1].y = mid + (firstBelow ? -6 : 6);
  }
  ends.forEach((l) => {
    svgEl(hs, 'text', {
      class: 'hs-endlabel', x: W - padR, y: clamp(l.y, top + 8, bot) - 4,
      'text-anchor': 'end', fill: l.fill,
    }, l.text);
  });
}

/* ---------------------------------------------------------------------- */
/* crawl space (dedicated chart: highs/lows, trends, mold thresholds)     */
/* ---------------------------------------------------------------------- */

const CRAWL_RANGES = ['24h', '7d', '30d'];
const CRAWL_BUCKET_MS = { '24h': 900e3, '7d': 3600e3, '30d': 10800e3 }; // mirror api.py
let crawlRange = '24h';
try {
  const saved = localStorage.getItem('hc_crawl_range');
  if (CRAWL_RANGES.indexOf(saved) !== -1) crawlRange = saved;
} catch (e) { /* ignore */ }
let crawlPts = [];
let crawlGeom = null;

function fmtWhen(ms, range) {
  const d = new Date(ms);
  if (range === '24h') {
    return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }).toLowerCase().replace(' ', '');
  }
  if (range === '7d') {
    const t = d.toLocaleTimeString('en-US', { hour: 'numeric' }).toLowerCase().replace(' ', '');
    return `${d.toLocaleDateString('en-US', { weekday: 'short' })} ${t}`;
  }
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function crawlTicks(winStart, winEnd, range) {
  const out = [];
  if (range === '24h') {
    const t = new Date(winStart);
    t.setMinutes(0, 0, 0);
    while (t.getHours() % 4 !== 0 || t.getTime() < winStart) t.setTime(t.getTime() + 3600e3);
    for (let ms = t.getTime(); ms <= winEnd; ms += 4 * 3600e3) {
      out.push({ ms, label: fmtHour(new Date(ms).getHours()) });
    }
    return out;
  }
  const stepDays = range === '7d' ? 1 : 5;
  const t = new Date(winStart);
  t.setHours(0, 0, 0, 0);
  if (t.getTime() < winStart) t.setDate(t.getDate() + 1);
  let i = 0;
  for (const d = new Date(t); d.getTime() <= winEnd; d.setDate(d.getDate() + 1), i++) {
    if (i % stepDays !== 0) continue;
    out.push({
      ms: d.getTime(),
      label: range === '7d'
        ? d.toLocaleDateString('en-US', { weekday: 'short' })
        : d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
    });
  }
  return out;
}

function updateCrawlTabs() {
  document.querySelectorAll('#crawl-tabs button').forEach((b) => {
    const on = b.getAttribute('data-crawl-range') === crawlRange;
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

function setCrawlRange(r) {
  if (CRAWL_RANGES.indexOf(r) === -1 || r === crawlRange) return;
  crawlRange = r;
  try { localStorage.setItem('hc_crawl_range', r); } catch (e) { /* ignore */ }
  updateCrawlTabs();
  j(`/api/crawl?range=${r}`).then(renderCrawl).catch((e) => console.error('crawl', e));
}

function renderCrawl(c) {
  const panel = document.getElementById('crawl');
  const lead = document.getElementById('crawl-lead');
  const foot = document.getElementById('crawl-foot');
  const note = document.getElementById('crawl-range-note');
  const legend = document.getElementById('crawl-legend');
  const svg = document.getElementById('crawl-chart');
  if (!panel) return;

  /* No crawl sensor in the config at all -> the panel is noise; hide it. */
  if (c && c.available === false && c.reason === 'not_configured') {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  updateCrawlTabs();

  /* Drop stale responses: a 60s refresh in flight when the user switched
     tabs would otherwise repaint the OLD range under the new tab. */
  if (c && c.available && c.range && c.range !== crawlRange) return;

  if (!c || !c.available) {
    lead.innerHTML = `<p class="loading">${c ? 'Crawl sensor configured — first readings on the way.' : 'Crawl space — waiting for data.'}</p>`;
    svg.innerHTML = '';
    foot.innerHTML = '';
    legend.hidden = true;
    note.textContent = '—';
    crawlGeom = null; crawlPts = [];
    return;
  }
  legend.hidden = false;

  /* head note: window + honesty about young data */
  const rangeWords = { '24h': 'the last 24 hours', '7d': 'the last 7 days', '30d': 'the last 30 days' };
  const rangeMs = { '24h': 864e5, '7d': 7 * 864e5, '30d': 30 * 864e5 }[c.range];
  let noteTxt = `highs & lows · ${rangeWords[c.range]}`;
  if (c.data_start && Date.now() - new Date(c.data_start).getTime() < rangeMs - 36e5) {
    noteTxt = `highs & lows · data since ${new Date(c.data_start).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`;
  }
  note.textContent = noteTxt;

  /* lead: current numbers + trend + stat chips */
  const stale = !!c.stale;
  let trendHtml = '';
  if (c.trend) {
    const arrow = c.trend.dir === 'rising' ? '↗' : c.trend.dir === 'falling' ? '↘' : '→';
    const cls = c.trend.dir === 'rising' ? 'ct-up' : c.trend.dir === 'falling' ? 'ct-down' : 'ct-flat';
    const amt = c.trend.dir === 'steady' ? 'steady' : `${arrow} ${Math.abs(c.trend.delta).toFixed(1)} pts over ${c.trend.window_h}h`;
    trendHtml = ` · <b class="${cls}">${c.trend.dir === 'steady' ? `${arrow} ${amt}` : amt}</b>`;
  }
  const chip = (label, val, cls) =>
    `<span class="cs-chip"><span class="cl">${label}</span><b class="${cls || ''}">${val}</b></span>`;
  const chips =
    chip('high', `${fmtPct(c.rh_high.v)}% · ${fmtWhen(new Date(c.rh_high.ts).getTime(), c.range)}`, crawlRhClass(c.rh_high.v)) +
    chip('low', `${fmtPct(c.rh_low.v)}% · ${fmtWhen(new Date(c.rh_low.ts).getTime(), c.range)}`, crawlRhClass(c.rh_low.v)) +
    chip('avg', `${fmtPct(c.rh_avg)}%`, crawlRhClass(c.rh_avg)) +
    chip('&gt;65%', `${c.hours_above_65}h`, c.hours_above_65 > 0 ? 'v-watch' : 'v-ok') +
    chip('&gt;75%', `${c.hours_above_75}h`, c.hours_above_75 > 0 ? 'v-out' : 'v-ok');

  lead.innerHTML = `
    <div class="hum-top">
      <div class="hum-big"><span class="v num ${stale ? '' : crawlRhClass(c.rh_now)}">${fmtPct(c.rh_now)}</span><span class="u">% RH crawl</span></div>
      <p class="hum-dew num"><b>${fmtTemp(c.temp_now, 0)}${DEG}</b> in the crawl · dew point <b>${fmtTemp(c.dew_now, 0)}${DEG}</b>${trendHtml}${stale ? ` · <span class="v-out">stale · ${fmtAge(c.age_s)} old</span>` : ''}</p>
    </div>
    <div class="cs-chips num">${chips}</div>
  `;

  drawCrawlChart(c);

  /* foot: mold verdict + vent advice */
  const th = c.thresholds || { watch: 65, mold: 75 };
  let mold, moldCls;
  if (c.hours_above_75 > 0) {
    mold = `${c.hours_above_75}h above ${th.mold}% in this window — mold territory; the crawl needs drying.`;
    moldCls = 'bad';
  } else if (c.hours_above_65 > 0) {
    const pct = c.hours_total > 0 ? Math.round(c.hours_above_65 / c.hours_total * 100) : 0;
    mold = `${c.hours_above_65}h above ${th.watch}% — in the watch zone ${pct}% of this window. Under ${th.mold}% mold can't take hold, but there's no margin.`;
    moldCls = 'watch';
  } else {
    mold = `Never crossed ${th.watch}% in this window — the crawl is staying dry.`;
    moldCls = 'ok';
  }
  const vent = c.vent ? `<span class="cs-vent num">${escapeHtml(c.vent.reason)}${c.outdoor_dp != null ? ` (outdoor dew point ${fmtTemp(c.outdoor_dp, 0)}${DEG})` : ''}</span>` : '';
  foot.innerHTML = `<span class="cs-mold ${moldCls}">${mold}</span>${vent}`;
}

function drawCrawlChart(c) {
  const svg = document.getElementById('crawl-chart');
  svg.innerHTML = '';
  const W = 960, H = 170, padL = 38, padR = 40, top = 10, bot = 132, tickY = 156;
  crawlGeom = null; crawlPts = [];

  const series = Array.isArray(c.series) ? c.series : [];
  const pts = series.filter((s) => s.rh_avg != null).map((s) => ({
    ms: new Date(s.ts).getTime(),
    avg: s.rh_avg,
    min: s.rh_min != null ? s.rh_min : s.rh_avg,
    max: s.rh_max != null ? s.rh_max : s.rh_avg,
    temp: s.temp_avg,
  }));
  if (pts.length < 2) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'Crawl history — collecting.');
    return;
  }
  crawlPts = pts;

  const gapMs = (CRAWL_BUCKET_MS[c.range] || 900e3) * 2.5;
  const winStart = pts[0].ms, winEnd = pts[pts.length - 1].ms;
  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart || 1), 0, 1) * (W - padL - padR);

  const vals = [];
  pts.forEach((p) => { vals.push(p.min, p.max); });
  let lo = Math.floor(Math.min.apply(null, vals) - 3);
  let hi = Math.ceil(Math.max.apply(null, vals) + 3);
  hi = Math.max(hi, 68);   // keep the 65% watch line in view even when dry
  lo = Math.min(lo, 58);   // and some healthy ground under it for context
  lo = Math.max(lo, 0); hi = Math.min(hi, 100);
  if (hi - lo < 12) hi = Math.min(100, lo + 12);
  const yOf = (v) => bot - (clamp(v, lo, hi) - lo) / (hi - lo) * (bot - top);
  crawlGeom = { W, H, padL, padR, top, bot, xOf, yOf, winStart, winEnd, range: c.range };

  /* mold zones + threshold lines (drawn only where they fall inside scale) */
  const zone = (a, b, cls) => {
    const a2 = clamp(a, lo, hi), b2 = clamp(b, lo, hi);
    if (b2 - a2 <= 0) return;
    svgEl(svg, 'rect', { class: cls, x: padL, y: yOf(b2), width: W - padL - padR, height: yOf(a2) - yOf(b2) });
  };
  zone(65, 75, 'cz-watch');
  zone(75, hi, 'cz-mold');
  [[65, 'cz-line watch'], [75, 'cz-line mold']].forEach(([v, cls]) => {
    if (v > lo && v < hi) svgEl(svg, 'line', { class: cls, x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
  });

  /* gridlines + left RH labels (skip the threshold values — already marked) */
  gridLevels(lo, hi, 4).filter((v) => v !== 65 && v !== 75).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v}%`);
  });

  /* min-max band: forward along max, back along min, per gap segment */
  const segs = [];
  let cur = [];
  let prevMs = null;
  pts.forEach((p) => {
    if (prevMs != null && p.ms - prevMs > gapMs) { if (cur.length > 1) segs.push(cur); cur = []; }
    cur.push(p); prevMs = p.ms;
  });
  if (cur.length > 1) segs.push(cur);
  segs.forEach((seg) => {
    let d = '';
    seg.forEach((p, k) => { d += `${k ? 'L' : 'M'}${xOf(p.ms).toFixed(1)} ${yOf(p.max).toFixed(1)} `; });
    for (let k = seg.length - 1; k >= 0; k--) d += `L${xOf(seg[k].ms).toFixed(1)} ${yOf(seg[k].min).toFixed(1)} `;
    svgEl(svg, 'path', { class: 'cz-band', d: `${d.trim()} Z` });
  });

  /* temperature overlay on its own right-hand scale */
  const temps = pts.filter((p) => p.temp != null).map((p) => ({ ms: p.ms, v: p.temp }));
  if (temps.length >= 2) {
    let tLo = Math.min.apply(null, temps.map((p) => p.v)) - 2;
    let tHi = Math.max.apply(null, temps.map((p) => p.v)) + 2;
    if (tHi - tLo < 6) { const m = (tHi + tLo) / 2; tLo = m - 3; tHi = m + 3; }
    const tyOf = (v) => bot - (v - tLo) / (tHi - tLo) * (bot - top);
    svgEl(svg, 'path', { class: 'cz-temp', d: timePath(temps, xOf, tyOf, gapMs) });
    gridLevels(tLo, tHi, 2).forEach((v) => {
      svgEl(svg, 'text', { class: 'cz-templabel', x: W - padR + 6, y: tyOf(v) + 3 }, `${v}${DEG}`);
    });
    crawlGeom.tyOf = tyOf;
  }

  /* the humidity line itself */
  svgEl(svg, 'path', { class: 'cz-avg', d: timePath(pts.map((p) => ({ ms: p.ms, v: p.avg })), xOf, yOf, gapMs) });

  /* exact high/low markers from the raw data (not the bucketed series) */
  const mark = (stat, cls, above) => {
    if (!stat || stat.v == null) return;
    const x = xOf(new Date(stat.ts).getTime());
    const y = yOf(stat.v);
    svgEl(svg, 'circle', { class: cls, cx: x, cy: y, r: 3.5 });
    const anchor = x > W - padR - 60 ? 'end' : (x < padL + 60 ? 'start' : 'middle');
    const ty = above ? Math.max(y - 8, top + 8) : Math.min(y + 15, bot - 3);
    svgEl(svg, 'text', { class: 'cz-marklabel', x, y: ty, 'text-anchor': anchor },
      `${above ? 'H' : 'L'} ${fmtPct(stat.v)}%`);
  };
  mark(c.rh_high, 'cz-hidot', true);
  mark(c.rh_low, 'cz-lodot', false);

  /* now dot */
  const last = pts[pts.length - 1];
  svgEl(svg, 'circle', { class: 'rb-now-halo', cx: xOf(last.ms), cy: yOf(last.avg), r: 9 });
  svgEl(svg, 'circle', { cx: xOf(last.ms), cy: yOf(last.avg), r: 3.5, fill: 'var(--crawl-line)' });

  /* time axis */
  crawlTicks(winStart, winEnd, c.range).forEach((t) => {
    svgEl(svg, 'line', { class: 'rb-tick', x1: xOf(t.ms), y1: bot + 6, x2: xOf(t.ms), y2: bot + 11 });
    svgEl(svg, 'text', { class: 'rb-hour', x: xOf(t.ms), y: tickY, 'text-anchor': 'middle' }, t.label);
  });
}

function initCrawlHover() {
  const svg = document.getElementById('crawl-chart');
  const tip = document.getElementById('crawl-tip');
  if (!svg || !tip) return;
  svg.addEventListener('mousemove', (e) => {
    if (!crawlGeom || !crawlPts.length) return;
    const rect = svg.getBoundingClientRect();
    const scale = crawlGeom.W / rect.width;
    const svgX = (e.clientX - rect.left) * scale;
    const best = nearestByMs(crawlPts, hoverTargetMs(svgX, crawlGeom));
    if (!best) return;
    const lines = [fmtWhen(best.ms, crawlGeom.range)];
    lines.push(`RH ${best.avg.toFixed(0)}%${best.min !== best.max ? ` (${Math.round(best.min)}–${Math.round(best.max)}%)` : ''}`);
    if (best.temp != null) lines.push(`crawl temp ${best.temp.toFixed(1)}${DEG}`);
    tip.innerHTML = lines.join('<br>');
    tip.hidden = false;
    tip.style.left = `${crawlGeom.xOf(best.ms) / crawlGeom.W * rect.width}px`;
    tip.style.top = `${crawlGeom.top / crawlGeom.H * rect.height + 6}px`;
  });
  svg.addEventListener('mouseleave', () => { tip.hidden = true; });
}

/* ---------------------------------------------------------------------- */
/* the day ribbon (24h chart + activity, merged)                          */
/* ---------------------------------------------------------------------- */

let ribbonSwept = false;
let ribbonPts = [];   // downsampled history for hover
let ribbonGeom = null;

function renderRibbon(history, timeline, cost) {
  const svg = document.getElementById('ribbon');
  svg.innerHTML = '';
  const rangeEl = document.getElementById('ribbon-range');

  const W = 960, H = 168, padL = 34, padR = 12, plotTop = 14, plotBot = 116, runY = 128, runH = 9, hourY = 158;
  const hist = Array.isArray(history) ? history : [];

  // canonical window: timeline's if present, else history span
  let winStart, winEnd;
  if (timeline && timeline.available) {
    winStart = new Date(timeline.window_start).getTime();
    winEnd = new Date(timeline.window_end).getTime();
  } else if (hist.length) {
    winStart = new Date(hist[0].ts).getTime();
    winEnd = new Date(hist[hist.length - 1].ts).getTime();
  }
  if (winStart == null || !(winEnd > winStart)) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'No history yet.');
    rangeEl.textContent = '—';
    ribbonGeom = null;
    return;
  }

  const wsDate = new Date(winStart);
  const wsDay = wsDate.toDateString() === new Date().toDateString() ? 'today' : 'yesterday';
  rangeEl.textContent = `${wsDate.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }).toLowerCase().replace(' ', '')} ${wsDay} → now`;

  // downsample history to <=200 points
  const step = Math.max(1, Math.ceil(hist.length / 200));
  const pts = [];
  for (let i = 0; i < hist.length; i += step) {
    const r = hist[i];
    pts.push({ ms: new Date(r.ts).getTime(), in: r.indoor_temp_f, out: r.outdoor_temp_f });
  }
  if (hist.length && (hist.length - 1) % step !== 0) {
    const r = hist[hist.length - 1];
    pts.push({ ms: new Date(r.ts).getTime(), in: r.indoor_temp_f, out: r.outdoor_temp_f });
  }
  ribbonPts = pts;

  const inP = pts.filter((p) => p.in != null).map((p) => ({ ms: p.ms, v: p.in }));
  const outP = pts.filter((p) => p.out != null).map((p) => ({ ms: p.ms, v: p.out }));
  const vals = inP.concat(outP).map((p) => p.v);

  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart), 0, 1) * (W - padL - padR);
  let lo = 60, hi = 90;
  if (vals.length) {
    lo = Math.floor((Math.min(...vals) - 2) / 5) * 5;
    hi = Math.ceil((Math.max(...vals) + 2) / 5) * 5;
    if (hi - lo < 10) hi = lo + 10;
  }
  const yOf = (t) => plotBot - (t - lo) / (hi - lo) * (plotBot - plotTop);
  ribbonGeom = { W, padL, padR, plotTop, plotBot, runY, runH, xOf, yOf, winStart, winEnd, lo, hi };

  // gradient for the indoor area fill
  const defs = document.createElementNS(SVGNS, 'defs');
  defs.innerHTML = '<linearGradient id="rb-grad" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="rgba(91,201,240,0.14)"/>' +
    '<stop offset="1" stop-color="rgba(91,201,240,0)"/></linearGradient>';
  svg.appendChild(defs);

  // On-peak shading: window(s) come from the API's config-driven
  // cost.peak_windows (the TOU band(s) at the peak rate), NOT a hardcoded
  // hour range — so a utility whose peak isn't weekday 17:00-21:00 still
  // shades correctly. No cost data yet, or a flat-rate config with no
  // distinct peak, means peak_windows is missing/empty and nothing is drawn.
  // Iterate CALENDAR days from local midnight — stepping raw timestamps
  // from winStart skips the newest day whenever the window is under 24h
  // (the timeline-fallback path), leaving the live on-peak span unshaded.
  const peakWindows = (cost && Array.isArray(cost.peak_windows)) ? cost.peak_windows : [];
  if (peakWindows.length) {
    const dayStart = new Date(winStart);
    dayStart.setHours(0, 0, 0, 0);
    for (const t = new Date(dayStart); t.getTime() <= winEnd; t.setDate(t.getDate() + 1)) {
      const ds = new Date(t.getFullYear(), t.getMonth(), t.getDate());
      const dow = ds.getDay();
      for (const w of peakWindows) {
        if (w.weekday_only && (dow === 0 || dow === 6)) continue;
        const [sh, sm] = w.start.split(':').map(Number);
        const [eh, em] = w.end.split(':').map(Number);
        const ps = new Date(ds.getFullYear(), ds.getMonth(), ds.getDate(), sh, sm).getTime();
        const pe = new Date(ds.getFullYear(), ds.getMonth(), ds.getDate(), eh, em).getTime();
        const a = Math.max(ps, winStart), b = Math.min(pe, winEnd);
        if (b <= a) continue;
        svgEl(svg, 'rect', { class: 'rb-peak', x: xOf(a), y: plotTop, width: xOf(b) - xOf(a), height: (runY + runH) - plotTop });
        // dashed edge on the inner boundary
        const edge = a === winStart ? b : a;
        svgEl(svg, 'line', { class: 'rb-peak-edge', x1: xOf(edge), y1: plotTop, x2: xOf(edge), y2: runY + runH });
      }
    }
  }

  // temperature gridlines + labels
  gridLevels(lo, hi, 4).forEach((v) => {
    svgEl(svg, 'line', { class: 'rb-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'rb-templabel', x: 4, y: yOf(v) + 3 }, `${v}${DEG}`);
  });

  // indoor area fill + the two curves
  if (inP.length >= 2) {
    const line = timePath(inP, xOf, yOf);
    const first = inP[0], last = inP[inP.length - 1];
    svgEl(svg, 'path', { class: 'rb-indoor-fill', d: `${line} L${xOf(last.ms).toFixed(1)} ${plotBot} L${xOf(first.ms).toFixed(1)} ${plotBot} Z` });
  }
  if (outP.length >= 2) svgEl(svg, 'path', { class: 'rb-outdoor', d: timePath(outP, xOf, yOf) });
  let indoorPathEl = null;
  if (inP.length >= 2) indoorPathEl = svgEl(svg, 'path', { class: 'rb-indoor', d: timePath(inP, xOf, yOf) });

  // AC run segments from the timeline (cooling cyan, heating amber)
  const segs = timeline && timeline.available && Array.isArray(timeline.segments) ? timeline.segments : [];
  segs.forEach((s) => {
    const heat = s.status === 'heating';
    if (s.status !== 'cooling' && s.status !== 'overcool' && !heat) return;
    const a = xOf(new Date(s.start).getTime());
    const w = Math.max(2.5, xOf(new Date(s.end).getTime()) - a);
    svgEl(svg, 'rect', { class: heat ? 'rb-heat-glow' : 'rb-cool-glow', x: a - 1.5, y: runY - 1.5, width: w + 3, height: runH + 3, rx: 4 });
    svgEl(svg, 'rect', { class: heat ? 'rb-heat-seg' : 'rb-cool-seg', x: a, y: runY, width: w, height: runH, rx: 2.5 });
  });

  // hour ticks every 4h on the hour
  const firstTick = new Date(winStart);
  firstTick.setMinutes(0, 0, 0);
  while (firstTick.getHours() % 4 !== 0 || firstTick.getTime() < winStart) {
    firstTick.setTime(firstTick.getTime() + 3600 * 1000);
  }
  for (let t = firstTick.getTime(); t <= winEnd; t += 4 * 3600 * 1000) {
    const d = new Date(t);
    svgEl(svg, 'line', { class: 'rb-tick', x1: xOf(t), y1: runY + runH + 6, x2: xOf(t), y2: runY + runH + 11 });
    svgEl(svg, 'text', { class: 'rb-hour', x: xOf(t), y: hourY, 'text-anchor': 'middle' }, fmtHour(d.getHours()));
  }

  // now cursor at the last indoor sample
  if (inP.length) {
    const last = inP[inP.length - 1];
    svgEl(svg, 'line', { class: 'rb-now', x1: xOf(last.ms), y1: plotTop, x2: xOf(last.ms), y2: runY + runH });
    svgEl(svg, 'circle', { class: 'rb-now-halo', cx: xOf(last.ms), cy: yOf(last.v), r: 10 });
    svgEl(svg, 'circle', { class: 'rb-now-dot', cx: xOf(last.ms), cy: yOf(last.v), r: 4 });
  }

  // sweep the indoor line in, once
  if (indoorPathEl && !ribbonSwept && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    const len = indoorPathEl.getTotalLength();
    indoorPathEl.style.strokeDasharray = len;
    indoorPathEl.style.strokeDashoffset = len;
    indoorPathEl.style.transition = 'stroke-dashoffset 1.6s ease-out 0.3s';
    requestAnimationFrame(() => requestAnimationFrame(() => { indoorPathEl.style.strokeDashoffset = '0'; }));
    ribbonSwept = true;
  }
}

/* ribbon hover: nearest point readout */
function initRibbonHover() {
  const wrap = document.querySelector('.ribbon-wrap');
  const svg = document.getElementById('ribbon');
  const tip = document.getElementById('ribbon-tip');
  svg.addEventListener('mousemove', (e) => {
    if (!ribbonGeom || !ribbonPts.length) return;
    const rect = svg.getBoundingClientRect();
    const scale = ribbonGeom.W / rect.width;
    const svgX = (e.clientX - rect.left) * scale;
    const best = nearestByMs(ribbonPts, hoverTargetMs(svgX, ribbonGeom));
    if (!best) return;
    const time = new Date(best.ms).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
    const lines = [time];
    if (best.in != null) lines.push(`indoor ${best.in.toFixed(1)}${DEG}`);
    if (best.out != null) lines.push(`outdoor ${best.out.toFixed(1)}${DEG}`);
    tip.innerHTML = lines.join('<br>');
    tip.hidden = false;
    const leftPx = ribbonGeom.xOf(best.ms) / ribbonGeom.W * rect.width;
    tip.style.left = `${leftPx}px`;
    tip.style.top = `${ribbonGeom.plotTop / 168 * rect.height + 6}px`;
  });
  svg.addEventListener('mouseleave', () => { tip.hidden = true; });
}

/* ---------------------------------------------------------------------- */
/* the quiet cards: Runtime, Health, Learning                             */
/* ---------------------------------------------------------------------- */

function renderRuntimeCard(rt) {
  const el = document.getElementById('runtime');
  if (!rt) { el.innerHTML = `<span class="micro">Runtime · 24h</span><p class="loading">Waiting for data.</p>`; return; }
  const m = rt.minutes || { cool: 0, heat: 0, fan: 0, idle: 0 };
  const total = (m.cool || 0) + (m.heat || 0) + (m.fan || 0) + (m.idle || 0) || 1;
  const pct = (v) => ((v || 0) / total * 100).toFixed(1);
  const heatDominant = (m.heat || 0) > (m.cool || 0);
  const leadMin = heatDominant ? m.heat : m.cool;
  const leadLabel = heatDominant ? 'h heating' : 'h cooling';

  const shortN = rt.short_cycles || 0;
  const induced = rt.short_cycles_setpoint_induced || 0;
  const healthy = shortN === 0;
  let note;
  if (healthy) {
    note = `<p class="note good">Healthy${induced > 0 ? ` — ${induced} short run${induced === 1 ? '' : 's'} excluded (we caused ${induced === 1 ? 'it' : 'them'} with a setpoint change).` : '.'}</p>`;
  } else {
    note = `<p class="note warn">${shortN} short cycle${shortN === 1 ? '' : 's'} — equipment cycling faster than expected.</p>`;
  }

  el.innerHTML = `
    <span class="micro">Runtime · 24h</span>
    <div class="lead"><span class="v num">${(leadMin / 60).toFixed(1)}</span><span class="u">${leadLabel}</span></div>
    <div class="minibar" role="img" aria-label="Cooling ${Math.round(pct(m.cool))}%, heating ${Math.round(pct(m.heat))}%, fan ${Math.round(pct(m.fan))}%, idle ${Math.round(pct(m.idle))}%">
      <i class="mb-cool" style="width:${pct(m.cool)}%"></i><i class="mb-heat" style="width:${pct(m.heat)}%"></i><i class="mb-fan" style="width:${pct(m.fan)}%"></i><i class="mb-idle" style="width:${pct(m.idle)}%"></i>
    </div>
    <div class="row"><span>Cycles</span><b class="v-ok">${rt.cycle_count ?? 0}</b></div>
    <div class="row"><span>Short cycles</span><b class="${shortN === 0 ? 'v-ok' : 'v-out'}">${shortN}</b></div>
    ${note}
  `;
}

function fmtDaysAgo(days) {
  if (days == null) return 'not logged';
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  return `${days} days ago`;
}

function renderHealthCard(h) {
  const el = document.getElementById('health');
  if (!h || !h.available) {
    el.classList.remove('due');
    el.innerHTML = `<span class="micro">Health</span><p class="loading">Waiting for data.</p>`;
    return;
  }
  const filt = h.filter || {};
  const pct = clamp(filt.pct ?? 0, 0, 100);
  const due = !!filt.due;
  el.classList.toggle('due', due);
  const barCls = due ? 'due' : (pct >= 80 ? 'watch' : '');
  const tol = h.tolerance != null ? fmtTemp(h.tolerance, 0) : '1';
  const holdPct = h.hold ? h.hold.pct_within_tol : null;

  el.innerHTML = `
    <span class="micro">Health</span>
    <div class="lead"><span class="v num ${filterClass(pct)}">${Math.round(pct)}</span><span class="u">% filter life used</span></div>
    <div class="minibar"><i class="mb-filter ${barCls}" style="width:${pct}%"></i></div>
    <div class="row"><span>Filter changed</span><b>${fmtDaysAgo(filt.days_since)}</b></div>
    <div class="row"><span>Holding setpoint ±${tol}${DEG}</span><b class="${holdClass(holdPct)}"${holdPct != null && holdPct < 70 ? ' title="Pre-cool swings pull this down on purpose; low is expected while pre-cooling."' : ''}>${holdPct != null ? Math.round(holdPct) + '%' : '—'}</b></div>
    <button type="button" class="ghost-btn${due ? ' due' : ''}" data-action="filter-changed">Changed Filters</button>
  `;
}

/* Pre-cool chip state. When HA has pushed the toggle's actual state (the
   precool_state_bridge automation), that fact wins. Otherwise fall back to
   the old heuristic: track when precool_days last grew to distinguish an
   active trial from a paused one. Persisted so the wall display survives
   reloads. */
function precoolChip(pe, ha) {
  if (pe.ready) {
    const dollars = pe.dollars_saved_per_day;
    return { cls: 'ready', text: dollars != null ? `saves ~$${dollars.toFixed(2)}/day` : 'measured' };
  }
  if (ha) {
    if (!ha.enabled) return { cls: '', text: 'paused' };
    return { cls: 'active', text: `day ${pe.precool_days || 0} · measuring` };
  }
  const days = pe.precool_days || 0;
  let lastGrow = 0, lastVal = -1;
  try {
    const raw = JSON.parse(localStorage.getItem('hc_precool') || '{}');
    lastGrow = raw.ts || 0; lastVal = raw.days != null ? raw.days : -1;
  } catch (e) { /* ignore */ }
  const now = Date.now();
  if (days !== lastVal) { lastGrow = now; lastVal = days; } // any movement = activity (a reset restarts the clock too)
  else if (lastGrow === 0) { lastGrow = now; }
  try { localStorage.setItem('hc_precool', JSON.stringify({ days: lastVal, ts: lastGrow })); } catch (e) { /* ignore */ }

  const stalled = (now - lastGrow) > 3 * 24 * 3600 * 1000;
  if (days === 0 || stalled) return { cls: '', text: 'paused' };
  return { cls: 'active', text: `day ${days} · measuring` };
}

function renderLearningCard(t) {
  const el = document.getElementById('learning');
  if (!t || !t.available) {
    el.innerHTML = `<span class="micro">Learning</span><p class="loading">Waiting for data.</p>`;
    return;
  }
  const c = t.coasting || {}, l = t.load || {}, pe = t.precool_eval || {};

  /* Each projection keeps its full detail line — chips summarize, subs carry
     the numbers (the old Thermal panel's content, compressed but complete). */
  let coastChip, coastSub;
  if (c.ready) {
    coastChip = { cls: 'ready', text: `τ ≈ ${c.tau_hours}h` };
    coastSub = `Loses half a temperature gap in ~${c.half_life_hours}h · drifts ~${c.drift_per_hr_at_20f}${DEG}/hr when it's 20${DEG} hotter out.`;
  } else {
    coastChip = { cls: '', text: c.reason === 'no_signal' ? 'no signal yet' : 'collecting' };
    coastSub = `${c.samples || 0} idle-drift samples so far — no confident fit yet.`;
  }

  let loadChip, loadSub;
  if (l.ready) {
    loadChip = { cls: 'ready', text: `balance ≈ ${l.balance_point_f}${DEG}` };
    loadSub = `No AC needed below ${l.balance_point_f}${DEG} · runtime grows ~${l.slope_pct_per_f}% per ${DEG}F above it.`;
  } else {
    loadChip = { cls: '', text: l.reason === 'noisy' ? 'noisy data' : 'collecting' };
    loadSub = l.reason === 'noisy'
      ? `Runtime vs outdoor temp still too scattered to fit (${l.bins || 0} bins).`
      : `Needs a wider spread of outdoor temps (${l.bins || 0} bins).`;
  }

  const pc = precoolChip(pe, t.ha_precool);
  let pcSub;
  if (pe.ready) {
    const min = pe.peak_min_saved_per_day;
    pcSub = `Saves ~${min != null ? min : '—'} min of peak cooling on hot weekdays (weather-adjusted).`;
  } else {
    pcSub = `Comparing hot weekdays with pre-cool on vs off — have ${pe.precool_days || 0} pre-cool + ${pe.normal_days || 0} normal; needs a few of each.`;
  }

  const item = (label, chip, sub) =>
    `<div class="learn-row"><span>${label}</span><span class="st ${chip.cls}">${escapeHtml(chip.text)}</span></div>` +
    `<p class="learn-sub num">${sub}</p>`;

  const days = t.history_hours != null ? (t.history_hours / 24).toFixed(1) : null;
  el.innerHTML = `
    <div class="learn-head"><span class="micro">Learning</span>${days != null ? `<span class="micro num">${days}d of data</span>` : ''}</div>
    ${item('Warm-up speed', coastChip, coastSub)}
    ${item('Load vs weather', loadChip, loadSub)}
    ${item('Pre-cool trial', pc, pcSub)}
    <p class="note">The house is teaching the system. Each estimate appears once it's confident — no guesses.</p>
  `;
}

/* ---------------------------------------------------------------------- */
/* alerts strip                                                           */
/* ---------------------------------------------------------------------- */

function renderAlerts(list) {
  const el = document.getElementById('alerts');
  if (!Array.isArray(list) || list.length === 0) { el.innerHTML = ''; return; }
  el.innerHTML = list.map((a) => {
    const sev = (a.severity || 'warning').toLowerCase();
    const crit = sev === 'critical' || sev === 'crit';
    return `<div class="alert${crit ? ' crit' : ''}"><span class="sev">${escapeHtml(crit ? 'critical' : 'warning')}</span>` +
      `<span>${escapeHtml(a.message || a.key || 'Alert')}</span></div>`;
  }).join('');
}

/* ---------------------------------------------------------------------- */
/* status + refresh loop (per-panel failure isolation)                    */
/* ---------------------------------------------------------------------- */

function setStatus(n, ok) {
  const text = document.getElementById('status-text');
  if (!ok) { root.setAttribute('data-conn', 'down'); text.textContent = 'connection error'; return; }
  const hasData = n && 'indoor_temp_f' in n;
  if (!hasData) { root.setAttribute('data-conn', 'down'); text.textContent = 'no data'; return; }
  if (n.stale) { root.setAttribute('data-conn', 'down'); text.textContent = `stale · ${fmtAge(n.age_s)} old`; return; }
  root.setAttribute('data-conn', 'up');
  text.textContent = 'live';
}

async function refresh() {
  const jobs = {
    now: j('/api/now'),
    history: j('/api/history?range=24h'),
    cost: j('/api/cost/summary'),
    forecast: j('/api/forecast'),
    precool: j('/api/precool'),
    humidity: j('/api/humidity'),
    crawl: j(`/api/crawl?range=${crawlRange}`),
    rooms: j('/api/rooms'),
    air: j('/api/air'),
    timeline: j('/api/timeline'),
    anomalies: j('/api/anomalies'),
    runtime: j('/api/runtime?days=1'),
    health: j('/api/health'),
    thermal: j('/api/thermal'),
  };
  const keys = Object.keys(jobs);
  const settled = await Promise.allSettled(keys.map((k) => jobs[k]));
  const data = {};
  let connOk = false;
  keys.forEach((k, i) => {
    if (settled[i].status === 'fulfilled') { data[k] = settled[i].value; connOk = true; }
    else { data[k] = null; console.error(`house-climate: /api/${k} failed`, settled[i].reason); }
  });

  const now = data.now || {};
  setStatus(now, connOk);

  try { renderScene(now, data.rooms, data.air, data.humidity); } catch (e) { console.error('scene', e); }
  try { renderRail(data.cost, data.forecast, data.precool); } catch (e) { console.error('rail', e); }
  try { renderHumidity(data.humidity); } catch (e) { console.error('humidity', e); }
  try { renderCrawl(data.crawl); } catch (e) { console.error('crawl', e); }
  try { renderRibbon(data.history, data.timeline, data.cost); } catch (e) { console.error('ribbon', e); }
  try { renderRuntimeCard(data.runtime); } catch (e) { console.error('runtime', e); }
  try { renderHealthCard(data.health); } catch (e) { console.error('health', e); }
  try { renderLearningCard(data.thermal); } catch (e) { console.error('learning', e); }
  try { renderAlerts(data.anomalies); } catch (e) { console.error('alerts', e); }
}

/* "Changed Filters" — delegated (the button is re-rendered every refresh).
   A native are-you-sure prompt guards the click: logging a change resets the
   filter runtime clock, which is not undoable from the UI. */
async function markFilterChanged(btn) {
  if (!window.confirm('Log a filter change now? This resets the filter runtime clock to zero.')) {
    return;
  }
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const res = await fetch('/api/filter/changed', { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await refresh();
  } catch (err) {
    console.error('house-climate: filter reset failed', err);
    btn.disabled = false;
    btn.textContent = 'Try again';
  }
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-action="filter-changed"]');
  if (btn) markFilterChanged(btn);
  const tab = e.target.closest('[data-crawl-range]');
  if (tab) setCrawlRange(tab.getAttribute('data-crawl-range'));
});

/* Pause ambience when the tab is hidden — this runs for months on a wall. */
document.addEventListener('visibilitychange', () => {
  root.setAttribute('data-anim', document.hidden ? 'paused' : 'running');
});

/* ---------------------------------------------------------------------- */
/* F0: dashboard feature toggles                                          */
/* ---------------------------------------------------------------------- */
async function postJSON(url, body) {
  const r = await fetch(url, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${url} -> HTTP ${r.status}`);
  return r.json();
}

function applyFeatureVisibility(features) {
  for (const f of features) {
    const el = document.querySelector(`[data-feature="${f.key}"]`);
    if (el) el.classList.toggle('feat-off', !f.enabled);
  }
  // Collapse the hero grid so a hidden scene or cost rail leaves no empty column.
  const hero = document.querySelector('.hero-row');
  if (hero) {
    const on = (k) => {
      const el = document.querySelector(`[data-feature="${k}"]`);
      return el ? !el.classList.contains('feat-off') : false;
    };
    const scene = on('scene'), cost = on('cost');
    hero.classList.toggle('hero-solo', scene !== cost);
    hero.classList.toggle('hero-empty', !scene && !cost);
  }
}

function renderSettingsList(features) {
  const list = document.getElementById('settings-list');
  if (!list) return;
  list.innerHTML = '';
  for (const f of features) {
    const row = document.createElement('label');
    row.className = 'settings-row';
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = !!f.enabled; cb.dataset.key = f.key;
    const span = document.createElement('span');
    span.textContent = f.label;
    row.append(cb, span);
    cb.addEventListener('change', async () => {
      try {
        const res = await postJSON('/api/settings', { features: { [f.key]: cb.checked } });
        applyFeatureVisibility(res.features);
      } catch (e) {
        cb.checked = !cb.checked;   // revert on failure
      }
    });
    list.append(row);
  }
}

async function initSettings() {
  const btn = document.getElementById('btn-settings');
  const panel = document.getElementById('settings-panel');
  const close = document.getElementById('settings-close');
  if (!btn || !panel) return;
  const setOpen = (open) => {
    panel.hidden = !open;
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  btn.addEventListener('click', () => setOpen(panel.hidden));
  if (close) close.addEventListener('click', () => setOpen(false));
  document.addEventListener('click', (e) => {           // click-away closes
    if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) setOpen(false);
  });
  try {
    const data = await j('/api/settings');
    applyFeatureVisibility(data.features);
    renderSettingsList(data.features);
  } catch (e) { /* best-effort: dashboard still renders if settings fail */ }
}

/* ---------------------------------------------------------------------- */
/* boot                                                                   */
/* ---------------------------------------------------------------------- */

spawnAmbience();
initRibbonHover();
initCrawlHover();
initSettings();
tickClock();
setInterval(tickClock, 1000);
refresh();
setInterval(refresh, REFRESH_MS);
