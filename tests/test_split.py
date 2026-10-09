"""Check split_policies_by_region on a synthetic book where the true regional split is known."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import numpy as np
import polars as pl

from catalloc.inuring import split_policies_by_region

rng = np.random.default_rng(3)
regions = ["US_FL", "US_GA", "US_SC", "US_NC", "US_TX", "US_LA"]
lobs = ["HO", "CO"]

# exposure: each policy has TIV in 1-4 regions
exp_rows, pol_lob = [], {}
for p in range(200):
    pol_lob[p] = rng.choice(lobs)
    for r in rng.choice(regions, rng.integers(1, 5), replace=False):
        exp_rows.append((p, r, rng.lognormal(16, 1)))
exposure = pl.DataFrame(exp_rows, schema=["POLICYID", "REGION", "TIV"], orient="row")

# events hit a contiguous run of regions with a regional damage ratio; true piece
# losses vary by policy around the regional rate
true_rows = []
for e in range(150):
    start = rng.integers(0, len(regions))
    hit = {regions[(start + k) % len(regions)]: rng.lognormal(-5, 1)
           for k in range(rng.integers(1, 4))}
    for p, r, tiv in exp_rows:
        if r in hit and rng.random() < 0.8:
            m = tiv * hit[r] * rng.lognormal(0, 0.5)
            true_rows.append((e, p, pol_lob[p], r, m, m * 0.6, m * 0.4))
truth = pl.DataFrame(true_rows, orient="row", schema=[
    "EVENTID", "POLICYID", "LOBNAME", "REGION", "PERSPVALUE", "STDDEVI", "STDDEVC"])

rms = [pl.col("PERSPVALUE").sum(),
       (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
       pl.col("STDDEVC").sum()]
policies = truth.group_by(["EVENTID", "POLICYID", "LOBNAME"]).agg(rms)      # no REGION
gross_cells = truth.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(rms)

# add a policy with no exposure record, to exercise the diagnostics
policies = pl.concat([policies, pl.DataFrame(
    {"EVENTID": [0], "POLICYID": [999], "LOBNAME": ["HO"],
     "PERSPVALUE": [1e5], "STDDEVI": [5e4], "STDDEVC": [3e4]},
    schema=policies.schema)])

pieces, diag = split_policies_by_region(policies, exposure, gross_cells)
print("Diagnostics:", {k: (round(v, 6) if isinstance(v, float) else v) for k, v in diag.items()})

# 1. policy totals preserved
back = pieces.group_by(["EVENTID", "POLICYID"]).agg(rms).join(
    policies, on=["EVENTID", "POLICYID"], suffix="_P")
for c in ["PERSPVALUE", "STDDEVI", "STDDEVC"]:
    print(f"policy totals {c:<10} max rel err {(back[c] / back[c + '_P'] - 1).abs().max():.1e}")

# 2. split vs the true regional pieces
cmp = (truth.join(pieces, on=["EVENTID", "POLICYID", "REGION"], how="full", coalesce=True,
                  suffix="_S")
       .with_columns(pl.col("PERSPVALUE").fill_null(0), pl.col("PERSPVALUE_S").fill_null(0)))
moved = (cmp["PERSPVALUE"] - cmp["PERSPVALUE_S"]).abs().sum() / 2 / truth["PERSPVALUE"].sum()
print(f"share of loss put in the wrong region: {moved:.1%}")

static = (policies.join(exposure.group_by(["POLICYID", "REGION"]).agg(pl.col("TIV").sum()),
                        on="POLICYID")
          .with_columns((pl.col("TIV") / pl.col("TIV").sum().over(["EVENTID", "POLICYID"]))
                        .alias("W"))
          .with_columns((pl.col("PERSPVALUE") * pl.col("W")).alias("PERSPVALUE")))
cmp2 = (truth.join(static.select("EVENTID", "POLICYID", "REGION", "PERSPVALUE"),
                   on=["EVENTID", "POLICYID", "REGION"], how="full", coalesce=True, suffix="_S")
        .with_columns(pl.col("PERSPVALUE").fill_null(0), pl.col("PERSPVALUE_S").fill_null(0)))
moved2 = (cmp2["PERSPVALUE"] - cmp2["PERSPVALUE_S"]).abs().sum() / 2 / truth["PERSPVALUE"].sum()
print(f"same measure with plain TIV weights:    {moved2:.1%}")

# 3. cell-level reconstruction
cells_back = pieces.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(pl.col("PERSPVALUE").sum())
c = gross_cells.join(cells_back, on=["EVENTID", "LOBNAME", "REGION"], how="left", suffix="_S")
err = (c["PERSPVALUE_S"].fill_null(0) - c["PERSPVALUE"]).abs().sum() / c["PERSPVALUE"].sum()
print(f"cell means: abs diff / total = {err:.1%}")
