// Wall-display state rules that used to live as untested inline code in
// app.js / square.js: the cost rail's unavailable state, the kiosk's stale
// room colors and price-band fallback, and the band split + forecast copy
// derived from the configured TOU bands instead of hardcoded names and hours.
// Run with `node --test` (tests/test_js.py runs it under pytest too).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'house_climate', 'web', 'static');
const sandbox = { document: undefined };
vm.createContext(sandbox);
vm.runInContext(readFileSync(join(staticDir, 'common.js'), 'utf8'), sandbox);
// Values born in the vm carry its realm's prototypes; JSON round-trip them so
// deepStrictEqual compares plain host values.
const host = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
const call = (name, ...a) => {
  assert.equal(typeof sandbox[name], 'function', `common.js is missing ${name}()`);
  return host(sandbox[name](...a));
};

// --------------------------------------------------------------- cost rail

test('costRailState: waiting, unavailable, ok', () => {
  assert.equal(call('costRailState', null), 'waiting');
  assert.equal(call('costRailState', undefined), 'waiting');
  // the server degrades to {available:false} on a TOU gap; the rail used to
  // throw on cost.today.dollars and leave the previous numbers frozen
  assert.equal(call('costRailState', { available: false, reason: 'tou_gap' }), 'unavailable');
  assert.equal(call('costRailState', { available: true }), 'unavailable');
  assert.equal(call('costRailState', { available: true, today: { dollars: null } }), 'unavailable');
  assert.equal(call('costRailState', { available: true, today: { dollars: 1.25 } }), 'ok');
});

test('costUnavailableHtml says so plainly and carries no dollar figure', () => {
  const html = call('costUnavailableHtml', { available: false, reason: 'tou_gap' });
  assert.match(html, /unavailable/i);
  assert.doesNotMatch(html, /\$\d/);
});

// ------------------------------------------------------ stale room colors

test('roomValueClasses: a stale room is never colored (both screens)', () => {
  const rm = { name: 'Upstairs', channel: '1', temp_f: 90, humidity: 80, stale: true };
  assert.deepEqual(call('roomValueClasses', rm, 68, 74), { t: '', h: '' });
});

test('roomValueClasses: a fresh room is colored by the shared rules', () => {
  const rm = { name: 'Upstairs', channel: '1', temp_f: 90, humidity: 80, stale: false };
  assert.deepEqual(call('roomValueClasses', rm, 68, 74), { t: 'v-out', h: 'v-out' });
  const crawl = { name: 'Crawl', channel: 'outdoor', temp_f: 60, humidity: 70 };
  assert.deepEqual(call('roomValueClasses', crawl, 68, 74), { t: 'v-ok', h: 'v-watch' });
});

// ----------------------------------------------------- kiosk band fallback

const NINE_PM = Date.parse('2026-08-12T21:00:00-07:00');
const peakCost = { tier_now: 'peak', next_change_at: '2026-08-12T21:00:00-07:00' };

test('kioskBand: a fresh cost payload wins and becomes the last good one', () => {
  const r = call('kioskBand', peakCost, null, NINE_PM - 3600e3);
  assert.equal(r.label.name, 'on-peak');
  assert.deepEqual(r.last, peakCost);
});

test('kioskBand: a failed fetch keeps the last label only until its band ends', () => {
  const before = call('kioskBand', null, peakCost, NINE_PM - 60e3);
  assert.equal(before.label.name, 'on-peak');
  // 'on-peak until 9pm' must not outlast 9pm
  const after = call('kioskBand', null, peakCost, NINE_PM + 60e3);
  assert.equal(after.label, null);
  assert.equal(after.last, null);
});

test('kioskBand: unavailable cost payload counts as a failed fetch', () => {
  const r = call('kioskBand', { available: false }, peakCost, NINE_PM + 1);
  assert.equal(r.label, null);
});

test('kioskBand: a last label with no known end is not kept', () => {
  const r = call('kioskBand', null, { tier_now: 'flat', next_change_at: null }, NINE_PM);
  assert.equal(r.label, null);
});

// ------------------------------------------- band split from configured bands

test('bandSplitRows: follows the configured band names, not peak/midpeak/offpeak', () => {
  const cost = {
    today: { by_band: { 'on-peak': { dollars: 3 }, shoulder: { dollars: 1 } } },
    bands: [
      { name: 'on-peak', tier: 'peak', rate: 0.5 },
      { name: 'shoulder', tier: 'mid', rate: 0.2 },
      { name: 'super-off', tier: 'off', rate: 0.05 },
    ],
  };
  const rows = call('bandSplitRows', cost);
  assert.deepEqual(rows.map((r) => [r.name, r.cls, r.dollars]),
    [['on-peak', 'b-peak', 3], ['shoulder', 'b-mid', 1], ['super-off', 'b-off', 0]]);
  assert.deepEqual(rows.map((r) => Math.round(r.pct)), [75, 25, 0]);
  assert.deepEqual(rows.map((r) => r.label), ['on-peak', 'mid-peak', 'off-peak']);
});

test('bandSplitRows: two bands on one tier keep their own names', () => {
  const cost = {
    today: { by_band: {} },
    bands: [
      { name: 'peak', tier: 'peak', rate: 0.5 },
      { name: 'morning', tier: 'mid', rate: 0.2 },
      { name: 'evening', tier: 'mid', rate: 0.25 },
      { name: 'night', tier: 'off', rate: 0.1 },
    ],
  };
  assert.deepEqual(call('bandSplitRows', cost).map((r) => r.label),
    ['on-peak', 'evening', 'morning', 'off-peak']);
});

test('bandSplitRows: a band that ran today but is not in the list still shows', () => {
  const cost = { today: { by_band: { legacy: { dollars: 2 } } }, bands: [] };
  const rows = call('bandSplitRows', cost);
  assert.deepEqual(rows.map((r) => [r.name, r.dollars, Math.round(r.pct)]), [['legacy', 2, 100]]);
});

test('bandSplitRows: flat rate is one row', () => {
  const cost = { today: { by_band: { all: { dollars: 2 } } },
    bands: [{ name: 'all', tier: 'flat', rate: 0.2 }] };
  const rows = call('bandSplitRows', cost);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].label, 'flat rate');
});

// ------------------------------------------------ forecast copy from config

test('fmtClockHHMM: 24h config times to wall-clock words', () => {
  assert.equal(call('fmtClockHHMM', '17:00'), '5pm');
  assert.equal(call('fmtClockHHMM', '16:30'), '4:30pm');
  assert.equal(call('fmtClockHHMM', '00:00'), '12am');
  assert.equal(call('fmtClockHHMM', '12:00'), '12pm');
});

test('forecastPeakText: names the configured window, not a hardcoded 5-9pm', () => {
  const fc = { has_peak: true, predicted_peak_dollars: 2.1,
    peak_windows: [{ start: '16:00', end: '20:00' }] };
  const txt = call('forecastPeakText', fc);
  assert.match(txt, /\$2\.10/);
  assert.match(txt, /4&ndash;8pm/);
  assert.doesNotMatch(txt, /5&ndash;9pm/);
});

test('forecastPeakText: two humps are both named', () => {
  const fc = { has_peak: true, predicted_peak_dollars: 1,
    peak_windows: [{ start: '07:00', end: '09:00' }, { start: '17:00', end: '20:00' }] };
  assert.match(call('forecastPeakText', fc), /7&ndash;9am and 5&ndash;8pm/);
});

test('forecastPeakText: no peak window tomorrow says so, no dollar claim', () => {
  const txt = call('forecastPeakText', { has_peak: false, predicted_peak_dollars: 0, peak_windows: [] });
  assert.match(txt, /no on-peak window/i);
  assert.doesNotMatch(txt, /\$/);
});

test('forecastPeakText: unknown peak cost is not shown as $0.00', () => {
  const txt = call('forecastPeakText', { has_peak: true, predicted_peak_dollars: null,
    peak_windows: [{ start: '17:00', end: '21:00' }] });
  assert.doesNotMatch(txt, /\$0\.00/);
  assert.match(txt, /not enough/i);
});

test('forecastPeakWord (kiosk): from has_peak, not a band name', () => {
  assert.equal(call('forecastPeakWord', { has_peak: true, peak_band: 'on-peak', predicted_peak_dollars: 1 }), 'in peak');
  assert.equal(call('forecastPeakWord', { has_peak: false, peak_band: 'peak' }), 'no peak');
});

// ------------------------------------------------------- humidity staleness

test('humidityStaleNote: shown only for a stale panel', () => {
  assert.equal(call('humidityStaleNote', { available: true, stale: false, age_s: 60 }), '');
  assert.match(call('humidityStaleNote', { available: true, stale: true, age_s: 7200 }), /2h/);
});

// ------------------------------------------------------ review follow-ups

test('alertsStripHtml: a failed fetch says so, an empty list is blank', () => {
  assert.match(call('alertsStripHtml', null), /unavailable/i);
  assert.equal(call('alertsStripHtml', []), '');
  assert.match(call('alertsStripHtml', [{ key: 'freeze', severity: 'critical', message: 'Cold <b>' }]),
    /class="alert crit".*Cold &lt;b&gt;/);
});

test('forecastUnavailableText: one line per reason', () => {
  assert.match(call('forecastUnavailableText', { available: false, reason: 'feed_unreachable' }), /unreachable/);
  assert.match(call('forecastUnavailableText', { available: false, reason: 'no_tomorrow_forecast' }), /no forecast/);
  assert.equal(call('forecastUnavailableText', { available: false }), '');
  assert.equal(call('forecastUnavailableText', { available: true }), '');
});

test('forecastPeakWord (kiosk): unknown peak cost is not "in peak"', () => {
  assert.equal(call('forecastPeakWord', { has_peak: true, predicted_peak_dollars: null }), 'peak cost unknown');
});
