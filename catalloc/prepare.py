"""
Build event-grouped policy ELT files from the raw policy ELT.

The allocation runs in one pass, with nothing written to disk, when every event's rows sit
in a single file. This module rewrites the policy ELT that way:

    prepare_event_buckets(files=[...parquet chunks...], out_folder=..., target_rows=3_000_000)
    prepare_event_buckets(query=dict(connection=..., query=...), out_folder=...)

Steps
  0  (query only) stream the query to Parquet chunks in <out>/_raw
  1  count rows per EVENTID (reads only the EVENTID column)
  2  pack whole events, in EVENTID order, into buckets of about target_rows rows
     (an event larger than target_rows gets a bucket of its own)
  3  split each input file into per-bucket parts (<out>/_parts/b00007/p00012.parquet)
  4  combine each bucket's parts into <out>/ev_bucket_0007.parquet, sorted by EVENTID
  5  write <out>/ev_buckets_manifest.csv and remove the working folders

Each input file is read twice (steps 1 and 3) and the data is written twice (3 and 4), but
no more than one output file is open at a time, however many buckets there are.

Columns: EVENTID, POLICYID, PERSPVALUE, STDDEVI, STDDEVC. Names are matched without regard
to case (PolicyID -> POLICYID), and `renames` maps any others ({"PERSPLOSS": "PERSPVALUE"}).
Other columns are dropped. IDs are stored as Int32 when they fit and values as Float32;
the allocation upcasts them.
"""
from __future__ import annotations

import glob
import logging
import os
import re
import shutil

import polars as pl

MYLOGGER = logging.getLogger(__name__)

COLUMNS = ["EVENTID", "POLICYID", "PERSPVALUE", "STDDEVI", "STDDEVC"]
BUCKET_PREFIX = "ev_bucket_"
MANIFEST = "ev_buckets_manifest.csv"
_I32 = 2 ** 31 - 1


def _say(progress, msg):
    MYLOGGER.info(msg)
    if progress is not None:
        progress(msg)


def column_map(names, renames=None):
    """Map the file's column names to the standard ones: explicit renames first, then a
    case-insensitive match. Returns {file name: standard name} for the columns found."""
    renames = {k.strip(): v.strip().upper() for k, v in (renames or {}).items()}
    out = {}
    for n in names:
        target = renames.get(n) or renames.get(n.strip())
        if target is None and n.strip().upper() in COLUMNS:
            target = n.strip().upper()
        if target in COLUMNS and target not in out.values():
            out[n] = target
    return out


def parse_renames(text):
    """'PolicyID=POLICYID, Loss=PERSPVALUE' -> {'PolicyID': 'POLICYID', 'Loss': 'PERSPVALUE'}."""
    if text is None or str(text).strip() in ("", "None", "nan"):
        return {}
    out = {}
    for part in str(text).split(","):
        if "=" not in part:
            raise ValueError(f"Column renames: '{part.strip()}' is not of the form Old=New")
        a, b = part.split("=", 1)
        out[a.strip()] = b.strip()
    return out


def _standard(lf: pl.LazyFrame, renames) -> pl.LazyFrame:
    names = lf.collect_schema().names()
    cmap = column_map(names, renames)
    missing = [c for c in COLUMNS if c not in cmap.values()]
    if missing:
        raise ValueError(f"Policy ELT is missing column(s) {', '.join(missing)} "
                         f"(found {', '.join(names)}). Add a Column Renames entry if they have other names.")
    return lf.select([pl.col(k).alias(v) for k, v in cmap.items()]).select(COLUMNS)


def _clear(out_folder):
    old = glob.glob(os.path.join(out_folder, f"{BUCKET_PREFIX}*.parquet"))
    for f in old:
        os.remove(f)
    for d in ("_parts", "_raw"):
        shutil.rmtree(os.path.join(out_folder, d), ignore_errors=True)
    return len(old)


def query_to_chunks(connection, query, out_folder, chunk_rows=1_000_000, progress=None):
    """Stream a SQL query to Parquet chunks <out>/_raw/raw_00000.parquet, ... and return their
    paths. connection is a SQLAlchemy URL; ${VAR} is filled from the environment."""
    import pandas as pd
    try:
        import sqlalchemy
    except ImportError as e:
        raise ImportError("A database query needs the sqlalchemy package (and the database's driver)") from e

    def env(m):
        if m.group(1) not in os.environ:
            raise ValueError(f"Environment variable {m.group(1)} is not set (used in the connection string)")
        return os.environ[m.group(1)]
    url = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", env, str(connection))
    raw = os.path.join(out_folder, "_raw")
    os.makedirs(raw, exist_ok=True)
    engine = sqlalchemy.create_engine(url)
    paths, rows = [], 0
    try:
        with engine.connect().execution_options(stream_results=True) as conn:
            for i, chunk in enumerate(pd.read_sql(sqlalchemy.text(str(query)), conn, chunksize=chunk_rows)):
                p = os.path.join(raw, f"raw_{i:05d}.parquet")
                pl.from_pandas(chunk).write_parquet(p, compression="zstd")
                paths.append(p)
                rows += len(chunk)
                _say(progress, f"Prepare: query chunk {i + 1} ({rows:,} rows)")
    finally:
        engine.dispose()
    return paths


def prepare_event_buckets(files=None, out_folder=None, target_rows=3_000_000, renames=None,
                          query=None, chunk_rows=1_000_000, progress=None, keep_raw=False):
    """
    Write event-grouped policy ELT files (see module docstring).

    files:       input Parquet files (any grouping), or None with `query`
    query:       dict(connection=SQLAlchemy URL, query=SQL) instead of files
    out_folder:  where the ev_bucket_*.parquet files go. Existing ev_bucket files there are
                 replaced, so the folder never mixes two preparations.
    target_rows: rows per output file (whole events, so files vary around this)
    Returns a DataFrame with one row per file written (File, Rows, Events, First Event,
    Last Event), also saved as ev_buckets_manifest.csv in out_folder.
    """
    if not out_folder:
        raise ValueError("An output folder is needed for the prepared files")
    os.makedirs(out_folder, exist_ok=True)
    n_old = _clear(out_folder)
    if n_old:
        _say(progress, f"Prepare: replaced {n_old} existing {BUCKET_PREFIX} files in {out_folder}")
    if query is not None:
        files = query_to_chunks(query["connection"], query["query"], out_folder, chunk_rows, progress)
    files = sorted(files or [])
    if not files:
        raise ValueError("No policy ELT input files were found")

    # 1. rows per event
    counts = []
    for i, f in enumerate(files):
        lf = _standard(pl.scan_parquet(f), renames)
        counts.append(lf.group_by("EVENTID").agg(pl.len().alias("N")).collect())
        _say(progress, f"Prepare: counting rows, file {i + 1} of {len(files)}")
    counts = (pl.concat(counts).group_by("EVENTID").agg(pl.col("N").sum()).sort("EVENTID"))
    total = int(counts["N"].sum())

    # 2. whole events into buckets of about target_rows
    bucket, filled, ids = 0, 0, []
    for n in counts["N"].to_list():
        if filled and filled + n > target_rows:
            bucket += 1
            filled = 0
        ids.append(bucket)
        filled += n
    bmap = counts.with_columns(pl.Series("_B", ids, dtype=pl.Int32)).select("EVENTID", "_B")
    n_b = bucket + 1
    _say(progress, f"Prepare: {total:,} rows, {counts.height:,} events -> {n_b} files")

    # ID widths: Int32 when they fit
    stats = []
    for f in files:
        stats.append(_standard(pl.scan_parquet(f), renames).select(
            pl.col("EVENTID").max().alias("E"), pl.col("POLICYID").max().alias("P"),
            pl.col("EVENTID").min().alias("e"), pl.col("POLICYID").min().alias("p")).collect())
    st = pl.concat(stats)
    def fits(col_max, col_min):
        try:
            return int(st[col_max].max()) <= _I32 and int(st[col_min].min()) >= -_I32
        except (TypeError, ValueError):
            return False
    eid_t = pl.Int32 if fits("E", "e") else pl.Int64
    pid_t = pl.Int32 if fits("P", "p") else pl.Int64
    casts = [pl.col("EVENTID").cast(eid_t), pl.col("POLICYID").cast(pid_t),
             pl.col("PERSPVALUE", "STDDEVI", "STDDEVC").cast(pl.Float32)]

    # 3. split each input into per-bucket parts
    parts = os.path.join(out_folder, "_parts")
    os.makedirs(parts, exist_ok=True)
    for i, f in enumerate(files):
        df = (_standard(pl.scan_parquet(f), renames)
              .join(bmap.lazy(), on="EVENTID", how="inner").collect())
        for (b,), piece in df.partition_by("_B", as_dict=True).items():
            d = os.path.join(parts, f"b{b:05d}")
            os.makedirs(d, exist_ok=True)
            piece.drop("_B").with_columns(casts).write_parquet(os.path.join(d, f"p{i:05d}.parquet"))
        del df
        _say(progress, f"Prepare: splitting, file {i + 1} of {len(files)}")

    # 4. combine each bucket's parts
    width = max(4, len(str(n_b - 1)))
    rows = []
    for b in range(n_b):
        d = os.path.join(parts, f"b{b:05d}")
        pieces = sorted(glob.glob(os.path.join(d, "*.parquet")))
        df = pl.concat([pl.read_parquet(p, memory_map=False) for p in pieces]).sort("EVENTID")
        name = f"{BUCKET_PREFIX}{b:0{width}d}.parquet"
        df.write_parquet(os.path.join(out_folder, name), compression="zstd")
        rows.append({"File": name, "Rows": df.height, "Events": df["EVENTID"].n_unique(),
                     "First Event": int(df["EVENTID"].min()), "Last Event": int(df["EVENTID"].max())})
        del df
        shutil.rmtree(d, ignore_errors=True)
        _say(progress, f"Prepare: writing file {b + 1} of {n_b}")

    # 5. manifest, clean up
    shutil.rmtree(parts, ignore_errors=True)
    if query is not None and not keep_raw:
        shutil.rmtree(os.path.join(out_folder, "_raw"), ignore_errors=True)
    manifest = pl.DataFrame(rows)
    manifest.write_csv(os.path.join(out_folder, MANIFEST))
    if int(manifest["Rows"].sum()) != total:
        raise RuntimeError(f"Prepare: wrote {int(manifest['Rows'].sum()):,} rows but read {total:,}")
    _say(progress, f"Prepare: wrote {n_b} files, {total:,} rows, to {out_folder}")
    return manifest
