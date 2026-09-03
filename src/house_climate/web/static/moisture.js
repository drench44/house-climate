'use strict';

/* Moisture case page. Fetches /api/moisture and renders the evidence:
   dew points, delta, source attribution, condensation, rainfall lags,
   thresholds, interventions, projection. Shares helpers via common.js.
   Every section renders its gate honestly when the data isn't there yet. */

const MO_REFRESH_MS = 5 * 60 * 1000;
let moData = null;

function moFmtDay(dayIso) {
  const d = new Date(`${dayIso}T12:00:00`);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function dpClass(v) {
  /* Dew point severity in a crawl: <55 comfortable, 55-60 elevated, >60 wet. */
  if (v == null) return '';
  if (v < 55) return 'v-ok';
  if (v <= 60) return 'v-watch';
  return 'v-out';
}

function deltaClass(v) {
  if (v == null) return '';
  if (v <= 0) return 'v-ok';
  if (v <= 3) return 'v-watch';
  return 'v-out';
}


/* ------------------------------------------------------------------ */
/* crawl-to-floor moisture gap + transport gain                        */
/* ------------------------------------------------------------------ */

const GAP_LINE_CLASSES = ['mo-gap-a', 'mo-gap-b', 'mo-gap-c', 'mo-gap-d'];

function gapClass(v) {
  /* Grams per cubic metre. Under 1 the crawl is barely wetter than the room;
     over 3 it is a different climate under the floor. Kept in step with
     crawlGapClass in app.js — a test pins them together. */
  if (v == null) return '';
  if (v < -1) return 'v-watch';
  if (v <= 1) return 'v-ok';
  if (v <= 3) return 'v-watch';
  return 'v-out';
}

/* Plain-language reason a transport number is not being shown. Each maps to a
   gate in analytics/coupling.py — the page never says "unavailable" without
   saying what it is waiting for. */
function couplingReason(c) {
  const need = c.need_days || 21;
  switch (c.reason) {
    case 'window_too_short':
      return `Needs about ${need} days of readings before this can be worked out. Collecting.`;
    case 'thin_coverage':
      return 'Too many hours missing from the '
           + { crawl: 'crawl', floor: 'indoor', outdoor: 'outdoor' }[c.thinnest]
           + ' readings to trust the answer. Collecting.';
    case 'outage':
      return 'There is a gap of more than a day in the readings. Waiting for a clean stretch.';
    case 'insufficient_n_eff':
      return 'One hour looks much like the last, so this stretch carries less information than its length suggests. Collecting.';
    case 'weak_signal':
      return 'The crawl is no longer getting damp on its own — it now follows the outside air closely. '
           + 'That means there is nothing left to trace into the house, which is a good sign, not a fault.';
    case 'straddles_intervention':
      return 'The window covers work done in the crawl, so before and after would be mixed together. '
           + 'Measuring them separately once there is enough of each.';
    case 'inconsistent_sign':
      return 'The numbers point the wrong way round, which is not physically possible. '
           + 'Something else is driving them, so no figure is shown.';
    case 'not_configured':
      return 'No indoor sensors are set up to compare against.';
    case 'not_computed':
      return 'Not worked out since the server last restarted. It appears on the next refresh.';
    case 'no_fit':
      return 'The sums could not be solved on this stretch of data \u2014 the readings are too '
           + 'alike to separate. Nothing is shown rather than a figure that would be noise.';
    case 'no_data':
      return 'No overlapping readings from the crawl and this floor yet. Collecting.';
    case 'collecting':
      return 'Not enough to go on yet. Collecting.';
    default:
      /* An unrecognised reason must not be dressed up as normal progress: a
         failed fit reported as "Collecting" reads as a healthy wait. */
      return `Not shown \u2014 the server reported "${reasonLabel(c.reason)}", which this page `
           + 'does not have an explanation for.';
  }
}

function reasonLabel(reason) {
  return escapeHtml(String(reason == null ? 'no reason given' : reason)).replace(/_/g, ' ');
}

/* The interval as the maths actually produced it. Clamping the lower end at
   zero would hide the fact that an interval reaching below zero is consistent
   with no crawl air arriving at all \u2014 on the page whose whole argument is
   that the uncertainty is shown honestly. */
function betaRange(beta, ci) {
  return `${Math.round((beta - ci) * 100)}&ndash;${Math.round((beta + ci) * 100)}%`;
}

function renderGapTiles(m) {
  const el = document.getElementById('mo-gap-tiles');
  const ah = m.ah;
  if (!el) return;
  /* Blank first: every branch below ends by assigning innerHTML, so a throw
     part-way would otherwise leave the previous poll's numbers on screen
     indefinitely, indistinguishable from current ones. */
  el.innerHTML = '';
  if (!ah || !ah.available) {
    el.innerHTML = `<p class="mo-read">${escapeHtml(couplingReason(ah || {}))}</p>`;
    return;
  }
  el.innerHTML = ah.floors.map((f) => {
    const t = f.gap_trend_7d;
    const arrow = t == null ? '' :
      (t < -0.1 ? '&darr; closing' : t > 0.1 ? '&uarr; widening' : '&rarr; steady');
    return `<div class="mo-dp">
      <span class="micro">crawl &minus; ${escapeHtml((f.name || '').toLowerCase())}</span>
      <span class="v num ${gapClass(f.gap_now)}">${f.gap_now == null ? '&mdash;'
        : (f.gap_now > 0 ? '+' : '') + f.gap_now.toFixed(2)}<span class="u">g/m&sup3;</span></span>
      <span class="micro">${arrow}${t == null ? '' :
        ` ${t > 0 ? '+' : ''}${t.toFixed(2)} vs last week`}</span>
    </div>`;
  }).join('');
}

function drawGapChart(m) {
  const svg = document.getElementById('mo-gap-chart');
  const legend = document.getElementById('mo-gap-legend');
  if (!svg) return;
  svg.innerHTML = '';
  if (legend) legend.innerHTML = '';
  const ah = m.ah;
  const W = 960, H = 170, padL = 40, padR = 40, top = 12, bot = 140, textY = 158;
  const floors = (ah && ah.available ? ah.floors : []).filter((f) => f.gap_daily.length >= 2);
  if (!floors.length) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 },
      'Gap history — collecting.');
    return;
  }
  const dayMs = (d) => new Date(`${d}T12:00:00`).getTime();
  let lo = Infinity, hi = -Infinity, winStart = Infinity, winEnd = -Infinity;
  floors.forEach((f) => f.gap_daily.forEach((g) => {
    lo = Math.min(lo, g.gap); hi = Math.max(hi, g.gap);
    const ms = dayMs(g.day);
    winStart = Math.min(winStart, ms); winEnd = Math.max(winEnd, ms);
  }));
  lo = Math.min(lo, 0); hi = Math.max(hi, 0.5);
  const pad = (hi - lo) * 0.1 || 0.5;
  lo -= pad; hi += pad;
  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart || 1), 0, 1) * (W - padL - padR);
  const yOf = (v) => bot - (v - lo) / (hi - lo) * (bot - top);

  gridLevels(lo, hi, 5).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v}`);
  });
  /* Zero matters here: below it the floor is damper than the crawl. */
  svgEl(svg, 'line', { class: 'mo-zero', x1: padL, y1: yOf(0), x2: W - padR, y2: yOf(0) });

  floors.forEach((f, i) => {
    const cls = GAP_LINE_CLASSES[i % GAP_LINE_CLASSES.length];
    const pts = f.gap_daily.map((g) => ({ ms: dayMs(g.day), v: g.gap }));
    svgEl(svg, 'path', { class: cls, d: timePath(pts, xOf, yOf, 864e5 * 2.5) });
    if (legend) {
      legend.insertAdjacentHTML('beforeend',
        `<span class="li"><span class="sw ${cls}-sw"></span>crawl &minus; ${escapeHtml((f.name || '').toLowerCase())}</span>`);
    }
  });
  moTimeTicks(svg, xOf, winStart, winEnd, bot + 4, bot + 9, textY);
  moMarkers(svg, xOf, winStart, winEnd, top, bot);
}

/* An `unchecked` change cleared the day-to-day noise but could not be tested
   against the outside air — usually because outdoor readings do not reach far
   enough back. Letting it fall through to the "within the noise" wording said
   the change did NOTHING, which is the opposite of what was measured. */
function gapVerdictText(verdict) {
  switch (verdict) {
    case 'real':
      return 'bigger than the day-to-day noise';
    case 'confounded':
      return 'but the outside air moved with it, so this is not proof';
    case 'unchecked':
      return 'bigger than the day-to-day noise, but there was not enough outdoor '
           + 'data either side to rule out the season, so this is not yet proof';
    case 'noise':
      return 'within the day-to-day noise';
    default:
      return 'not yet possible to judge';
  }
}

function renderGapLead(m) {
  const el = document.getElementById('mo-gap-lead');
  if (!el) return;
  /* Blank first: every branch below ends by assigning innerHTML, so a throw
     part-way would otherwise leave the previous poll's numbers on screen
     indefinitely, indistinguishable from current ones. */
  el.innerHTML = '';
  const ah = m.ah;
  if (!ah || !ah.available) { el.innerHTML = ''; return; }
  const parts = [];
  const ivs = [];
  ah.floors.forEach((f) => (f.interventions || []).forEach((iv) => {
    const mm = iv.metric || {};
    if (mm.verdict === 'collecting' || mm.diff == null) return;
    ivs.push(`<li><b>${escapeHtml(f.name)}</b> after ${escapeHtml(iv.label)}: `
      + `${mm.diff > 0 ? 'widened' : 'closed'} by ${Math.abs(mm.diff).toFixed(2)} g/m&sup3;`
      + (mm.ci95 != null ? ` (&plusmn;${mm.ci95.toFixed(2)})` : '')
      + ` &mdash; ${gapVerdictText(mm.verdict)}.</li>`);
  }));
  if (ivs.length) {
    parts.push(`<p class="mo-read">Since the work was marked (first ${ah.settle_days} days `
      + 'left out while things settle):</p>'
      + `<ul class="mo-list">${ivs.join('')}</ul>`);
  }
  parts.push('<p class="mo-read micro">A closing gap and a widening gap can both mean the work '
    + 'succeeded &mdash; sealing the ground dries the crawl and closes it, while sealing the air '
    + 'path separates the floors and widens it. That is why no direction is called good here. '
    + 'The reading below is the one that tells you whether the air is actually moving.</p>');
  el.innerHTML = parts.join('');
}

function renderCoupling(m) {
  const el = document.getElementById('mo-coupling');
  if (!el) return;
  /* Blank first: every branch below ends by assigning innerHTML, so a throw
     part-way would otherwise leave the previous poll's numbers on screen
     indefinitely, indistinguishable from current ones. */
  el.innerHTML = '';
  const ah = m.ah;
  if (!ah || !ah.available) {
    el.innerHTML = `<p class="mo-read">${escapeHtml(couplingReason(ah || {}))}</p>`;
    return;
  }
  const rows = ah.floors.map((f) => {
    const c = f.coupling || {};
    if (!c.ready) {
      return `<li><b>${escapeHtml(f.name)}</b> &mdash; ${escapeHtml(couplingReason(c))}</li>`;
    }
    const pct = Math.round(c.beta * 100);
    const lag = c.lag === 0 ? 'within the hour' : `about ${c.lag} hour${c.lag === 1 ? '' : 's'} later`;
    const strength = !c.significant ? 'too small to separate from nothing at all'
      : pct >= 40 ? 'a large share' : pct >= 15 ? 'a meaningful share' : 'a small but real share';
    return `<li><b>${escapeHtml(f.name)}</b> &mdash; when the crawl gets damper on its own, `
      + `<b class="${gapClass(c.beta * 5)}">${pct}%</b> of it turns up here, ${lag} `
      + `(${betaRange(c.beta, c.ci95)}). That is ${strength}.`
      + `<span class="micro"> Based on ${c.days} days; because one hour looks much like the next, `
      + `that is worth about ${c.n_eff} independent readings, and the range above already allows for it.</span></li>`;
  });
  const stackRows = ah.floors.map((f) => {
    const st = f.stack || {};
    /* The temperature check is what separates real air movement from both
       sensors following the local weather. If its row silently disappeared,
       the percentage above would read as settled while the method note below
       still pointed at a guard that was not there. */
    if (!st.ready || st.delta == null) {
      return `<li class="micro"><b>${escapeHtml(f.name)}</b>: the temperature check that `
        + 'separates moving air from both sensors just following the weather cannot run yet '
        + `&mdash; ${escapeHtml(couplingReason(st))} Treat the figure above as unconfirmed.</li>`;
    }
    const solid = st.delta - st.ci95 > 0;
    return `<li class="micro"><b>${escapeHtml(f.name)}</b>: `
      + (solid
        ? 'the link gets stronger as the temperature difference between inside and outside grows, '
          + 'which is what air genuinely moving up through the building looks like.'
        : 'the link does not change with the temperature difference between inside and outside, '
          + 'so some of it may just be both sensors following the local weather.')
      + '</li>';
  });

  const cons = ah.consistency || {};
  el.innerHTML = `<ul class="mo-list">${rows.join('')}</ul>`
    + (stackRows.length ? `<ul class="mo-list">${stackRows.join('')}</ul>` : '')
    + (cons.text ? `<p class="mo-read ${cons.verdict === 'bypass_suspected' ? 'v-out' : ''}">`
        + `${escapeHtml(cons.text)}</p>` : '');
}

function renderPrediction(m) {
  const el = document.getElementById('mo-prediction');
  if (!el) return;
  /* Blank first: every branch below ends by assigning innerHTML, so a throw
     part-way would otherwise leave the previous poll's numbers on screen
     indefinitely, indistinguishable from current ones. */
  el.innerHTML = '';
  const ah = m.ah;
  if (!ah || !ah.available) { el.innerHTML = ''; return; }
  const rows = [];
  ah.floors.forEach((f) => (f.prediction || []).forEach((p) => {
    if (p.verdict === 'collecting') {
      rows.push(`<li><b>${escapeHtml(f.name)}</b> after ${escapeHtml(p.label)} &mdash; `
        + 'not enough settled days on both sides of the work yet. Collecting.</li>');
      return;
    }
    if (p.verdict === 'inconclusive') {
      /* An unmeasurable margin is not a failed prediction. Reporting it as one
         would print a confident "the crawl is not the source" from a
         measurement that was never made. */
      rows.push(`<li><b>${escapeHtml(f.name)}</b> after ${escapeHtml(p.label)} &mdash; `
        + 'the readings either side are too flat to put a margin on, so this cannot be '
        + 'called either way. Not evidence for or against.</li>');
      return;
    }
    const ok = p.verdict === 'confirmed';
    rows.push(`<li><b>${escapeHtml(f.name)}</b> after ${escapeHtml(p.label)} &mdash; `
      + `expected this floor to dry out by ${Math.abs(p.predicted).toFixed(2)} g/m&sup3; `
      + `(&plusmn;${p.ci95.toFixed(2)}); it actually changed by ${Math.abs(p.observed).toFixed(2)}. `
      + `<b class="${ok ? 'v-ok' : 'v-out'}">${ok
        ? 'That matches, which is the strongest evidence on this page that crawl air reaches this floor.'
        : 'That does not match, so the crawl is not the main source of moisture here.'}</b></li>`);
  }));
  el.innerHTML = rows.length
    ? `<p class="mo-read"><b>The test:</b> predict how much drier each floor should get, then check it.</p>`
      + `<ul class="mo-list">${rows.join('')}</ul>`
    : '';
}

/* Which outdoor reading everything is measured against. This is not a detail:
   a vented crawl is effectively a weather station under the house, so if the
   outdoor reference is a station some distance away, the crawl can appear to
   drive the indoor air purely by knowing the local weather better. */
function outdoorSourceNote(ah) {
  const src = ah.outdoor_source;
  if (!src || !src.source) {
    return 'Which outdoor reading these figures are measured against could not be '
      + 'determined, so treat the comparison below with caution.';
  }
  if (src.ignored_sensor) {
    const why = src.reason === 'stale'
      ? `has not reported for ${src.stale_days} days`
      : `has only ${src.sensor_days} full days of readings so far (${src.need_days} needed)`;
    return `An outdoor sensor at the house (${escapeHtml(src.ignored_sensor)}) is set up but `
      + `${why}, so these figures fall back to the weather feed. That is the safer of the two `
      + 'while it settles: a sensor that is barely reporting would empty these comparisons '
      + 'rather than improve them.';
  }
  if (src.source === 'sensor') {
    return 'The gap, excess and transport figures on this page are measured against a sensor '
      + 'at the house'
      + (src.name ? ` (${escapeHtml(src.name)})` : '')
      + ', which is what makes them trustworthy — a vented crawl is effectively a weather '
      + 'station under the floor, and measuring it against a distant station would let it look '
      + 'influential simply by knowing the local weather better. (Rainfall, the condensation '
      + 'history and the winter projection still come from the weather feed.)';
  }
  return 'Outdoor conditions come from a weather feed rather than a sensor at the house. '
    + 'A vented crawl is effectively a weather station under the floor, so it can appear to '
    + 'drive the indoor air simply by knowing the local weather better than a distant station '
    + 'does. The temperature-difference check above is the guard against that; an outdoor '
    + 'sensor at the house would settle it properly.';
}

function renderCouplingMethod(m) {
  const el = document.getElementById('mo-coupling-method');
  if (!el) return;
  /* Blank first: every branch below ends by assigning innerHTML, so a throw
     part-way would otherwise leave the previous poll's numbers on screen
     indefinitely, indistinguishable from current ones. */
  el.innerHTML = '';
  const ah = m.ah || {};
  el.innerHTML = `
    <p class="mo-read micro">Every sensor in and under the house follows the weather, so two
    damp-looking charts prove nothing on their own. Each reading here is first measured against
    its own trailing week, which strips out the slow drift of the seasons, and against its own
    time of day, which strips out the daily rhythm of showers, cooking and the air handler.
    What is left is the crawl getting damp for its own reasons &mdash; and the question is how
    much of that shows up upstairs.</p>
    <p class="mo-read micro">Readings an hour apart are not independent, so a month of them
    carries far less information than its length suggests. Every range shown is widened to
    account for that, and the honest count is printed next to it${ah.window_days
      ? ` (window: ${ah.window_days} days)` : ''}.</p>
    <p class="mo-read micro"><b>What could still fool this:</b> ${outdoorSourceNote(ah)}
    Hours where the crawl sensor reads 95% humidity or above are left out, because sensors stop
    being accurate there. A bedroom door closed against the hallway will reduce what actually
    reaches the room below what the hallway sensor sees.</p>`;
}

/* ------------------------------------------------------------------ */
/* verdict strip                                                       */
/* ------------------------------------------------------------------ */

function renderVerdicts(m) {
  const el = document.getElementById('mo-verdicts');
  const items = [];

  const attr = m.attribution && m.attribution.verdict;
  items.push({
    k: 'Source', cls: attr ? (attr.source === 'soil' ? 'bad' : attr.source === 'mixed' ? 'watch' : 'info') : '',
    text: attr ? attr.text
      : `Not yet attributable — needs ${m.attribution && m.attribution.r7 && m.attribution.r7.need
        ? m.attribution.r7.need : 96}+ overlapping hours and real outdoor dew-point movement. Collecting.`,
  });

  const rv = m.rain && m.rain.verdict;
  items.push({
    k: 'Rain', cls: rv ? (rv[0] === 'rain_driven' ? 'bad' : rv[0] === 'weak' ? 'watch' : 'ok') : '',
    text: rv ? rv[1]
      : (m.rain && m.rain.reason === 'no_rain_yet'
        ? `No verdict possible yet — only ${m.rain.wet_days} wet day${m.rain.wet_days === 1 ? '' : 's'} observed; the drainage question needs ${m.rain.need_wet || 3}+ real rains. Collecting.`
        : `Collecting — needs ${(m.rain && m.rain.need_days) || 10}+ overlapping days of rain and crawl data.`),
  });

  const c = m.condensation || {};
  const condBad = (c.hours_7d || 0) > 0 || (c.duct_hours_7d || 0) > 0;
  items.push({
    k: 'Wetting', cls: condBad ? 'bad' : 'ok',
    text: condBad
      ? `${c.hours_7d}h of condensation-risk conditions in the last 7 days` +
        ((c.duct_hours_7d || 0) > 0 ? `, plus ~${c.duct_hours_7d}h of likely duct sweat while the AC ran (assumes ${Math.round(c.assumed_duct_f)}${DEG}F duct surface)` : '') +
        ' — surfaces are getting wet.'
      : 'No condensation-risk hours in the last 7 days — surfaces at air temperature are staying dry.',
  });

  el.innerHTML = `<div class="panel-head"><span class="micro" style="color:var(--dim)">Findings</span>` +
    `<span class="micro right">each line appears only when its data supports it</span></div>` +
    items.map((i) => `<div class="mo-verdict ${i.cls}"><span class="mo-vk">${i.k}</span><span>${escapeHtml(i.text)}</span></div>`).join('');
}

/* ------------------------------------------------------------------ */
/* now row                                                             */
/* ------------------------------------------------------------------ */

function renderNow(m) {
  const el = document.getElementById('mo-now');
  const dp = m.dp_now || {};
  const refName = dp.reference_name || 'Indoor';
  const chip = (label, v, cls) =>
    `<div class="mo-dp"><span class="micro">${escapeHtml(label)}</span>` +
    `<span class="v num ${cls || ''}">${fmtTemp(v, 1)}<span class="u">${DEG}</span></span></div>`;
  const d = m.delta ? m.delta.now : null;
  el.innerHTML = `<div class="mo-now-row num">
    ${chip('Crawl space', dp.crawl, dpClass(dp.crawl))}
    ${chip(refName, dp.reference)}
    ${chip('Thermostat', dp.thermostat)}
    ${chip('Outdoor', dp.outdoor)}
    <div class="mo-dp mo-delta-big"><span class="micro">Crawl &minus; ${escapeHtml(refName.toLowerCase())}</span>
      <span class="v num ${deltaClass(d)}">${d != null && d > 0 ? '+' : ''}${fmtTemp(d, 1)}<span class="u">${DEG}</span></span>
      <span class="mo-dp-sub">${d == null ? '' : d > 0 ? 'crawl is wetter — moisture drives upward' : d < 0 ? 'house is wetter than the crawl' : 'even — no gradient either way'}</span>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------ */
/* charts                                                              */
/* ------------------------------------------------------------------ */

const MO_HOUR_MS = 3600e3;

function moTimeTicks(svg, xOf, winStart, winEnd, y1, y2, textY) {
  const t = new Date(winStart);
  t.setHours(0, 0, 0, 0);
  if (t.getTime() < winStart) t.setDate(t.getDate() + 1);
  const dayCount = Math.round((winEnd - winStart) / 864e5);
  const step = dayCount > 12 ? Math.ceil(dayCount / 8) : 1;
  let i = 0;
  for (const d = new Date(t); d.getTime() <= winEnd; d.setDate(d.getDate() + 1), i++) {
    if (i % step !== 0) continue;
    svgEl(svg, 'line', { class: 'rb-tick', x1: xOf(d.getTime()), y1, x2: xOf(d.getTime()), y2 });
    svgEl(svg, 'text', { class: 'rb-hour', x: xOf(d.getTime()), y: textY, 'text-anchor': 'middle' },
      d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }));
  }
}

function moMarkers(svg, xOf, winStart, winEnd, top, bot) {
  (moData.interventions || []).forEach((iv) => {
    let ms = new Date(`${iv.marked_on}T00:00:00`).getTime();
    // A marker at midnight of the window's FIRST day sits just before the
    // first hourly bucket — clamp it into view instead of dropping the
    // before/after divider exactly when it matters most.
    if (ms < winStart && ms + 864e5 > winStart) ms = winStart;
    if (ms < winStart || ms > winEnd) return;
    svgEl(svg, 'line', { class: 'mo-marker', x1: xOf(ms), y1: top, x2: xOf(ms), y2: bot });
    svgEl(svg, 'text', { class: 'mo-marker-label', x: xOf(ms) + 4, y: top + 10 }, iv.label);
  });
}

function drawDeltaChart(m) {
  const svg = document.getElementById('mo-delta-chart');
  svg.innerHTML = '';
  const W = 960, H = 130, padL = 38, padR = 14, top = 8, bot = 100, textY = 122;
  const series = (m.delta && m.delta.series) || [];
  const pts = series.filter((s) => s.delta != null)
    .map((s) => ({ ms: new Date(s.ts).getTime(), v: s.delta }));
  if (pts.length < 2) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 },
      'Delta history — needs the downstairs and crawl sensors reporting together. Collecting.');
    return;
  }
  const winStart = pts[0].ms, winEnd = pts[pts.length - 1].ms;
  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart || 1), 0, 1) * (W - padL - padR);
  const vals = pts.map((p) => p.v);
  let lo = Math.floor(Math.min(0, ...vals) - 1), hi = Math.ceil(Math.max(2, ...vals) + 1);
  if (hi - lo < 6) hi = lo + 6;
  const yOf = (v) => bot - (v - lo) / (hi - lo) * (bot - top);

  // shade the positive (bad) region faintly
  if (hi > 0) {
    svgEl(svg, 'rect', { class: 'mo-pos-zone', x: padL, y: yOf(hi),
      width: W - padL - padR, height: Math.max(0, yOf(Math.max(0, lo)) - yOf(hi)) });
  }
  svgEl(svg, 'line', { class: 'mo-zero', x1: padL, y1: yOf(0), x2: W - padR, y2: yOf(0) });
  svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(0) + 3 }, '0');
  gridLevels(lo, hi, 3).filter((v) => v !== 0).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v > 0 ? '+' : ''}${v}${DEG}`);
  });
  svgEl(svg, 'path', { class: 'mo-delta-line', d: timePath(pts, xOf, yOf, MO_HOUR_MS * 2.5) });
  const last = pts[pts.length - 1];
  svgEl(svg, 'circle', { cx: xOf(last.ms), cy: yOf(last.v), r: 3.5, fill: last.v > 0 ? 'var(--warn)' : 'var(--mint)' });
  moTimeTicks(svg, xOf, winStart, winEnd, bot + 4, bot + 9, textY);
  moMarkers(svg, xOf, winStart, winEnd, top, bot);

  const leadEl = document.getElementById('mo-delta-lead');
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  leadEl.innerHTML = `<p class="mo-read num">7-day mean <b class="${deltaClass(mean)}">${mean > 0 ? '+' : ''}${mean.toFixed(1)}${DEG}</b> — ` +
    `${mean > 1 ? 'the crawl consistently holds more moisture than the living space; the gradient (and the odor path) points up into the house.'
      : mean > 0 ? 'the crawl runs slightly wetter than the living space.'
        : 'the crawl is currently drier than the living space — no upward moisture gradient.'}</p>`;
}

function drawAttrChart(m) {
  const svg = document.getElementById('mo-attr-chart');
  svg.innerHTML = '';
  const W = 960, H = 150, padL = 38, padR = 14, top = 8, bot = 118, textY = 142;
  const series = (m.delta && m.delta.series) || [];
  const crawlP = series.filter((s) => s.crawl != null).map((s) => ({ ms: new Date(s.ts).getTime(), v: s.crawl }));
  const outP = series.filter((s) => s.outdoor != null).map((s) => ({ ms: new Date(s.ts).getTime(), v: s.outdoor }));
  const refP = series.filter((s) => s.indoor != null).map((s) => ({ ms: new Date(s.ts).getTime(), v: s.indoor }));
  const all = crawlP.concat(outP, refP);
  if (all.length < 4) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'Overlay — collecting.');
    return;
  }
  const winStart = Math.min(...all.map((p) => p.ms)), winEnd = Math.max(...all.map((p) => p.ms));
  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart || 1), 0, 1) * (W - padL - padR);
  const vals = all.map((p) => p.v);
  let lo = Math.floor(Math.min(...vals) - 2), hi = Math.ceil(Math.max(...vals) + 2);
  if (hi - lo < 8) hi = lo + 8;
  const yOf = (v) => bot - (v - lo) / (hi - lo) * (bot - top);
  gridLevels(lo, hi, 4).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v}${DEG}`);
  });
  const gap = MO_HOUR_MS * 2.5;
  if (refP.length >= 2) svgEl(svg, 'path', { class: 'mo-ref-line', d: timePath(refP, xOf, yOf, gap) });
  if (outP.length >= 2) svgEl(svg, 'path', { class: 'mo-out-line', d: timePath(outP, xOf, yOf, gap) });
  if (crawlP.length >= 2) svgEl(svg, 'path', { class: 'cz-avg', d: timePath(crawlP, xOf, yOf, gap) });
  moTimeTicks(svg, xOf, winStart, winEnd, bot + 4, bot + 9, textY);
  moMarkers(svg, xOf, winStart, winEnd, top, bot);

  const readEl = document.getElementById('mo-attr-read');
  const a = m.attribution || {};
  const rChip = (label, w) => {
    if (!w) return '';
    if (w.ready) return `<span class="cs-chip"><span class="cl">${label}</span><b>r=${w.r > 0 ? '+' : ''}${w.r}</b><span class="mo-chipsub">${w.n}h</span></span>`;
    const why = w.reason === 'outdoor_flat' ? 'outdoor dp too flat' : `${w.n || 0}/${w.need || '?'}h`;
    return `<span class="cs-chip"><span class="cl">${label}</span><b class="mo-dim">collecting</b><span class="mo-chipsub">${escapeHtml(why)}</span></span>`;
  };
  readEl.innerHTML = `<div class="cs-chips num">${rChip('7-day r', a.r7)}${rChip('30-day r', a.r30)}</div>` +
    (a.verdict ? `<p class="mo-read">${escapeHtml(a.verdict.text)}</p>`
      : `<p class="mo-read mo-dim">Attribution readout appears once a correlation window has enough overlapping hours (and outdoor dew point has actually moved). ` +
        `Correlation near +1 &rarr; ventilation air dominates; near 0 &rarr; soil vapor dominates.</p>`);
}

function drawCondChart(m) {
  const svg = document.getElementById('mo-cond-chart');
  svg.innerHTML = '';
  const W = 960, H = 130, padL = 38, padR = 14, top = 10, bot = 100, textY = 122;
  const days = ((m.condensation && m.condensation.days) || []).slice(-30);
  if (!days.length) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'Condensation history — collecting.');
    return;
  }
  const hi = Math.max(4, ...days.map((d) => d.hours + d.duct_hours));
  const yOf = (v) => bot - v / hi * (bot - top);
  const bw = (W - padL - padR) / days.length;
  gridLevels(0, hi + 1, 3).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: yOf(v), x2: W - padR, y2: yOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: yOf(v) + 3 }, `${v}h`);
  });
  days.forEach((d, i) => {
    const x = padL + i * bw + bw * 0.15;
    const w = bw * 0.7;
    if (d.hours > 0) svgEl(svg, 'rect', { class: 'mo-cond-bar', x, y: yOf(d.hours), width: w, height: yOf(0) - yOf(d.hours) });
    if (d.duct_hours > 0) svgEl(svg, 'rect', { class: 'mo-duct-bar', x, y: yOf(d.hours + d.duct_hours), width: w, height: yOf(0) - yOf(d.duct_hours) });
    if (i % Math.ceil(days.length / 8) === 0) {
      svgEl(svg, 'text', { class: 'rb-hour', x: x + w / 2, y: textY, 'text-anchor': 'middle' }, moFmtDay(d.day));
    }
  });
  const c = m.condensation;
  document.getElementById('mo-cond-read').innerHTML =
    `<p class="mo-read num">Last 7 days: <b class="${c.hours_7d > 0 ? 'v-out' : 'v-ok'}">${c.hours_7d}h</b> under a ${c.spread_f}${DEG}F spread` +
    `${c.duct_hours_7d > 0 ? ` + <b class="v-watch">${c.duct_hours_7d}h</b> probable duct sweat (amber; assumes ${Math.round(c.assumed_duct_f)}${DEG}F duct surface while cooling — not measured)` : ''}. ` +
    `Hours at risk map directly to wood staying wet.</p>`;
}

function drawRainChart(m) {
  const svg = document.getElementById('mo-rain-chart');
  svg.innerHTML = '';
  const W = 960, H = 150, padL = 38, padR = 40, top = 10, bot = 116, textY = 140;
  const rainDays = (m.rain && m.rain.days) || [];
  const daily = m.daily || [];
  if (!rainDays.length && daily.length < 2) {
    svgEl(svg, 'text', { class: 'rb-empty', x: padL, y: H / 2 }, 'Rainfall history — collecting.');
    return;
  }
  const dayMs = (iso) => new Date(`${iso}T12:00:00`).getTime();
  const allDays = rainDays.map((d) => dayMs(d.day)).concat(daily.map((d) => dayMs(d.day)));
  const winStart = Math.min(...allDays) - 432e5, winEnd = Math.max(...allDays) + 432e5;
  const xOf = (ms) => padL + clamp((ms - winStart) / (winEnd - winStart || 1), 0, 1) * (W - padL - padR);
  const bw = Math.max(4, (W - padL - padR) / Math.max(1, (winEnd - winStart) / 864e5) * 0.6);

  // rain bars, left axis
  const rHi = Math.max(0.5, ...rainDays.map((d) => d.inches || 0));
  const ryOf = (v) => bot - v / rHi * (bot - top);
  [0.5, 1, 1.5, 2].filter((v) => v <= rHi).forEach((v) => {
    svgEl(svg, 'line', { class: 'hs-grid', x1: padL, y1: ryOf(v), x2: W - padR, y2: ryOf(v) });
    svgEl(svg, 'text', { class: 'hs-label', x: 4, y: ryOf(v) + 3 }, `${v}"`);
  });
  rainDays.forEach((d) => {
    if (!d.inches) return;
    const x = xOf(dayMs(d.day));
    svgEl(svg, 'rect', { class: d.source === 'station' ? 'mo-rain-bar' : 'mo-rain-bar est', x: x - bw / 2, y: ryOf(d.inches), width: bw, height: ryOf(0) - ryOf(d.inches) });
  });

  // crawl dp line, right axis
  const dpP = daily.filter((d) => d.dp_mean != null).map((d) => ({ ms: dayMs(d.day), v: d.dp_mean }));
  if (dpP.length >= 2) {
    let dLo = Math.floor(Math.min(...dpP.map((p) => p.v)) - 2), dHi = Math.ceil(Math.max(...dpP.map((p) => p.v)) + 2);
    if (dHi - dLo < 6) dHi = dLo + 6;
    const dyOf = (v) => bot - (v - dLo) / (dHi - dLo) * (bot - top);
    svgEl(svg, 'path', { class: 'cz-avg', d: timePath(dpP, xOf, dyOf, 864e5 * 2.5) });
    gridLevels(dLo, dHi, 2).forEach((v) => {
      svgEl(svg, 'text', { class: 'cz-templabel', x: W - padR + 6, y: dyOf(v) + 3 }, `${v}${DEG}`);
    });
  }
  moTimeTicks(svg, xOf, winStart, winEnd, bot + 4, bot + 9, textY);
  moMarkers(svg, xOf, winStart, winEnd, top, bot);

  const readEl = document.getElementById('mo-rain-read');
  const r = m.rain || {};
  let lagTable = '';
  if (Array.isArray(r.lags)) {
    lagTable = `<div class="mo-lags num">` + r.lags.map((l) =>
      `<span class="cs-chip${r.best && r.best.lag === l.lag && r.ready ? ' mo-best' : ''}">` +
      `<span class="cl">lag ${l.lag}d</span><b>${l.r == null ? '—' : (l.r > 0 ? '+' : '') + l.r}</b>` +
      `<span class="mo-chipsub">${l.n}d</span></span>`).join('') + `</div>`;
  }
  const verdict = r.verdict
    ? `<p class="mo-read">${escapeHtml(r.verdict[1])}</p>`
    : `<p class="mo-read mo-dim">${r.reason === 'no_rain_yet'
      ? `Only ${r.wet_days || 0} wet day${(r.wet_days || 0) === 1 ? '' : 's'} on record — the correlation stays unscored until ${r.need_wet || 3}+ real rains have been observed. No verdict is better than a fake one.`
      : `Needs ${r.need_days || 10}+ overlapping days of rainfall and crawl data before scoring. Collecting.`}</p>`;
  readEl.innerHTML = lagTable + verdict +
    `<p class="mo-foot-note">Rain source: solid bars = the house's own gauge; hollow = Open-Meteo gridded backfill (pre-station days only).</p>`;
}

/* ------------------------------------------------------------------ */
/* thresholds, interventions, projection, daily table                  */
/* ------------------------------------------------------------------ */

function renderThresholds(m) {
  const el = document.getElementById('mo-thresholds');
  const t = m.thresholds || { weeks: [], months: [] };
  const table = (rows, label) => {
    if (!rows.length) return '';
    const tr = rows.map((r) => {
      const covPct = r.obs_h > 0 ? Math.min(100, Math.round(r.obs_h / (label === 'Week' ? 168 : 720) * 100)) : 0;
      return `<tr><td>${escapeHtml(r.period)}</td>` +
        `<td class="${r.h60 > 0 ? 'v-watch' : 'v-ok'}">${r.h60}h</td>` +
        `<td class="${r.h70 > 0 ? 'v-out' : 'v-ok'}">${r.h70}h</td>` +
        `<td class="${r.h80 > 0 ? 'v-out' : 'v-ok'}">${r.h80}h</td>` +
        `<td class="mo-dim">${r.obs_h}h observed${covPct < 95 ? ` (${covPct}% of the ${label.toLowerCase()})` : ''}</td></tr>`;
    }).join('');
    return `<table class="mo-table num"><thead><tr><th>${label}</th><th>&gt;60%</th><th>&gt;70%</th><th>&gt;80%</th><th>coverage</th></tr></thead><tbody>${tr}</tbody></table>`;
  };
  el.innerHTML = `<div class="mo-twocol">${table(t.weeks, 'Week')}${table(t.months, 'Month')}</div>`;
}

function ivMetricRow(name, mm) {
  const d = mm.diff;
  const fmt = (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v}`);
  let verdict;
  if (mm.verdict === 'real') verdict = `<b class="${d < 0 ? 'v-ok' : 'v-out'}">real ${d < 0 ? 'improvement' : 'worsening'}</b>`;
  else if (mm.verdict === 'noise') verdict = `<span class="mo-dim">within noise</span>`;
  else verdict = `<span class="mo-dim">collecting (${mm.baseline_n}+${mm.post_n} days, need ${10} each)</span>`;
  return `<tr><td>${name}</td>` +
    `<td>${mm.baseline_mean != null ? mm.baseline_mean : '—'}<span class="mo-chipsub"> ±${mm.baseline_sd != null ? mm.baseline_sd : '—'} · n=${mm.baseline_n}</span></td>` +
    `<td>${mm.post_mean != null ? mm.post_mean : '—'}<span class="mo-chipsub"> ±${mm.post_sd != null ? mm.post_sd : '—'} · n=${mm.post_n}</span></td>` +
    `<td>${fmt(d)}${mm.ci95 != null ? `<span class="mo-chipsub"> ±${mm.ci95}</span>` : ''}</td>` +
    `<td>${verdict}</td></tr>`;
}

function renderInterventions(m) {
  const el = document.getElementById('mo-interventions');
  const ivs = m.interventions || [];
  if (!ivs.length) {
    el.innerHTML = `<p class="mo-read mo-dim">No interventions marked yet. When work happens (vapor barrier, sealing, drainage), mark the date below — ` +
      `the period before it freezes as the baseline, and every metric gets a before/after comparison with honest sample sizes.</p>`;
    return;
  }
  el.innerHTML = ivs.map((iv) => {
    const badge = iv.overall === 'real_change' ? '<span class="mo-badge real">real change</span>'
      : iv.overall === 'no_change_detected' ? '<span class="mo-badge">no change detected</span>'
        : '<span class="mo-badge">collecting</span>';
    return `<div class="mo-iv num">
      <div class="mo-iv-head"><b>${escapeHtml(iv.label)}</b>
        <span class="mo-dim">${moFmtDay(iv.marked_on)}</span>${badge}
        <span class="mo-dim">baseline ${iv.baseline_from ? `${moFmtDay(iv.baseline_from)} &rarr; ${moFmtDay(iv.marked_on)}` : '—'} (${iv.baseline_days}d) &middot; after: ${iv.post_days}d</span>
        <button type="button" class="mo-iv-del" data-iv-del="${iv.id}" title="Remove this marker">&times;</button>
      </div>
      ${iv.note ? `<p class="mo-dim mo-iv-note">${escapeHtml(iv.note)}</p>` : ''}
      <table class="mo-table num"><thead><tr><th>metric</th><th>before</th><th>after</th><th>change</th><th>call</th></tr></thead><tbody>
        ${ivMetricRow('RH mean (%)', iv.metrics.rh_mean)}
        ${ivMetricRow(`dew point mean (${DEG}F)`, iv.metrics.dp_mean)}
        ${ivMetricRow('h/day above 60%', iv.metrics.h60_per_day)}
        ${ivMetricRow('h/day above 70%', iv.metrics.h70_per_day)}
      </tbody></table>
    </div>`;
  }).join('');
}

function renderProjection(m) {
  const el = document.getElementById('mo-projection');
  const p = m.projection || {};
  if (!p.ready) {
    const why = p.reason === 'narrow_temp_range'
      ? `the data so far only spans ${p.temp_span_f}${DEG}F of outdoor temperature (needs ${p.need_span_f}${DEG}F — cooler weather must arrive first)`
      : `${p.n_days || 0} of ${p.need_days} days of paired daily means collected`;
    el.innerHTML = `<p class="mo-read mo-dim">A winter projection would be a guess right now: ${why}. ` +
      `The fit (crawl dew point vs outdoor temp + dew point) will appear here with a confidence interval once it is actually supported.</p>`;
    return;
  }
  el.innerHTML = `<p class="mo-read num">At a typical cold-winter design point (${Math.round(p.at_outdoor_temp_f)}${DEG}F, dew point ${Math.round(p.at_outdoor_dp_f)}${DEG}F), ` +
    `expected crawl dew point: <b class="${dpClass(p.predicted_dp_f)}">${p.predicted_dp_f}${DEG}F</b> ` +
    `&plusmn; ${p.ci95_f}${DEG} (95% PI, ${p.n_days} days fitted, outdoor span ${p.temp_span_f}${DEG}F).</p>`;
}

function renderDaily(m) {
  const el = document.getElementById('mo-daily');
  const rows = (m.daily || []).slice(-14);
  if (!rows.length) { el.innerHTML = '<p class="loading">Collecting.</p>'; return; }
  el.innerHTML = `<table class="mo-table num"><thead><tr>
      <th>day</th><th>RH min/mean/max</th><th>dew pt min/mean/max</th>
      <th>&gt;60%</th><th>&gt;70%</th><th>cond.</th><th>rain</th><th>obs</th>
    </tr></thead><tbody>` +
    rows.map((d) => `<tr><td>${moFmtDay(d.day)}</td>` +
      `<td>${fmtPct(d.rh_min)} / <b class="${d.rh_mean > 70 ? 'v-out' : d.rh_mean > 60 ? 'v-watch' : 'v-ok'}">${fmtPct(d.rh_mean)}</b> / ${fmtPct(d.rh_max)}%</td>` +
      `<td>${fmtTemp(d.dp_min, 0)} / <b>${fmtTemp(d.dp_mean, 0)}</b> / ${fmtTemp(d.dp_max, 0)}${DEG}</td>` +
      `<td>${d.h60}h</td><td>${d.h70}h</td><td>${d.cond_h}h</td>` +
      `<td>${d.rain_in != null ? `${d.rain_in.toFixed(2)}"` : '—'}</td>` +
      `<td class="mo-dim">${d.obs_h}h</td></tr>`).join('') +
    `</tbody></table>`;
}

/* ------------------------------------------------------------------ */
/* boot + actions                                                      */
/* ------------------------------------------------------------------ */

async function moRefresh() {
  let m;
  try {
    m = await j('/api/moisture');
  } catch (e) {
    document.getElementById('mo-sub').textContent = 'connection error';
    console.error('moisture', e);
    return;
  }
  if (!m.available) {
    document.getElementById('mo-sub').textContent = m.reason === 'no_data'
      ? 'crawl sensor configured — waiting for first readings' : 'no crawl sensor configured';
    return;
  }
  moData = m;
  const start = moFmtDay(m.data_start);
  document.getElementById('mo-sub').textContent = `crawl data since ${start}`;
  document.getElementById('mo-generated').textContent =
    `generated ${new Date(m.generated).toLocaleString('en-US')}`;
  document.getElementById('mo-print-meta').textContent =
    `Data range: ${start} to today · generated ${new Date(m.generated).toLocaleString('en-US')} · ` +
    `sensor: Ecowitt WH32 in the crawl space, 3-minute cadence · dew points via Magnus formula`;

  try { renderVerdicts(m); } catch (e) { console.error('verdicts', e); }
  try { renderNow(m); } catch (e) { console.error('now', e); }
  try { drawDeltaChart(m); } catch (e) { console.error('delta', e); }
  try { drawAttrChart(m); } catch (e) { console.error('attr', e); }
  try { drawCondChart(m); } catch (e) { console.error('cond', e); }
  try { drawRainChart(m); } catch (e) { console.error('rain', e); }
  try { renderGapTiles(m); } catch (e) { console.error('gap tiles', e); }
  try { drawGapChart(m); } catch (e) { console.error('gap chart', e); }
  try { renderGapLead(m); } catch (e) { console.error('gap lead', e); }
  try { renderCoupling(m); } catch (e) { console.error('coupling', e); }
  try { renderPrediction(m); } catch (e) { console.error('prediction', e); }
  try { renderCouplingMethod(m); } catch (e) { console.error('coupling method', e); }
  try { renderThresholds(m); } catch (e) { console.error('thresholds', e); }
  try { renderInterventions(m); } catch (e) { console.error('interventions', e); }
  try { renderProjection(m); } catch (e) { console.error('projection', e); }
  try { renderDaily(m); } catch (e) { console.error('daily', e); }
}

document.getElementById('mo-print').addEventListener('click', () => window.print());

document.getElementById('mo-iv-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const date = document.getElementById('mo-iv-date').value;
  const label = document.getElementById('mo-iv-label').value.trim();
  const note = document.getElementById('mo-iv-note').value.trim();
  if (!date || !label) return;
  const btn = e.target.querySelector('button[type="submit"]');
  try {
    btn.disabled = true;
    const res = await fetch('/api/interventions', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date, label, note }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    document.getElementById('mo-iv-form').reset();
    btn.textContent = 'Mark intervention';
    await moRefresh();
  } catch (err) {
    console.error('intervention add failed', err);
    btn.textContent = 'Failed — try again';
  } finally {
    btn.disabled = false;
  }
});

document.addEventListener('click', async (e) => {
  const del = e.target.closest('[data-iv-del]');
  if (!del) return;
  if (!del.dataset.armed) {
    del.dataset.armed = '1';
    del.classList.add('is-armed');
    del.title = 'Click again to remove';
    setTimeout(() => { del.dataset.armed = ''; del.classList.remove('is-armed'); }, 3000);
    return;
  }
  try {
    const res = await fetch(`/api/interventions/${del.getAttribute('data-iv-del')}`, { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    await moRefresh();
  } catch (err) { console.error('intervention delete failed', err); }
});

moRefresh();
setInterval(moRefresh, MO_REFRESH_MS);
