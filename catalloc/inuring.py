"""
Inuring covers on a region x BU ELT, per event, without simulation.

Cells
-----
A cell is one EVENTID x LOBNAME x REGION row of the region/BU ELT (net of per-risk),
with columns EVENTID, RATE, LOBNAME, REGION, PERSPVALUE, STDDEVI, STDDEVC, EXPVALUE.
REGION is COUNTRY and STATE concatenated (see add_region).

Subjects
--------
A subject is a DataFrame with columns LOBNAME (str) and REGIONS (list[str]); rows are OR'ed.
  - empty DataFrame (or None)       -> all LOBs, all regions
  - LOBNAME null or blank           -> all LOBs (for that row's regions)
  - REGIONS null or empty list      -> all regions (for that row's LOB)

Inuring mechanics (per event, per subject)
------------------------------------------
1. Aggregate the subject's cells by the RMS rule: means add, correlated SDs add,
   independent SDs add in quadrature, exposures add.
2. Fit a beta on [0, exposure] to the subject loss S.
3. Net loss N(S) = S - sum_j p_j * min(max(S - a_j, 0), l_j) over the layers in the stage
   that share this subject. N is piecewise linear, so E[N] and E[N^2] are exact from the
   beta's partial moments at the kinks (no grid).
4. Push net back to the subject's cells with per-event ratios:
     r_mean = E[N] / E[S],  r_sd = SD[N] / SD[S],  r_exp = N(exposure) / exposure.
   Each cell's mean, STDDEVC and STDDEVI are scaled, so re-aggregating the cells by the
   RMS rule reproduces the subject's net mean and SD exactly, with the net SD split into
   correlated and independent parts in the gross proportion.

Stages are applied in order, each on the output of the previous one. Within a stage,
layers with the same subject act on the same loss (a tower); different subjects in one
stage must not share cells.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl
from scipy import special

from .distortion import beta_params

KEY = ["EVENTID", "LOBNAME", "REGION"]
MOMENT_COLS = ["PERSPVALUE", "STDDEVI", "STDDEVC", "EXPVALUE"]


# --------------------------------------------------------------------------- #
# Regions and subjects
# --------------------------------------------------------------------------- #
def add_region(df: pl.DataFrame, country="COUNTRY", state="STATE", sep="_") -> pl.DataFrame:
    """REGION = COUNTRY + sep + STATE."""
    return df.with_columns(
        pl.concat_str([pl.col(country), pl.col(state)], separator=sep).alias("REGION")
    )


def make_subject(rows) -> pl.DataFrame:
    """Convenience: make_subject([("HO", ["US_FL"]), ("", [])]) etc."""
    if not rows:
        return pl.DataFrame(schema={"LOBNAME": pl.Utf8, "REGIONS": pl.List(pl.Utf8)})
    lobs, regs = zip(*rows)
    return pl.DataFrame(
        {"LOBNAME": list(lobs), "REGIONS": [list(r) if r else [] for r in regs]},
        schema={"LOBNAME": pl.Utf8, "REGIONS": pl.List(pl.Utf8)},
    )


def subject_expr(subject: pl.DataFrame | None) -> pl.Expr:
    """Boolean expression selecting the subject's cells (or policies)."""
    if subject is None or subject.height == 0:
        return pl.lit(True)
    lob_col, reg_col = subject.columns[0], subject.columns[1]
    expr = pl.lit(False)
    for lob, regions in subject.select(lob_col, reg_col).iter_rows():
        lob_all = lob is None or str(lob).strip() == ""
        reg_all = regions is None or len(regions) == 0
        cond = pl.lit(True)
        if not lob_all:
            cond = cond & (pl.col("LOBNAME") == str(lob).strip())
        if not reg_all:
            cond = cond & pl.col("REGION").is_in(list(regions))
        expr = expr | cond
    return expr.fill_null(False)


def region_sensitive_lobs(subjects) -> set[str] | None:
    """LOBs whose policies need a region, judged from subject definitions.

    A subject row that lists regions restricts its LOB to those regions (a listed set is
    taken to be a strict subset of all regions). Returns the set of such LOBs, or None
    if a row lists regions with a blank LOB, meaning every LOB is region-sensitive.
    Subject rows with an empty region list cover all regions and add nothing.
    """
    out: set[str] = set()
    for subject in subjects:
        if subject is None or subject.height == 0:
            continue
        for lob, regions in subject.select(subject.columns[0], subject.columns[1]).iter_rows():
            if regions is None or len(regions) == 0:
                continue
            if lob is None or str(lob).strip() == "":
                return None
            out.add(str(lob).strip())
    return out


def region_requirements(subject_flags):
    """Which LOBs need a regional split, and which only need each policy's region.

    subject_flags: (subject, multi_region) pairs, one per layer (inuring and upper).
    A subject row that lists regions makes its LOB region-sensitive (a listed set is
    taken to be a strict subset of all regions); a blank LOB with regions means every
    LOB. If any such layer has multi_region True, that LOB is split; if all such layers
    say False, the LOB only needs each policy's region.

    Returns (split, label): each a set of LOBs, or None meaning every LOB (label=None
    means every LOB not in split). Both empty: no exposure data needed.
    """
    split, label = set(), set()
    split_all = label_all = False
    for subject, multi in subject_flags:
        if subject is None or subject.height == 0:
            continue
        for lob, regions in subject.select(subject.columns[0], subject.columns[1]).iter_rows():
            if regions is None or len(regions) == 0:
                continue
            blank = lob is None or str(lob).strip() == ""
            if multi:
                split_all |= blank
                if not blank:
                    split.add(str(lob).strip())
            else:
                label_all |= blank
                if not blank:
                    label.add(str(lob).strip())
    if split_all:
        return None, set()
    return split, (None if label_all else label - split)


def stage_layers(stages) -> list:
    """Every InuringLayer in `stages`, in order."""
    out = []
    for stage in stages:
        out += [stage] if isinstance(stage, InuringLayer) else list(stage)
    return out


def stage_subjects(stages) -> list:
    """Subjects of every inuring layer in `stages`."""
    out = []
    for stage in stages:
        stage = [stage] if isinstance(stage, InuringLayer) else list(stage)
        out += [l.subject for l in stage]
    return out


def _subject_key(subject: pl.DataFrame | None):
    """Canonical form, for grouping layers that share a subject."""
    if subject is None or subject.height == 0:
        return ("ALL",)
    rows = []
    for lob, regions in subject.select(subject.columns[0], subject.columns[1]).iter_rows():
        lob = "" if lob is None else str(lob).strip()
        rows.append((lob, tuple(sorted(regions or []))))
    return tuple(sorted(rows))


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #
@dataclass
class InuringLayer:
    attachment: float
    limit: float
    placement: float = 1.0
    subject: pl.DataFrame | None = None
    name: str = ""
    multi_region: bool = True      # can a policy in this subject span several regions?


MULTI_COL = "Multiple Regions Per Policy"


def parse_flag(v, default: bool = True) -> bool:
    """Read a yes/no cell: bool, 1/0, or text like TRUE/False/Yes/n. Blank -> default."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    t = str(v).strip().lower()
    if t == "":
        return default
    if t in ("true", "t", "yes", "y", "1"):
        return True
    if t in ("false", "f", "no", "n", "0"):
        return False
    raise ValueError(f"Cannot read {v!r} as true/false")


def stages_from_table(
    layers: pl.DataFrame,
    subj: dict,
    percent: bool = False,
    cols: dict | None = None,
) -> list[list[InuringLayer]]:
    """Build `stages` for apply_inuring from a layer table.

    layers: one row per layer with columns Stage, Layer, Limit, Retention,
            Placement %, Subject. Stage numbers set the inuring order (ascending);
            gaps are fine. Subject is a key into `subj`.
    subj:   dict of subject key -> subject DataFrame (from make_subject).
    percent: False (default): Placement % is a fraction in (0, 1]. True: percent (90).
    cols:   optional renames, e.g. {"Retention": "Attachment"}.

    Optional column "Multiple Regions Per Policy": False means no policy in the layer's
    subject has exposure in more than one region, so region-restricted layers only
    need each policy's region, not a regional split. Blank or absent means True.
    """
    c = {"Stage": "Stage", "Layer": "Layer", "Limit": "Limit",
         "Retention": "Retention", "Placement": "Placement %", "Subject": "Subject"}
    c.update(cols or {})
    multi_col = c.pop("Multi", MULTI_COL)

    missing = [v for v in c.values() if v not in layers.columns]
    if missing:
        raise KeyError(f"Layer table is missing columns: {missing}")
    unknown = set(layers[c["Subject"]].to_list()) - set(subj)
    if unknown:
        raise KeyError(f"Subject keys not in subj dict: {sorted(unknown)}")

    pct = layers[c["Placement"]].cast(pl.Float64)
    scale = 0.01 if percent else 1.0
    bad = [(n, x) for n, x in zip(layers[c["Layer"]].to_list(), (pct * scale).to_list())
           if x is None or not 0 < x <= 1]
    if bad:
        raise ValueError(f"Placement % must be a fraction in (0, 1]; got {bad}")

    stages = []
    for stage_no in sorted(layers[c["Stage"]].unique().to_list()):
        rows = layers.filter(pl.col(c["Stage"]) == stage_no)
        stages.append([
            InuringLayer(
                attachment=float(r[c["Retention"]]),
                limit=float(r[c["Limit"]]),
                placement=float(r[c["Placement"]]) * scale,
                subject=subj[r[c["Subject"]]],
                name=str(r[c["Layer"]]),
                multi_region=parse_flag(r.get(multi_col)),
            )
            for r in rows.iter_rows(named=True)
        ])
    return stages


@dataclass
class _Tower:
    subject: pl.DataFrame | None
    layers: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Exact moments of a piecewise-linear function of a scaled beta
# --------------------------------------------------------------------------- #
def net_moments(mean, sd, exposure, layers, with_ratio_terms=False, n_sub=200):
    """E[N], SD[N] and N(exposure) for N(S) = S - sum p_j min(max(S - a_j, 0), l_j).

    mean, sd, exposure: arrays over events. layers: list of (attachment, limit, placement).
    """
    mean = np.asarray(mean, float)
    sd = np.asarray(sd, float)
    E = np.maximum(np.asarray(exposure, float), mean * (1 + 1e-9))
    a, b = beta_params(mean, sd, E)
    mu = a / (a + b)

    def N(s):
        out = np.array(s, float, copy=True)
        for att, lim, p in layers:
            out -= p * np.minimum(np.maximum(s - att, 0.0), lim)
        return out

    # kink points, per event, clipped to [0, E]
    pts = sorted({0.0} | {float(att) for att, _, _ in layers}
                 | {float(att + lim) for att, lim, _ in layers})
    t = np.minimum(np.array(pts)[None, :], E[:, None])
    t = np.concatenate([t, E[:, None]], axis=1)           # last edge = exposure

    def partial(tt):
        x = np.clip(tt / E[:, None], 0.0, 1.0)
        p0 = special.betainc(a[:, None], b[:, None], x)
        p1 = E[:, None] * mu[:, None] * special.betainc(a[:, None] + 1, b[:, None], x)
        p2 = (E[:, None] ** 2 * mu[:, None] * (a[:, None] + 1) / (a + b + 1)[:, None]
              * special.betainc(a[:, None] + 2, b[:, None], x))
        return p0, p1, p2

    P0, P1, P2 = partial(t)
    d0, d1, d2 = np.diff(P0, axis=1), np.diff(P1, axis=1), np.diff(P2, axis=1)

    lo, hi = t[:, :-1], t[:, 1:]
    n_lo, n_hi = N(lo), N(hi)
    width = hi - lo
    slope = np.divide(n_hi - n_lo, width, out=np.zeros_like(width), where=width > 0)
    c0 = n_lo - slope * lo

    m1 = (c0 * d0 + slope * d1).sum(1)
    m2 = (c0 ** 2 * d0 + 2 * c0 * slope * d1 + slope ** 2 * d2).sum(1)
    var = np.maximum(m2 - m1 ** 2, 0.0)
    if not with_ratio_terms:
        return m1, np.sqrt(var), N(E)

    # J_k = E[ 1/S ; S in interval k ], needed only where c0 != 0 (never at s = 0,
    # since N(0) = 0 and N is linear from 0). Geometric sub-bins, CDF differences.
    J = np.zeros_like(c0)
    need = (np.abs(c0) > 0) & (width > 0) & (lo > 0)
    if need.any():
        ev, k = np.nonzero(need)
        g = np.linspace(0.0, 1.0, n_sub + 1)[None, :]
        lo_n, hi_n = lo[ev, k][:, None], hi[ev, k][:, None]
        edges = lo_n * (hi_n / lo_n) ** g
        x = np.clip(edges / E[ev][:, None], 0.0, 1.0)
        cdf = special.betainc(a[ev][:, None], b[ev][:, None], x)
        mid = np.sqrt(edges[:, 1:] * edges[:, :-1])
        J[ev, k] = (np.diff(cdf, axis=1) / mid).sum(1)

    eg = (c0 * J + slope * d0).sum(1)                                   # E[N/S]
    en2s = (c0 ** 2 * J + 2 * c0 * slope * d0 + slope ** 2 * d1).sum(1)  # E[N^2/S]
    return {"EN": m1, "SDN": np.sqrt(var), "NE": N(E), "EG": eg, "COV_GN": en2s - eg * m1}


def subject_integrals(mean, sd, exposure, layers, n_sub=200):
    """Per-event integrals for the co-recovery sharing rule.

    Returns dict of arrays over events:
      EN = E[N], SDN = SD[N], NE = N(exposure), EG = E[N/S], COV_GN = Cov(N/S, N).
    """
    return net_moments(mean, sd, exposure, layers, with_ratio_terms=True, n_sub=n_sub)


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def _rms_agg():
    return [
        pl.col("RATE").first(),
        pl.col("PERSPVALUE").sum(),
        (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
        pl.col("STDDEVC").sum(),
        pl.col("EXPVALUE").sum(),
    ]


def subject_event_elt(cells: pl.DataFrame, subject: pl.DataFrame | None = None) -> pl.DataFrame:
    """Event-level ELT for a subject, aggregated by the RMS rule.

    Output columns: EVENTID, RATE, PERSPVALUE, STDDEVI, STDDEVC, EXPVALUE.
    Exposure is the sum of the cells' exposures.
    """
    return (
        cells.filter(subject_expr(subject) & (pl.col("PERSPVALUE") > 0))
        .group_by("EVENTID")
        .agg(_rms_agg())
        .sort("EVENTID")
    )


# --------------------------------------------------------------------------- #
# Inuring
# --------------------------------------------------------------------------- #
def _towers(stage):
    towers: dict = {}
    for layer in stage:
        k = _subject_key(layer.subject)
        towers.setdefault(k, _Tower(layer.subject)).layers.append(layer)
    return list(towers.values())


@dataclass
class InuringResult:
    """What apply_inuring hands on to the policy step.

    towers: list of (subject, tower_table) in application order. tower_table has one row
            per event with the subject aggregates (MU_S, SDC_S, SDI_S) and integrals
            (EN, EG, K = Cov(G,N)/Var(N), SDN, R_SD, R_EXP).
    ratios: cumulative per-cell factors (diagnostic): R_MEAN, R_SD, R_EXP.
    sharing: "conditional" (co-recovery) or "prorata".
    """
    towers: list
    ratios: pl.DataFrame
    sharing: str


def _tower_update(mode: str, has_exp: bool) -> list[pl.Expr]:
    """Expressions updating unit moments (cells or policy pieces) for one tower.

    Unit moments: PERSPVALUE (mu), STDDEVC (sc), STDDEVI (si). Joined tower columns:
    MU_S, SDC_S, SDI_S, EN, EG, K, SDN, R_SD, R_EXP.

    prorata:      mu' = mu * EN/MU_S,  sc' = sc * R_SD,  si' = si * R_SD
    conditional:  beta = (sc + si^2/SDI_S) / (SDC_S + SDI_S),  a = mu - beta*MU_S
                  mu'  = a*EG + beta*EN                         (= E[X N/S] under linear CE)
                  beta'= beta + a*K                             (= Cov(Y, N)/Var(N))
                  si'  = si * R_SD
                  sc'  = beta'*SDN - R_SD * si^2/SDI_S          (reproduces beta' under RMS rule)
    """
    mu, sc, si = pl.col("PERSPVALUE"), pl.col("STDDEVC"), pl.col("STDDEVI")
    out = []
    if mode == "prorata":
        out += [(mu * pl.col("EN") / pl.col("MU_S")).alias("PERSPVALUE"),
                (sc * pl.col("R_SD")).alias("STDDEVC"),
                (si * pl.col("R_SD")).alias("STDDEVI")]
    elif mode == "conditional":
        si2 = pl.when(pl.col("SDI_S") > 0).then(si ** 2 / pl.col("SDI_S")).otherwise(0.0)
        denom = pl.col("SDC_S") + pl.col("SDI_S")
        beta = pl.when(denom > 0).then((sc + si2) / denom).otherwise(mu / pl.col("MU_S"))
        a = mu - beta * pl.col("MU_S")
        beta_n = beta + a * pl.col("K")
        out += [(a * pl.col("EG") + beta * pl.col("EN")).alias("PERSPVALUE"),
                (beta_n * pl.col("SDN") - pl.col("R_SD") * si2).alias("STDDEVC"),
                (si * pl.col("R_SD")).alias("STDDEVI")]
    else:
        raise ValueError("sharing must be 'conditional' or 'prorata'")
    if has_exp:
        out.append((pl.col("EXPVALUE") * pl.col("R_EXP")).alias("EXPVALUE"))
    return out


_TOWER_COLS = ["MU_S", "SDC_S", "SDI_S", "EN", "EG", "K", "SDN", "R_SD", "R_EXP"]


def apply_tower(units: pl.DataFrame, subject, table: pl.DataFrame, mode: str) -> pl.DataFrame:
    """Apply one tower's event table to units (cells or policy pieces) in its subject.

    units need EVENTID, LOBNAME, REGION, PERSPVALUE, STDDEVI, STDDEVC (EXPVALUE optional).
    Units outside the subject, or in events the tower table lacks, are unchanged.
    """
    has_exp = "EXPVALUE" in units.columns
    upd = _tower_update(mode, has_exp)
    j = (units.with_row_index("_ROW")
         .with_columns(subject_expr(subject).alias("_IN"))
         .join(table.select(["EVENTID"] + _TOWER_COLS), on="EVENTID", how="left"))
    hit = pl.col("_IN") & pl.col("EN").is_not_null()
    cols = ["PERSPVALUE", "STDDEVC", "STDDEVI"] + (["EXPVALUE"] if has_exp else [])
    j = j.with_columns([pl.when(hit).then(e).otherwise(pl.col(c)).alias(c)
                        for e, c in zip(upd, cols)])
    return j.sort("_ROW").drop(["_ROW", "_IN"] + _TOWER_COLS)


def apply_inuring(cells: pl.DataFrame, stages, sharing: str = "conditional"):
    """Apply inuring stages to a region x BU ELT.

    stages:  list whose items are an InuringLayer (a stage of one layer) or a list of
             InuringLayer (layers acting together; same subject -> same tower).
    sharing: how each tower's recovery is shared among the units in its subject.
             "conditional" (default): pro rata to loss in every outcome of the event,
                 E[X_i R(S)/S], using the linear conditional expectation of X_i given S.
             "prorata": pro rata to expected loss (the same percentage for every unit).
    Returns (net_cells, InuringResult). Pass the InuringResult to
    apply_inuring_to_policies; it carries the tower tables the policies need.
    """
    cur = cells.with_columns(pl.col("PERSPVALUE").alias("_M0"),
                             (pl.col("STDDEVI") + pl.col("STDDEVC")).alias("_S0"),
                             pl.col("EXPVALUE").alias("_E0"))
    towers_out = []
    for stage in stages:
        stage = [stage] if isinstance(stage, InuringLayer) else list(stage)
        towers = _towers(stage)

        masks = [cur.select(subject_expr(t.subject)).to_series() for t in towers]
        if len(masks) > 1:
            overlap = np.sum([m.to_numpy() for m in masks], axis=0)
            if (overlap > 1).any():
                raise ValueError("Towers in one stage have overlapping subjects; "
                                 "put them in separate stages.")

        tables = []
        for tower, mask in zip(towers, masks):
            subj = (cur.filter(mask & (pl.col("PERSPVALUE") > 0))
                    .group_by("EVENTID").agg(_rms_agg()).sort("EVENTID"))
            if subj.height == 0:
                continue
            mean = subj["PERSPVALUE"].to_numpy()
            sdc = subj["STDDEVC"].to_numpy()
            sdi = subj["STDDEVI"].to_numpy()
            E = np.maximum(subj["EXPVALUE"].to_numpy(), mean * (1 + 1e-9))
            layers = [(l.attachment, l.limit, l.placement) for l in tower.layers]
            sd = np.maximum(sdc + sdi, 0.0)
            it = subject_integrals(mean, sd, E, layers)
            var_n = it["SDN"] ** 2
            table = pl.DataFrame({
                "EVENTID": subj["EVENTID"],
                "MU_S": mean, "SDC_S": sdc, "SDI_S": sdi,
                "EN": it["EN"], "EG": it["EG"],
                "K": np.divide(it["COV_GN"], var_n, out=np.zeros_like(var_n), where=var_n > 0),
                "SDN": it["SDN"],
                "R_SD": np.divide(it["SDN"], sd, out=np.ones_like(sd), where=sd > 0),
                "R_EXP": it["NE"] / E,
            })
            tables.append((tower.subject, table))

        # apply after all of the stage's tables are built (subjects are disjoint)
        for subject, table in tables:
            cur = apply_tower(cur, subject, table, sharing)
        towers_out += tables

    ratios = cur.select(KEY + [
        pl.when(pl.col("_M0") > 0).then(pl.col("PERSPVALUE") / pl.col("_M0")).otherwise(1.0).alias("R_MEAN"),
        pl.when(pl.col("_S0") > 0).then((pl.col("STDDEVI") + pl.col("STDDEVC")) / pl.col("_S0"))
        .otherwise(1.0).alias("R_SD"),
        pl.when(pl.col("_E0") > 0).then(pl.col("EXPVALUE") / pl.col("_E0")).otherwise(1.0).alias("R_EXP"),
    ])
    return cur.drop("_M0", "_S0", "_E0"), InuringResult(towers_out, ratios, sharing)


# --------------------------------------------------------------------------- #
# Pushing ratios down to policies
# --------------------------------------------------------------------------- #
def per_risk_ratios(gross_cells: pl.DataFrame, net_cells: pl.DataFrame) -> pl.DataFrame:
    """BU-level per-event ratios net/gross of per-risk: mean, correlated SD, independent SD."""
    def bu(df, sfx):
        return df.group_by(["EVENTID", "LOBNAME"]).agg(
            pl.col("PERSPVALUE").sum().alias("M" + sfx),
            pl.col("STDDEVC").sum().alias("C" + sfx),
            (pl.col("STDDEVI") ** 2).sum().sqrt().alias("I" + sfx),
        )
    j = bu(gross_cells, "G").join(bu(net_cells, "N"), on=["EVENTID", "LOBNAME"], how="left")
    safe = lambda n, g: pl.when(pl.col(g) > 0).then(pl.col(n).fill_null(0) / pl.col(g)).otherwise(1.0)
    return j.select(
        "EVENTID", "LOBNAME",
        safe("MN", "MG").alias("PR_MEAN"),
        safe("CN", "CG").alias("PR_SDC"),
        safe("IN", "IG").alias("PR_SDI"),
    )


def apply_per_risk(policies: pl.DataFrame, pr: pl.DataFrame) -> pl.DataFrame:
    """Scale gross policy ELT rows to net of per-risk with BU-level ratios."""
    return (
        policies.join(pr, on=["EVENTID", "LOBNAME"], how="left")
        .with_columns(
            (pl.col("PERSPVALUE") * pl.col("PR_MEAN").fill_null(1.0)).alias("PERSPVALUE"),
            (pl.col("STDDEVC") * pl.col("PR_SDC").fill_null(1.0)).alias("STDDEVC"),
            (pl.col("STDDEVI") * pl.col("PR_SDI").fill_null(1.0)).alias("STDDEVI"),
        )
        .drop("PR_MEAN", "PR_SDC", "PR_SDI")
    )


def apply_inuring_to_policies(policies: pl.DataFrame, inuring) -> pl.DataFrame:
    """Take policy rows (or regional pieces) net of inuring.

    policies: EVENTID, LOBNAME, REGION, PERSPVALUE, STDDEVI, STDDEVC, already net of
              per-risk (and split by region where needed).
    inuring:  the InuringResult from apply_inuring. Each tower is applied in order, with
              its sharing rule. For co-recovery sharing, each policy's share of a tower's
              recovery depends on its own mean and SD components relative to the tower's
              subject aggregates.
              (A plain ratios DataFrame is also accepted, for pro-rata scaling by cell.)
    """
    if isinstance(inuring, InuringResult):
        out = policies
        for subject, table in inuring.towers:
            out = apply_tower(out, subject, table, inuring.sharing)
        return out
    ratios = inuring
    return (
        policies.join(ratios, on=KEY, how="left")
        .with_columns(
            (pl.col("PERSPVALUE") * pl.col("R_MEAN").fill_null(1.0)).alias("PERSPVALUE"),
            (pl.col("STDDEVC") * pl.col("R_SD").fill_null(1.0)).alias("STDDEVC"),
            (pl.col("STDDEVI") * pl.col("R_SD").fill_null(1.0)).alias("STDDEVI"),
        )
        .drop("R_MEAN", "R_SD", "R_EXP")
    )


def subject_policies(policies: pl.DataFrame, subject: pl.DataFrame | None) -> pl.DataFrame:
    return policies.filter(subject_expr(subject))


# --------------------------------------------------------------------------- #
# Splitting a region-less policy ELT across regions
# --------------------------------------------------------------------------- #
def split_policies_by_region(
    policies: pl.DataFrame,
    exposure: pl.DataFrame,
    gross_cells: pl.DataFrame,
    tiv_col: str = "TIV",
    lobs=None,
    label_lobs=None,
) -> tuple[pl.DataFrame, dict]:
    """Split each policy-event row of a gross policy ELT across the policy's regions.

    lobs: restrict the split to these LOBs (e.g. from region_requirements); rows of
          other LOBs pass through with REGION = null and are not looked up in the
          exposure data. None splits every LOB.
    label_lobs: LOBs (not in `lobs`) whose policies only need their region: each gets
          its largest-TIV region, with no split. Policies with exposure in more than one
          region are counted in the diagnostics.

    policies:    gross policy ELT: EVENTID, POLICYID, LOBNAME, PERSPVALUE, STDDEVI,
                 STDDEVC (EXPVALUE optional). No REGION.
    exposure:    POLICYID, REGION, TIV (any grain, e.g. location level; summed to
                 policy x region).
    gross_cells: gross region x BU ELT (EVENTID, LOBNAME, REGION, PERSPVALUE, ...).

    Weight for policy p, event e, region r (LOB b is the policy's LOB):
        w_per = TIV_pr * MDR_ebr / sum_r(TIV_pr * MDR_ebr)
        MDR_ebr = gross cell mean / cell TIV, where cell TIV is the summed exposure TIV
                  of the LOB's policies in that region.
    So a policy's loss goes to the regions where it has exposure AND the event hits
    that LOB. If none of its regions show loss for the event (data mismatch), it falls
    back to TIV weights and is counted in the diagnostics.

    Splitting keeps the policy totals exactly under the RMS rules:
        mean * w,  STDDEVC * w,  STDDEVI * sqrt(w).
    Returns (pieces with a REGION column, diagnostics).
    """
    if label_lobs:
        label_lobs = [l for l in label_lobs if lobs is None or l not in lobs]
        lab = policies.filter(pl.col("LOBNAME").is_in(label_lobs))
        others = policies.filter(~pl.col("LOBNAME").is_in(label_lobs))
        reg, n_multi = policy_main_region(exposure, lab.select("POLICYID").unique(), tiv_col)
        lab_out = lab.join(reg, on="POLICYID")
        if lobs is None or len(lobs):
            pieces, diag = split_policies_by_region(others, exposure, gross_cells, tiv_col, lobs)
        else:
            pieces = others.with_columns(pl.lit(None, dtype=pl.Utf8).alias("REGION"))
            diag = {"policy_event_rows": 0, "rows_without_exposure": 0,
                    "policies_without_exposure": 0, "policy_events_tiv_fallback": 0,
                    "fallback_mean_share": 0.0, "passed_through_rows": others.height}
        diag["label_rows"] = lab.height
        diag["label_rows_without_exposure"] = lab.height - lab_out.height
        diag["label_policies_multi_region"] = n_multi
        return pl.concat([pieces, lab_out.select(pieces.columns)], how="vertical_relaxed"), diag

    if lobs is not None:
        lobs = list(lobs)
        rest = policies.filter(~pl.col("LOBNAME").is_in(lobs)).with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("REGION"))
        policies = policies.filter(pl.col("LOBNAME").is_in(lobs))
        if policies.height == 0:
            return rest, {"policy_event_rows": 0, "rows_without_exposure": 0,
                          "policies_without_exposure": 0, "policy_events_tiv_fallback": 0,
                          "fallback_mean_share": 0.0, "passed_through_rows": rest.height}
        pieces, diag = split_policies_by_region(policies, exposure, gross_cells, tiv_col)
        diag["passed_through_rows"] = rest.height
        return pl.concat([pieces, rest.select(pieces.columns)], how="vertical_relaxed"), diag

    pt = (exposure.group_by(["POLICYID", "REGION"])
          .agg(pl.col(tiv_col).sum().alias("_TIV"))
          .filter(pl.col("_TIV") > 0))

    pol_lob = policies.select("POLICYID", "LOBNAME").unique()
    if pol_lob.group_by("POLICYID").len().filter(pl.col("len") > 1).height:
        raise ValueError("Some POLICYIDs have more than one LOBNAME; split by LOB first.")

    cell_tiv = (pt.join(pol_lob, on="POLICYID")
                .group_by(["LOBNAME", "REGION"]).agg(pl.col("_TIV").sum().alias("_CELL_TIV")))
    mdr = (gross_cells.group_by(["EVENTID", "LOBNAME", "REGION"])
           .agg(pl.col("PERSPVALUE").sum().alias("_CELL_MEAN"))
           .join(cell_tiv, on=["LOBNAME", "REGION"], how="left")
           .select("EVENTID", "LOBNAME", "REGION",
                   (pl.col("_CELL_MEAN") / pl.col("_CELL_TIV")).alias("_MDR")))

    no_exp = policies.join(pt.select("POLICYID").unique(), on="POLICYID", how="anti")

    x = (policies.join(pt, on="POLICYID")
         .join(mdr, on=["EVENTID", "LOBNAME", "REGION"], how="left")
         .with_columns((pl.col("_TIV") * pl.col("_MDR").fill_null(0.0)).alias("_RAW")))
    grp = ["EVENTID", "POLICYID"]
    x = x.with_columns(
        pl.col("_RAW").sum().over(grp).alias("_RAW_SUM"),
        pl.col("_TIV").sum().over(grp).alias("_TIV_SUM"),
    ).with_columns(
        pl.when(pl.col("_RAW_SUM") > 0)
        .then(pl.col("_RAW") / pl.col("_RAW_SUM"))
        .otherwise(pl.col("_TIV") / pl.col("_TIV_SUM")).alias("_W"),
        (pl.col("_RAW_SUM") <= 0).alias("_FALLBACK"),
    )

    fallback = x.filter(pl.col("_FALLBACK")).select(grp).unique()
    scale = [
        (pl.col("PERSPVALUE") * pl.col("_W")).alias("PERSPVALUE"),
        (pl.col("STDDEVC") * pl.col("_W")).alias("STDDEVC"),
        (pl.col("STDDEVI") * pl.col("_W").sqrt()).alias("STDDEVI"),
    ]
    if "EXPVALUE" in policies.columns:
        scale.append((pl.col("EXPVALUE") * pl.col("_W")).alias("EXPVALUE"))

    pieces = (x.filter(pl.col("_W") > 0)
              .with_columns(scale)
              .drop("_TIV", "_MDR", "_RAW", "_RAW_SUM", "_TIV_SUM", "_W", "_FALLBACK"))

    diag = {
        "policy_event_rows": policies.height,
        "rows_without_exposure": no_exp.height,
        "policies_without_exposure": no_exp["POLICYID"].n_unique(),
        "policy_events_tiv_fallback": fallback.height,
        "fallback_mean_share": (
            policies.join(fallback, on=grp)["PERSPVALUE"].sum()
            / max(policies["PERSPVALUE"].sum(), 1e-300)
        ),
    }
    return pieces, diag


def policy_main_region(exposure: pl.DataFrame, policy_ids: pl.DataFrame, tiv_col: str = "TIV"):
    """Each policy's region with the largest TIV, and the number of policies whose
    exposure shows more than one region (for policies flagged as single-region)."""
    pt = (exposure.join(policy_ids, on="POLICYID", how="semi")
          .group_by(["POLICYID", "REGION"]).agg(pl.col(tiv_col).sum().alias("_TIV"))
          .filter(pl.col("_TIV") > 0))
    n_multi = pt.group_by("POLICYID").len().filter(pl.col("len") > 1).height
    main = (pt.sort(["POLICYID", "_TIV", "REGION"], descending=[False, True, False])
            .group_by("POLICYID", maintain_order=True).first().select("POLICYID", "REGION"))
    return main, n_multi
