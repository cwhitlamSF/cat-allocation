"""Prepare Data: shuffled raw chunks -> event-grouped files; same allocation as the raw chunks."""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
_os.chdir(_os.path.dirname(_os.path.abspath(__file__)))
import glob, os, sqlite3, tempfile, warnings
import numpy as np, polars as pl
warnings.filterwarnings("ignore")
src = open("test_driver.py").read()
exec(src[:src.index("results = {}")])            # synthetic book; shuffled pol_part chunks in `folder`
from catalloc.driver import run_allocation, prepare_lookups
from catalloc.prepare import prepare_event_buckets

# raw chunks with awkward column names and an extra column
raw = tempfile.mkdtemp()
for i, f in enumerate(sorted(glob.glob(f"{folder}/pol_part_*.parquet"))):
    pl.read_parquet(f).rename({"POLICYID": "PolicyID", "PERSPVALUE": "Loss"}).with_columns(
        pl.lit(1).alias("JUNK")).write_parquet(f"{raw}/chunk_{i:03d}.parquet")
out = tempfile.mkdtemp()
open(f"{out}/ev_bucket_9999.parquet", "w").close()                     # stale file: must be replaced
man = prepare_event_buckets(sorted(glob.glob(f"{raw}/*.parquet")), out, target_rows=15_000,
                            renames={"Loss": "PERSPVALUE"}, progress=None)
print(man)
files = sorted(glob.glob(f"{out}/ev_bucket_*.parquet"))
evs = [set(pl.read_parquet(f)["EVENTID"].to_list()) for f in files]
overlap = sum(len(a & b) for i, a in enumerate(evs) for b in evs[i + 1:])
print(f"{len(files)} files; events in more than one file: {overlap}; rows {man['Rows'].sum():,} vs {policies.height:,};"
      f" schema {pl.read_parquet(files[0]).schema}")
pmap = policies.select("POLICYID", "LOBNAME").unique()
lk = prepare_lookups(exposure, gross_cells, cells, stages, upper, subj, pmap, verbose=False)
_, a, _, da = run_allocation(folder, None, None, None, None, None, subj, lookups=lk, verbose=False)
_, b, _, db = run_allocation(out, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                             lookups=lk, verbose=False)
c = a.join(b, on="POLICYID", suffix="_B")
print(f"raw chunks (two-pass) vs prepared (one pass): max |diff| {(c['PREMIUM'] - c['PREMIUM_B']).abs().max():.2e}; "
      f"spanning {da['events_spanning_files']} vs {db['events_spanning_files']}")

# database source (SQLite through SQLAlchemy)
db_path = os.path.join(tempfile.mkdtemp(), "elt.db")
con = sqlite3.connect(db_path)
policies.select("EVENTID", "POLICYID", "PERSPVALUE", "STDDEVI", "STDDEVC").to_pandas().to_sql("polelt", con, index=False)
con.close()
out2 = tempfile.mkdtemp()
man2 = prepare_event_buckets(None, out2, target_rows=15_000, chunk_rows=7_000,
                             query=dict(connection=f"sqlite:///{db_path}", query="select * from polelt"))
_, b2, _, _ = run_allocation(out2, None, None, None, None, None, subj, pattern="ev_bucket_*.parquet",
                             lookups=lk, verbose=False)
c2 = a.join(b2, on="POLICYID", suffix="_B")
print(f"database query: {man2.height} files, raw folder removed {not os.path.exists(out2 + '/_raw')}; "
      f"max |diff| {(c2['PREMIUM'] - c2['PREMIUM_B']).abs().max():.2e}")
