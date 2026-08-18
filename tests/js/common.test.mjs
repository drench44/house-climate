// Executable tests for the shared chart logic in common.js — run with
// `node --test` (any Node >= 18). The pytest wrapper tests/test_js.py runs
// this via local node or, on the box, a disposable node:20-alpine container.
//
// common.js is a classic script (no exports): load it into a vm sandbox and
// pull the functions out of the sandbox's globals.
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
// Function declarations attach to the vm global; top-level consts stay in
// the context's lexical scope — pull those out with a second script.
const {
  clamp, fmtTemp, fmtPct, fmtAge, fmtHour, escapeHtml,
  timePath, hoverTargetMs, nearestByMs,
  tempClass, rhClass, crawlRhClass, crawlTempClass,
  equipmentState, bandTierLabel, pmChip, peakStripHtml, smokeBannerHtml,
  backupBadge,
} = sandbox;
const { GAP_MS } = vm.runInContext('({ GAP_MS })', sandbox);
// Arrays/objects born inside the vm carry the vm realm's prototypes, which
// deepStrictEqual rejects — rewrap results in host values.
const gridLevels = (...a) => Array.from(sandbox.gridLevels(...a));
const band = (cost) => {
  const b = sandbox.bandTierLabel(cost);
  return b == null ? null : { ...b };
};

// ------------------------------------------------------------- timePath

const id = (v) => v;

test('timePath joins contiguous points with L', () => {
  const pts = [{ ms: 0, v: 10 }, { ms: 1000, v: 20 }, { ms: 2000, v: 30 }];
  assert.equal(timePath(pts, id, id, 5000), 'M0.0 10.0 L1000.0 20.0 L2000.0 30.0');
});

test('timePath breaks into a new subpath across a gap', () => {
  const pts = [{ ms: 0, v: 1 }, { ms: 1000, v: 2 }, { ms: 99000, v: 3 }];
  // 98s gap > 5s threshold -> M restarts the line; no bridge across the outage
  assert.equal(timePath(pts, id, id, 5000), 'M0.0 1.0 L1000.0 2.0 M99000.0 3.0');
});

test('timePath boundary: a gap of exactly gapMs still connects', () => {
  const pts = [{ ms: 0, v: 1 }, { ms: 5000, v: 2 }];
  assert.equal(timePath(pts, id, id, 5000), 'M0.0 1.0 L5000.0 2.0');
});

test('timePath default gap is GAP_MS (15 min)', () => {
  assert.equal(GAP_MS, 15 * 60 * 1000);
  const joined = [{ ms: 0, v: 1 }, { ms: GAP_MS, v: 2 }];
  const broken = [{ ms: 0, v: 1 }, { ms: GAP_MS + 1, v: 2 }];
  assert.ok(!timePath(joined, id, id).includes('M', 1));
  assert.ok(timePath(broken, id, id).lastIndexOf('M') > 0);
});

test('timePath on empty/single input', () => {
  assert.equal(timePath([], id, id), '');
  assert.equal(timePath([{ ms: 7, v: 3 }], id, id), 'M7.0 3.0');
});

// ------------------------------------------------------------ gridLevels

test('gridLevels: 5-multiples strictly inside the range', () => {
  assert.deepEqual(gridLevels(41, 79, 10), [45, 50, 55, 60, 65, 70, 75]);
  // endpoints excluded: lo on a multiple is included only if > lo? ceil(40/5)*5 = 40 -> included at lo
  assert.deepEqual(gridLevels(40, 50, 10), [40, 45]);
});

test('gridLevels thinning keeps survivors evenly spaced', () => {
  const out = gridLevels(0, 100, 4);
  assert.ok(out.length <= 4);
  const steps = out.slice(1).map((v, i) => v - out[i]);
  assert.equal(new Set(steps).size, 1,
    `uneven spacing after thinning: ${out} — this was audit bug #gridlines`);
});

test('gridLevels respects maxN for any range', () => {
  for (const [lo, hi, n] of [[0, 500, 3], [10, 11, 2], [-40, 120, 5]]) {
    assert.ok(gridLevels(lo, hi, n).length <= n, `${lo}..${hi} maxN=${n}`);
  }
});

// ------------------------------------------------------------ hover math

const GEOM = { W: 960, padL: 40, padR: 20, winStart: 1000, winEnd: 2000 };

test('hoverTargetMs maps plot edges to window edges', () => {
  assert.equal(hoverTargetMs(40, GEOM), 1000);           // left plot edge
  assert.equal(hoverTargetMs(940, GEOM), 2000);          // right plot edge
  assert.equal(hoverTargetMs(490, GEOM), 1500);          // midpoint
});

test('hoverTargetMs clamps outside the plot area', () => {
  assert.equal(hoverTargetMs(0, GEOM), 1000);    // in the axis label gutter
  assert.equal(hoverTargetMs(9999, GEOM), 2000); // off the right edge
});

test('nearestByMs picks the closest point and survives empties', () => {
  const pts = [{ ms: 100 }, { ms: 200 }, { ms: 300 }];
  assert.equal(nearestByMs(pts, 149), pts[0]);
  assert.equal(nearestByMs(pts, 151), pts[1]);
  assert.equal(nearestByMs(pts, 1e12), pts[2]);
  assert.equal(nearestByMs([], 5), null);
});

// ------------------------------------------------------------ formatters

test('fmtHour covers the am/pm edges', () => {
  assert.equal(fmtHour(0), '12am');
  assert.equal(fmtHour(7), '7am');
  assert.equal(fmtHour(12), '12pm');
  assert.equal(fmtHour(17), '5pm');
  assert.equal(fmtHour(21), '9pm');
  assert.equal(fmtHour(24), '12am');   // "until 24" wraps to midnight
});

test('fmtAge unit boundaries', () => {
  assert.equal(fmtAge(null), '—');
  assert.equal(fmtAge(89), '89s');
  assert.equal(fmtAge(90), '2m');      // rounds 1.5m up
  assert.equal(fmtAge(5399), '90m');
  assert.equal(fmtAge(5400), '2h');    // 1.5h rounds up
});

test('fmtTemp / fmtPct handle nulls without NaN', () => {
  assert.equal(fmtTemp(null), '—');
  assert.equal(fmtTemp(72.46), '72.5');
  assert.equal(fmtTemp(72.46, 0), '72');
  assert.equal(fmtPct(null), '—');
  assert.equal(fmtPct(54.6), 55);
});

test('clamp', () => {
  assert.equal(clamp(5, 0, 10), 5);
  assert.equal(clamp(-1, 0, 10), 0);
  assert.equal(clamp(11, 0, 10), 10);
});

test('escapeHtml neutralizes markup', () => {
  assert.equal(escapeHtml('<b a="1">&\'</b>'),
    '&lt;b a=&quot;1&quot;&gt;&amp;&#39;&lt;/b&gt;');
});

// -------------------------------------------- shared status/state logic
// (moved from app.js into common.js 2026-08-12 so the square tile and the
// dashboard can never color the same reading differently)

test('tempClass: in-band ok, within 2° watch, beyond out, nulls blank', () => {
  assert.equal(tempClass(72, 69, 74), 'v-ok');
  assert.equal(tempClass(69, 69, 74), 'v-ok');       // band edges inclusive
  assert.equal(tempClass(76, 69, 74), 'v-watch');    // 2° over -> watch
  assert.equal(tempClass(76.1, 69, 74), 'v-out');
  assert.equal(tempClass(67.2, 69, 74), 'v-watch');
  assert.equal(tempClass(null, 69, 74), '');
  assert.equal(tempClass(72, null, 74), '');
});

test('rhClass living-space bands (30-60 ok, ±5 watch)', () => {
  assert.equal(rhClass(45), 'v-ok');
  assert.equal(rhClass(27), 'v-watch');
  assert.equal(rhClass(63), 'v-watch');
  assert.equal(rhClass(24), 'v-out');
  assert.equal(rhClass(66), 'v-out');
  assert.equal(rhClass(null), '');
});

test('crawlRhClass mirrors the mold thresholds (watch >65, mold >75)', () => {
  assert.equal(crawlRhClass(64.9), 'v-ok');
  assert.equal(crawlRhClass(65), 'v-watch');
  assert.equal(crawlRhClass(75), 'v-watch');
  assert.equal(crawlRhClass(75.1), 'v-out');
});

test('crawlTempClass bands', () => {
  assert.equal(crawlTempClass(64), 'v-ok');
  assert.equal(crawlTempClass(45), 'v-watch');
  assert.equal(crawlTempClass(85), 'v-watch');
  assert.equal(crawlTempClass(35), 'v-out');
  assert.equal(crawlTempClass(95), 'v-out');
});

test('equipmentState maps the Daikin equipment/mode fields', () => {
  assert.equal(equipmentState({ equipment_status: 'cooling' }), 'cooling');
  assert.equal(equipmentState({ equipment_status: 'overcool' }), 'cooling');
  assert.equal(equipmentState({ equipment_status: 'heating' }), 'heating');
  assert.equal(equipmentState({ equipment_status: 'fan' }), 'fan');
  assert.equal(equipmentState({ equipment_status: 'idle', mode: 'off' }), 'off');
  assert.equal(equipmentState({ equipment_status: 'idle', mode: 'cool' }), 'idle');
});

test('bandTierLabel: derives the kiosk band label from the cost tier fields', () => {
  // Peak tier with a known boundary -> "on-peak until <time>" (the exact time
  // string is locale/tz dependent, so assert the shape, not the clock value).
  const peak = band({ tier_now: 'peak', next_change_at: '2026-08-12T21:00:00-07:00' });
  assert.equal(peak.name, 'on-peak');
  assert.equal(peak.cls, 'sq-peak');
  assert.match(peak.until, /^ until /);
  // Mid and off map to their own classes; off with no next change carries no "until".
  assert.deepEqual(band({ tier_now: 'mid', next_change_at: null }),
    { name: 'mid-peak', cls: 'sq-mid', until: '' });
  assert.deepEqual(band({ tier_now: 'off', next_change_at: null }),
    { name: 'off-peak', cls: 'sq-off', until: '' });
  // Flat-rate utility: labeled, off-styled, never a peak/countdown.
  assert.deepEqual(band({ tier_now: 'flat', next_change_at: null }),
    { name: 'flat rate', cls: 'sq-off', until: '' });
  // No cost / no tier yet -> null, so the caller keeps the last label.
  assert.equal(band(null), null);
  assert.equal(band({}), null);
});

test('aqiChipClass US AQI band edges', () => {
  assert.equal(sandbox.aqiChipClass(50), 'aqi-ok');
  assert.equal(sandbox.aqiChipClass(51), 'aqi-neutral');
  assert.equal(sandbox.aqiChipClass(100), 'aqi-neutral');
  assert.equal(sandbox.aqiChipClass(101), 'aqi-warn');
  assert.equal(sandbox.aqiChipClass(150), 'aqi-warn');
  assert.equal(sandbox.aqiChipClass(151), 'aqi-crit');
});

const AIR = {
  available: true,
  rooms: [
    { room: 'upstairs', pm25: 1.0, age_s: 8, stale: false },
    { room: 'garage', pm25: 20.0, age_s: 8, stale: false },
    { room: 'downstairs', pm25: 40.0, age_s: 3000, stale: true },
  ],
  thresholds: { elevated: 12, bad: 35 },
};

test('pmChip severity classes follow the thresholds', () => {
  assert.ok(pmChip({ name: 'Upstairs' }, AIR).includes('pm-ok'));
  assert.ok(pmChip({ name: 'Upstairs' }, AIR).includes('PM 1'));
  assert.ok(pmChip({ name: 'Garage' }, AIR).includes('pm-warn'));
  const bad = { ...AIR, rooms: [{ room: 'garage', pm25: 40, age_s: 8, stale: false }] };
  assert.ok(pmChip({ name: 'Garage' }, bad).includes('pm-bad'));
});

test('pmChip: stale shows a dash chip, unknown rooms and no data show nothing', () => {
  const stale = pmChip({ name: 'Downstairs' }, AIR);
  assert.ok(stale.includes('pm-stale') && stale.includes('PM —'));
  assert.equal(pmChip({ name: 'Crawl Space' }, AIR), '');
  assert.equal(pmChip({ name: 'Upstairs' }, { available: false }), '');
  assert.equal(pmChip({ name: 'Upstairs' }, null), '');
});

// -------------------------------------------------------- peakStripHtml
// (moved from app.js into common.js 2026-08-13 — was untested despite
// commit b5fd5e0 existing to fix its mid/flat labeling)

// Same formatting app.js uses, so the test doesn't hardcode a locale string.
const atTxtOf = (iso) => new Date(iso).toLocaleTimeString(
  'en-US', { hour: 'numeric', minute: '2-digit' },
);

test('peakStripHtml: no cost / no tier_now renders nothing', () => {
  assert.equal(peakStripHtml(null, null), '');
  assert.equal(peakStripHtml({}, null), '');
});

test('peakStripHtml: peak tier shows the rate and "until", no coasting by default', () => {
  const cost = { tier_now: 'peak', rate_now: 0.42, next_change_at: '2026-08-13T21:00:00' };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('ON-PEAK'), html);
  assert.ok(html.includes('$0.42/kWh'), html);
  assert.ok(html.includes(`until ${atTxtOf(cost.next_change_at)}`), html);
  assert.ok(!html.includes('coasting'), 'no precool -> no coasting copy');
  assert.ok(html.includes('hold off on the dryer'));
});

test('peakStripHtml: peak tier + enabled HA precool adds the coasting sub-branch', () => {
  const cost = { tier_now: 'peak', rate_now: 0.42, next_change_at: '2026-08-13T21:00:00' };
  const withPrecool = peakStripHtml(cost, { ha_precool: { enabled: true } });
  assert.ok(withPrecool.includes('coasting — hold off'), withPrecool);
  const disabledPrecool = peakStripHtml(cost, { ha_precool: { enabled: false } });
  assert.ok(!disabledPrecool.includes('coasting'), disabledPrecool);
});

test('peakStripHtml: approaching peak warns with a countdown and beats the next rate', () => {
  const cost = {
    tier_now: 'mid', next_tier: 'peak', minutes_to_change: 12,
    next_rate: 0.42, next_change_at: '2026-08-13T17:00:00',
  };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('id="peak-countdown"'), html);
  assert.ok(html.includes('>12<'), html);
  assert.ok(html.includes('PEAK IN'), html);
  assert.ok(html.includes(`beats $0.42/kWh at ${atTxtOf(cost.next_change_at)}`), html);
});

test('peakStripHtml: flat rate has no peak/off framing and no countdown', () => {
  const cost = { tier_now: 'flat', rate_now: 0.31 };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('FLAT RATE'), html);
  assert.ok(html.includes('$0.31/kWh'), html);
  assert.ok(!html.includes('PEAK'), html);
  assert.ok(!html.includes('OFF-PEAK'), html);
  assert.ok(!html.includes('peak-countdown'), html);
});

test('peakStripHtml: mid tier is labeled distinctly from off-peak, never "cheap power"', () => {
  const cost = { tier_now: 'mid', rate_now: 0.28 };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('MID-PEAK'), html);
  assert.ok(!html.includes('OFF-PEAK'), html);
  assert.ok(!html.includes('cheap power'), html);
  assert.ok(html.includes('is-mid'), html);
});

test('peakStripHtml: off tier shows plain "cheap power" when no peak is next', () => {
  const cost = { tier_now: 'off', rate_now: 0.18, next_tier: 'mid' };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('OFF-PEAK'), html);
  assert.ok(html.includes('$0.18/kWh · cheap power'), html);
  assert.ok(!html.includes('peak at'), html);
});

test('peakStripHtml: off tier facing an upcoming peak shows "peak at <time>" instead', () => {
  const cost = {
    tier_now: 'off', rate_now: 0.18, next_tier: 'peak',
    next_change_at: '2026-08-13T17:00:00',
  };
  const html = peakStripHtml(cost, null);
  assert.ok(html.includes('OFF-PEAK'), html);
  assert.ok(html.includes(`peak at ${atTxtOf(cost.next_change_at)}`), html);
  assert.ok(!html.includes('cheap power'), html);
});

// -------------------------------------------------------- smokeBannerHtml
// (extracted from renderScene in app.js 2026-08-13 — mirrors the
// peakStripHtml extraction so the decoupled-from-rooms behavior is testable
// without a DOM instead of only grep-tested.)

test('smokeBannerHtml: renders a non-empty banner from humidity alone, no rooms object involved', () => {
  const humidity = { outdoor_aqi: 180, aqi_category: 'Unhealthy' };
  const html = smokeBannerHtml(humidity, 101);
  assert.ok(html.length > 0, html);
  assert.ok(html.includes('smoke-banner'), html);
  assert.ok(html.includes('AQI 180'), html);
  assert.ok(html.includes('Unhealthy'), html);
  // The whole point of the decouple: no `rooms` argument exists at all —
  // this call only ever reads from `humidity`.
});

test('smokeBannerHtml: below the fallback threshold renders nothing', () => {
  assert.equal(smokeBannerHtml({ outdoor_aqi: 40 }, 101), '');
});

test('smokeBannerHtml: a custom aqi_unhealthy is honored over the fallback', () => {
  assert.equal(smokeBannerHtml({ outdoor_aqi: 160, aqi_unhealthy: 175 }, 101), '');
  assert.ok(smokeBannerHtml({ outdoor_aqi: 180, aqi_unhealthy: 175 }, 101).length > 0);
});

test('smokeBannerHtml: null/missing humidity or AQI renders nothing', () => {
  assert.equal(smokeBannerHtml(null, 101), '');
  assert.equal(smokeBannerHtml({ outdoor_aqi: null }, 101), '');
});

// ------------------------------------------------------------- backupBadge

test('backupBadge: hidden when the payload is missing or errored', () => {
  assert.equal(backupBadge(null).show, false);
  assert.equal(backupBadge(undefined).show, false);
});

test('backupBadge: hidden when unknown (no heartbeat recorded yet)', () => {
  assert.equal(backupBadge({ known: false, stale: false, age_s: null }).show, false);
});

test('backupBadge: hidden when a known backup is fresh', () => {
  assert.equal(backupBadge({ known: true, stale: false, age_s: 3600 }).show, false);
});

test('backupBadge: shown amber with age when a known backup is stale', () => {
  const b = backupBadge({ known: true, stale: true, age_s: 111600, threshold_s: 108000 });
  assert.equal(b.show, true);
  assert.equal(b.level, 'warn');
  assert.match(b.text, /Backup stale/);
  assert.match(b.text, /31h/);   // fmtAge(111600)
});
