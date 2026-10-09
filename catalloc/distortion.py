"""
Distortion-calibrated allocation of a cat layer, occurrence basis, from an ELT,
without simulation. (Module name kept from the earlier co-TVaR version so imports
do not change.)

Model
-----
For each event e: Poisson rate lam_e; subject loss S_e ~ exposure * Beta(a_e, b_e),
moment-matched to (mean, sd = sd_ind + sd_corr, exposure).
Layer occurrence loss: L(s) = min(max(s - attach, 0), limit).

    Lambda(x) = sum_e lam_e P(L_e > x)          OEP(x) = 1 - exp(-Lambda(x))
    h(x)      = g(OEP(x)) / OEP(x)              (0 where Lambda(x) = 0)
    psi(s)    = integral_0^{L(s)} h(x) dx

g is a distortion (Wang by default, proportional hazards as a sensitivity), with its
parameter calibrated per layer so that

    sum_e lam_e E[psi(S_e)] = integral_0^l h(x) Lambda(x) dx = 100% premium.

With g = identity, h = 1, psi = L and every allocation is expected layer loss.
There is no VaR threshold and no special treatment of the limit atom: the atom is
inside P(L_e > x) for x < l.

Event integrals, on midpoints x_k of an even grid on [0, l):
    B_e = E[psi(S_e)]   = sum_k h(x_k) P(S_e > a + x_k) dx
    A_e = E[psi(S_e)/S_e] = sum_k h(x_k) E[1{S_e > a + x_k} / S_e] dx

Policy split within event e (unchanged):
    c_ei = lam_e * [ mu_i * A_e + beta_i * (B_e - mu_S * A_e) ]
    mean share : beta_i = mu_i / mu_S                       ->  c_ei = lam_e B_e mu_i/mu_S
    conditional: beta_i = (sdc_i + sdi_i**2 / sdi_S) / (sdc_S + sdi_S)
The conditional beta_i come from the linear conditional expectation
E[X_i | S = s] = mu_i + beta_i (s - mu_S), consistent with RMS aggregation.
Both splits sum to the event total; event totals sum to the 100% premium.

Column conventions (RMS ELT names):
    EVENTID, RATE, PERSPVALUE (mean), STDDEVI, STDDEVC, EXPVALUE
"""

from __future__ import annotations

import os
import warnings
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import polars as pl
from scipy import optimize, special


# --------------------------------------------------------------------------- #
# Beta moment matching
# --------------------------------------------------------------------------- #
def beta_params(mean, sd, exposure, max_var_frac=0.999):
    """Moment-match Beta(a, b) on [0, exposure]. Arrays in, arrays out.

    The variance is capped at max_var_frac * mu(1 - mu) so the beta is feasible.
    Events with sd == 0 get a very tight beta (effectively a point mass).
    """
    mean = np.asarray(mean, float)
    sd = np.asarray(sd, float)
    exposure = np.asarray(exposure, float)
    mu = np.clip(mean / exposure, 1e-12, 1 - 1e-12)
    var = (sd / exposure) ** 2
    var = np.minimum(var, max_var_frac * mu * (1 - mu))
    var = np.maximum(var, 1e-14 * mu * (1 - mu))
    k = mu * (1 - mu) / var - 1.0
    return mu * k, (1 - mu) * k


def layer_loss(s, attach, limit):
    return np.minimum(np.maximum(s - attach, 0.0), limit)


# --------------------------------------------------------------------------- #
# Distortions: g(u) / u, evaluated stably
# --------------------------------------------------------------------------- #
DISTORTIONS = ("wang", "ph", "identity")


def g_over_u(u, distortion, theta):
    """g(u)/u for u in (0, 1]. Wang: g = Phi(Phi^-1(u) + theta);
    PH: g = u^(1/theta) (theta = rho > 0); identity: g = u."""
    u = np.clip(np.asarray(u, float), 1e-300, 1.0)
    if distortion == "identity":
        return np.ones_like(u)
    if distortion == "wang":
        return np.exp(special.log_ndtr(special.ndtri(u) + theta) - np.log(u))
    if distortion == "ph":
        return np.exp((1.0 / theta - 1.0) * np.log(u))
    raise ValueError(f"distortion must be one of {DISTORTIONS}")


# --------------------------------------------------------------------------- #
# Grids and per-event survival / ratio terms
# --------------------------------------------------------------------------- #
def _x_grid(limit, n_x):
    dx = limit / n_x
    return (np.arange(n_x) + 0.5) * dx, dx


def _prep(events: pl.DataFrame):
    ev = events.filter(pl.col("PERSPVALUE") > 0)
    rate = ev["RATE"].to_numpy().astype(float)
    mu = ev["PERSPVALUE"].to_numpy().astype(float)
    sd = np.maximum((ev["STDDEVI"] + ev["STDDEVC"]).to_numpy().astype(float), 0.0)
    E = np.maximum(ev["EXPVALUE"].to_numpy().astype(float), mu * (1 + 1e-9))
    a, b = beta_params(mu, sd, E)
    return ev, rate, a, b, E


def _survival(a, b, E, attach, x):
    """P(S > attach + x) for a block of events, shape (events, len(x))."""
    u = np.minimum((attach + x[None, :]) / E[:, None], 1.0)
    return special.betaincc(a[:, None], b[:, None], u)


def _ratio_terms(a, b, E, attach, limit, x, n_tail):
    """S(x) = P(S > a+x) and T(x) = E[1{S > a+x}/S] at the grid points x.

    T is a reverse cumulative sum over bins whose edges are a + x_k, a + l, and
    geometric points from a + l up to the exposure; 1/s at each bin's geometric
    midpoint (s >= a + x_0 > 0, so 1/s is bounded)."""
    lo = attach + x[None, :] + 0.0 * E[:, None]                         # (e, n)
    top = attach + limit
    g = np.linspace(0.0, 1.0, n_tail + 1)[None, 1:]
    hi_end = np.maximum(E, top)[:, None]
    tail = top * (hi_end / top) ** g                                    # (e, n_tail)
    edges = np.concatenate([lo, np.full((len(E), 1), top), tail], axis=1)
    cdf = special.betainc(a[:, None], b[:, None], np.minimum(edges / E[:, None], 1.0))
    sf = 1.0 - cdf[:, : len(x)]
    prob = np.diff(cdf, axis=1)
    mid = np.sqrt(edges[:, 1:] * edges[:, :-1])
    contrib = np.divide(prob, mid, out=np.zeros_like(prob), where=mid > 0)
    T = np.cumsum(contrib[:, ::-1], axis=1)[:, ::-1][:, : len(x)]
    return sf, T


# --------------------------------------------------------------------------- #
# Layer exceedance and calibration
# --------------------------------------------------------------------------- #
def _n_jobs(n_jobs):
    return max(1, os.cpu_count() or 1) if n_jobs in (None, 0, -1) else max(1, int(n_jobs))


def _chunks(n, chunk):
    return [slice(i, min(i + chunk, n)) for i in range(0, n, chunk)]


def layer_exceedance(rate, a, b, E, attach, limit, n_x=500, chunk=5000, n_jobs=None):
    """Lambda at grid midpoints on [0, limit), plus the attachment and exhaustion rates.
    Event blocks run in parallel threads (the incomplete beta releases the GIL)."""
    x, dx = _x_grid(limit, n_x)
    work = lambda sl: rate[sl] @ _survival(a[sl], b[sl], E[sl], attach, x)
    with ThreadPoolExecutor(_n_jobs(n_jobs)) as ex:
        lam_x = sum(ex.map(work, _chunks(len(rate), chunk)), np.zeros_like(x))
    lam_a = float(rate @ special.betaincc(a, b, np.minimum(attach / E, 1.0)))
    lam_l = float(rate @ special.betaincc(a, b, np.minimum((attach + limit) / E, 1.0)))
    return x, dx, lam_x, lam_a, lam_l


def distorted_total(lam_x, dx, distortion, theta):
    """integral_0^l h(x) Lambda(x) dx for a distortion parameter."""
    oep = -np.expm1(-lam_x)
    h = np.where(lam_x > 0, g_over_u(oep, distortion, theta), 0.0)
    return float((h * lam_x).sum() * dx)


def max_attainable(lam_x, dx):
    """Limit of distorted_total as the distortion becomes maximal (g -> 1 for u > 0):
    the integral of Lambda/OEP over the reachable part of the layer. Slightly above the
    reachable length of the layer."""
    oep = -np.expm1(-lam_x)
    return float(np.where(lam_x > 0, lam_x / np.where(oep > 0, oep, 1.0), 0.0).sum() * dx)


def _range_message(lam_x, dx, target, name):
    el = float(lam_x.sum() * dx)
    top = max_attainable(lam_x, dx)
    limit = len(lam_x) * dx
    reach = float((lam_x > 0).sum() * dx)
    return (f"Cannot calibrate {name}: 100% premium {target:,.0f} is outside the attainable "
            f"range ({el:,.0f} expected layer loss up to about {top:,.0f}). Layer limit "
            f"{limit:,.0f}, of which {reach:,.0f} can be reached by any event; implied rate "
            f"on line at 100% {target / limit:.0%}. Check the premium (layer vs tower, "
            f"it should be the 100% premium), units against the ELT, and the layer's subject.")


def _solve(total, target, distortion, message):
    """Distortion parameter with total(theta) = target (total increasing in theta)."""
    f = lambda t: total(t) - target
    if distortion == "wang":
        lo, hi = -1.0, 1.0
        while f(lo) > 0 and lo > -40:
            lo *= 2
        while f(hi) < 0 and hi < 40:
            hi *= 2
        if f(lo) > 0 or f(hi) < 0:
            raise ValueError(message())
        return float(optimize.brentq(f, lo, hi, xtol=1e-12))
    if distortion == "ph":
        fl = lambda lr: f(np.exp(lr))
        lo, hi = -1.0, 1.0
        while fl(lo) > 0 and lo > -20:
            lo *= 2
        while fl(hi) < 0 and hi < 20:
            hi *= 2
        if fl(lo) > 0 or fl(hi) < 0:
            raise ValueError(message())
        return float(np.exp(optimize.brentq(fl, lo, hi, xtol=1e-12)))
    raise ValueError(f"distortion must be one of {DISTORTIONS}")


def calibrate(lam_x, dx, target, distortion):
    """Occurrence basis: distortion parameter with distorted_total = target."""
    if distortion == "identity":
        return None
    name = {"wang": "Wang", "ph": "PH"}.get(distortion, distortion)
    return _solve(lambda t: distorted_total(lam_x, dx, distortion, t), target, distortion,
                  lambda: _range_message(lam_x, dx, target, name))


# --------------------------------------------------------------------------- #
# Aggregate (annual) basis
# --------------------------------------------------------------------------- #
def annual_distribution(lam_x, total_rate, dx, agg_limit, agg_ded, max_pow=24):
    """Annual layer loss Y = sum of occurrence layer losses, compound Poisson, on the grid
    j*dx (j = 0, 1, ...). Occurrence severities are rounded to the grid from Lambda at the
    midpoints: rate mass at j is Lambda(x_{j-1}) - Lambda(x_j) (j = n_x is the limit).
    FFT with exponential tilting against wrap-around. Returns (pmf, tail_mass): tail_mass
    is the probability beyond the grid (kept in the AEP tail)."""
    n_x = len(lam_x)
    sev = np.empty(n_x + 1)
    sev[0] = total_rate - lam_x[0]
    sev[1:n_x] = lam_x[:-1] - lam_x[1:]
    sev[n_x] = lam_x[-1]
    sev = np.maximum(sev, 0.0)
    lam = sev.sum()
    j = np.arange(n_x + 1)
    m1 = (sev * j).sum()                       # E[Y] / dx
    m2 = (sev * j ** 2).sum()                  # Var[Y] / dx^2
    need = agg_ded / dx + (agg_limit / dx if np.isfinite(agg_limit) else 0) + n_x
    need = max(need, m1 + 12 * np.sqrt(m2) + n_x) * 1.25
    N = int(2 ** min(max_pow, max(12, int(np.ceil(np.log2(need))))))
    tilt = 20.0 / N
    f = np.zeros(N)
    f[: n_x + 1] = sev / lam * np.exp(-tilt * j)
    g = np.real(np.fft.ifft(np.exp(lam * (np.fft.fft(f) - 1.0))))
    g = np.maximum(g * np.exp(tilt * np.arange(N)), 0.0)
    tail = max(0.0, 1.0 - g.sum())
    return g, tail


def _annual_parts(g, tail, dx, agg_limit, agg_ded):
    """AEP of recoveries R = min(max(Y - D, 0), AL) at r midpoints, plus grid helpers."""
    N = len(g)
    d = int(round(agg_ded / dx))
    n_r = int(round(agg_limit / dx)) if np.isfinite(agg_limit) else N - d
    n_r = max(1, min(n_r, N - d))
    sf = np.concatenate([np.cumsum(g[::-1])[::-1][1:], [0.0]]) + tail   # P(Y > j), j < N
    aep = sf[d: d + n_r]                     # P(R > (k + 1/2) dx) = P(Y >= d + k + 1)
    return d, n_r, aep


def annual_total(aep, dx, distortion, theta):
    """Distorted value of annual recoveries: integral of g(AEP(r)) dr."""
    h = np.where(aep > 0, g_over_u(aep, distortion, theta), 0.0)
    return float((h * aep).sum() * dx)


def annual_kernel(lam_x, total_rate, dx, limit, distortion, theta=None, premium=None,
                  reinstatements=0, reinstatement_rate=1.0, agg_limit=None, agg_ded=0.0,
                  include_rp=True):
    """Aggregate basis: the per-occurrence density h(x) on the occurrence grid so that
    every event integral (A, B) and the policy split work unchanged.

    Annual recoveries R(Y) = min(max(Y - D, 0), AL), AL = limit x (1 + reinstatements)
    unless agg_limit is given. With psi_R(r) = integral_0^r g(AEP)/AEP, each occurrence j
    in a year is allocated L_j / Y x psi_R(R(Y)). By the Poisson property the expected
    allocation to event e is rate_e E[L_e m(L_e)], m(z) = E[phi(Y' + z)], phi(y) =
    psi_R(R(y)) / y, Y' an independent annual loss. So psi_occ(x) = x m(x) plays the
    role of the occurrence psi, and h = d psi_occ / dx.

    Calibration target: 100% premium plus the expected reinstatement premium
    (rate x 100% premium x E[min(R, n x limit)] / limit), i.e. the price of R."""
    n_x = len(lam_x)
    AL = limit * (1 + reinstatements) if agg_limit is None else float(agg_limit)
    g, tail = annual_distribution(lam_x, total_rate, dx, AL, agg_ded)
    d, n_r, aep = _annual_parts(g, tail, dx, AL, agg_ded)
    el = float(aep.sum() * dx)                                   # E[R]
    n_ri = min(reinstatements * limit, AL)
    k_ri = int(round(n_ri / dx))
    e_ri = float(aep[:k_ri].sum() * dx)                          # E[min(R, n x limit)]
    target = None
    exp_rp = (reinstatement_rate * premium * e_ri / limit) if premium is not None else 0.0
    if theta is None and distortion != "identity":
        if premium is None:
            raise ValueError("premium (or theta) is required to calibrate the distortion")
        target = float(premium) + (exp_rp if include_rp else 0.0)
        top = float(dx * (aep > 0).sum())
        theta = _solve(lambda t: annual_total(aep, dx, distortion, t), target, distortion,
                       lambda: (f"Cannot calibrate {distortion} (aggregate basis): target "
                                f"{target:,.0f} (100% premium + expected reinstatement premium) "
                                f"is outside the attainable range ({el:,.0f} expected annual "
                                f"recovery up to about {top:,.0f}). Check the premium, the "
                                f"reinstatement terms and the aggregate limit."))
    hR = np.where(aep > 0, g_over_u(aep, distortion, theta), 0.0)
    psiR = np.concatenate([[0.0], np.cumsum(hR) * dx])           # psi_R at r = i dx
    N = len(g)
    jj = np.arange(N + n_x + 1)
    rr = np.clip(jj - d, 0, n_r)
    phi = np.where(jj > 0, psiR[rr] / np.maximum(jj, 1) / dx, 0.0)
    # m(z_k) = E[phi(Y' + k dx)]; mass beyond the grid sits at its edge
    m = np.array([g @ phi[k: k + N] + tail * phi[min(N + k, len(phi) - 1)]
                  for k in range(n_x + 1)])
    psi_occ = np.arange(n_x + 1) * dx * m
    h = np.diff(psi_occ) / dx                                    # at midpoints x_k
    info = {"agg_limit": AL, "agg_deductible": agg_ded, "reinstatements": reinstatements,
            "reinstatement_rate": reinstatement_rate, "theta": theta, "target": target,
            "annual_value": annual_total(aep, dx, distortion, theta),
            "expected_annual_recovery": el,
            "expected_reinstatement_premium_100pct": exp_rp,
            "P_attach": float(aep[0]) if len(aep) else 0.0,
            "P_exhaust": float(aep[-1]) if len(aep) else 0.0,
            "grid_tail_mass": tail, "grid_points": N, "aep": aep}
    return h, info


# --------------------------------------------------------------------------- #
# Event weights
# --------------------------------------------------------------------------- #
def layer_event_weights(
    events: pl.DataFrame,
    attach: float,
    limit: float,
    premium: float | None = None,
    distortion: str = "wang",
    theta: float | None = None,
    n_x: int = 500,
    n_tail: int = 50,
    chunk: int = 5000,
    drop_rel: float = 1e-12,
    n_jobs: int | None = None,
    basis: str = "occurrence",
    reinstatements: int = 0,
    reinstatement_rate: float = 1.0,
    agg_limit: float | None = None,
    agg_deductible: float = 0.0,
    include_reinstatement_premium: bool = True,
):
    """Event-level distortion-weighted allocation for one layer.

    events:     one row per EVENTID for the layer's subject, with
                RATE, PERSPVALUE, STDDEVI, STDDEVC, EXPVALUE.
    premium:    the layer's upfront premium at 100% (the calibration target).
    distortion: "wang" (default), "ph" or "identity".
    theta:      give a parameter to skip calibration (premium then unused for it).
    drop_rel:   events whose contribution is below drop_rel x total are dropped
                (the dropped share is reported).
    n_x, n_tail: grid sizes (500 and 50 by default; expected-loss error ~1e-4).
    n_jobs:     threads for the event integrals (default: all cores).
    basis:      "occurrence" (default): the distortion acts on the layer's OEP.
                "aggregate": on the AEP of annual recoveries
                min(max(annual loss - agg_deductible, 0), agg_limit), with
                agg_limit = limit x (1 + reinstatements) unless given; the calibration
                target adds the expected reinstatement premium (reinstatement_rate x
                premium x E[min(R, reinstatements x limit)] / limit) unless
                include_reinstatement_premium is False. See annual_kernel.

    Returns (event_df, info). event_df adds A, B, EL (expected layer loss per
    occurrence) and ALLOC = RATE * B (100% basis); info holds the parameter, the
    target, the layer's expected loss, attachment and exhaustion probabilities, and
    the OEP curve.
    """
    ev, rate, a, b, E = _prep(events)
    # events that cannot reach the attachment carry no weight: skip them early
    reach = special.betaincc(a, b, np.minimum(attach / E, 1.0)) > 0
    ev = ev.filter(pl.Series(reach))
    rate, a, b, E = rate[reach], a[reach], b[reach], E[reach]
    x, dx, lam_x, lam_a, lam_l = layer_exceedance(rate, a, b, E, attach, limit, n_x,
                                                  n_jobs=n_jobs)

    target = None
    agg = None
    if basis == "occurrence":
        if theta is None and distortion != "identity":
            if premium is None:
                raise ValueError("premium (or theta) is required to calibrate the distortion")
            target = float(premium)
            theta = calibrate(lam_x, dx, target, distortion)
        oep = -np.expm1(-lam_x)
        h = np.where(lam_x > 0, g_over_u(oep, distortion, theta), 0.0)
    elif basis == "aggregate":
        h, agg = annual_kernel(lam_x, float(rate.sum()), dx, limit, distortion, theta, premium,
                               int(reinstatements or 0), float(reinstatement_rate),
                               agg_limit, float(agg_deductible or 0.0),
                               include_reinstatement_premium)
        theta, target = agg["theta"], agg["target"]
        oep = -np.expm1(-lam_x)
    else:
        raise ValueError("basis must be 'occurrence' or 'aggregate'")

    A = np.zeros(len(rate))
    B = np.zeros(len(rate))
    EL = np.zeros(len(rate))
    def work(sl):
        sf, T = _ratio_terms(a[sl], b[sl], E[sl], attach, limit, x, n_tail)
        B[sl] = sf @ h * dx
        A[sl] = T @ h * dx
        EL[sl] = sf.sum(1) * dx
    with ThreadPoolExecutor(_n_jobs(n_jobs)) as ex:
        list(ex.map(work, _chunks(len(rate), chunk)))

    alloc = rate * B
    total = float(alloc.sum())
    keep = alloc > drop_rel * total
    el_total = float(lam_x.sum() * dx) if agg is None else agg["expected_annual_recovery"]
    if target is not None and el_total > 0 and target < el_total:
        warnings.warn(f"Layer {attach:,.0f} xs: 100% premium {target:,.0f} is below "
                      f"expected layer loss {el_total:,.0f}; the distortion loads "
                      "the body rather than the tail.")
    out = ev.with_columns(pl.Series("A", A), pl.Series("B", B), pl.Series("EL", EL),
                          pl.Series("ALLOC", alloc)).filter(pl.Series(keep))
    info = {
        "basis": basis,
        "distortion": distortion,
        "theta": theta,
        "target_100pct": target if target is not None else total,
        "distorted_total": total,
        "expected_loss": el_total,
        "load_ratio": total / el_total if el_total > 0 else np.nan,
        "P_attach": float(-np.expm1(-lam_a)) if agg is None else agg["P_attach"],
        "P_exhaust": float(-np.expm1(-lam_l)) if agg is None else agg["P_exhaust"],
        "dropped_share": float(alloc[~keep].sum() / total) if total > 0 else 0.0,
        "oep_x": x,
        "oep": oep,
        "h": h,
    }
    if agg is not None:
        info.update({k: agg[k] for k in ("agg_limit", "agg_deductible", "reinstatements",
                                         "reinstatement_rate", "annual_value",
                                         "expected_reinstatement_premium_100pct",
                                         "grid_tail_mass", "aep")})
    return out, info


# --------------------------------------------------------------------------- #
# Policy split
# --------------------------------------------------------------------------- #
_TOL = 1e-12


def beta_bounds(beta: pl.Expr, mu: pl.Expr, mu_s: pl.Expr, A: pl.Expr, B: pl.Expr):
    """The least restrictive bounds that make every contribution
    c = mu*A + beta*(B - mu_S*A) non-negative:

        beta >= 0,  and  beta <= mu*A / (mu_S*A - B)  when B < mu_S*A

    (B < mu_S*A happens for events that usually exhaust the layer: psi(S)/S then falls
    as S rises, so policies with large beta relative to their mean go negative under
    the linear conditional expectation.) Policies inside the bounds are untouched.
    Returns (clipped beta, cap). The caps sum to mu_S*A / (mu_S*A - B) > 1, so the
    clipped betas can always be rescaled to sum to 1 (see beta_rescale)."""
    D = mu_s * A - B
    cap = pl.when(D > 0).then(mu * A / D).otherwise(float("inf"))
    bc = pl.when(beta < 0).then(0.0).when(beta > cap).then(cap).otherwise(beta)
    return bc, cap


def beta_rescale(bc: pl.Expr, cap: pl.Expr, sb: pl.Expr, sh: pl.Expr,
                 mu: pl.Expr, mu_s: pl.Expr) -> pl.Expr:
    """Restore sum(beta) = 1 within an event after clipping, staying within the bounds.

    sb = sum of clipped betas, sh = sum of headroom (cap - clipped beta), per event.
    Short of 1 (caps bound): add the shortfall in proportion to headroom.
    Over 1 (negatives floored): scale down in proportion. Either step is exact."""
    d = 1.0 - sb
    return (pl.when(sb <= 0).then(mu / mu_s)
            .when(d > _TOL).then(bc + d * (cap - bc) / sh)
            .when(d < -_TOL).then(bc / sb)
            .otherwise(bc))


def allocate_to_policies(
    policies: pl.DataFrame,
    event_weights: pl.DataFrame,
    method: str = "conditional",
    clamp_negative: bool = False,
    bounded: bool = False,
):
    """Split each event's allocation across policies.

    policies: EVENTID, POLICYID, PERSPVALUE, STDDEVI, STDDEVC (already on the
              layer's subject basis, e.g. net of per-risk and inuring).
    event_weights: output of layer_event_weights.
    method: "mean" or "conditional".
    bounded: bound the conditional betas (beta_bounds) so no contribution is negative,
             rescaled within each event so event totals are unchanged. False: the raw
             linear conditional expectation, which can give negative contributions.
    Returns the policy-event contributions with column ALLOC.
    """
    # Event aggregates from the policies themselves, so the split ties exactly.
    agg = policies.group_by("EVENTID").agg(
        pl.col("PERSPVALUE").sum().alias("MU_S"),
        pl.col("STDDEVC").sum().alias("SDC_S"),
        (pl.col("STDDEVI") ** 2).sum().sqrt().alias("SDI_S"),
    )
    df = (
        policies.join(agg, on="EVENTID")
        .join(event_weights.select("EVENTID", "RATE", "A", "B"), on="EVENTID")
    )
    if method == "mean":
        beta = pl.col("PERSPVALUE") / pl.col("MU_S")
    elif method == "conditional":
        sdi_term = pl.when(pl.col("SDI_S") > 0).then(
            pl.col("STDDEVI") ** 2 / pl.col("SDI_S")
        ).otherwise(0.0)
        denom = pl.col("SDC_S") + pl.col("SDI_S")
        beta = pl.when(denom > 0).then((pl.col("STDDEVC") + sdi_term) / denom).otherwise(
            pl.col("PERSPVALUE") / pl.col("MU_S")
        )
    else:
        raise ValueError("method must be 'mean' or 'conditional'")

    df = df.with_columns(beta.alias("BETA_I"))
    if bounded and method == "conditional":
        mu, mu_s = pl.col("PERSPVALUE"), pl.col("MU_S")
        bc, cap = beta_bounds(pl.col("BETA_I"), mu, mu_s, pl.col("A"), pl.col("B"))
        df = (df.with_columns(bc.alias("_BC"), cap.alias("_CAP"))
              .with_columns(pl.col("_BC").sum().over("EVENTID").alias("_SB"),
                            (pl.col("_CAP") - pl.col("_BC")).sum().over("EVENTID").alias("_SH"))
              .with_columns(beta_rescale(pl.col("_BC"), pl.col("_CAP"), pl.col("_SB"),
                                         pl.col("_SH"), mu, mu_s).alias("BETA_I"))
              .drop("_BC", "_CAP", "_SB", "_SH"))
    df = df.with_columns(
        (
            pl.col("RATE")
            * (
                pl.col("PERSPVALUE") * pl.col("A")
                + pl.col("BETA_I") * (pl.col("B") - pl.col("MU_S") * pl.col("A"))
            )
        ).alias("ALLOC")
    )

    if clamp_negative:
        # Clamp per event and rescale so each event still sums to its total.
        df = df.with_columns(
            pl.col("ALLOC").sum().over("EVENTID").alias("_tot"),
            pl.col("ALLOC").clip(lower_bound=0).alias("_pos"),
        ).with_columns(
            (pl.col("_pos") * pl.col("_tot") / pl.col("_pos").sum().over("EVENTID")).alias("ALLOC")
        ).drop("_tot", "_pos")

    return df.drop("MU_S", "SDC_S", "SDI_S")


def premium_by_policy(policy_event: pl.DataFrame, premium: float) -> pl.DataFrame:
    """Sum contributions over events and scale to the layer's upfront premium.

    premium: the amount to allocate, normally the placed cost (100% premium x placement).
    With a calibrated distortion the contributions already sum to the 100% premium
    (exactly, when the policies tie to the subject); the shares are applied to
    `premium` so the result ties to it exactly.
    """
    out = policy_event.group_by("POLICYID").agg(pl.col("ALLOC").sum())
    total = out["ALLOC"].sum()
    return out.with_columns(
        (pl.col("ALLOC") / total).alias("SHARE"),
        (pl.col("ALLOC") / total * premium).alias("PREMIUM"),
    ).sort("POLICYID")
