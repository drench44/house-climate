// Executable tests for the crawl-to-floor gap and transport-gain rendering in
// moisture.js and app.js.
//
// Both files are classic scripts that touch `document` at load time, so the
// sandbox below stands in a minimal DOM: enough for the render functions to
// find their elements and write into them, and nothing more.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const staticDir = join(dirname(fileURLToPath(import.meta.url)),
  '..', '..', 'src', 'house_climate', 'web', 'static');

function makeEl(id) {
  return {
    id, innerHTML: '', textContent: '', hidden: false, dataset: {},
    style: { setProperty() {}, removeProperty() {}, getPropertyValue: () => '' },
    addEventListener() {},
    querySelector() { return makeEl('q'); },
    querySelectorAll() { return []; },
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    appendChild() {}, classList: { add() {}, remove() {}, toggle() {} },
  };
}

function loadPage(file) {
  const els = new Map();
  const document = {
    getElementById(id) {
      if (!els.has(id)) els.set(id, makeEl(id));
      return els.get(id);
    },
    querySelector() { return makeEl('q'); },
    querySelectorAll() { return []; },
    createElementNS() { return makeEl('svg'); },
    createElement() { return makeEl('el'); },
    addEventListener() {},
    body: makeEl('body'),
    documentElement: makeEl('html'),
  };
  const sandbox = {
    document, console, window: { location: { search: '' } },
    localStorage: { getItem: () => null, setItem: () => {} },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    setInterval: () => 0, setTimeout: () => 0, Date, Math, JSON, Intl,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(join(staticDir, 'common.js'), 'utf8'), sandbox);
  vm.runInContext(readFileSync(join(staticDir, file), 'utf8'), sandbox);
  return { sandbox, els, document };
}

const mo = loadPage('moisture.js');
const grab = (names) => vm.runInContext(`({${names.join(',')}})`, mo.sandbox);
const { couplingReason, gapClass, renderGapTiles, renderCoupling, renderPrediction } =
  grab(['couplingReason', 'gapClass', 'renderGapTiles', 'renderCoupling', 'renderPrediction']);

// ------------------------------------------------------------------ gates

test('every refusal reason is explained in plain words', () => {
  const reasons = ['window_too_short', 'thin_coverage', 'outage', 'insufficient_n_eff',
    'weak_signal', 'straddles_intervention', 'inconsistent_sign', 'not_configured',
    'not_computed', 'no_fit', 'no_data', 'collecting'];
  for (const reason of reasons) {
    const text = couplingReason({ reason, need_days: 21, thinnest: 'outdoor' });
    assert.ok(text.length > 20, `${reason} needs a real explanation`);
    assert.ok(!/unavailable|error|failed|n\/a/i.test(text),
      `${reason} must say what it is waiting for, not that something broke: ${text}`);
  }
});

test('a dried-out crawl is explained as a good sign, not a fault', () => {
  const text = couplingReason({ reason: 'weak_signal' });
  assert.match(text, /good sign/i);
});

test('an unrecognised reason is not dressed up as normal progress', () => {
  /* The old default branch reported every unknown server reason — including a
     failed fit — as "Collecting", which reads as a healthy wait. */
  const text = couplingReason({ reason: 'something_new' });
  assert.match(text, /something new/);
  assert.ok(!/^Not enough to go on yet/.test(text), text);
});

test('a cold cache is not reported as missing data', () => {
  const text = couplingReason({ reason: 'not_computed' });
  assert.match(text, /server/);
  assert.ok(!/Collecting/.test(text), text);
});

test('a failed fit says the sums could not be solved', () => {
  assert.match(couplingReason({ reason: 'no_fit' }), /could not be solved/);
});

// ------------------------------------------------------------------ classes

test('gap severity bands', () => {
  assert.equal(gapClass(null), '');
  assert.equal(gapClass(0.5), 'v-ok');
  assert.equal(gapClass(2), 'v-watch');
  assert.equal(gapClass(5), 'v-out');
});

// ------------------------------------------------------------------ tiles

test('gap tiles show a signed number and a direction', () => {
  renderGapTiles({ ah: { available: true, settle_days: 7, floors: [
    { name: 'Upstairs', gap_now: 2.35, gap_trend_7d: -0.42, gap_daily: [], interventions: [] },
  ] } });
  const html = mo.document.getElementById('mo-gap-tiles').innerHTML;
  assert.match(html, /\+2\.35/);
  assert.match(html, /g\/m/);
  assert.match(html, /closing/);
});

test('gap tiles cope with a floor that has never reported', () => {
  renderGapTiles({ ah: { available: true, settle_days: 7, floors: [
    { name: 'Upstairs', gap_now: null, gap_trend_7d: null, gap_daily: [], interventions: [] },
  ] } });
  const html = mo.document.getElementById('mo-gap-tiles').innerHTML;
  assert.match(html, /&mdash;/);
  assert.ok(!/NaN|null|undefined/.test(html), html);
});

// --------------------------------------------------------------- coupling

test('a ready transport gain reads as a percentage with a range', () => {
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.42, ci95: 0.08, lag: 3, days: 30,
                n_eff: 61, significant: true },
    stack: { ready: true, delta: 0.004, ci95: 0.001 },
    prediction: [],
  }], consistency: { verdict: 'collecting', text: 'Needs two floors.' } } });
  const html = mo.document.getElementById('mo-coupling').innerHTML;
  assert.match(html, /42%/);
  assert.match(html, /34&ndash;50%/);
  assert.match(html, /about 3 hours later/);
  assert.match(html, /61 independent readings/);
});

test('a transport gain that is not significant says so', () => {
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.04, ci95: 0.09, lag: 0, days: 30,
                n_eff: 44, significant: false },
    stack: {}, prediction: [],
  }], consistency: {} } });
  const html = mo.document.getElementById('mo-coupling').innerHTML;
  assert.match(html, /too small to separate from nothing/);
  assert.match(html, /within the hour/);
});

test('the confidence range is shown as computed, including below zero', () => {
  /* Clamping the lower end at zero hid the fact that the interval was
     consistent with no crawl air arriving at all. */
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.05, ci95: 0.2, lag: 1, days: 30,
                n_eff: 40, significant: false },
    stack: {}, prediction: [],
  }], consistency: {} } });
  const html = mo.document.getElementById('mo-coupling').innerHTML;
  assert.match(html, /&ndash;25%/);
  assert.match(html, /too small to separate from nothing/);
});

test('the temperature check says so when it cannot run', () => {
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.4, ci95: 0.05, lag: 2, days: 30,
                n_eff: 60, significant: true },
    stack: { ready: false, reason: 'weak_signal' },
    prediction: [],
  }], consistency: {} } });
  const html = mo.document.getElementById('mo-coupling').innerHTML;
  assert.match(html, /cannot run yet/);
  assert.match(html, /unconfirmed/);
});

test('an unmeasurable prediction is not reported as a miss', () => {
  renderPrediction({ ah: { available: true, floors: [{
    name: 'Upstairs',
    prediction: [{ label: 'Vapor barrier', verdict: 'inconclusive',
                   predicted: -1.2, ci95: null, observed: -0.05 }],
  }] } });
  const html = mo.document.getElementById('mo-prediction').innerHTML;
  assert.match(html, /cannot be called either way/);
  assert.ok(!/not the main source/.test(html), html);
});

test('a panel is blanked before it re-renders', () => {
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.62, ci95: 0.05, lag: 2, days: 30,
                n_eff: 60, significant: true },
    stack: {}, prediction: [],
  }], consistency: {} } });
  assert.match(mo.document.getElementById('mo-coupling').innerHTML, /62%/);
  /* A payload the renderer bails out of must not leave the old number up. */
  renderCoupling({ ah: { available: false, reason: 'not_configured' } });
  assert.ok(!/62%/.test(mo.document.getElementById('mo-coupling').innerHTML));
});

test('a stack signature that does not strengthen warns about local weather', () => {
  renderCoupling({ ah: { available: true, floors: [{
    name: 'Upstairs',
    coupling: { ready: true, beta: 0.3, ci95: 0.05, lag: 2, days: 30, n_eff: 50, significant: true },
    stack: { ready: true, delta: 0.0002, ci95: 0.004 },
    prediction: [],
  }], consistency: {} } });
  assert.match(mo.document.getElementById('mo-coupling').innerHTML, /following the local weather/);
});

// ------------------------------------------------------------- prediction

test('a confirmed prediction is called the strongest evidence', () => {
  renderPrediction({ ah: { available: true, floors: [{
    name: 'Upstairs',
    prediction: [{ label: 'Vapor barrier', verdict: 'confirmed',
                   predicted: -1.2, ci95: 0.3, observed: -1.15 }],
  }] } });
  const html = mo.document.getElementById('mo-prediction').innerHTML;
  assert.match(html, /1\.20/);
  assert.match(html, /strongest evidence/);
});

test('a missed prediction says the crawl is not the main source', () => {
  renderPrediction({ ah: { available: true, floors: [{
    name: 'Upstairs',
    prediction: [{ label: 'Vapor barrier', verdict: 'not_confirmed',
                   predicted: -1.2, ci95: 0.2, observed: -0.05 }],
  }] } });
  assert.match(mo.document.getElementById('mo-prediction').innerHTML,
    /not the main source/);
});

// ------------------------------------------------------- dashboard strip

const dash = loadPage('app.js');
const { renderGapStrip, crawlGapClass } =
  vm.runInContext('({renderGapStrip, crawlGapClass})', dash.sandbox);

test('the dashboard strip stays hidden until a floor has a gap', () => {
  renderGapStrip(null);
  assert.equal(dash.document.getElementById('gap-strip').hidden, true);
  renderGapStrip({ available: true, floors: [{ name: 'Upstairs', gap_now: null }] });
  assert.equal(dash.document.getElementById('gap-strip').hidden, true);
});

test('the dashboard strip shows the gap and links to the evidence', () => {
  renderGapStrip({ available: true, floors: [
    { name: 'Upstairs', gap_now: 2.4, trend_7d: -0.3, coupling_ready: false, beta: null },
  ] });
  const el = dash.document.getElementById('gap-strip');
  assert.equal(el.hidden, false);
  assert.match(el.innerHTML, /\+2\.40/);
  assert.match(el.innerHTML, /Still working out/);
  assert.match(el.innerHTML, /moisture\.html/);
});

test('the dashboard strip reports the share once it is known', () => {
  renderGapStrip({ available: true, floors: [
    { name: 'Upstairs', gap_now: 2.4, trend_7d: 0.0, coupling_ready: true,
      beta: 0.37, ci95: 0.09, significant: true },
  ] });
  assert.match(dash.document.getElementById('gap-strip').innerHTML, /37%/);
});

test('the dashboard strip does not state a share it cannot distinguish from zero', () => {
  renderGapStrip({ available: true, floors: [
    { name: 'Upstairs', gap_now: 2.4, trend_7d: 0.0, coupling_ready: true,
      beta: 0.08, ci95: 0.40, significant: false },
  ] });
  const html = dash.document.getElementById('gap-strip').innerHTML;
  assert.ok(!/8%/.test(html), `stated a number four times smaller than its own margin: ${html}`);
  assert.match(html, /too little crawl air/);
});

test('the dashboard strip distinguishes an outage from ordinary waiting', () => {
  renderGapStrip({ available: true, floors: [
    { name: 'Upstairs', gap_now: 2.4, trend_7d: null, coupling_ready: false,
      beta: null, reason: 'outage' },
  ] });
  assert.match(dash.document.getElementById('gap-strip').innerHTML, /check the sensors/);
});

test('the dashboard strip reports a dried-out crawl as good news', () => {
  renderGapStrip({ available: true, floors: [
    { name: 'Upstairs', gap_now: 0.4, trend_7d: -0.9, coupling_ready: false,
      beta: null, reason: 'weak_signal' },
  ] });
  assert.match(dash.document.getElementById('gap-strip').innerHTML,
    /nothing left to trace upstairs/);
});

test('a floor damper than the crawl is flagged, not shown as healthy', () => {
  assert.equal(crawlGapClass(-2.5), 'v-watch');
  assert.equal(gapClass(-2.5), 'v-watch');
});

test('dashboard gap severity bands match the moisture page', () => {
  for (const v of [null, -2.5, 0.5, 2, 5]) assert.equal(crawlGapClass(v), gapClass(v));
});

// ------------------------------------------------- outdoor reference

const { outdoorSourceNote, renderCouplingMethod } =
  vm.runInContext('({outdoorSourceNote, renderCouplingMethod})', mo.sandbox);

test('a weather-feed reference is named as the weaker one', () => {
  const text = outdoorSourceNote({ outdoor_source: { source: 'weather_feed', name: null } });
  assert.match(text, /weather feed/);
  assert.match(text, /outdoor sensor at the house would settle it/);
});

test('an on-site reference says so and names the sensor', () => {
  const text = outdoorSourceNote({ outdoor_source: { source: 'sensor', name: 'Outdoor' } });
  assert.match(text, /sensor at the house/);
  assert.match(text, /Outdoor/);
  assert.ok(!/would settle it properly/.test(text), text);
});

test('an unknown source is not reported as the weather feed', () => {
  /* Two states in the code, three in reality. A payload with no source block
     used to print the confident "comes from a weather feed" sentence. */
  for (const payload of [{}, { outdoor_source: {} }, { outdoor_source: null }]) {
    const text = outdoorSourceNote(payload);
    assert.match(text, /could not be determined/);
    assert.ok(!/undefined|null/.test(text), text);
  }
});

test('a rejected sensor is named, with why, instead of being hidden', () => {
  const stale = outdoorSourceNote({ outdoor_source: {
    source: 'weather_feed', ignored_sensor: 'Outdoor', reason: 'stale', stale_days: 9 } });
  assert.match(stale, /Outdoor/);
  assert.match(stale, /has not reported for 9 days/);
  const young = outdoorSourceNote({ outdoor_source: {
    source: 'weather_feed', ignored_sensor: 'Outdoor', reason: 'too_little_history',
    sensor_days: 3, need_days: 7 } });
  assert.match(young, /3 full days/);
  assert.match(young, /7 needed/);
});

test('the on-site claim is scoped to the figures it actually covers', () => {
  const text = outdoorSourceNote({ outdoor_source: { source: 'sensor', name: 'Outdoor' } });
  assert.match(text, /Rainfall, the condensation history and the winter projection/);
});

test('the method note reaches the page', () => {
  renderCouplingMethod({ ah: { available: true, window_days: 30,
    outdoor_source: { source: 'sensor', name: 'Outdoor' } } });
  assert.match(mo.document.getElementById('mo-coupling-method').innerHTML,
    /sensor at the house/);
});


// --------------------------------------- intervention verdict wording

const { gapVerdictText, renderGapLead } =
  vm.runInContext('({gapVerdictText, renderGapLead})', mo.sandbox);

test('an unchecked change is not reported as no change at all', () => {
  /* It used to fall through to the "within the noise" wording, which says the
     work did nothing — the opposite of what was measured. */
  const text = gapVerdictText('unchecked');
  assert.match(text, /bigger than the day-to-day noise/);
  assert.match(text, /not yet proof/);
  assert.notEqual(text, gapVerdictText('noise'));
});

test('each intervention verdict has its own wording', () => {
  const seen = new Set(['real', 'confounded', 'unchecked', 'noise'].map(gapVerdictText));
  assert.equal(seen.size, 4, 'two verdicts share wording');
  assert.match(gapVerdictText('something_new'), /not yet possible to judge/);
});

test('an unchecked verdict reaches the page with its own wording', () => {
  renderGapLead({ ah: { available: true, settle_days: 7, floors: [{
    name: 'Upstairs', gap_daily: [], gap_now: 1.0,
    interventions: [{ label: 'Vapor barrier',
                      metric: { verdict: 'unchecked', diff: -2.0, ci95: 0.3 } }],
  }] } });
  const html = mo.document.getElementById('mo-gap-lead').innerHTML;
  assert.match(html, /not yet proof/);
  assert.ok(!/within the day-to-day noise/.test(html), html);
});


test('thin coverage names which readings are missing', () => {
  assert.match(couplingReason({ reason: 'thin_coverage', thinnest: 'outdoor' }),
    /outdoor readings/);
  assert.match(couplingReason({ reason: 'thin_coverage', thinnest: 'crawl' }),
    /crawl readings/);
});
