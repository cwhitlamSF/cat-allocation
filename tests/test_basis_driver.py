"""Driver with per-layer basis: aggregate layers vs in-memory, occurrence layers unchanged."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np, polars as pl, warnings, glob, tempfile
warnings.filterwarnings("ignore")
src = open("test_driver.py").read()
exec(src[:src.index("results = {}")])
from catalloc.driver import run_allocation, prepare_lookups

up = upper.with_columns(pl.Series("Basis", ["aggregate", "occurrence", "aggregate"]),
                        pl.Series("Reinstatements", [1, None, 0]),
                        pl.Series("Agg Deductible", [None, None, 1e6]))
by_pl, by_pol, info, diag = run_allocation(folder, exposure, gross_cells, cells, stages, up, subj,
                                           verbose=True)
print(info.select("Layer", "basis", "theta", "calibration_target_100pct", "agg_limit",
                  "expected_reinstatement_premium_100pct", "placed_premium", "negative_rows_before_clamp"))
net_cells, inur = apply_inuring(cells, stages)
pieces, _ = split_policies_by_region(policies, exposure, gross_cells, lobs=["HO"])
pol = apply_inuring_to_policies(apply_per_risk(pieces, per_risk_ratios(gross_cells, cells)), inur)
worst = 0
for r in up.iter_rows(named=True):
    s_ = subj[r["Subject"]]
    ew, _ = layer_event_weights(subject_event_elt(net_cells, s_), r["Retention"], r["Limit"],
                                premium=r["Premium"], basis=r["Basis"],
                                reinstatements=r["Reinstatements"] or 0,
                                agg_deductible=r["Agg Deductible"] or 0.0)
    ref = premium_by_policy(allocate_to_policies(
        subject_policies(pol, s_).group_by(["EVENTID", "POLICYID"]).agg(
            pl.col("PERSPVALUE").sum(), pl.col("STDDEVC").sum(), (pl.col("STDDEVI") ** 2).sum().sqrt()),
        ew, "conditional", bounded=True), r["Premium"] * placed[r["Layer"]])
    got = by_pl.filter(pl.col("Layer") == r["Layer"])
    c = ref.join(got, on="POLICYID", how="full", coalesce=True, suffix="_D").fill_null(0)
    worst = max(worst, (c["PREMIUM"] - c["PREMIUM_D"]).abs().max())
print(f"aggregate/occurrence mix: max |driver - in-memory| {worst:.2e}; total {by_pol['PREMIUM'].sum():,.2f}")

# occurrence layer U2 identical to an all-occurrence run
_, _, _, _ = None, None, None, None
b_occ, _, _, _ = run_allocation(folder, exposure, gross_cells, cells, stages, upper, subj, verbose=False)
c = (by_pl.filter(pl.col("Layer") == "U2").join(b_occ.filter(pl.col("Layer") == "U2"), on="POLICYID", suffix="_O"))
print(f"U2 (occurrence in both runs): max diff {(c['PREMIUM'] - c['PREMIUM_O']).abs().max():.2e}")
# basis argument instead of column
b_all, _, i_all, _ = run_allocation(folder, exposure, gross_cells, cells, stages,
                                    upper.with_columns(pl.lit(1).alias("Reinstatements")), subj,
                                    basis="aggregate", verbose=False)
print("basis='aggregate' argument:", i_all["basis"].to_list(), i_all["agg_limit"].to_list())
