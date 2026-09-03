"""Does crawl-space air actually reach the floors above?

The crawl is the wettest air in the building. Whether that matters upstairs
depends entirely on whether the air moves, and no amount of staring at two
humidity charts settles it — both sensors follow the weather, so they look
related whatever the truth is.

This module measures the TRANSPORT GAIN: when the crawl gets damper by some
amount on its own (not because the weather got damper), how much of that shows
up on the floor above? A gain of 0.4 means four tenths of every crawl
excursion arrives upstairs. A gain indistinguishable from zero means the crawl
is a contained problem.

Three deliberate choices, each closing a way of fooling yourself:

1. A regression SLOPE, not a correlation. A correlation coefficient scales
   with how much the crawl happens to be swinging, so it falls after a barrier
   dries the crawl even when the air path is untouched. The slope does not
   move for that reason, and it is the number that supports a prediction.

2. Everything is measured against the trailing week, not against zero. Both
   sensors drift together across a season; subtracting a centred seven-day
   mean removes the drift and leaves the multi-day excursions where the signal
   actually lives.

3. Hourly readings are not independent observations. This hour looks like the
   last one, so a month of data carries far less information than 720 would
   suggest. Every interval here is widened for that, and the honest count is
   reported alongside the raw one.

Same house rule as the rest of the stack: nothing is shown until it can be
shown honestly. Every gate below refuses with a named reason.
"""
import math
from datetime import timedelta

# --- preprocessing -----------------------------------------------------------
# Window for the centred moving mean that removes slow drift. Seven days, not
# one: the signal of interest is a multi-day one (rain wets the soil, the crawl
# runs damp for days, the house follows hours later). A 24-hour window would
# delete exactly the band being measured.
DETREND_WINDOW_H = 168
DETREND_MIN_COVERAGE = 0.7   # of the window's hours, before an anomaly is formed

# Capacitive RH sensors go nonlinear near saturation, and a vented crawl sits
# there often. Moisture computed from a pinned sensor is not a measurement.
CRAWL_SATURATED_RH = 95.0

# --- model -------------------------------------------------------------------
MAX_LAG_H = 6                # transport delay searched, 0..6 hours
N_LAGS_TESTED = MAX_LAG_H + 1
# Newey-West bandwidth, in hours. Fixed from building physics (a house's
# moisture time constants run under a day or two) rather than fitted, so the
# number is deterministic and cheap to recompute on every page load.
HAC_BANDWIDTH_H = 48
# Hour-of-day means are removed from each series before fitting. That is
# algebraically identical to putting 23 hour dummies plus an intercept in the
# regression, but leaves a 3-column solve instead of a 26-column one. The
# degrees of freedom still have to pay for them, hence this count.
HOUR_OF_DAY_PARAMS = 24

# --- gates -------------------------------------------------------------------
MIN_WINDOW_DAYS = 21         # a 7-day window can never clear MIN_N_EFF honestly
MIN_N_EFF = 30
MIN_COVERAGE = 0.85
MAX_GAP_H = 24
# Minimum spread the crawl must show on its own, after the weather and the
# daily rhythm are accounted for, in g/m^3. Roughly twice sensor noise.
MIN_CRAWL_ANOM_SD = 0.3

# Smallest degrees of freedom the interval is allowed to be built on. Below
# this the critical value climbs so steeply (dof 3 needs ~7.5, dof 1 needs ~89)
# that any table would be extrapolating, so the fit is refused instead.
MIN_DOF = 4

# Two-sided t critical values at alpha = 0.05/7 — Bonferroni across the seven
# lags searched, the same discipline the rainfall lag correlation already
# applies. Computed, not remembered. dof rounds DOWN to the nearest anchor,
# giving a LARGER critical value and so a wider interval: conservative in the
# direction that matters. The low anchors are here because they are reachable:
# 24 of the degrees of freedom are spent absorbing the time-of-day pattern, so
# a window that only just clears MIN_N_EFF lands in single digits.
_T_CRIT_BONF7 = [(4, 5.068), (5, 4.382), (6, 3.997), (8, 3.584), (10, 3.368),
                 (12, 3.236), (15, 3.112), (20, 2.996), (30, 2.887),
                 (40, 2.836), (60, 2.785), (120, 2.737), (10 ** 9, 2.690)]


def t_crit_bonf7(dof):
    """Critical value for the transport-gain interval.

    Below MIN_DOF there is no honest answer, so this returns infinity and the
    callers refuse. Returning the smallest tabulated value there — which is
    what an unguarded table lookup does — would hand back an interval roughly
    half the width the data supports, on exactly the marginal fits the gates
    exist to catch."""
    if dof < MIN_DOF:
        return float("inf")
    t = _T_CRIT_BONF7[0][1]
    for max_dof, tv in _T_CRIT_BONF7:
        if max_dof <= dof:
            t = tv
        else:
            break
    return t


# ---------------------------------------------------------------------------
# Linear algebra (pure Python — this package ships no numeric dependency, and
# the matrices here are tiny by construction)
# ---------------------------------------------------------------------------

def solve(a, b):
    """Solve a square system by Gaussian elimination with partial pivoting.
    None when singular — which for this module means collinear predictors, a
    refusal rather than an error."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-12:
            return None
        m[col], m[piv] = m[piv], m[col]
        for r in range(n):
            if r == col:
                continue
            f = m[r][col] / m[col][col]
            for c in range(col, n + 1):
                m[r][c] -= f * m[col][c]
    return [m[i][n] / m[i][i] for i in range(n)]


def ols(X, y):
    """Ordinary least squares by normal equations.
    Returns {beta, resid, xtx} or None when the columns are collinear."""
    if not X or len(X) != len(y):
        return None
    k = len(X[0])
    xtx = [[0.0] * k for _ in range(k)]
    xty = [0.0] * k
    for row, yi in zip(X, y):
        for i in range(k):
            xty[i] += row[i] * yi
            for j in range(i, k):
                xtx[i][j] += row[i] * row[j]
    for i in range(k):
        for j in range(i):
            xtx[i][j] = xtx[j][i]
    beta = solve(xtx, xty)
    if beta is None:
        return None
    resid = [yi - sum(b * v for b, v in zip(beta, row)) for row, yi in zip(X, y)]
    return {"beta": beta, "resid": resid, "xtx": xtx}


def _unit(k, col):
    e = [0.0] * k
    e[col] = 1.0
    return e


def hac_var(X, resid, xtx, buckets, col=0, bandwidth=HAC_BANDWIDTH_H):
    """Newey-West variance of ONE coefficient, tolerant of gaps.

    Only one element of the sandwich is ever needed, so instead of building a
    full k-by-k covariance this projects each row onto v = (X'X)^-1 e_col and
    works with scalars. That turns an O(n * bandwidth * k^2) computation into
    O(n * bandwidth), which is what makes it affordable on a page load.

    Lags are matched on the CLOCK, not on row position: after an outage, the
    row before is not the hour before, and pairing them would invent a
    correlation across the gap.
    """
    k = len(X[0])
    v = solve(xtx, _unit(k, col))
    if v is None:
        return None
    z = [sum(vi * xi for vi, xi in zip(v, row)) for row in X]
    ez = [r * zi for r, zi in zip(resid, z)]
    total = sum(t * t for t in ez)
    index = {b: i for i, b in enumerate(buckets)}
    for lag in range(1, bandwidth + 1):
        weight = 1.0 - lag / (bandwidth + 1.0)
        acc = 0.0
        delta = timedelta(hours=lag)
        for i, b in enumerate(buckets):
            j = index.get(b - delta)
            if j is not None:
                acc += ez[i] * ez[j]
        total += 2.0 * weight * acc
    # A HAC estimate can come out non-positive in small samples. The tempting
    # fallback — the uncorrected variance — is the NARROWEST number this
    # function can produce, an order of magnitude below the honest one on
    # hourly data. Substituting it would turn "the correction broke down" into
    # "we are very confident", which is the exact failure this module exists to
    # avoid. Refuse instead; the caller drops the lag.
    if total <= 0:
        return None
    return total


# ---------------------------------------------------------------------------
# Preprocessing
# ---------------------------------------------------------------------------

def centered_anomalies(by_bucket, buckets, window_h=DETREND_WINDOW_H,
                       min_coverage=DETREND_MIN_COVERAGE):
    """Each hour's departure from its own centred moving mean.

    Returns {bucket: anomaly or None}. An hour whose window is less than
    `min_coverage` full gets None rather than an anomaly measured against a
    half-built baseline — near an outage that difference is large and would
    read as a real excursion.
    """
    half = window_h // 2
    need = int(window_h * min_coverage)
    out = {}
    for b in buckets:
        vals = []
        for off in range(-half, half + 1):
            v = by_bucket.get(b + timedelta(hours=off))
            if v is not None:
                vals.append(v)
        if len(vals) < need:
            out[b] = None
            continue
        here = by_bucket.get(b)
        out[b] = None if here is None else here - sum(vals) / len(vals)
    return out


def _remove_hour_of_day_means(rows, keys):
    """Subtract each series' own hour-of-day mean, in place.

    This is the same thing as fitting 23 hour-of-day dummies alongside the
    predictors and reading off the same slope, only far cheaper. It exists
    because a house's daily rhythm — showers, cooking, the AC cycling — is
    periodic without being weather-driven, and can line up with the crawl's
    daily rhythm by pure coincidence. Left in, that coincidence would be
    reported as air movement.
    """
    for key in keys:
        sums, counts = {}, {}
        for r in rows:
            h = r["bucket"].hour
            sums[h] = sums.get(h, 0.0) + r[key]
            counts[h] = counts.get(h, 0) + 1
        for r in rows:
            h = r["bucket"].hour
            r[key] -= sums[h] / counts[h]


def lag1_autocorr(vals, stamps=None, step=timedelta(hours=1)):
    """How much each reading resembles the one before it.

    When `stamps` is given, "the one before it" means one `step` earlier ON THE
    CLOCK, not one position earlier in the list. Rows either side of an outage
    are far less alike than true neighbours, so pairing them across the gap
    drags the estimate down — which would inflate the effective sample size and
    narrow every interval built from it. hac_var already matches its lags on
    the clock for the same reason.

    Returns 0.0 when undefined (too few points, no spread)."""
    n = len(vals)
    if n < 3:
        return 0.0
    if stamps is None:
        pairs = [(vals[i], vals[i - 1]) for i in range(1, n)]
    else:
        index = {b: i for i, b in enumerate(stamps)}
        pairs = [(vals[i], vals[index[b - step]])
                 for i, b in enumerate(stamps) if (b - step) in index]
    if len(pairs) < 3:
        return 0.0
    # Correlation of the paired vectors, each against its own mean, so dropped
    # pairs cannot bias the result the way a shared denominator would.
    n_p = len(pairs)
    mx = sum(a for a, _ in pairs) / n_p
    my = sum(b for _, b in pairs) / n_p
    sxx = sum((a - mx) ** 2 for a, _ in pairs)
    syy = sum((b - my) ** 2 for _, b in pairs)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    return max(-0.99, min(0.99, sxy / math.sqrt(sxx * syy)))


def effective_n(x, resid, stamps=None):
    """How many independent observations a run of correlated hours is really
    worth, via the standard Bartlett adjustment for a regression coefficient.

    Hour-to-hour readings repeat each other, so the raw count badly overstates
    how much the data actually says. With the crawl and the residuals each
    correlating about 0.85 with their own previous hour, a 30-day window of 720
    hours is worth roughly 115 independent readings — a sixth of its length.
    Reporting the raw hour count beside an interval computed from it would be
    the single most misleading thing this module could do, so the honest count
    is what gets shown.
    """
    n = min(len(x), len(resid))
    if n < 3:
        return n
    rho = lag1_autocorr(x, stamps) * lag1_autocorr(resid, stamps)
    if rho >= 1.0:
        return 2
    eff = n * (1.0 - rho) / (1.0 + rho)
    return max(2, min(n, int(round(eff))))


# ---------------------------------------------------------------------------
# The transport-gain fit
# ---------------------------------------------------------------------------

def _hourly_map(series, key="ah"):
    return {r["bucket"]: r[key] for r in series
            if r.get(key) is not None and r.get("bucket") is not None}


def _prepare(crawl, floor, outdoor, days, now, crawl_rh, extra=None):
    """Shared setup: trim to the window, drop saturated crawl hours, check
    coverage, and turn every series into departures from its trailing week.

    Returns (rows, info) where rows carry aligned anomalies, or (None, refusal).
    """
    since = now - timedelta(days=days)
    crawl_by = _hourly_map(crawl)
    floor_by = _hourly_map(floor)
    out_by = _hourly_map(outdoor)
    extra_by = {name: _hourly_map(s, key) for name, (s, key) in (extra or {}).items()}

    dropped = 0
    if crawl_rh:
        for r in crawl_rh:
            rh, bucket = r.get("rh"), r.get("bucket")
            # A null humidity cannot be saturated-checked, but it also cannot
            # have produced a dew point, so sensor_hourly_ah has already
            # excluded that hour — there is nothing here to drop.
            if rh is not None and rh >= CRAWL_SATURATED_RH and bucket in crawl_by:
                del crawl_by[bucket]
                dropped += 1

    # Anomalies need DETREND_WINDOW_H/2 of context either side of the window,
    # so they are computed over the wider span and only then trimmed.
    all_buckets = sorted(set(crawl_by) | set(floor_by) | set(out_by))
    if not all_buckets:
        return None, {"ready": False, "reason": "no_data", "dropped_saturated": dropped}

    hours_in_window = int(days * 24)
    window_buckets = [b for b in all_buckets if b >= since]
    coverage_pairs = [b for b in window_buckets
                      if b in crawl_by and b in floor_by and b in out_by]
    coverage = len(coverage_pairs) / hours_in_window if hours_in_window else 0.0
    info = {"dropped_saturated": dropped, "coverage": round(coverage, 3),
            "n_window_hours": len(coverage_pairs)}

    if coverage < MIN_COVERAGE:
        return None, {"ready": False, "reason": "thin_coverage",
                      "need_coverage": MIN_COVERAGE, **info}

    gap = _longest_gap_h(coverage_pairs, since, now)
    info["longest_gap_h"] = gap
    if gap > MAX_GAP_H:
        return None, {"ready": False, "reason": "outage",
                      "max_gap_h": MAX_GAP_H, **info}

    ca = centered_anomalies(crawl_by, all_buckets)
    fa = centered_anomalies(floor_by, all_buckets)
    oa = centered_anomalies(out_by, all_buckets)

    rows = []
    for b in window_buckets:
        if ca.get(b) is None or fa.get(b) is None or oa.get(b) is None:
            continue
        row = {"bucket": b, "crawl": ca[b], "floor": fa[b], "outdoor": oa[b]}
        # Extra covariates enter at their raw level, not as anomalies: the
        # temperature difference that drives stack flow matters as a level.
        ok = True
        for name, m in extra_by.items():
            v = m.get(b)
            if v is None:
                ok = False
                break
            row[name] = v
        if ok:
            rows.append(row)
    if len(rows) < MIN_N_EFF:
        return None, {"ready": False, "reason": "insufficient_n_eff", **info}
    return rows, info


def _longest_gap_h(buckets, since, now):
    """Largest run of consecutive missing hours inside the window, counting the
    edges — an outage at the start of the window is still an outage."""
    if not buckets:
        return int((now - since).total_seconds() // 3600)
    ordered = sorted(buckets)
    worst = int((ordered[0] - since).total_seconds() // 3600)
    worst = max(worst, int((now - ordered[-1]).total_seconds() // 3600) - 1)
    for a, b in zip(ordered, ordered[1:]):
        worst = max(worst, int((b - a).total_seconds() // 3600) - 1)
    return max(0, worst)


def _straddles(interventions, since, now):
    for iv in interventions or []:
        d = iv.get("marked_on")
        if d is not None and since.date() < d <= now.date():
            return True
    return False


def _fit_at_lag(rows, lag, extra_cols=(), diagnose=False):
    """Fit the floor's moisture on the crawl's, at one transport delay.

    Hour-of-day means come out of every column first (see
    _remove_hour_of_day_means), which is why no dummy columns appear here.
    """
    by_bucket = {r["bucket"]: r for r in rows}
    paired = []
    delta = timedelta(hours=lag)
    for r in rows:
        src = by_bucket.get(r["bucket"] - delta)
        if src is None:
            continue
        row = {"bucket": r["bucket"], "crawl": src["crawl"],
               "floor": r["floor"], "outdoor": r["outdoor"]}
        for name in extra_cols:
            row[name] = r[name]
        paired.append(row)
    if len(paired) < MIN_N_EFF:
        return None

    # EVERY column has to have its hour-of-day mean removed, not just the main
    # three. Absorbing the daily rhythm this way only reproduces the answer a
    # full set of hour dummies would give if each predictor is treated the same
    # way; leaving a covariate with its daily shape intact (blower duty has a
    # very strong one) would bias the transport gain.
    _remove_hour_of_day_means(paired, ["crawl", "floor", "outdoor", *extra_cols])

    # An optional covariate can survive that with nothing left — air-handler
    # duty that runs to the same schedule every day IS the time of day, and
    # once the daily rhythm is removed it is a column of zeros. Including it
    # would make the system singular and lose the transport gain entirely, so
    # a covariate that has stopped varying is simply left out. The two main
    # predictors are never dropped: if either has gone flat, that is a real
    # refusal and the gates upstream report it.
    usable_extra = tuple(name for name in extra_cols
                         if _sd([r[name] for r in paired]) > 1e-9)

    X, y, buckets = [], [], []
    for r in paired:
        cols = [r["crawl"], r["outdoor"]]
        cols.extend(r[name] for name in usable_extra)
        cols.append(1.0)
        X.append(cols)
        y.append(r["floor"])
        buckets.append(r["bucket"])
    fit = ols(X, y)
    if fit is None:
        return None

    n_params = len(X[0]) + HOUR_OF_DAY_PARAMS - 1     # intercept already counted
    n_eff = effective_n([r[0] for r in X], fit["resid"], buckets)
    dof = n_eff - n_params
    if diagnose:
        # Used only to explain a refusal: report what the window is worth
        # without applying the gates that rejected it.
        return {"beta": fit["beta"][0], "se": None, "lag": lag, "n": len(X),
                "n_eff": n_eff, "dof": dof, "t": 0.0, "dropped_covariates": []}
    if dof < MIN_DOF:
        return None
    var = hac_var(X, fit["resid"], fit["xtx"], buckets, col=0)
    # A zero variance would sail through as a significant result with a
    # zero-width interval, so it is refused alongside the impossible ones.
    if var is None or var <= 0:
        return None
    # The HAC sum is built from the full hourly sample; the interval it feeds
    # must be scaled to the honest sample size, or the correlation correction
    # would be applied to the critical value and quietly undone here.
    se = math.sqrt(var) * math.sqrt(max(len(X), 1) / n_eff)
    return {"beta": fit["beta"][0], "se": se, "lag": lag, "n": len(X),
            "n_eff": n_eff, "dof": dof, "resid": fit["resid"],
            "t": (fit["beta"][0] / se) if se > 0 else 0.0,
            "dropped_covariates": [n for n in extra_cols if n not in usable_extra]}


def coupling_window(crawl, floor, outdoor, days=30, now=None, crawl_rh=None,
                    interventions=None, blower=None):
    """How much of a crawl moisture excursion reaches this floor.

    Returns {ready, beta, ci95, lag, n, n_eff, t, ...} or {ready: False,
    reason} naming which gate refused.

    `beta` is a fraction: 0.4 means four tenths of the crawl's independent
    dampness arrives here, roughly `lag` hours later.
    """
    if now is None:
        raise ValueError("coupling_window needs an explicit `now`")
    base = {"days": days, "need_days": MIN_WINDOW_DAYS}
    if days < MIN_WINDOW_DAYS:
        return {"ready": False, "reason": "window_too_short", **base}
    since = now - timedelta(days=days)
    if _straddles(interventions, since, now):
        return {"ready": False, "reason": "straddles_intervention", **base}

    extra = {"blower": (blower, "duty")} if blower else None
    rows, info = _prepare(crawl, floor, outdoor, days, now, crawl_rh, extra=extra)
    if rows is None:
        # _prepare's refusals set their own reason; ready is forced here so a
        # future refusal branch that forgets it cannot read as a success.
        return {"ready": False, **base, **info}

    # How much the crawl moves on its OWN, once the weather and the daily
    # rhythm are accounted for. This is checked BEFORE any fit: when the crawl
    # has become an echo of outdoor air the regression is singular and every
    # lag fails, and reporting that as a broken fit would disguise a
    # meaningful result — the expected state after a barrier works — as a
    # malfunction.
    crawl_sd = _crawl_independent_sd(rows)
    if crawl_sd < MIN_CRAWL_ANOM_SD:
        return {"ready": False, "reason": "weak_signal",
                "need_crawl_sd": MIN_CRAWL_ANOM_SD, "crawl_sd": round(crawl_sd, 3),
                **base, **info}

    extra_cols = ("blower",) if extra else ()
    fits = [f for f in (_fit_at_lag(rows, lag, extra_cols)
                        for lag in range(N_LAGS_TESTED)) if f is not None]
    if not fits:
        # Say WHICH wall was hit. Much the commonest cause is that the window,
        # once discounted for how much each hour repeats the last, is worth too
        # few independent readings to carry an interval — a fact about the data
        # worth reporting, not the shrug that "no fit" gives the reader.
        diag = _fit_at_lag(rows, 0, extra_cols, diagnose=True)
        if diag is not None and diag["dof"] < MIN_DOF:
            return {"ready": False, "reason": "insufficient_n_eff",
                    "need_n_eff": MIN_N_EFF, **base, **info,
                    "n": diag["n"], "n_eff": diag["n_eff"],
                    "crawl_sd": round(crawl_sd, 3), "beta": None}
        return {"ready": False, "reason": "no_fit", **base, **info}

    best = max(fits, key=lambda f: abs(f["t"]))
    result = {**base, **info, "lag": best["lag"], "n": best["n"],
              "n_eff": best["n_eff"], "dof": best["dof"],
              "crawl_sd": round(crawl_sd, 3),
              "beta": round(best["beta"], 3), "t": round(best["t"], 2),
              "dropped_covariates": best["dropped_covariates"],
              "alpha": round(0.05 / N_LAGS_TESTED, 4)}
    if best["n_eff"] < MIN_N_EFF or best["dof"] < MIN_DOF:
        # A refused fit must not leave a usable-looking estimate behind: a
        # caller filtering on "is there a beta?" would otherwise publish it.
        return {"ready": False, "reason": "insufficient_n_eff",
                "need_n_eff": MIN_N_EFF, **result, "beta": None}

    crit = t_crit_bonf7(best["dof"])
    ci = crit * best["se"]
    result["ci95"] = round(ci, 3)
    if best["beta"] < 0 and abs(best["beta"]) > ci:
        return {"ready": False, "reason": "inconsistent_sign", **result}
    result["ready"] = True
    result["significant"] = best["beta"] > ci
    return result


def _sd(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


def _crawl_independent_sd(rows):
    """Spread of the crawl's moisture that the weather and the time of day do
    NOT already explain, in g/m^3.

    This is the quantity the whole estimate divides by. When it approaches
    sensor noise there is no independent crawl signal left to trace into the
    house, and any transport number would be noise over noise. A crawl that
    has become a perfect echo of outdoor air makes the regression singular,
    which is reported here as zero rather than as a failure to fit.
    """
    work = [{"bucket": r["bucket"], "crawl": r["crawl"], "outdoor": r["outdoor"]}
            for r in rows]
    if len(work) < 3:
        return 0.0
    _remove_hour_of_day_means(work, ["crawl", "outdoor"])
    fit = ols([[r["outdoor"], 1.0] for r in work], [r["crawl"] for r in work])
    if fit is None:
        return 0.0
    return _sd(fit["resid"])


def stack_signature(crawl, floor, outdoor, temp_diff, days=30, now=None,
                    crawl_rh=None, interventions=None):
    """Does the transport strengthen as indoor-minus-outdoor temperature grows?

    This is the guard against the confounder that would otherwise sink the
    whole analysis. A vented crawl is, in effect, a better local weather
    station than a real one several miles away. So the crawl can appear to
    drive the upstairs air simply by carrying local weather information the
    official feed misses — with no air moving between them at all.

    Stack effect is driven by the temperature difference across the building.
    Genuine transport therefore gets stronger as that difference grows; a
    crawl-as-local-weather artefact does not care about it. A clearly positive
    result here is affirmative evidence that air is actually moving.
    """
    if now is None:
        raise ValueError("stack_signature needs an explicit `now`")
    base = {"days": days, "need_days": MIN_WINDOW_DAYS}
    if days < MIN_WINDOW_DAYS:
        return {"ready": False, "reason": "window_too_short", **base}
    since = now - timedelta(days=days)
    if _straddles(interventions, since, now):
        return {"ready": False, "reason": "straddles_intervention", **base}

    rows, info = _prepare(crawl, floor, outdoor, days, now, crawl_rh,
                          extra={"dt": (temp_diff, "dt")})
    if rows is None:
        return {"ready": False, **base, **info}

    # The same identifiability gate coupling_window applies. Without it this
    # function would publish the page's strongest causal claim ("air genuinely
    # moving up through the building") from a crawl that no longer moves on
    # its own.
    crawl_sd = _crawl_independent_sd(rows)
    if crawl_sd < MIN_CRAWL_ANOM_SD:
        return {"ready": False, "reason": "weak_signal", **base, **info,
                "crawl_sd": round(crawl_sd, 3)}

    # The transport delay is chosen on the interaction term itself — the thing
    # actually being tested — rather than borrowed from the plain fit.
    fits = [f for f in (_interaction_fit(rows, lag)
                        for lag in range(N_LAGS_TESTED)) if f is not None]
    if not fits:
        return {"ready": False, "reason": "no_fit", **base, **info}
    best = max(fits, key=lambda f: abs(f["t"]))
    if best["n_eff"] < MIN_N_EFF:
        return {"ready": False, "reason": "insufficient_n_eff",
                "need_n_eff": MIN_N_EFF, **base, **info,
                "n_eff": best["n_eff"], "delta": None}
    return {"ready": True, **base, **info, "crawl_sd": round(crawl_sd, 3), **best}


def _interaction_fit(rows, lag):
    """Fit the floor on the lagged crawl, the outdoor air, and the crawl scaled
    by the indoor-to-outdoor temperature difference.

    That last column is the whole point: it asks whether the crawl's influence
    grows when the building is being pushed harder. Its coefficient is
    reported as `delta`."""
    by_bucket = {r["bucket"]: r for r in rows}
    paired = []
    delta = timedelta(hours=lag)
    for r in rows:
        src = by_bucket.get(r["bucket"] - delta)
        if src is None:
            continue
        paired.append({"bucket": r["bucket"], "crawl": src["crawl"],
                       "floor": r["floor"], "outdoor": r["outdoor"],
                       "inter": src["crawl"] * r["dt"]})
    if len(paired) < MIN_N_EFF:
        return None
    _remove_hour_of_day_means(paired, ["crawl", "floor", "outdoor", "inter"])
    X = [[r["inter"], r["crawl"], r["outdoor"], 1.0] for r in paired]
    y = [r["floor"] for r in paired]
    buckets = [r["bucket"] for r in paired]
    fit = ols(X, y)
    if fit is None:
        return None
    n_eff = effective_n([r[0] for r in X], fit["resid"], buckets)
    dof = n_eff - (len(X[0]) + HOUR_OF_DAY_PARAMS - 1)
    if dof < MIN_DOF:
        return None
    var = hac_var(X, fit["resid"], fit["xtx"], buckets, col=0)
    if var is None or var <= 0:
        return None
    se = math.sqrt(var) * math.sqrt(max(len(X), 1) / n_eff)
    ci = t_crit_bonf7(dof) * se
    return {"delta": round(fit["beta"][0], 5), "ci95": round(ci, 5),
            "n_eff": n_eff, "lag": lag, "n": len(X),
            "beta": round(fit["beta"][1], 3),
            "t": (fit["beta"][0] / se) if se > 0 else 0.0}


# ---------------------------------------------------------------------------
# The proof: predict the upstairs benefit, then check it
# ---------------------------------------------------------------------------

def prediction_test(beta, beta_ci, d_crawl, d_crawl_ci, d_floor, d_floor_ci):
    """Did the floor above change by as much as the transport gain predicted?

    A ground vapour barrier changes where the moisture COMES FROM, not the path
    air takes through the building. So the transport gain itself should not
    move — what moves is the crawl, and the floor above should follow it by
    the gain. Predicting that number in advance and then checking it is a far
    stronger claim than any single correlation: it says the mechanism is real
    AND says how much of it reaches the living space.
    """
    if beta is None or beta_ci is None or d_crawl is None or d_floor is None:
        return {"verdict": "collecting", "predicted": None, "observed": d_floor}
    predicted = beta * d_crawl
    # An unknown uncertainty is NOT a zero uncertainty. Treating it as zero
    # narrows the bar and pushes the answer toward "did not match", which the
    # page states as a confident negative — "the crawl is not the main source
    # of moisture here" — on the strength of a measurement that was never made.
    if d_crawl_ci is None or d_floor_ci is None:
        return {"verdict": "inconclusive", "predicted": round(predicted, 3),
                "ci95": None, "observed": round(d_floor, 3),
                "observed_ci95": d_floor_ci}
    # Interval for a product of two measured quantities, each with its own
    # uncertainty, treated as independent.
    ci = math.sqrt((beta_ci * d_crawl) ** 2 + (beta * d_crawl_ci) ** 2)
    total = ci + d_floor_ci
    out = {"predicted": round(predicted, 3), "ci95": round(ci, 3),
           "observed": round(d_floor, 3), "observed_ci95": d_floor_ci}
    out["verdict"] = "confirmed" if abs(d_floor - predicted) <= total else "not_confirmed"
    return out


def consistency_check(floors, ordered=True, excluded=None):
    """Crawl air rising through the building has to pass the lower floor first.

    So the floor nearest the crawl should couple at least as hard, and at least
    as quickly, as the one above it. When an upper floor couples HARDER, the
    air is not seeping up through the floor assembly — it is taking a shortcut,
    which in practice means leaky ducts running through the crawl, or an open
    chase. That is a different repair, so it is worth saying out loud.

    `floors` must be ordered from the crawl upward, and the caller must say so
    via `ordered`. Nothing in a sensor's configuration records how high up the
    building it sits, so when the order cannot be established this refuses
    rather than guessing — an inverted list would produce a confident,
    specific and expensive repair recommendation from nothing but the order
    two channels happen to be listed in.
    """
    note = ""
    if excluded:
        # Saying which sensors took part is not a detail. Without it the
        # verdict below reads as a statement about the whole house while
        # having been computed on a subset — and the largest coupling on the
        # page can be the one that was left out.
        note = (" Not included: " + ", ".join(excluded)
                + " — the name does not say where in the house it sits.")
    if not ordered:
        return {"verdict": "unknown_order", "excluded": excluded or [],
                "text": "Which sensor sits higher in the house is not recorded, "
                        "so the path between floors cannot be checked. Naming the "
                        "channels for their floors (for example 'Downstairs' and "
                        "'Upstairs') is enough."}
    # `ready` is the contract, not "has a beta": a refused fit still carries
    # its point estimate for display, and must never be reasoned from.
    usable = [f for f in floors
              if f.get("ready") and f.get("beta") is not None
              and f.get("ci95") is not None]
    if len(usable) < 2:
        return {"verdict": "collecting", "excluded": excluded or [],
                "text": "Needs a confident reading on two floors before the "
                        "path can be checked." + note}
    for lower, upper in zip(usable, usable[1:]):
        gap = upper["beta"] - lower["beta"]
        bar = lower["ci95"] + upper["ci95"]
        if gap > bar:
            return {"verdict": "bypass_suspected", "excluded": excluded or [],
                    "compared": [f["name"] for f in usable],
                    "text": (f"{upper['name']} follows the crawl more closely than "
                             f"{lower['name']} does ({upper['beta']:+.2f} vs "
                             f"{lower['beta']:+.2f}). Air rising from the crawl "
                             f"has to pass {lower['name']} before it reaches "
                             f"{upper['name']}, so this points at a shortcut — "
                             "leaky ducts running through the crawl, or an open "
                             "chase — rather than the floor itself." + note)}
    names = " then ".join(f["name"] for f in usable)
    return {"verdict": "consistent", "excluded": excluded or [],
            "compared": [f["name"] for f in usable],
            "text": (f"Going up through the house ({names}), each floor follows "
                     "the crawl less closely than the one below it, which is "
                     "what air working its way up looks like." + note)}
