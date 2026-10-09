"""Negative contributions: report and per-event clamp on event-grouped chunks."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import glob, os, tempfile, warnings
import numpy as np, polars as pl
warnings.filterwarnings("ignore")
src = open(os.path.join(os.path.dirname(__file__) or ".", "test_multiflag.py")).read()
exec(src[:src.index('print("\\n2. HO single-region book')])
from catalloc.driver import prepare_lookups, run_allocation

folder, exposure, gross, net = book()
st = stages_from_table(inuring(False), subj)
allp = pl.concat([pl.read_parquet(f) for f in glob.glob(f"{folder}/*.parquet")])
pmap = allp.select("POLICYID", "LOBNAME").unique()
ev_dir = tempfile.mkdtemp()                                   # event-grouped copy
for b in range(4):
    allp.filter(pl.col("EVENTID") % 4 == b).drop("LOBNAME").write_parquet(f"{ev_dir}/ev_bucket_{b:03d}.parquet")
lk = prepare_lookups(exposure, gross, net, st, upper, subj, policy_lob=pmap, verbose=False)
_, a, ia, da = run_allocation(ev_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                              lookups=lk, verbose=False)
_, b, ib, db = run_allocation(ev_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                              lookups=lk, clamp_negative=True, verbose=False)
print(ia.select("Layer", "negative_rows_before_clamp", "negative_share_before_clamp", "negative_policies"))
print(ib.select("Layer", "negative_policies"))
cmp = a.join(b, on="POLICYID", suffix="_CL")
print(f"events spanning files: {da['events_spanning_files']}; totals {a['PREMIUM'].sum():,.2f} vs "
      f"{b['PREMIUM'].sum():,.2f}; max |change| {(cmp['PREMIUM'] - cmp['PREMIUM_CL']).abs().max():,.2f}")
try:
    run_allocation(folder, None, None, None, None, None, subj, lookups=lk, clamp_negative=True, verbose=False)
except ValueError as e:
    print("shuffled chunks:", e)

# force negatives: some policies with a negative correlated-SD coefficient (as co-recovery can give)
rng2 = np.random.default_rng(4)
neg_dir = tempfile.mkdtemp()
for f in glob.glob(f"{ev_dir}/ev_bucket_*.parquet"):
    d = pl.read_parquet(f)
    flag = pl.Series(rng2.random(d.height) < 0.05)
    d.with_columns(pl.when(flag).then(-2.0 * pl.col("PERSPVALUE")).otherwise(pl.col("STDDEVC"))
                   .alias("STDDEVC")).write_parquet(f"{neg_dir}/{os.path.basename(f)}")
_, a, ia, _ = run_allocation(neg_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                             lookups=lk, positive_split=False, verbose=False)
_, b, ib, _ = run_allocation(neg_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                             lookups=lk, positive_split=False, clamp_negative=True, verbose=False)
_, c, ic, _ = run_allocation(neg_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                             lookups=lk, verbose=False)
print("\nwith forced negatives, before clamp:")
print(ia.select("Layer", "negative_rows_before_clamp", "negative_share_before_clamp", "negative_policies"))
print("after clamp:", ib["negative_policies"].to_list(),
      f"totals {a['PREMIUM'].sum():,.2f} vs {b['PREMIUM'].sum():,.2f}")
print("bounded split (default):", ic["negative_rows_before_clamp"].to_list(),
      "negative rows; bounded rows", ic["bounded_rows"].to_list(), f"total {c['PREMIUM'].sum():,.2f}")

# one pass (default for event-grouped files) vs forced two-pass cache: identical
_, s1, i1, _ = run_allocation(ev_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                              lookups=lk, verbose=False)
_, s2, i2, _ = run_allocation(ev_dir, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                              lookups=lk, stream=False, verbose=False)
cmp = s1.join(s2, on="POLICYID", suffix="_C")
print(f"one pass vs cached: max |diff| {(cmp['PREMIUM'] - cmp['PREMIUM_C']).abs().max():.2e}; "
      f"bounded rows {i1['bounded_rows'].to_list()} vs {i2['bounded_rows'].to_list()}")
