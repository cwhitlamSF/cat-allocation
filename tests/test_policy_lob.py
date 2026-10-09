"""Chunks without LOBNAME + policy_lob map vs chunks with LOBNAME."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))

import glob, os, tempfile, warnings
import polars as pl
warnings.filterwarnings("ignore")
src = open(os.path.join(os.path.dirname(__file__) or ".", "test_multiflag.py")).read()
exec(src[:src.index('print("\\n2. HO single-region book')])
from catalloc.driver import run_allocation

folder, exposure, gross, net = book()
st = stages_from_table(inuring(False), subj)
_, with_lob, _, d1 = run_allocation(folder, exposure, gross, net, st, upper, subj, verbose=False)

# copy chunks without LOBNAME; map from the chunks, minus one policy
no_lob = tempfile.mkdtemp()
maps = []
for f in sorted(glob.glob(f"{folder}/pol_part_*.parquet")):
    df = pl.read_parquet(f)
    maps.append(df.select("POLICYID", "LOBNAME"))
    df.drop("LOBNAME").write_parquet(os.path.join(no_lob, os.path.basename(f)))
pmap = pl.concat(maps).unique()

_, mapped, _, d2 = run_allocation(no_lob, exposure, gross, net, st, upper, subj,
                                  policy_lob=pmap, verbose=False)
cmp = with_lob.join(mapped, on="POLICYID", suffix="_MAP")
print(f"chunks with LOBNAME vs policy_lob map: max per-policy diff "
      f"{(cmp['PREMIUM'] - cmp['PREMIUM_MAP']).abs().max():.2e}; rows_no_lob {d2['rows_no_lob']}")

missing = pmap["POLICYID"][0]
_, m2, _, d3 = run_allocation(no_lob, exposure, gross, net, st, upper, subj,
                              policy_lob=pmap.filter(pl.col("POLICYID") != missing), verbose=False)
print(f"one policy missing from the map: rows_no_lob {d3['rows_no_lob']}, rows_no_exposure "
      f"{d3['rows_no_exposure']}, policy in output: {missing in m2['POLICYID'].to_list()}, "
      f"total {m2['PREMIUM'].sum():,.2f}")


# compact storage: Int32 IDs and Float32 values in the chunks
small = tempfile.mkdtemp()
for f in sorted(glob.glob(f"{no_lob}/pol_part_*.parquet")):
    (pl.read_parquet(f)
       .with_columns(pl.col("EVENTID", "POLICYID").cast(pl.Int32),
                     pl.col("PERSPVALUE", "STDDEVI", "STDDEVC").cast(pl.Float32))
       .write_parquet(os.path.join(small, os.path.basename(f))))
_, m3, _, d4 = run_allocation(small, exposure, gross, net, st, upper, subj,
                              policy_lob=pmap, verbose=False)
cmp = mapped.join(m3, on="POLICYID", suffix="_32")
rel = ((cmp["PREMIUM_32"] - cmp["PREMIUM"]).abs() / cmp["PREMIUM"].abs().max())
size = lambda d: sum(os.path.getsize(x) for x in glob.glob(f"{d}/*.parquet"))
print(f"Int32/Float32 chunks vs 64-bit: max per-policy diff {(cmp['PREMIUM_32'] - cmp['PREMIUM']).abs().max():.4f} "
      f"(relative to largest policy {rel.max():.1e}); total {m3['PREMIUM'].sum():,.2f}; "
      f"files {size(small) / size(no_lob):.0%} of 64-bit size; POLICYID dtype {m3.schema['POLICYID']}")

# reusable lookups: build once, then trial and full runs
from catalloc.driver import prepare_lookups
import time
t = time.time()
lk = prepare_lookups(exposure, gross, net, st, upper, subj, policy_lob=pmap, verbose=True)
t_build = time.time() - t
t = time.time()
_, trial, _, _ = run_allocation(no_lob, None, None, None, None, None, subj,
                                pattern="pol_part_00[0-1].parquet", lookups=lk, verbose=False)
_, full, _, _ = run_allocation(no_lob, None, None, None, None, None, subj, lookups=lk, verbose=False)
cmp = mapped.join(full, on="POLICYID", suffix="_LK")
print(f"lookups built in {t_build:.1f}s; trial + full with reused lookups {time.time() - t:.1f}s; "
      f"max diff vs fresh build {(cmp['PREMIUM'] - cmp['PREMIUM_LK']).abs().max():.2e}")
