"""
Chunked driver: allocate upper-tower premiums to policies from a large policy ELT
stored as Parquet chunks (e.g. pol_part_001.parquet, pol_part_002.parquet, ...).

Policy ELT columns: EVENTID, POLICYID, LOBNAME, PERSPVALUE, STDDEVI, STDDEVC
(gross, no REGION). Each policy has one LOBNAME. Chunks can be split any way.

Pass 0  small tables: per-risk ratios, inuring tower tables (per event: subject
        aggregates and integrals), cell damage ratios (MDR), policy x region TIV, and
        each upper layer's distortion-calibrated event weights (A, B, RATE), dropping
        events with negligible weight.
Pass 1  per chunk: apply per-risk, split rows into regional pieces (TIV x MDR weights),
        apply each inuring tower to the pieces in its subject with the chosen sharing
        rule, and collapse back to one row per policy-event with each layer's in-subject
        mean and SDs (RMS rule). Accumulate per-event subject aggregates
        (sum mean, sum STDDEVC, sum STDDEVI^2), which add across chunks.
Pass 2  per chunk: policy contributions
            c = RATE * [mu*A + beta*(B - MU_S*A)]
        summed by POLICYID and layer, then scaled to each layer's premium.

Inuring sharing (see inuring.apply_tower):
    "conditional"  co-recovery: each piece's net loss is E[X_i N(S)/S] under the linear
                   conditional expectation of X_i given the tower's subject loss S, so
                   pieces that drive the bad outcomes of an event get more recovery.
    "prorata"      the same percentage for every piece in the subject for that event.
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
import tempfile
import time

import numpy as np
import polars as pl

from .inuring import (MULTI_COL, InuringResult, apply_inuring, apply_tower, parse_flag,
                     per_risk_ratios, policy_main_region, region_requirements, stage_layers,
                     subject_event_elt, subject_expr)
from .distortion import beta_bounds, beta_rescale, layer_event_weights

ELT_COLS = ["EVENTID", "POLICYID", "LOBNAME", "PERSPVALUE", "STDDEVI", "STDDEVC"]
BASE_COLS = ["EVENTID", "POLICYID", "PERSPVALUE", "STDDEVI", "STDDEVC"]   # with policy_lob


ID_COLS = ("EVENTID", "POLICYID")
VALUE_COLS = ("PERSPVALUE", "STDDEVI", "STDDEVC", "EXPVALUE", "RATE", "TIV")


def _upcast(df: pl.DataFrame) -> pl.DataFrame:
    """Integer IDs to Int64 and values to Float64, so compact storage types (Int32,
    Float32) never cause join mismatches or lose precision in sums of squares.
    String IDs are left as they are."""
    casts = []
    for c in ID_COLS:
        if c in df.columns and df.schema[c].is_integer() and df.schema[c] != pl.Int64:
            casts.append(pl.col(c).cast(pl.Int64))
    for c in VALUE_COLS:
        if c in df.columns and df.schema[c] != pl.Float64:
            casts.append(pl.col(c).cast(pl.Float64))
    return df.with_columns(casts) if casts else df


def _check_policy_lob(policy_lob: pl.DataFrame) -> pl.DataFrame:
    m = policy_lob.select("POLICYID", "LOBNAME").unique()
    dup = m.group_by("POLICYID").len().filter(pl.col("len") > 1)
    if dup.height:
        raise ValueError(f"{dup.height} POLICYIDs have more than one LOBNAME in policy_lob.")
    return m


MYLOGGER = logging.getLogger(__name__)


def _reporter(verbose, progress, t0=None):
    """Messages go to `progress` (e.g. the platform's status.update) when given, else are
    printed when verbose; always logged."""
    t0 = time.time() if t0 is None else t0

    def say(msg, *, transient=False):
        MYLOGGER.debug(msg) if transient else MYLOGGER.info(msg)
        if progress is not None:
            progress(msg)
        elif verbose:
            if transient:
                print(msg, end="\r", flush=True)
            else:
                print(f"[{time.time() - t0:7.1f}s] {msg}", flush=True)
    return say


def _chunk_files(folder: str, pattern: str) -> list[str]:
    files = sorted(glob.glob(os.path.join(folder, pattern)))
    if not files:
        raise FileNotFoundError(f"No files matching {pattern} in {folder}")
    return files


# --------------------------------------------------------------------------- #
# Pass 0
# --------------------------------------------------------------------------- #
def _placements(upper_layers: pl.DataFrame, col: str = "Placement %") -> list[float]:
    """Placed share per layer, entered as a fraction between 0 and 1 (1.0 if the column
    is absent or the cell is blank)."""
    if col not in upper_layers.columns:
        return [1.0] * upper_layers.height
    v = upper_layers[col].cast(pl.Float64).fill_null(1.0).to_list()
    bad = [(n, x) for n, x in zip(upper_layers["Layer"].to_list(), v) if not 0 < x <= 1]
    if bad:
        raise ValueError(f"Placement % must be a fraction in (0, 1]; got {bad}")
    return v


def build_lookups(files, exposure, gross_cells, cells, stages, upper_layers, subj,
                  tiv_col="TIV", sharing="conditional", distortion="wang", split_lobs="auto",
                  policy_lob=None, n_x=500, n_jobs=None, verbose=True, basis="occurrence",
                  include_reinstatement_premium=True, progress=None):
    """Small tables shared by both passes. Build once with prepare_lookups() and pass to
    run_allocation(lookups=...) to reuse them (e.g. a trial run, then the full run)."""
    t0 = time.time()
    say = _reporter(verbose, progress, t0)
    say("building lookups")
    gross_cells, cells = _upcast(gross_cells), _upcast(cells)
    exposure = None if exposure is None else _upcast(exposure)
    policy_lob = None if policy_lob is None else _upcast(policy_lob)
    # which LOBs need regions, and which of those need a split, from the subject
    # definitions and the "Multiple Regions Per Policy" flags (inuring and upper layers)
    if split_lobs == "auto":
        upper_multi = ([parse_flag(v) for v in upper_layers[MULTI_COL].to_list()]
                       if MULTI_COL in upper_layers.columns else [True] * upper_layers.height)
        pairs = ([(l.subject, l.multi_region) for l in stage_layers(stages)]
                 + list(zip([subj[k] for k in upper_layers["Subject"].to_list()], upper_multi)))
        split_set, label_set = region_requirements(pairs)
    elif split_lobs == "all":
        split_set, label_set = None, set()
    else:
        split_set, label_set = set(split_lobs), set()
    needs_exposure = split_set is None or label_set is None or bool(split_set) or bool(label_set)
    if not needs_exposure:
        exposure = None                                   # nothing is region-specific
    elif exposure is None:
        raise ValueError("Exposure data is needed: some subjects restrict regions.")

    # policy LOBs (one per policy)
    if policy_lob is not None:
        pol_lob_all = _check_policy_lob(policy_lob)
    else:
        pol_lob_all = _upcast(pl.scan_parquet(files).select("POLICYID", "LOBNAME").unique().collect())
    dup = pol_lob_all.group_by("POLICYID").len().filter(pl.col("len") > 1)
    if dup.height:
        raise ValueError(f"{dup.height} POLICYIDs have more than one LOBNAME.")
    in_split = (pl.lit(True) if split_set is None else pl.col("LOBNAME").is_in(list(split_set)))
    in_label = (~in_split) & (pl.lit(True) if label_set is None
                              else pl.col("LOBNAME").is_in(list(label_set)))
    pol_lob = pol_lob_all.filter(in_split)
    pol_label = pol_lob_all.filter(in_label)

    empty_pt = pl.DataFrame(schema={"POLICYID": pol_lob_all.schema["POLICYID"], "REGION": pl.Utf8,
                                    "_TIV": pl.Float64})
    if exposure is not None and pol_lob.height:
        pt = (exposure.join(pol_lob.select("POLICYID"), on="POLICYID", how="semi")
              .group_by(["POLICYID", "REGION"])
              .agg(pl.col(tiv_col).sum().alias("_TIV")).filter(pl.col("_TIV") > 0))
    else:
        pt = empty_pt
    if exposure is not None and pol_label.height:
        plabel, label_multi = policy_main_region(exposure, pol_label.select("POLICYID"), tiv_col)
    else:
        plabel, label_multi = empty_pt.select("POLICYID", "REGION"), 0
    cell_tiv = (pt.join(pol_lob, on="POLICYID")
                .group_by(["LOBNAME", "REGION"]).agg(pl.col("_TIV").sum().alias("_CELL_TIV")))

    # inuring
    say(f"inuring: {cells['EVENTID'].n_unique():,} events, {cells.height:,} cells")
    net_cells, inur = apply_inuring(cells, stages, sharing=sharing)
    say("inuring done")

    # upper layers: calibrated distortion weights per event
    layers = []
    for r, placed in zip(upper_layers.iter_rows(named=True), _placements(upper_layers)):
        s = subj[r["Subject"]]
        dist = str(r.get("Distortion") or distortion).lower()
        theta = r.get("Theta")
        bas = str(r.get("Basis") or basis).lower()
        def num(c, d):
            v = r.get(c)
            return d if v is None or (isinstance(v, float) and np.isnan(v)) else float(v)
        ev = subject_event_elt(net_cells, s)
        say(f"layer {r['Layer']}: calibrating on {ev.height:,} events ({bas} basis)")
        ew, info = layer_event_weights(
            ev, float(r["Retention"]), float(r["Limit"]), premium=float(r["Premium"]),
            distortion=dist, n_x=n_x, n_jobs=n_jobs,
            theta=None if theta is None else float(theta),
            basis=bas, reinstatements=int(num("Reinstatements", 0)),
            reinstatement_rate=num("Reinstatement Rate", 1.0),
            agg_limit=num("Agg Limit", None),
            agg_deductible=num("Agg Deductible", 0.0),
            include_reinstatement_premium=include_reinstatement_premium)
        th = "n/a" if info["theta"] is None else f"{info['theta']:.4f}"
        say(f"layer {r['Layer']}: theta {th}, load {info['load_ratio']:.2f}x, "
            f"{ew.height:,} events with weight")
        ew = ew.select("EVENTID", "RATE", "A", "B")
        layers.append({"name": str(r["Layer"]), "subject": s, "placement": placed,
                       "attach": float(r["Retention"]),
                       "premium_100": float(r["Premium"]),
                       "premium": float(r["Premium"]) * placed,      # placed cost: allocated
                       "events": ew, "info": info})

    events = pl.concat([l["events"].select("EVENTID") for l in layers]).unique()

    gc = gross_cells.filter(in_split) if pol_lob.height else gross_cells.clear()
    mdr = (gc.join(events, on="EVENTID", how="semi")
           .group_by(["EVENTID", "LOBNAME", "REGION"])
           .agg(pl.col("PERSPVALUE").sum().alias("_CELL_MEAN"))
           .join(cell_tiv, on=["LOBNAME", "REGION"], how="left")
           .select("EVENTID", "LOBNAME", "REGION",
                   (pl.col("_CELL_MEAN") / pl.col("_CELL_TIV")).alias("_MDR")))

    pr = per_risk_ratios(gross_cells.join(events, on="EVENTID", how="semi"),
                         cells.join(events, on="EVENTID", how="semi"))

    towers = [(s_, t.join(events, on="EVENTID", how="semi")) for s_, t in inur.towers]
    inur_ev = InuringResult(towers, inur.ratios, inur.sharing)

    multi = pt.group_by("POLICYID").len().filter(pl.col("len") > 1).height
    say("lookups ready")
    return {"pt": pt, "plabel": plabel, "mdr": mdr, "pr": pr, "inuring": inur_ev,
            "events": events, "layers": layers, "net_cells": net_cells,
            "split_lobs": split_set, "label_lobs": label_set,
            "multi_region_policies": multi, "label_multi_region_policies": label_multi,
            "policy_lob": pol_lob_all if policy_lob is not None else None}


def prepare_lookups(exposure, gross_cells, cells, stages, upper_layers, subj, policy_lob,
                    tiv_col="TIV", sharing="conditional", distortion="wang", split_lobs="auto",
                    n_x=500, n_jobs=None, verbose=True, basis="occurrence",
                    include_reinstatement_premium=True, progress=None):
    """Build the lookups once, for reuse across run_allocation calls (lookups=...).
    Needs policy_lob, so that nothing depends on which chunk files a run reads."""
    if policy_lob is None:
        raise ValueError("prepare_lookups needs policy_lob (POLICYID -> LOBNAME).")
    lk = build_lookups(None, exposure, gross_cells, cells, stages, upper_layers, subj, tiv_col,
                       sharing, distortion, split_lobs, policy_lob, n_x, n_jobs, verbose,
                       basis, include_reinstatement_premium, progress)
    lk["_settings"] = {"sharing": sharing, "distortion": distortion, "basis": basis}
    return lk


# --------------------------------------------------------------------------- #
# Per-chunk transform
# --------------------------------------------------------------------------- #
def collapse_chunk(chunk: pl.DataFrame, lk: dict) -> tuple[pl.DataFrame, dict]:
    """Gross policy rows -> one row per policy-event with each layer's
    in-subject net mean (M_k), STDDEVC (C_k) and STDDEVI (I_k)."""
    chunk = _upcast(chunk)
    if lk.get("policy_lob") is not None:
        df = chunk.select(BASE_COLS).join(lk["events"], on="EVENTID", how="semi")
        n_in = df.height
        df = df.join(lk["policy_lob"], on="POLICYID", how="inner")
        n_no_lob = n_in - df.height
    else:
        df = chunk.select(ELT_COLS).join(lk["events"], on="EVENTID", how="semi")
        n_in = df.height
        n_no_lob = 0

    # per-risk (BU level, uniform across regions)
    df = (df.join(lk["pr"], on=["EVENTID", "LOBNAME"], how="left")
          .with_columns(
              (pl.col("PERSPVALUE") * pl.col("PR_MEAN").fill_null(1.0)).alias("PERSPVALUE"),
              (pl.col("STDDEVC") * pl.col("PR_SDC").fill_null(1.0)).alias("STDDEVC"),
              (pl.col("STDDEVI") * pl.col("PR_SDI").fill_null(1.0)).alias("STDDEVI"))
          .drop("PR_MEAN", "PR_SDC", "PR_SDI"))

    grp = ["EVENTID", "POLICYID"]
    split_set, label_set = lk["split_lobs"], lk["label_lobs"]
    piece_cols = grp + ["LOBNAME", "REGION", "PERSPVALUE", "STDDEVC", "STDDEVI", "_FB"]
    in_split = (pl.lit(True) if split_set is None else pl.col("LOBNAME").is_in(list(split_set)))
    in_label = (~in_split) & (pl.lit(True) if label_set is None
                              else pl.col("LOBNAME").is_in(list(label_set)))

    # LOBs that no subject restricts by region: no lookup, REGION left null
    rest = (df.filter(~in_split & ~in_label)
            .with_columns(pl.lit(None, dtype=pl.Utf8).alias("REGION"), pl.lit(False).alias("_FB"))
            .select(piece_cols))
    # LOBs flagged single-region: just the policy's region, no split
    lab = (df.filter(in_label).join(lk["plabel"], on="POLICYID")
           .with_columns(pl.lit(False).alias("_FB")).select(piece_cols))
    df_s = df.filter(in_split)

    # region-sensitive LOBs: split into regional pieces (TIV x event damage rate)
    x = (df_s.join(lk["pt"], on="POLICYID")
         .join(lk["mdr"], on=["EVENTID", "LOBNAME", "REGION"], how="left")
         .with_columns((pl.col("_TIV") * pl.col("_MDR").fill_null(0.0)).alias("_RAW"))
         .with_columns(pl.col("_RAW").sum().over(grp).alias("_RS"),
                       pl.col("_TIV").sum().over(grp).alias("_TS"))
         .with_columns(
             pl.when(pl.col("_RS") > 0).then(pl.col("_RAW") / pl.col("_RS"))
             .otherwise(pl.col("_TIV") / pl.col("_TS")).alias("_W"),
             (pl.col("_RS") <= 0).alias("_FB"))
         .filter(pl.col("_W") > 0)
         .with_columns((pl.col("PERSPVALUE") * pl.col("_W")).alias("PERSPVALUE"),
                       (pl.col("STDDEVC") * pl.col("_W")).alias("STDDEVC"),
                       (pl.col("STDDEVI") * pl.col("_W").sqrt()).alias("STDDEVI"))
         .select(piece_cols))
    n_split = x.group_by(grp).len().filter(pl.col("len") > 1).height
    in_split_rows = df_s.height
    lab_in = df.filter(in_label)
    in_label_rows = lab_in.height
    # rows dropped for want of exposure: region-sensitive rows whose policy has none
    lost = pl.concat([lab_in.join(lk["plabel"], on="POLICYID", how="anti").select("PERSPVALUE"),
                      df_s.join(lk["pt"].select("POLICYID").unique(), on="POLICYID", how="anti")
                      .select("PERSPVALUE")])
    n_dup = df.height - df.select(grp).n_unique()
    x = pl.concat([x, lab, rest], how="vertical_relaxed")

    # inuring towers, in order, on the pieces
    for subject, table in lk["inuring"].towers:
        x = apply_tower(x, subject, table, lk["inuring"].sharing)

    # collapse pieces back to one row per policy-event, per layer subject
    aggs = [pl.col("_FB").first()]
    for k, layer in enumerate(lk["layers"]):
        ins = subject_expr(layer["subject"])
        aggs += [pl.col("PERSPVALUE").filter(ins).sum().alias(f"M_{k}"),
                 pl.col("STDDEVC").filter(ins).sum().alias(f"C_{k}"),
                 (pl.col("STDDEVI").filter(ins) ** 2).sum().sqrt().alias(f"I_{k}")]
    col = x.group_by(grp).agg(aggs)
    res = col.drop("_FB")

    diag = {"rows_in": n_in,
            "rows_no_lob": n_no_lob,
            "rows_split_lobs": in_split_rows,
            "rows_label_lobs": in_label_rows,
            "rows_split_multi_region": n_split,
            "rows_no_exposure": lost.height,
            "mean_no_exposure": float(lost["PERSPVALUE"].sum() or 0.0),
            "mean_in": float(df["PERSPVALUE"].sum() or 0.0),
            "duplicate_policy_event_rows": n_dup,
            "rows_tiv_fallback": int(col["_FB"].sum())}
    return res, diag


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def run_allocation(
    folder: str,
    exposure: pl.DataFrame,
    gross_cells: pl.DataFrame,
    cells: pl.DataFrame,
    stages,
    upper_layers: pl.DataFrame,
    subj: dict,
    method: str = "conditional",
    pattern: str = "pol_part_*.parquet",
    cache_dir: str | None = None,
    tiv_col: str = "TIV",
    sharing: str = "conditional",
    distortion: str = "wang",
    split_lobs="auto",
    policy_lob: pl.DataFrame | None = None,
    lookups: dict | None = None,
    clamp_negative: bool = False,
    positive_split: bool = True,
    stream: bool | None = None,
    basis: str = "occurrence",
    include_reinstatement_premium: bool = True,
    n_x: int = 500,
    n_jobs: int | None = None,
    verbose: bool = True,
    progress=None,
):
    """Allocate each upper layer's premium to policies.

    upper_layers: one row per layer with Layer, Retention, Limit, Subject, Premium, and
                  optionally "Placement %" (default 1). Premium is the 100% premium and
                  calibrates the weights; Placement % is a fraction in (0, 1], and the
                  placed cost (Premium x Placement %) is what is allocated.
                  Also optional: "Distortion" (overrides the
                  argument for that layer) and "Theta" (skip calibration), and for the
                  aggregate basis "Basis" ("occurrence"/"aggregate", overrides the
                  argument), "Reinstatements" (number, default 0), "Reinstatement Rate"
                  (fraction of premium per full reinstatement, default 1.0 = 100%),
                  "Agg Limit" (default Limit x (1 + Reinstatements)), "Agg Deductible"
                  (default 0).
    basis:      "occurrence" (default) or "aggregate": whether the distortion acts on
                the layer's OEP or on the AEP of annual recoveries after aggregate terms.
                Aggregate calibrates to 100% premium + expected reinstatement premium
                (the price of the annual recoveries) unless
                include_reinstatement_premium=False. Either way the amount allocated is
                the placed upfront premium. Run all perils together: the annual loss
                (and the occurrence OEP) is across every peril the tower covers.
    distortion: "wang" (default), "ph" or "identity" (expected-loss allocation).
    split_lobs: which LOBs get regions from the exposure data and, where a policy has
                several, a regional split. "auto" (default) reads the subject definitions:
                a LOB is region-sensitive if any inuring or upper-layer subject row lists
                regions for it (a blank LOB with regions makes every LOB sensitive). If
                every such layer has "Multiple Regions Per Policy" = False (inuring stages
                via stages_from_table; upper layers via that column in upper_layers), the
                LOB's policies just take their region (largest TIV), with no split.
                "all" splits every LOB; or pass a list of LOBs to split. If no LOB is
                sensitive, `exposure` is not used and may be None.
    stream:    None (default): if every event's rows sit in one file (event-grouped
               chunks), process each file completely in one pass and write nothing to
               disk; otherwise use two passes with a disk cache. True forces one pass
               (error if events span files); False forces the cache.
    cache_dir: where the two-pass mode keeps collapsed chunks (a temp dir if None,
               removed at the end, also when the run fails, unless you pass your own).
    sharing: how inuring recoveries are shared among policies within an event:
             "conditional" (co-recovery, default) or "prorata".
    policy_lob: optional POLICYID -> LOBNAME table. When given, the chunks need not
                carry LOBNAME: it is joined onto each chunk as it is read, and rows whose
                policy is not in the table are dropped and counted (diag["rows_no_lob"]).
    lookups:    output of prepare_lookups(), to skip rebuilding them (the event-level
                calibration is the slow part and does not depend on the chunk files).
                The other inputs (exposure, cells, ...) are then not used.
    n_x, n_jobs: grid size and threads for the layer calibration.
    positive_split: (conditional split) bound each policy's beta to
                0 <= beta <= mu*A / (mu_S*A - B) (upper bound only when B < mu_S*A),
                the least restrictive bounds giving non-negative contributions, and
                rescale within the event so betas sum to 1. Policies already inside the
                bounds keep their beta. False: the raw linear split (old behaviour).
                Rows where a bound binds are reported in layer_info (bounded_rows).
                Works with any chunking.
    clamp_negative: set negative policy contributions within each event to zero and
                rescale the event's positive ones so the event total is unchanged.
                Needs every event's rows in a single file (event-grouped chunks);
                checked in pass 1. Negative counts and shares before clamping are
                reported in layer_info either way.
    progress:   optional callable taking a message (e.g. the platform's status.update);
                messages go there instead of being printed.
    Returns (by_policy_layer, by_policy, layer_info, diagnostics).
    """
    say = _reporter(verbose, progress)
    files = _chunk_files(folder, pattern)
    if lookups is not None:
        lk = lookups
    else:
        lk = build_lookups(files, exposure, gross_cells, cells, stages, upper_layers, subj,
                           tiv_col, sharing, distortion, split_lobs, policy_lob, n_x, n_jobs,
                           verbose, basis, include_reinstatement_premium, progress)
    L = lk["layers"]
    if verbose or progress is not None:
        def fmt(x, rest_word):
            return rest_word if x is None else (", ".join(sorted(x)) or "none")
        say(f"LOBs split by region: {fmt(lk['split_lobs'], 'all')} "
              f"(policies with exposure in more than one region: {lk['multi_region_policies']:,}); "
              f"LOBs given a region only: {fmt(lk['label_lobs'], 'all others')}")
        if lk["label_multi_region_policies"]:
            say(f"WARNING: {lk['label_multi_region_policies']:,} policies flagged single-region "
                  "have exposure in more than one region; each is placed in its largest-TIV region.")
        for l in L:
            i = l["info"]
            th = "n/a" if i["theta"] is None else f"{i['theta']:.4f}"
            agg = (f"aggregate (AL {i['agg_limit']:,.0f}, exp. reinst. premium "
                   f"{i['expected_reinstatement_premium_100pct']:,.0f})  "
                   if i.get("basis") == "aggregate" else "")
            say(f"{l['name']}: {agg}P(attach) {i['P_attach']:.3%}  P(exhaust) {i['P_exhaust']:.3%}  "
                  f"EL {i['expected_loss']:,.0f}  target {i['target_100pct']:,.0f}  "
                  f"load {i['load_ratio']:.2f}x  {i['distortion']} theta {th}  "
                  f"events {l['events'].height:,}")

    if method not in ("mean", "conditional"):
        raise ValueError("method must be 'mean' or 'conditional'")
    bounded = positive_split and method == "conditional"

    # Do events span files? (Reads only the EVENTID column of each file.)
    ev_files = [pl.scan_parquet(f).select(pl.col("EVENTID").cast(pl.Int64)).unique().collect()
                for f in files]
    spanning = (pl.concat(ev_files).group_by("EVENTID").len().filter(pl.col("len") > 1).height
                if ev_files else 0)
    del ev_files
    if clamp_negative and spanning:
        raise ValueError(f"clamp_negative needs each event in one file; {spanning:,} events "
                         "span several files. Use event-grouped chunks.")
    if stream is None:
        stream = spanning == 0
    elif stream and spanning:
        raise ValueError(f"stream=True needs each event in one file; {spanning:,} events span "
                         "several files.")
    say("one pass per file, nothing written to disk" if stream else
        f"{spanning:,} events span files: two passes with a disk cache")

    def raw_beta(k):
        M, C, I = pl.col(f"M_{k}"), pl.col(f"C_{k}"), pl.col(f"I_{k}")
        if method == "mean":
            return M / pl.col("MU_S")
        denom = pl.col("SDC_S") + pl.col("SDI_S")
        sdi_term = pl.when(pl.col("SDI_S") > 0).then(I ** 2 / pl.col("SDI_S")).otherwise(0.0)
        return pl.when(denom > 0).then((C + sdi_term) / denom).otherwise(M / pl.col("MU_S"))

    def bounds(k):
        return beta_bounds(raw_beta(k), pl.col(f"M_{k}"), pl.col("MU_S"), pl.col("A"), pl.col("B"))

    def event_moments(res, k):
        """Per-event subject aggregates (additive pieces) from collapsed rows."""
        return (res.filter(pl.col(f"M_{k}") > 0).group_by("EVENTID").agg(
            pl.col(f"M_{k}").sum().alias("MU_S"), pl.col(f"C_{k}").sum().alias("SDC_S"),
            (pl.col(f"I_{k}") ** 2).sum().alias("SDI2_S")))

    def finish_moments(parts, k):
        return (pl.concat(parts).group_by("EVENTID")
                .agg(pl.col("MU_S").sum(), pl.col("SDC_S").sum(), pl.col("SDI2_S").sum())
                .with_columns(pl.col("SDI2_S").sqrt().alias("SDI_S")).drop("SDI2_S")
                .join(L[k]["events"], on="EVENTID"))

    def beta_sums(res, agg, k):
        bc, cap = bounds(k)
        return (res.filter(pl.col(f"M_{k}") > 0).join(agg, on="EVENTID")
                .with_columns(bc.alias("_BC"), cap.alias("_CAP"))
                .group_by("EVENTID").agg(pl.col("_BC").sum().alias("_SB"),
                                         (pl.col("_CAP") - pl.col("_BC")).sum().alias("_SH"),
                                         (pl.col("_BC") != raw_beta(k)).sum().alias("_NB")))

    contrib = [[] for _ in L]
    neg_rows = [0] * len(L)
    neg_sum = [0.0] * len(L)
    n_bound = [0] * len(L)

    def contributions(res, agg, k):
        M = pl.col(f"M_{k}")
        if bounded:
            bc, cap = bounds(k)
            beta = beta_rescale(bc, cap, pl.col("_SB"), pl.col("_SH"), M, pl.col("MU_S"))
        else:
            beta = raw_beta(k)
        c = pl.col("RATE") * (M * pl.col("A") + beta * (pl.col("B") - pl.col("MU_S") * pl.col("A")))
        rows = (res.filter(M > 0).join(agg, on="EVENTID")
                .select("EVENTID", "POLICYID", c.alias("ALLOC")))
        neg = rows.filter(pl.col("ALLOC") < 0)
        neg_rows[k] += neg.height
        neg_sum[k] += float(neg["ALLOC"].sum() or 0.0)
        if clamp_negative and neg.height:
            rows = (rows.with_columns(
                        pl.col("ALLOC").sum().over("EVENTID").alias("_TOT"),
                        pl.col("ALLOC").clip(lower_bound=0).alias("_POS"))
                    .with_columns(pl.col("_POS").sum().over("EVENTID").alias("_PSUM"))
                    .with_columns(pl.when(pl.col("_PSUM") > 0)
                                  .then(pl.col("_POS") * pl.col("_TOT") / pl.col("_PSUM"))
                                  .otherwise(pl.col("ALLOC")).alias("ALLOC"))
                    .drop("_TOT", "_POS", "_PSUM"))
        contrib[k].append(rows.group_by("POLICYID").agg(pl.col("ALLOC").sum()))
        if len(contrib[k]) >= 10:                     # keep memory flat: fold as we go
            contrib[k] = [pl.concat(contrib[k]).group_by("POLICYID").agg(pl.col("ALLOC").sum())]

    diag = {"rows_in": 0, "rows_no_lob": 0, "mean_in": 0.0, "duplicate_policy_event_rows": 0,
            "rows_split_lobs": 0, "rows_label_lobs": 0, "rows_split_multi_region": 0,
            "rows_no_exposure": 0, "mean_no_exposure": 0.0, "rows_tiv_fallback": 0}
    read_cols = BASE_COLS if lk["policy_lob"] is not None else ELT_COLS

    def collapse(f):
        res, d = collapse_chunk(pl.read_parquet(f, columns=read_cols), lk)
        for key in diag:
            diag[key] += d[key]
        return res

    if stream:
        # ---- one pass: every event is complete within its file -------------------
        for n, f in enumerate(files):
            res = collapse(f)
            for k in range(len(L)):
                agg = finish_moments([event_moments(res, k)], k)
                if bounded:
                    s_ = beta_sums(res, agg, k)
                    n_bound[k] += int(s_["_NB"].sum())
                    agg = agg.join(s_.drop("_NB"), on="EVENTID", how="left")
                contributions(res, agg, k)
            del res
            say(f"Allocating: file {n + 1} of {len(files)}", transient=True)
        if verbose and progress is None:
            print()
    else:
        # ---- two passes with a disk cache (events span files) --------------------
        own_cache = cache_dir is None
        cache_dir = cache_dir or tempfile.mkdtemp(prefix="catalloc_")
        os.makedirs(cache_dir, exist_ok=True)
        try:
            partial = [[] for _ in L]
            cached = []
            for n, f in enumerate(files):
                res = collapse(f)
                path = os.path.join(cache_dir, f"collapsed_{n:05d}.parquet")
                res.write_parquet(path, compression="zstd")
                cached.append(path)
                for k in range(len(L)):
                    partial[k].append(event_moments(res, k))
                say(f"Allocating, pass 1: file {n + 1} of {len(files)}", transient=True)
            if verbose and progress is None:
                print()
            ev_agg = [finish_moments(partial[k], k) for k in range(len(L))]
            read = pl.read_parquet
            if bounded:
                sums = [[] for _ in L]
                for path in cached:
                    res = read(path)
                    for k in range(len(L)):
                        sums[k].append(beta_sums(res, ev_agg[k], k))
                for k in range(len(L)):
                    s_ = pl.concat(sums[k]).group_by("EVENTID").agg(pl.col("_SB", "_SH", "_NB").sum())
                    n_bound[k] = int(s_["_NB"].sum())
                    ev_agg[k] = ev_agg[k].join(s_.drop("_NB"), on="EVENTID", how="left")
            for n, path in enumerate(cached):
                res = read(path)
                for k in range(len(L)):
                    contributions(res, ev_agg[k], k)
                say(f"Allocating, pass 2: file {n + 1} of {len(cached)}", transient=True)
            if verbose and progress is None:
                print()
        finally:                                      # removed even if the run fails
            if own_cache:
                shutil.rmtree(cache_dir, ignore_errors=True)

    # ---- scale to premium -----------------------------------------------------
    out, info = [], []
    for k, l in enumerate(L):
        pol = pl.concat(contrib[k]).group_by("POLICYID").agg(pl.col("ALLOC").sum())
        tot = pol["ALLOC"].sum()
        out.append(pol.with_columns(
            pl.lit(l["name"]).alias("Layer"),
            (pl.col("ALLOC") / tot).alias("SHARE"),
            (pl.col("ALLOC") / tot * l["premium"]).alias("PREMIUM")))
        i = l["info"]
        info.append({"Layer": l["name"], "basis": i.get("basis", "occurrence"),
                     "distortion": i["distortion"], "theta": i["theta"],
                     "calibration_target_100pct": i["target_100pct"],
                     "agg_limit": i.get("agg_limit"),
                     "expected_reinstatement_premium_100pct":
                         i.get("expected_reinstatement_premium_100pct", 0.0),
                     "premium_100pct": l["premium_100"], "placement": l["placement"],
                     "placed_premium": l["premium"], "expected_loss_100pct": i["expected_loss"],
                     "load_ratio": i["load_ratio"], "P_attach": i["P_attach"],
                     "P_exhaust": i["P_exhaust"],
                     "policy_ALLOC_total": tot,
                     "negative_policies": int((pol["ALLOC"] < 0).sum()),
                     "negative_rows_before_clamp": neg_rows[k],
                     "negative_share_before_clamp": -neg_sum[k] / tot if tot else 0.0,
                     "bounded_rows": n_bound[k],
                     "events": l["events"].height, "dropped_share": i["dropped_share"]})
    by_policy_layer = pl.concat(out).select("POLICYID", "Layer", "ALLOC", "SHARE", "PREMIUM")
    by_policy = by_policy_layer.group_by("POLICYID").agg(pl.col("PREMIUM").sum()).sort("POLICYID")
    diag["label_policies_multi_region"] = lk["label_multi_region_policies"]
    diag["events_spanning_files"] = spanning
    return by_policy_layer, by_policy, pl.DataFrame(info), diag
