# modelkit/frames.py
"""
Polars DataFrame / LazyFrame helpers: schema validation, concatenation with
column alignment, column selection, numeric comparison, null cleanup, parsing.
"""

import warnings
from itertools import chain
from typing import Dict, Optional, Set, Union

import polars as pl
from .common import get_logger

MYLOGGER = get_logger('modelkit.frames')


def validate_polars_schema(
    df: Union[pl.DataFrame, pl.LazyFrame],
    expected_schema: Dict[str, pl.DataType],
    cast: bool = False,
    name: str = "DataFrame",
    optional_cols: Optional[Set[str]] = None,
    warn_only: bool = False,
    remove_extra_cols: bool = True,
) -> Union[pl.DataFrame, pl.LazyFrame]:
    """
    Validates and optionally transforms a Polars DataFrame or LazyFrame
    according to a schema, with optional casting and column pruning.

    Parameters:
        df: The Polars DataFrame or LazyFrame to validate.
        expected_schema: Dict of expected column names and their data types.
        cast: Whether to cast mismatched types.
        name: Name used in warnings/errors.
        optional_cols: Columns allowed to be missing.
        warn_only: Whether to issue warnings instead of errors.
        remove_extra_cols: Whether to drop columns not in the expected schema.

    Returns:
        A validated (and optionally transformed) Polars DataFrame or LazyFrame.
    """
    is_lazy = isinstance(df, pl.LazyFrame)
    optional_cols = optional_cols or set()
    schema = df.schema if not is_lazy else df.collect_schema()

    exprs = []
    for col, expected_dtype in expected_schema.items():
        if col not in schema:
            if col in optional_cols:
                continue
            msg = f"{name} is missing required column: '{col}'"
            if warn_only:
                warnings.warn(msg)
                continue
            raise ValueError(msg)

        actual_dtype = schema[col]
        if isinstance(expected_dtype, pl.List) and isinstance(actual_dtype, pl.List):
            if expected_dtype.inner != actual_dtype.inner:
                msg = f"{name} column '{col}' should be List[{expected_dtype.inner}], got List[{actual_dtype.inner}]"
                if warn_only:
                    warnings.warn(msg)
                    continue
                raise TypeError(msg)
        elif actual_dtype != expected_dtype:
            if cast:
                exprs.append(pl.col(col).cast(expected_dtype))
            else:
                msg = f"{name} column '{col}' should be {expected_dtype}, got {actual_dtype}"
                if warn_only:
                    warnings.warn(msg)
                    continue
                raise TypeError(msg)
        else:
            exprs.append(pl.col(col))

    # Add optional columns that are present
    for col in optional_cols:
        if col in schema and col not in expected_schema:
            exprs.append(pl.col(col))

    # Remove extra columns
    if remove_extra_cols:
        expected_plus_optional = set(expected_schema.keys()).union(optional_cols)
        exprs = [pl.col(c) for c in schema.keys() if c in expected_plus_optional]
    elif not exprs:
        exprs = [pl.all()]

    # Apply projection
    return df.select(exprs)

def normalize_lazy(df_lazy: pl.LazyFrame, all_cols: list[str]) -> pl.LazyFrame:
    # Add missing cols as nulls
    missing = [c for c in all_cols if c not in df_lazy.collect_schema().names()]
    if missing:
        df_lazy = df_lazy.with_columns([pl.lit(None).alias(c) for c in missing])
    # Select ensures column order is consistent
    return df_lazy.select(all_cols)

def concat_normalize_lazy(dfs: list[pl.LazyFrame]) -> pl.LazyFrame:
    # Concatenate all DataFrames and normalize the result

    all_cols = list(
        dict.fromkeys(
            chain.from_iterable(df.collect_schema().names() for df in dfs)))

    return pl.concat([normalize_lazy(src, all_cols) for src in dfs],how='vertical')

def concat_normalize_collect(dfs: list[pl.LazyFrame]) -> pl.DataFrame:
    # Concatenate all DataFrames and normalize the result

    all_cols = list(
        dict.fromkeys(
            chain.from_iterable(df.collect_schema().names() for df in dfs)))
    return pl.concat([normalize_lazy(src, all_cols) for src in dfs],how='vertical').collect(streaming=True)

def firstRowToDict(tbl):
    result= dict(zip(tbl.columns,tbl[0].transpose().get_column("column_0").to_list()))
    return result

def df_to_nested_dict(df: pl.DataFrame) -> dict:
    """
    Convert a Polars DataFrame to a nested dictionary.
    The number of nested levels is one less than the number of columns.
    The last column's values are used as leaf values.
    """
    cols = df.columns
    if len(cols) < 2:
        raise ValueError("Need at least 2 columns to form a nested dictionary")

    result = {}

    for row in df.iter_rows(named=True):
        current = result
        for col in cols[:-2]:
            current = current.setdefault(row[col], {})
        # Second-to-last key level
        current.setdefault(row[cols[-2]], row[cols[-1]])

    return result

def filterDataFrameColumns(df,excluded_suffixes=[],excluded_cols=[]):
    #Remove columns that end with any of the excluded suffixes or are in the excluded columns list
    return  df.select([col for col in df.columns
        if not any(col.endswith(suf) for suf in excluded_suffixes)
        and col not in excluded_cols])

def colListWithSuffix(df, suffix):
    #returns a list of column names in df that end with the specified suffix
    return [col for col in df.columns if col.endswith(suffix)]

def dropSuffixFromList(lst, suffix):
    #returns a list of column names in lst with the specified suffix removed
    return [col[:-len(suffix)] for col in lst if col.endswith(suffix)]

def pl_isclose(a: pl.Expr, b: pl.Expr, rel_tol: float = 1e-9, abs_tol: float = 1e-6) -> pl.Expr:
    """
    Polars expression equivalent of math.isclose(a, b).

    Parameters
    ----------
    a, b : pl.Expr
        Polars expressions (e.g. columns) to compare.
    rel_tol : float
        Relative tolerance.
    abs_tol : float
        Absolute tolerance.

    Returns
    -------
    pl.Expr
        A boolean Polars expression indicating where a and b are close.
    """
    return (
        (a - b).abs()
        <= (rel_tol * pl.max_horizontal(a.abs(), b.abs()) + abs_tol)
    )

def _gt_zero(expr: pl.Expr) -> pl.Expr:
    """Return True where expr is meaningfully greater than zero (Float64-safe)."""
    return ~pl_isclose(expr, pl.lit(0.0)) & (expr > 0)

def clip(_val, minval, maxval):
    # Ensure dtype matches _val
    return _val.clip(pl.lit(minval, dtype=pl.Float64), pl.lit(maxval, dtype=pl.Float64))

def dfReplaceNanNone(df):
    #replace NaN with None
    result= (df
              .with_columns(
                pl.when(pl.col(pl.Utf8).is_in(["nan","None","NaN"]))
                .then(None)
                .otherwise(pl.col(pl.Utf8))
                .name.keep()))
    return result

def stringToFloat(x):
    if x.strip()[-1]=="%":
        return float(x.strip().strip('%'))/100
    else:
        return float(x.strip())
