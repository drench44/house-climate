'use strict';

/* House Climate — the square tile (square.html).
   A condensed, glanceable version of the wall dashboard for the kitchen
   wall calendar (Apolosign, Android kiosk browser): inside vs outside,
   what the HVAC is doing, every room's temp / humidity / PM2.5, the crawl
   space, windows-open advice, today's cost, tomorrow's forecast, and the
   current TOU band. Numbers only — no charts, no hover; everything
   must read from across the room. All thresholds, chips and state logic
   come from common.js so this can never disagree with the dashboard. */

const REFRESH_MS = 60000;

const sqRoot = document.getElementById('sq-root');

/* One human phrase for what the system is doing right now. */
function sqStateLine(n) {
  const st = equipmentState(n);
  if (st === 'cooling') return `cooling to ${fmtTemp(n.cool_setpoint_f, 0)}°`;
  if (st === 'heating') return `heating to ${fmtTemp(n.heat_setpoint_f, 0)}°`;
  if (st === 'fan') return 'fan running';
  if (st === 'off') return 'system off';
  if (n.mode === 'cool') return `idle · set ${fmtTemp(n.cool_setpoint_f, 0)}°`;
  if (n.mode === 'heat') return `idle · set ${fmtTemp(n.heat_setpoint_f, 0)}°`;
  return 'idle';
}

/* Compact outdoor AQI chip (band logic shared via aqiChipClass). */
function sqAqi(hum) {
  if (!hum || !hum.available || hum.outdoor_aqi == null) return '';
  const n = Math.round(hum.outdoor_aqi);
  const cat = hum.aqi_category ? ` ${escapeHtml(hum.aqi_category.toLowerCase())}` : '';
  return `<span class="aqi num ${aqiChipClass(n)}">AQI ${n}${cat}</span>`;
}

/* The humidity engine's windows verdict, one short line. */
function sqWindows(hum) {
  const action = hum && hum.available && hum.window && hum.window.action;
  if (action === 'vent') return '<span class="sq-vent">windows: open them</span>';
  if (action === 'keep_closed') return '<span class="sq-winline">windows: keep closed</span>';
  if (action === 'neutral') return '<span class="sq-winline">windows: either way</span>';
  return '';
}

function renderHero(n, hum) {
  const hero = document.getElementById('sq-hero');
  if (!n || n.indoor_temp_f == null) {
    hero.innerHTML = `<p class="loading">no fresh reading from the thermostat</p>`;
    sqRoot.setAttribute('data-state', 'idle');
    return;
  }
  sqRoot.setAttribute('data-state', equipmentState(n));
  const tCls = tempClass(n.indoor_temp_f, n.heat_setpoint_f, n.cool_setpoint_f);
  const cond = n.wx_conditions ? escapeHtml(n.wx_conditions.toLowerCase()) : '';
  hero.classList.toggle('is-stale', !!n.stale);
  hero.innerHTML = `
    <div class="sq-hero-in">
      <div class="sq-big num ${tCls}">${fmtTemp(n.indoor_temp_f)}&deg;</div>
      <div class="sq-inmeta"><span class="sq-inlabel">inside</span>
        · <span class="num ${rhClass(n.indoor_humidity)}">${fmtPct(n.indoor_humidity)}</span>% RH
        · <span class="sq-state">${sqStateLine(n)}</span></div>
    </div>
    <div class="sq-hero-out">
      <div class="sq-big2 num">${fmtTemp(n.wx_outdoor_temp_f)}&deg;</div>
      <div class="sq-outmeta"><span class="sq-outlabel">outside</span>${cond ? ` · ${cond}` : ''}</div>
      <div class="sq-outchips">${sqAqi(hum)}</div>
      <div class="sq-outmeta">${sqWindows(hum)}</div>
    </div>`;
}

function sqRoomRow(rm, heat, cool, air) {
  const isCrawl = rm.channel === 'outdoor' || /crawl/i.test(rm.name || '');
  const pm = pmChip(rm, air) || '<span></span>';
  const name = isCrawl ? 'Crawl space' : escapeHtml(rm.name);
  if (!rm.present) {
    return `<div class="sq-room"><span class="sq-rname">${name}</span>` +
      `<span class="sq-rval dim">&mdash;</span><span class="sq-rval dim">&mdash;</span>${pm}</div>`;
  }
  const tCls = isCrawl ? crawlTempClass(rm.temp_f) : tempClass(rm.temp_f, heat, cool);
  const hCls = isCrawl ? crawlRhClass(rm.humidity) : rhClass(rm.humidity);
  return `<div class="sq-room${isCrawl ? ' sq-crawl' : ''}${rm.stale ? ' is-stale' : ''}">` +
    `<span class="sq-rname">${name}</span>` +
    `<span class="sq-rval num ${tCls}">${fmtTemp(rm.temp_f)}&deg;</span>` +
    `<span class="sq-rval num ${hCls}">${fmtPct(rm.humidity)}%</span>${pm}</div>`;
}

function renderRooms(n, rooms, air) {
  const el = document.getElementById('sq-rooms');
  if (!rooms || !rooms.available || !Array.isArray(rooms.rooms)) {
    el.innerHTML = '';
    return;
  }
  const heat = n && n.heat_setpoint_f, cool = n && n.cool_setpoint_f;
  el.innerHTML = rooms.rooms.map((rm) => sqRoomRow(rm, heat, cool, air)).join('');
}

/* Today's real spend + tomorrow's forecast, one quiet line. */
function renderStats(cost, forecast) {
  const el = document.getElementById('sq-stats');
  const parts = [];
  if (cost && cost.today && cost.today.dollars != null) {
    parts.push(`<span>AC today so far $${cost.today.dollars.toFixed(2)} · ${Math.round(cost.today.kwh)} kWh</span>`);
  }
  if (forecast && forecast.available && forecast.fc_high_f != null) {
    const pk = (forecast.predicted_peak_dollars || 0).toFixed(2);
    const band = forecast.peak_band === 'peak' ? 'in peak' : 'wknd';
    parts.push(`<span>tomorrow ${Math.round(forecast.fc_high_f)}° · ~$${pk} ${band}</span>`);
  }
  el.innerHTML = parts.join('');
}

function renderBand() {
  const b = bandNow(new Date());
  const el = document.getElementById('sq-band');
  const cls = b.name === 'on-peak' ? 'sq-peak' : b.name === 'mid-peak' ? 'sq-mid' : 'sq-off';
  el.className = `sq-band num ${cls}`;
  el.textContent = b.until == null ? b.name : `${b.name} until ${fmtHour(b.until)}`;
}

function tickSqClock() {
  document.getElementById('sq-clock').textContent = new Date()
    .toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

async function refresh() {
  const upd = document.getElementById('sq-updated');
  try {
    const [n, rooms, air] = await Promise.all([
      j('/api/now'), j('/api/rooms'), j('/api/air'),
    ]);
    // secondary feeds degrade to blank sections, never to "offline"
    const [hum, cost, fc] = await Promise.all([
      j('/api/humidity').catch(() => null),
      j('/api/cost/summary').catch(() => null),
      j('/api/forecast').catch(() => null),
    ]);
    sqRoot.setAttribute('data-conn', 'up');
    renderHero(n, hum);
    renderRooms(n, rooms, air);
    renderStats(cost, fc);
    upd.textContent = n && n.stale ? `stale · ${fmtAge(n.age_s)}` : 'live';
    upd.classList.toggle('v-watch', !!(n && n.stale));
  } catch (e) {
    // keep the last numbers on screen (dimmed via the dot) — a wall display
    // that blanks on one failed poll is worse than one showing 60s-old data
    sqRoot.setAttribute('data-conn', 'down');
    upd.textContent = 'offline';
  }
  renderBand();
}

tickSqClock();
setInterval(tickSqClock, 1000);
refresh();
setInterval(refresh, REFRESH_MS);
