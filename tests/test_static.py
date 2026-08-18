"""Frontend contract tests — no browser, no server.

The dashboard has no JS test runner, so these pin the frontend's load-bearing
conventions at the file level: every class the pages reference must exist in
the stylesheet, and the location palette (indoor = teal solid, outside = amber
dashed, crawl = violet) must stay consistent across BOTH pages. That palette
is the fix for "hard to tell inside from outside" — a chart quietly reusing a
place hue for something else is a regression even though nothing crashes.
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "src" / "house_climate" / "web" / "static"

CSS = (STATIC / "styles.css").read_text()
APP_JS = (STATIC / "app.js").read_text()
MO_JS = (STATIC / "moisture.js").read_text()
SQ_JS = (STATIC / "square.js").read_text()
COMMON_JS = (STATIC / "common.js").read_text()
INDEX_HTML = (STATIC / "index.html").read_text()
MO_HTML = (STATIC / "moisture.html").read_text()
SQ_HTML = (STATIC / "square.html").read_text()

ALL_JS = APP_JS + MO_JS + SQ_JS + COMMON_JS
ALL_HTML = INDEX_HTML + MO_HTML + SQ_HTML


def _css_rule(selector):
    """Every declaration block that mentions `selector` (screen AND print)."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS):
        if re.search(rf"{re.escape(selector)}(?![\w-])", m.group(1)):
            out.append(m.group(2))
    return "\n".join(out)


# ---------------------------------------------------------------- classes

# Classes referenced by markup/JS that intentionally have no CSS rule of
# their own (semantic hooks, or styled purely via JS attributes). Add here
# ONLY with a reason.
UNSTYLED_OK = set()

_CLASS_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]*$")


def _referenced_classes():
    """Literal class tokens from HTML and JS template strings. Attribute
    values mix static tokens with ${...} expressions; keep only tokens that
    look like class names so JS operators/quotes don't produce noise."""
    refs = set()
    for m in re.finditer(r'class="([^"]*)"', ALL_HTML + ALL_JS):
        for tok in m.group(1).split():
            if "${" not in tok and _CLASS_TOKEN.fullmatch(tok):
                refs.add(tok)
    # svgEl(..., { class: 'foo bar' }) and classList.add('foo')
    for m in re.finditer(r"class:\s*'([^']*)'", ALL_JS):
        for tok in m.group(1).split():
            if "${" not in tok and _CLASS_TOKEN.fullmatch(tok):
                refs.add(tok)
    for m in re.finditer(r"classList\.(?:add|toggle)\('([\w-]+)'", ALL_JS):
        refs.add(m.group(1))
    # classes appended conditionally inside template expressions, e.g.
    # `${cond ? ' mo-best' : ''}` — the leading space marks the append-a-class
    # idiom and keeps display-string ternaries (`? 'Cooling'`) out
    for m in re.finditer(r"\?\s*'\s+([A-Za-z][A-Za-z0-9_ -]*)'", ALL_JS):
        for tok in m.group(1).split():
            if _CLASS_TOKEN.fullmatch(tok):
                refs.add(tok)
    return refs


def test_every_referenced_class_is_styled():
    """A renamed/deleted CSS class must fail here, not silently unstyle a
    page — this exact test found the invisible sw-rainbar swatch and the
    never-rendered mo-best lag highlight on 2026-08-12."""
    missing = sorted(
        c for c in _referenced_classes()
        if c not in UNSTYLED_OK and not re.search(rf"\.{re.escape(c)}(?![\w-])", CSS)
    )
    assert missing == [], f"classes referenced but absent from styles.css: {missing}"


def test_signal_bar_off_state_is_styled():
    assert re.search(r"\.sig\b[^{}]*\.off|\.sig\b[^{}]*\bi\b", CSS), \
        "signal-bar 'off' bars lost their styling"


# ---------------------------------------------------------------- palette

def test_place_palette_tokens_exist():
    assert "--crawl-line:" in CSS and "--crawl-soft:" in CSS
    assert "--cool:" in CSS and "--heat:" in CSS


def test_crawl_series_use_the_crawl_hue():
    for sel in (".cz-avg", ".sw-crawlavg"):
        assert "var(--crawl-line)" in _css_rule(sel), f"{sel} left the crawl hue"
    assert "var(--crawl-soft)" in _css_rule(".cz-band")
    for sel in (".cz-temp", ".sw-crawltemp", ".sw-crawlband", ".cz-templabel"):
        rule = _css_rule(sel)
        assert "195, 155, 234" in rule, f"{sel} left the crawl hue"
        assert "245, 164, 92" not in rule, \
            f"{sel} uses the OUTSIDE amber — crawl temp in amber-dash was the original confusion"


def test_indoor_series_are_teal():
    for sel in (".hs-indoor", ".rb-indoor", ".sw-in"):
        assert "var(--cool)" in _css_rule(sel), f"{sel} left the indoor teal"
    for sel in (".mo-ref-line", ".sw-moref"):
        assert "91, 201, 240" in _css_rule(sel), \
            f"{sel}: the moisture page's indoor reference must match the dashboard's indoor teal"


def test_outdoor_series_are_amber_and_dashed():
    for sel in (".hs-outdoor", ".rb-outdoor", ".mo-out-line"):
        rule = _css_rule(sel)
        assert "245, 164, 92" in rule, f"{sel} left the outside amber"
        assert "stroke-dasharray" in rule, f"{sel} must stay dashed (solid = a place's primary series)"
    for sel in (".sw-out", ".sw-moout"):
        assert "245, 164, 92" in _css_rule(sel), f"{sel} left the outside amber"


def test_moisture_outdoor_line_has_its_own_class():
    """The attribution chart's outdoor line must NOT borrow the crawl-temp class."""
    assert "class: 'mo-out-line'" in MO_JS
    assert "class: 'cz-temp'" not in MO_JS


def test_print_stylesheet_keeps_the_palette_split():
    print_block = CSS[CSS.index("@media print"):]
    for sel in (".cz-avg", ".cz-temp", ".mo-out-line", ".mo-ref-line"):
        assert sel in print_block, f"{sel} lost its print override"


# ------------------------------------------------------------ labels/legends

def test_dew_sparkline_has_direct_end_labels():
    assert "hs-endlabel" in APP_JS
    assert "'indoor'" in APP_JS and "'outside'" in APP_JS


def test_crawl_legend_names_the_place():
    assert "crawl humidity" in INDEX_HTML
    assert "crawl temp (right axis)" in INDEX_HTML


def test_big_numbers_self_identify():
    assert "% RH indoor" in APP_JS
    assert "% RH crawl" in APP_JS


def test_crawl_tooltip_labels_its_temperature():
    assert "crawl temp ${" in APP_JS


def test_attribution_legend_swatches():
    attr = MO_HTML
    assert re.search(r'sw-crawlavg"></span>crawl dew point', attr)
    assert re.search(r'sw-moout"></span>outdoor dew point', attr)
    assert re.search(r'sw-moref"></span>indoor \(reference\)', attr)


def test_scene_tooltip_explains_the_outdoor_slot_misnomer():
    assert "crawl space" in APP_JS.split("WH32", 1)[1][:120].lower(), \
        "the WH32 tooltip must say the sensor is in the crawl, not just 'outdoor slot'"


# ------------------------------------------------------------- square tile

def test_square_labels_carry_the_place_hues():
    """The tile has no charts, so the place palette lives on its labels:
    inside teal, outside amber, crawl violet — same meaning as every chart."""
    assert "var(--cool)" in _css_rule(".sq-inlabel")
    assert "245, 164, 92" in _css_rule(".sq-outlabel")
    assert "var(--crawl-line)" in _css_rule(".sq-crawl")


def test_square_uses_only_shared_threshold_logic():
    """square.js must not define its own copies of the range/status logic —
    a threshold tweaked in one file but not the other would let the same
    reading be green on the calendar and red on the dashboard."""
    for fn in ("tempClass", "rhClass", "crawlRhClass", "crawlTempClass",
               "equipmentState", "bandTierLabel", "pmChip", "aqiChipClass"):
        assert f"function {fn}" in COMMON_JS, f"{fn} left common.js"
        assert f"function {fn}" not in SQ_JS, f"square.js redefines {fn}"
        assert f"function {fn}" not in APP_JS, f"app.js redefines {fn} — single-source it in common.js"


def test_square_has_no_charts():
    """The tile stays numbers-only by request (2026-08-12: the mini-chart
    was tried and removed — 'keep it clean'). Reintroducing an svg chart
    here should be a deliberate decision, not drift."""
    assert "<svg" not in SQ_HTML.split("<body", 1)[1]
    assert "svgEl(" not in SQ_JS and "timePath(" not in SQ_JS


def test_square_scales_with_the_viewport():
    """The same page must work as a small widget square and the full 21.5"
    screen: the type scale is vmin-based, never fixed-only."""
    sq_block = CSS[CSS.index("square tile (square.html)"):CSS.index("@media print")]
    assert "vmin" in _css_rule(".sq-big")
    assert "100dvh" in sq_block, "the tile must fill the viewport height"
    assert "overflow: hidden" in _css_rule(".sq-page"), "a wall tile must never scroll"


def test_square_keeps_last_data_on_poll_failure():
    """One failed poll must dim, not blank: the catch path may only touch
    conn state + the updated stamp, never wipe the rendered numbers."""
    catch_body = SQ_JS.split("} catch", 1)[1].split("}", 1)[0]
    assert "innerHTML" not in catch_body
    assert "'offline'" in catch_body


# ---------------------------------------------------------------- plumbing

@pytest.mark.parametrize("html,page_js", [(INDEX_HTML, "app.js"), (MO_HTML, "moisture.js"),
                                          (SQ_HTML, "square.js")])
def test_common_js_loads_before_page_script(html, page_js):
    # ?v=N cache busters (2026-08-13) are not part of the script's identity
    scripts = [s.split("?")[0] for s in re.findall(r'<script src="([^"]+)"', html)]
    assert scripts.index("common.js") < scripts.index(page_js), \
        f"{page_js} depends on common.js globals — order matters for classic scripts"


def test_app_js_has_peak_strip():
    """peakStripHtml lives in common.js (shared, testable); app.js only
    calls it — 2026-08-13 move so it could get real behavioral tests."""
    src = (STATIC / "app.js").read_text()
    assert "peakStripHtml(" in src
    assert "function peakStripHtml" not in src, "app.js redefines peakStripHtml — single-source it in common.js"
    assert "function peakStripHtml" in COMMON_JS
    assert "peak-strip" in COMMON_JS
    assert "bandTierLabel(" not in src.split("function renderRail")[1][:2000]  # band label is the square tile's, not the rail's


def test_app_js_has_smoke_banner():
    """smokeBannerHtml lives in common.js (shared, testable); app.js only
    calls it — 2026-08-13 move (mirrors peakStripHtml) so it could get real
    behavioral tests instead of only this grep."""
    src = (STATIC / "app.js").read_text()
    assert "smokeBannerHtml(" in src
    assert "function smokeBannerHtml" not in src, "app.js redefines smokeBannerHtml — single-source it in common.js"
    assert "function smokeBannerHtml" in COMMON_JS
    assert "smoke-banner" in COMMON_JS
    assert "AQI_UNHEALTHY" in src


def test_legend_peak_rate_is_config_driven():
    """The ribbon legend's on-peak rate used to be a hardcoded '$0.43/kWh'
    that disagreed with the config-driven peak strip for any non-default
    TOU config. It must now be JS-settable from the API's peak_rate field."""
    assert "$0.43" not in INDEX_HTML, "on-peak rate must not be hardcoded in the legend"
    assert 'id="legend-peak-rate"' in INDEX_HTML
    assert "legend-peak-rate" in APP_JS
    assert "peak_rate" in APP_JS


def test_ribbon_peak_shading_is_config_driven():
    """The ribbon's on-peak shading used to hardcode a weekday 17:00-21:00
    window (`new Date(..., 17)` / `new Date(..., 21)`), which shaded the
    wrong hours for any utility whose peak isn't weekday 5-9pm. It must now
    derive the shaded window(s) from the API's peak_windows field instead."""
    shading_block = APP_JS.split("// On-peak shading", 1)[1].split("// temperature gridlines", 1)[0]
    assert ", 17)" not in shading_block and ", 21)" not in shading_block, \
        "ribbon shading must not hardcode peak hours"
    assert "peak_windows" in shading_block
    assert "renderRibbon(history, timeline, cost)" in APP_JS, \
        "renderRibbon must receive cost so it can read peak_windows"


def test_no_external_resources():
    """LAN wall display, no internet: every asset must be local."""
    for name, text in (("index.html", INDEX_HTML), ("moisture.html", MO_HTML),
                       ("square.html", SQ_HTML), ("styles.css", CSS)):
        assert not re.search(r'(?:src|href)="https?://', text), f"{name} references the internet"
        assert "@import" not in text, f"{name} pulls a remote stylesheet"


# --- versioning guards ------------------------------------------------------
# The dashboard's (index.html) asset cache-busts are unified to the app version
# (scripts/release.py is the sole writer). These pin that invariant so a stray
# hand-edit that reintroduces a drifting ?v= number fails CI. (moisture.html and
# square.html keep their own independent per-page counters — release.py stamps
# only index.html, matching where the versioned frontend lives.)
ROOT = Path(__file__).resolve().parents[1]


def test_version_file_is_clean_semver():
    v = (ROOT / "VERSION").read_text().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), f"VERSION must be clean SemVer, got {v!r}"


def test_every_index_cache_bust_equals_the_version():
    version = (ROOT / "VERSION").read_text().strip()
    busts = re.findall(r"\?v=([\w.]+)", INDEX_HTML)
    assert busts, "index.html should carry ?v= cache-busts on its assets"
    drifted = sorted({b for b in busts if b != version})
    assert not drifted, (f"index.html cache-busts must all equal VERSION ({version}); "
                         f"drifted: {drifted} — run scripts/release.py, don't hand-edit ?v=")


def test_dunder_version_matches_the_version_file():
    import house_climate
    assert house_climate.__version__ == (ROOT / "VERSION").read_text().strip()


def test_dockerfile_ships_version():
    """The running app reads VERSION at the repo root (=/app in the image) for
    /api/version + house_climate.__version__. If the Dockerfile doesn't COPY it,
    the readout silently falls back to '0.0.0+unknown' — fail-soft hiding a real
    break. Guard so the image always carries it."""
    dockerfile = (ROOT / "web.Dockerfile").read_text()
    assert re.search(r"COPY\s+.*\bVERSION\b", dockerfile), \
        "web.Dockerfile must COPY VERSION into the image"
