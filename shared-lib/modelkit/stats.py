# modelkit/stats.py
"""
Summary statistics: mean, std dev, CV, min, max, VaR and TVaR, return periods.
"""

from typing import Optional

import numpy as np
import polars as pl
from .common import get_logger

MYLOGGER = get_logger('modelkit.stats')


def calcSummaryStats(
    x,
    var_percentiles=None,
    tvar_percentiles=None,
    tail="largest",
    include_tvar=True,
    already_sorted=False,
):
    x = np.asarray(x, float).ravel()
    x = x[np.isfinite(x)]
    n = x.size
    n_nonzero=n
    if n == 0:
        raise ValueError("No finite values in x")

    tail = tail.lower()
    if tail not in ("largest", "smallest"):
        raise ValueError('tail must be "largest" or "smallest"')

    cv_eps = 1e-12
    mean = x.mean()
    std  = x.std(ddof=1)
    cv   = std / (abs(mean) + cv_eps)
    xmin = x.min()
    xmax = x.max()

    if not already_sorted:
        # Filter zeros before sort — sort is the expensive step
        x = np.sort(x[x != 0.0])  # sort only nonzero values
        n_nonzero = x.size

    metrics = ["Mean", "Std Dev", "CV", "Min", "Max"]
    percs   = [None,   None,       None,  None,  None ]
    vals    = [float(mean), float(std), float(cv),
               float(xmin), float(xmax)]

    var_p  = np.asarray(var_percentiles, float) if var_percentiles is not None and len(var_percentiles) else np.array([], float)
    tvar_p = np.asarray(tvar_percentiles, float) if tvar_percentiles is not None and len(tvar_percentiles) else np.array([], float)

    # Filter to valid range instead of raising
    var_p  = var_p[(var_p > 0) & (var_p < 1)]
    tvar_p = tvar_p[(tvar_p > 0) & (tvar_p < 1)]

    if var_p.size == 0 and tvar_p.size == 0:
        # Nothing to compute beyond base stats
        pass

    want_tvar = bool(include_tvar and tvar_p.size)

    if want_tvar:
        var_for_tvar, tvar_vals = _tvar_include_var_mass_partition(x, tvar_p, tail,n)
        #assert len(tvar_vals) == len(tvar_p), f"tvar mismatch: {len(tvar_vals)} vs {len(tvar_p)}"
        metrics += ["TVaR"] * len(tvar_p)
        percs   += [float(p) for p in tvar_p]
        vals    += [float(v) for v in tvar_vals]

    var_vals = np.array([])  # initialize before the if block

    if var_p.size:
        m_var = np.clip(np.ceil((1.0 - var_p) * n).astype(int), 1, n)
        if tail == "largest":
            nonzero_in_tail = np.minimum(m_var, n_nonzero)
            kth = n_nonzero - nonzero_in_tail
            if n_nonzero > 0:
                var_vals = np.where(
                    nonzero_in_tail > 0,
                    x[np.clip(kth, 0, n_nonzero - 1)],
                    0.0
                )
            else:
                var_vals = np.zeros(len(var_p))
        else:  # smallest
            n_zeros = n - n_nonzero
            nonzero_in_tail = np.maximum(0, m_var - n_zeros).astype(int)
            if n_nonzero > 0:
                var_vals = np.where(
                    nonzero_in_tail > 0,
                    x[np.clip(nonzero_in_tail - 1, 0, n_nonzero - 1)],
                    0.0
                )
            else:
                var_vals = np.zeros(len(var_p))
        var_vals = np.asarray(var_vals, dtype=np.float64).ravel()
        metrics += ["VaR"] * len(var_p)
        percs   += [float(p) for p in var_p]
        vals    += [float(v) for v in var_vals]
    return pl.DataFrame({
        "Metric":     pl.Series(metrics, dtype=pl.Utf8),
        "Percentile": pl.Series(percs,   dtype=pl.Float64),
        "Value":      pl.Series(vals,    dtype=pl.Float64),
    })

def _tvar_include_var_mass_partition(x_sorted, p, tail, n_total):
    """
    x_sorted: sorted array of NON-ZERO values only (ascending)
    n_total: total number of simulations including zeros
    """
    n_nonzero = x_sorted.size
    p = np.asarray(p, float)

    var_vals  = np.empty_like(p, dtype=np.float64)
    tvar_vals = np.empty_like(p, dtype=np.float64)

    # m is tail count based on TOTAL simulations, not just nonzero
    m = np.clip(np.ceil((1.0 - p) * n_total).astype(int), 1, n_total)

    for i, mi in enumerate(m):
        if tail == "largest":
            # How many of the top-mi values come from nonzero array?
            nonzero_in_tail = min(mi, n_nonzero)
            zeros_in_tail   = mi - nonzero_in_tail

            if nonzero_in_tail == 0:
                # VaR is zero, TVaR is zero
                var_vals[i]  = 0.0
                tvar_vals[i] = 0.0
            else:
                kth = n_nonzero - nonzero_in_tail
                var_vals[i]  = x_sorted[kth]
                tail_sum     = x_sorted[kth:].sum()  # nonzero tail
                # zeros contribute 0 to TVaR sum
                tvar_vals[i] = tail_sum / mi  # divide by total tail count including zeros
        else:
            # smallest tail — zeros would be at the bottom
            # if mi > n_zeros, some nonzero values are in tail
            n_zeros = n_total - n_nonzero
            nonzero_in_tail = max(0, mi - n_zeros)

            if nonzero_in_tail == 0:
                var_vals[i]  = 0.0
                tvar_vals[i] = 0.0
            else:
                var_vals[i]  = x_sorted[nonzero_in_tail - 1]
                tail_sum     = x_sorted[:nonzero_in_tail].sum()
                tvar_vals[i] = tail_sum / mi

    return var_vals, tvar_vals

def summarizeByGroup(
    df: pl.DataFrame,
    group_col: str,
    drop_cols: list[str] = [],
    percentiles: list[float] = [0.996],
    tvarpctiles: Optional[list[float]] = None
) -> pl.DataFrame:

    NUMERIC_POLARS_DTYPES = [
    pl.Int8, pl.Int16, pl.Int32, pl.Int64,
    pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64,
    pl.Float32, pl.Float64,]

    number_cols=pl.col(NUMERIC_POLARS_DTYPES)
    percentiles=set(percentiles)  # Ensure unique percentiles

    if tvarpctiles is None:
        tvarpctiles = percentiles.copy()  # If not provided, use the same as percentiles
    else:
        tvarpctiles=list(set(percentiles.intersection(tvarpctiles)))  # Ensure unique percentiles for TVaR, must be a subset of percentiles

    # Drop unwanted columns
    df_clean = df.drop(drop_cols)

    # Identify numeric data columns

    data_cols = [col for col in df_clean.select(number_cols).columns if col != group_col]

    # 1. Compute basic stats
    basic_stats = df_clean.group_by(group_col).agg([
        pl.col(col).mean().alias(f"{col}_Mean") for col in data_cols
    ] + [
        pl.col(col).std().alias(f"{col}_Std") for col in data_cols
    ] + [
        (pl.col(col).std() / pl.col(col).mean()).alias(f"{col}_CV") for col in data_cols
    ] + [
        pl.col(col).min().alias(f"{col}_Min") for col in data_cols
    ] + [
        pl.col(col).max().alias(f"{col}_Max") for col in data_cols
    ])

    # 2. Compute quantiles
    quantile_aggs = []
    for q in percentiles:
        quantile_aggs.extend([
            pl.col(col).quantile(q).alias(f"{col}_q{str(q).replace('.', '')}") for col in data_cols
        ])
    quantiles = df_clean.group_by(group_col).agg(quantile_aggs)

    # 3. Join quantiles back
    df_joined = df_clean.join(quantiles, on=group_col, how="left")

    # 4. Compute tail means (values > quantile)
    tail_means_aggs = []
    for q in percentiles:
        qstr = f"q{str(q).replace('.', '')}"
        for col in data_cols:
            tail_means_aggs.append(
                pl.when(pl.col(col) > pl.col(f"{col}_{qstr}"))
                  .then(pl.col(col))
                  .otherwise(None)
                  .mean()
                  .alias(f"{col}_TVaR_{qstr}")
            )
    tail_means = df_joined.group_by(group_col).agg(tail_means_aggs)

    # 5. Combine all summaries
    summary = basic_stats.join(quantiles, on=group_col, how="left").join(tail_means, on=group_col, how="left")

    # 6. Melt into long format
    long_rows = []
    for col in data_cols:
        rows = []

        # Basic stats
        for metric in ["Mean", "Std", "CV", "Min", "Max"]:
            rows.append(summary.select([
                group_col,
                pl.lit(col).alias("View"),
                pl.lit(metric).alias("Metric"),
                pl.lit(None, pl.Float64).alias("Percentile"),
                pl.col(f"{col}_{metric}").alias("Value")
            ]))

        # Quantiles and TVaRs
        for q in percentiles:
            qstr = f"q{str(q).replace('.', '')}"
            qval = pl.lit(q).alias("Percentile")
            rows.append(summary.select([
                group_col,
                pl.lit(col).alias("View"),
                pl.lit("Quantile").alias("Metric"),
                qval,
                pl.col(f"{col}_{qstr}").alias("Value")
            ]))

        for q in tvarpctiles:
            qstr = f"q{str(q).replace('.', '')}"
            qval = pl.lit(q).alias("Percentile")
            rows.append(summary.select([
                group_col,
                pl.lit(col).alias("View"),
                pl.lit("TVaR").alias("Metric"),
                qval,
                pl.col(f"{col}_TVaR_{qstr}").alias("Value")
            ]))

        long_rows.append(pl.concat(rows))

    return pl.concat(long_rows).with_columns(pl.col('Value').fill_null(0.0))

def getReturnPeriod(pctile):
    try:
        if pctile<.5:
            suffix=" (Best)"
            rp=1/(pctile)
        elif pctile==1:
            return "max"
        elif pctile==0:
            return "min"
        else:
            suffix=""
            rp=1/(1-pctile)

        if round(rp,0)==round(rp,4):
            return str(int(round(rp,0)))+ " yr" + suffix
        else:
            return str(round(rp,3))+ " yr" + suffix
    except:
        return ""
