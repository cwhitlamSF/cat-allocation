"""
CCA_Functions.py
================
Model hooks for the Cat Cost Allocation tool. The shared platform (shared-lib/main.py and
_analysis.py) loads the specs and calls, in order:

    initialCleanSpecs(specs, specpath)       Excel runs only: tidy the input tables
    createPreppedSpecs(preppedspecs)          check the tables; set preppedspecs['error'] to stop
    modelSpecificAnalysisSteps(analysis, ...) run (Prepare Data or Allocate) and write the results

The engine is the catalloc package (distortion, inuring, driver, prepare).
"""
import glob
import logging
import math
import os
import sys
import time

import polars as pl

_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_here, 'shared-lib'))
sys.path.insert(0, _here)
from modelkit.logs import DATA                                          # noqa: E402
from modelkit.status import getCurrentStatus                            # noqa: E402
from modelkit.transform import readSource, resolvePath                  # noqa: E402

from cca_tables import (RUN_TYPES, DATASETS, SOURCETYPES, PREP_SOURCETYPES, DISTORTIONS, BASES,  # noqa: E402
                        SHARINGS, SPLITS, RESULT_BY_TABLE,
                        SPEC_GENERAL, SPEC_PREPARE, SPEC_SOURCES, SPEC_SUBJECTS, SPEC_INURING, SPEC_UPPER,
                        G_RUNNAME, G_RUNTYPE, G_OUTPUT, G_ELTFOLDER, G_ELTPATTERN, G_ELTNET, G_DISTORTION,
                        G_BASIS, G_SHARING, G_SPLIT, G_BOUNDED, G_CLAMP, G_RP, G_BENCHMARK, G_GRID,
                        P_SOURCETYPE, P_FOLDER, P_PATTERN, P_CONNECTION, P_QUERY, P_TARGET, P_RENAMES, P_KEEPRAW,
                        S_NAME, S_TYPE, S_PATH, S_SHEET, S_TABLE, S_DELIM, S_CONN, S_QUERY, S_RENAMES,
                        SUB_NAME, SUB_LOB, SUB_REGIONS,
                        L_STAGE, L_LAYER, L_RET, L_LIM, L_PLACE, L_SUBJ, L_MULTI, L_INCLUDE, L_PREM,
                        L_BASIS, L_REINST, L_RRATE, L_AGGLIM, L_AGGDED, L_DIST, L_THETA)
from catalloc.inuring import make_subject, stages_from_table, subject_event_elt, MULTI_COL   # noqa: E402
from catalloc.driver import prepare_lookups, run_allocation, collapse_chunk                 # noqa: E402
from catalloc.prepare import prepare_event_buckets, parse_renames                           # noqa: E402

MYLOGGER = logging.getLogger(__name__)

# Folder of the workbook being run; relative paths in the tables are taken from it
BASEFOLDER = None

# Columns each dataset must have (after renames; names are matched without regard to case)
NEEDED = {
    'BU ELT Gross': ['EVENTID', 'LOBNAME', 'REGION', 'RATE', 'PERSPVALUE', 'STDDEVI', 'STDDEVC', 'EXPVALUE'],
    'BU ELT Net': ['EVENTID', 'LOBNAME', 'REGION', 'RATE', 'PERSPVALUE', 'STDDEVI', 'STDDEVC', 'EXPVALUE'],
    'Exposure': ['POLICYID', 'REGION', 'TIV'],
    'Policy LOB': ['POLICYID', 'LOBNAME'],
}
OPTIONAL = ['COUNTRY', 'STATE']                 # REGION = COUNTRY_STATE when there is no REGION column

RATIO_TOLERANCE = 0.05                          # policies-vs-cells check: warn if the median is off by more


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def _blank(x):
    if x is None:
        return True
    if isinstance(x, float) and math.isnan(x):
        return True
    return str(x).strip() in ('', 'None', 'nan', 'NaN', '<NA>')


def _text(x, default=None):
    if _blank(x):
        return default
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x).strip()


def _bool(x, default=False):
    if _blank(x):
        return default
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ('true', 'yes', 'y', '1', '1.0'):
        return True
    if s in ('false', 'no', 'n', '0', '0.0'):
        return False
    raise ValueError(f"'{x}' is not TRUE or FALSE")


def _number(x, default=None):
    if _blank(x):
        return default
    return float(str(x).replace(',', '')) if isinstance(x, str) else float(x)


def _settings(df):
    """Information / Value table -> {Information: Value} (blank values as None)."""
    if df is None or df.height == 0 or 'Information' not in df.columns:
        return {}
    vals = df['Value'].to_list() if 'Value' in df.columns else [None] * df.height
    return {str(k).strip(): (None if _blank(v) else v) for k, v in zip(df['Information'].to_list(), vals)
            if not _blank(k)}


def _rows(df):
    return [] if df is None else df.to_dicts()


def _included(row):
    return _bool(row.get(L_INCLUDE), True)


def _safe_bool(x, default):
    """For the checks: an unreadable TRUE/FALSE counts as the default (it is reported separately)."""
    try:
        return _bool(x, default)
    except ValueError:
        return default


def _safe_included(row):
    return _safe_bool(row.get(L_INCLUDE), True)


def _regions(cell):
    return [] if _blank(cell) else [r.strip() for r in str(cell).split(',') if r.strip()]


def _path(p):
    return resolvePath(p, BASEFOLDER)


# ---------------------------------------------------------------------------------------------
# initialCleanSpecs
# ---------------------------------------------------------------------------------------------
def initialCleanSpecs(specdict, specpath):
    """Excel runs: remember the workbook's folder and trim stray spaces from names."""
    global BASEFOLDER
    BASEFOLDER = specpath.replace('\\', os.sep) if specpath and os.sep != '\\' else specpath
    names = {SPEC_SOURCES: [S_NAME, S_TYPE], SPEC_SUBJECTS: [SUB_NAME, SUB_LOB, SUB_REGIONS],
             SPEC_INURING: [L_LAYER, L_SUBJ], SPEC_UPPER: [L_LAYER, L_SUBJ, L_BASIS, L_DIST]}
    for spec, columns in names.items():
        if spec in specdict:
            present = [c for c in columns if c in specdict[spec].columns and specdict[spec].schema[c] == pl.Utf8]
            if present:
                specdict[spec] = specdict[spec].with_columns(pl.col(present).str.strip_chars())


# ---------------------------------------------------------------------------------------------
# Checks before any data is read
# ---------------------------------------------------------------------------------------------
def _check_choice(problems, where, value, choices, required=True):
    if _blank(value):
        if required:
            problems.append(f"{where} is blank (choose one of {', '.join(choices)})")
        return
    if str(value).strip().lower() not in [c.lower() for c in choices]:
        problems.append(f"{where}: '{value}' is not one of {', '.join(choices)}")


def _check_bool(problems, where, value):
    try:
        _bool(value)
    except ValueError:
        problems.append(f"{where}: '{value}' is not TRUE or FALSE")


def _check_number(problems, where, value, lo=None, hi=None, required=False, lo_open=False, integer=False):
    if _blank(value):
        if required:
            problems.append(f"{where} is blank")
        return None
    try:
        v = _number(value)
    except (TypeError, ValueError):
        problems.append(f"{where}: '{value}' is not a number")
        return None
    if integer and not float(v).is_integer():
        problems.append(f"{where}: {value} is not a whole number")
    if lo is not None and (v <= lo if lo_open else v < lo):
        problems.append(f"{where}: {value} must be {'above' if lo_open else 'at least'} {lo:g}")
    if hi is not None and v > hi:
        problems.append(f"{where}: {value} must be at most {hi:g}")
    return v


def _subject_regions_used(subjects, layer_rows):
    """Subjects named by included layers that list regions (these need exposure data)."""
    used = {r.get(L_SUBJ) for r in layer_rows if _safe_included(r)}
    return sorted({r[SUB_NAME] for r in subjects if r.get(SUB_NAME) in used and _regions(r.get(SUB_REGIONS))})


def checkSpecs(specdict):
    """Problems that would stop the run, as a list of messages. Problems that depend on the data
    (column names, LOB names) are found when the data is read."""
    problems = []
    g = _settings(specdict.get(SPEC_GENERAL))
    run_type = _text(g.get(G_RUNTYPE), 'Allocate')
    _check_choice(problems, f"General, {G_RUNTYPE}", run_type, RUN_TYPES)
    if _blank(g.get(G_ELTFOLDER)):
        problems.append(f"General, {G_ELTFOLDER} is blank")

    if run_type == 'Prepare Data':
        pr = _settings(specdict.get(SPEC_PREPARE))
        kind = _text(pr.get(P_SOURCETYPE), 'Parquet Files')
        _check_choice(problems, f"Prepare Data, {P_SOURCETYPE}", kind, PREP_SOURCETYPES)
        if kind == 'Parquet Files':
            if _blank(pr.get(P_FOLDER)):
                problems.append(f"Prepare Data, {P_FOLDER} is blank")
            else:
                folder = _path(pr.get(P_FOLDER))
                files = glob.glob(os.path.join(folder, _text(pr.get(P_PATTERN), '*.parquet')))
                if not os.path.isdir(folder):
                    problems.append(f"Prepare Data, {P_FOLDER}: folder not found: {folder}")
                elif not files:
                    problems.append(f"Prepare Data: no files matching {_text(pr.get(P_PATTERN), '*.parquet')} in {folder}")
                elif not _blank(g.get(G_ELTFOLDER)) and os.path.normpath(folder) == os.path.normpath(_path(g.get(G_ELTFOLDER))):
                    problems.append(f"Prepare Data: {P_FOLDER} and General {G_ELTFOLDER} must be different folders")
        elif kind == 'Database Query':
            for k in (P_CONNECTION, P_QUERY):
                if _blank(pr.get(k)):
                    problems.append(f"Prepare Data, {k} is required for a Database Query")
        _check_number(problems, f"Prepare Data, {P_TARGET}", pr.get(P_TARGET), lo=1000, integer=True)
        try:
            parse_renames(pr.get(P_RENAMES))
        except ValueError as e:
            problems.append(f"Prepare Data, {e}")
        _check_bool(problems, f"Prepare Data, {P_KEEPRAW}", pr.get(P_KEEPRAW))
        return problems

    # ----- Allocate -----
    for key, choices in [(G_DISTORTION, DISTORTIONS), (G_BASIS, BASES), (G_SHARING, SHARINGS), (G_SPLIT, SPLITS)]:
        _check_choice(problems, f"General, {key}", g.get(key), choices, required=False)
    for key in (G_ELTNET, G_BOUNDED, G_CLAMP, G_RP, G_BENCHMARK):
        _check_bool(problems, f"General, {key}", g.get(key))
    _check_number(problems, f"General, {G_GRID}", g.get(G_GRID), lo=50, hi=20000, integer=True)
    if _blank(g.get(G_OUTPUT)):
        problems.append(f"General, {G_OUTPUT} is blank")
    if not _blank(g.get(G_ELTFOLDER)):
        folder = _path(g.get(G_ELTFOLDER))
        pattern = _text(g.get(G_ELTPATTERN), 'ev_bucket_*.parquet')
        if not os.path.isdir(folder):
            problems.append(f"General, {G_ELTFOLDER}: folder not found: {folder} (run Prepare Data first?)")
        elif not glob.glob(os.path.join(folder, pattern)):
            problems.append(f"General: no files matching {pattern} in {folder} (run Prepare Data first?)")

    net_elt = _safe_bool(g.get(G_ELTNET), False)

    # Data Sources
    sources = _rows(specdict.get(SPEC_SOURCES))
    named = {}
    for r in sources:
        name = r.get(S_NAME)
        where = f"Data Sources, {name}"
        if name not in DATASETS:
            problems.append(f"Data Sources: '{name}' is not one of {', '.join(DATASETS)}")
            continue
        named[name] = r
        kind = r.get(S_TYPE)
        if kind not in SOURCETYPES:
            problems.append(f"{where}: unknown Source Type '{kind}'")
        elif kind in ('Parquet', 'CSV', 'Excel Table'):
            if _blank(r.get(S_PATH)):
                problems.append(f"{where}: File Path is required for {kind}")
            else:
                p = _path(r.get(S_PATH))
                if not (os.path.exists(p) or glob.glob(p)):
                    problems.append(f"{where}: file not found: {p}")
            if kind == 'Excel Table' and _blank(r.get(S_TABLE)):
                problems.append(f"{where}: Table Name is required for Excel Table")
        elif kind == 'Database Query' and (_blank(r.get(S_CONN)) or _blank(r.get(S_QUERY))):
            problems.append(f"{where}: Connection and Query are required for Database Query")
        try:
            parse_renames(r.get(S_RENAMES))
        except ValueError as e:
            problems.append(f"{where}: {e}")
    needed = ['BU ELT Net', 'Policy LOB'] + ([] if net_elt else ['BU ELT Gross'])
    for d in needed:
        if d not in named:
            problems.append(f"Data Sources: no row for {d}"
                            + (" (or set Policy ELT Is Net Of Per-Risk to TRUE)" if d == 'BU ELT Gross' else ""))

    # Subjects
    subjects = _rows(specdict.get(SPEC_SUBJECTS))
    subject_names = {r[SUB_NAME] for r in subjects}

    # Layers
    inuring, upper = _rows(specdict.get(SPEC_INURING)), _rows(specdict.get(SPEC_UPPER))
    for table, rows in ((SPEC_INURING, inuring), (SPEC_UPPER, upper)):
        for r in rows:
            where = f"{table}, {r.get(L_LAYER)}"
            try:
                inc = _included(r)
            except ValueError:
                problems.append(f"{where}: Include '{r.get(L_INCLUDE)}' is not TRUE or FALSE")
                continue
            if not inc:
                continue
            if r.get(L_SUBJ) not in subject_names:
                problems.append(f"{where}: Subject '{r.get(L_SUBJ)}' is not on the Subjects sheet")
            _check_number(problems, f"{where}, {L_RET}", r.get(L_RET), lo=0, required=True)
            _check_number(problems, f"{where}, {L_LIM}", r.get(L_LIM), lo=0, lo_open=True, required=True)
            _check_number(problems, f"{where}, {L_PLACE}", r.get(L_PLACE), lo=0, hi=1, lo_open=True)
            _check_bool(problems, f"{where}, {L_MULTI}", r.get(L_MULTI))
            if table == SPEC_UPPER:
                _check_number(problems, f"{where}, {L_PREM}", r.get(L_PREM), lo=0, lo_open=True, required=True)
                _check_choice(problems, f"{where}, {L_BASIS}", r.get(L_BASIS), BASES, required=False)
                _check_choice(problems, f"{where}, {L_DIST}", r.get(L_DIST), DISTORTIONS, required=False)
                _check_number(problems, f"{where}, {L_REINST}", r.get(L_REINST), lo=0, integer=True)
                _check_number(problems, f"{where}, {L_RRATE}", r.get(L_RRATE), lo=0)
                _check_number(problems, f"{where}, {L_AGGLIM}", r.get(L_AGGLIM), lo=0, lo_open=True)
                _check_number(problems, f"{where}, {L_AGGDED}", r.get(L_AGGDED), lo=0)
                _check_number(problems, f"{where}, {L_THETA}", r.get(L_THETA))
    if not any(_safe_included(r) for r in upper if not _blank(r.get(L_LAYER))):
        problems.append("Upper Layers: no layers to allocate")
    names = [x[L_LAYER] for x in inuring + upper if _safe_included(x)]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        problems.append(f"Layer names used more than once: {', '.join(dupes)}")

    regional = _subject_regions_used(subjects, inuring + upper)
    if regional and 'Exposure' not in named:
        problems.append(f"Data Sources: no row for Exposure, which is needed because subject(s) "
                        f"{', '.join(regional)} list regions")
    return problems


def createPreppedSpecs(specdict):
    """Check the tables, and record the settings, run name and base folder."""
    specdict['settings'] = _settings(specdict.get(SPEC_GENERAL))
    specdict['prepare'] = _settings(specdict.get(SPEC_PREPARE))
    specdict['analysisname'] = _text(specdict['settings'].get(G_RUNNAME), 'Cat cost allocation')
    specdict['basefolder'] = BASEFOLDER or os.getcwd()
    problems = checkSpecs(specdict)
    if problems:
        for p in problems:
            MYLOGGER.log(DATA, p)
        specdict['error'] = (f"{len(problems)} problem(s) in the input tables:\n  " + "\n  ".join(problems[:15])
                             + ("\n  ..." if len(problems) > 15 else ""))


# ---------------------------------------------------------------------------------------------
# Reading the data
# ---------------------------------------------------------------------------------------------
def _standardise(df, dataset, renames):
    """Apply renames, match the needed columns without regard to case, build REGION from COUNTRY and
    STATE if needed, trim LOB and region text. Raises ValueError listing missing columns."""
    renames = {k.strip(): v.strip() for k, v in (renames or {}).items()}
    wanted = NEEDED[dataset] + OPTIONAL + (['LOBNAME'] if dataset == 'Exposure' else [])
    mapping = {}
    for c in df.columns:
        target = renames.get(c)
        if target is None and c.strip().upper() in wanted:
            target = c.strip().upper()
        if target is not None and target.upper() in wanted and target.upper() not in mapping.values():
            mapping[c] = target.upper()
    df = df.select([pl.col(k).alias(v) for k, v in mapping.items()])
    if 'REGION' in NEEDED[dataset] and 'REGION' not in df.columns and {'COUNTRY', 'STATE'} <= set(df.columns):
        df = df.with_columns(pl.concat_str([pl.col('COUNTRY').cast(pl.Utf8).str.strip_chars(),
                                            pl.col('STATE').cast(pl.Utf8).str.strip_chars()], separator='_')
                             .alias('REGION'))
    missing = [c for c in NEEDED[dataset] if c not in df.columns]
    if missing:
        raise ValueError(f"{dataset}: missing column(s) {', '.join(missing)} (has {', '.join(df.columns)}). "
                         f"Add Column Renames on the Data Sources sheet if they have other names"
                         + ("; REGION can also come from COUNTRY and STATE" if 'REGION' in missing else ""))
    for c in ('LOBNAME', 'REGION'):
        if c in df.columns:
            df = df.with_columns(pl.col(c).cast(pl.Utf8).str.strip_chars())
    return df.select([c for c in df.columns if c in NEEDED[dataset] + (['LOBNAME'] if dataset == 'Exposure' else [])])


def readDataset(row):
    """One Data Sources row -> polars DataFrame with the dataset's standard columns."""
    name, kind = row[S_NAME], row[S_TYPE]
    renames = parse_renames(row.get(S_RENAMES))
    if kind == 'Parquet':
        p = _path(row[S_PATH])
        files = sorted(glob.glob(os.path.join(p, '*.parquet'))) if os.path.isdir(p) else sorted(glob.glob(p)) or [p]
        df = pl.read_parquet(files)
    elif kind == 'CSV':
        sep = _text(row.get(S_DELIM), ',')
        sep = {'(tab)': '\t', '(space)': ' '}.get(sep.lower(), sep)
        df = pl.read_csv(_path(row[S_PATH]), separator=sep, infer_schema_length=100_000)
    else:                       # Excel Table, Database Query: the DTX readers
        src = {'Dataset Name': name, 'Source Type': kind, 'File Path': row.get(S_PATH), 'Sheet': row.get(S_SHEET),
               'Table Name': row.get(S_TABLE), 'Connection': row.get(S_CONN), 'Query': row.get(S_QUERY)}
        df = pl.from_pandas(readSource(src, BASEFOLDER))
    return _standardise(df, name, renames)


def _match_policy_ids(frames, elt_dtype):
    """POLICYID in the exposure and LOB tables as the same type as in the policy ELT files."""
    target = pl.Int64 if elt_dtype.is_integer() else pl.Utf8
    out = {}
    for k, df in frames.items():
        if df is None or 'POLICYID' not in df.columns:
            out[k] = df
            continue
        col = pl.col('POLICYID')
        if target == pl.Utf8:
            df = df.with_columns(col.cast(pl.Utf8).str.strip_chars())
        else:
            try:
                df = df.with_columns(col.cast(pl.Utf8).str.strip_chars().str.replace(r'\.0$', '').cast(pl.Int64))
            except Exception as e:
                raise ValueError(f"{k}: POLICYID values aren't whole numbers, but the policy ELT's are ({e})")
        out[k] = df
    return out


# ---------------------------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------------------------
def _writeResultTables(analysis, tables):
    """Fill the result sheets: in the open workbook (xlwings) or, for a closed-workbook run, by
    writing the table rows into the file (modelkit.excel_tablewriter). Failures are warnings."""
    book, xl = analysis.book, getattr(analysis, 'xlconnectiontype', 0)
    tables = {k: v for k, v in tables.items() if k in RESULT_BY_TABLE}
    for name, df in tables.items():
        cols = [c[0] for c in RESULT_BY_TABLE[name]['columns']]
        tables[name] = df.select([pl.col(c) if c in df.columns else pl.lit(None).alias(c) for c in cols])
    if xl == 1 and book is not None:
        for name, df in tables.items():
            try:
                sheet = RESULT_BY_TABLE[name]['sheet']
                t = book.sheets[sheet].tables[name]
                if df.height:
                    t.update(df.to_pandas(), index=False)
                else:
                    t.data_body_range.clear_contents()
            except Exception as e:
                MYLOGGER.warning(f"Could not write the {name} table: {e}")
        return
    path = analysis.preppedspecs.get('specfile')
    if xl == 2 and path and os.path.exists(path):
        try:
            if hasattr(book, 'close'):
                book.close()                 # release the file before rewriting it
            from modelkit.excel_tablewriter import writeTables, tableParts
            present = tableParts(path)
            rows = {n: [[None if (isinstance(v, float) and math.isnan(v)) else v for v in r] for r in df.rows()]
                    for n, df in tables.items() if n in present}
            writeTables(path, path, rows)
        except Exception as e:
            MYLOGGER.warning(f"Could not write the result tables into {os.path.basename(path)}: {e}")


def _layer_summary(info, by_pl):
    pol = by_pl.group_by('Layer').agg(pl.col('PREMIUM').sum().alias('Allocated'),
                                      pl.col('POLICYID').n_unique().alias('Policies'))
    df = info.join(pol, on='Layer', how='left')
    return df.select(
        pl.col('Layer'), pl.col('basis').alias('Basis'), pl.col('distortion').alias('Distortion'),
        pl.col('theta').cast(pl.Float64).alias('Theta'), pl.col('premium_100pct').alias('Premium 100%'),
        pl.col('placement').alias('Placement'), pl.col('placed_premium').alias('Placed Premium'),
        pl.col('calibration_target_100pct').alias('Calibration Target 100%'),
        pl.col('expected_loss_100pct').alias('Expected Loss 100%'), pl.col('load_ratio').alias('Load'),
        pl.col('P_attach').alias('P(Attach)'), pl.col('P_exhaust').alias('P(Exhaust)'),
        pl.col('agg_limit').cast(pl.Float64).alias('Agg Limit'),
        pl.col('expected_reinstatement_premium_100pct').alias('Exp. Reinstatement Premium 100%'),
        'Allocated', 'Policies', pl.col('events').alias('Events'), pl.col('bounded_rows').alias('Bounded Rows'),
        pl.col('negative_share_before_clamp').alias('Negative Share Before Fix'))


def _lob_summary(by_pl, lob1, info):
    placed = info.select(pl.col('Layer'), pl.col('placed_premium').alias('_P'))
    j = by_pl.join(lob1, on='POLICYID', how='left').with_columns(pl.col('LOBNAME').fill_null('(no LOB)'))
    per = (j.group_by(['LOBNAME', 'Layer']).agg(pl.col('PREMIUM').sum().alias('Premium'),
                                                 pl.col('POLICYID').n_unique().alias('Policies'))
           .join(placed, on='Layer').with_columns((pl.col('Premium') / pl.col('_P')).alias('Share of Layer'))
           .drop('_P'))
    total = (j.group_by('LOBNAME').agg(pl.col('PREMIUM').sum().alias('Premium'),
                                       pl.col('POLICYID').n_unique().alias('Policies'))
             .with_columns(pl.lit('All layers').alias('Layer'),
                           (pl.col('Premium') / placed['_P'].sum()).alias('Share of Layer')))
    order = {n: i for i, n in enumerate(info['Layer'].to_list() + ['All layers'])}
    return (pl.concat([per, total.select(per.columns)])
            .with_columns(pl.col('Layer').replace_strict(order, default=len(order)).alias('_o'))
            .sort(['_o', 'Premium'], descending=[False, True]).drop('_o')
            .select('LOBNAME', 'Layer', 'Premium', 'Share of Layer', 'Policies'))


def _ratio_check(lk, first_file):
    """Policies vs cells on one file: for each layer, the ratio of the policies' net mean in the
    layer's subject to the BU x region net mean, per event. Returns {layer: (p5, median, p95)}."""
    res, _ = collapse_chunk(pl.read_parquet(first_file), lk)
    out = {}
    for k, l in enumerate(lk['layers']):
        pol = res.group_by('EVENTID').agg(pl.col(f'M_{k}').sum().alias('P'))
        cel = subject_event_elt(lk['net_cells'], l['subject']).select(
            pl.col('EVENTID').cast(pl.Int64), pl.col('PERSPVALUE').alias('C'))
        r = (pol.join(cel, on='EVENTID').filter(pl.col('C') > 0)
             .select((pl.col('P') / pl.col('C')).alias('R'))['R'])
        if len(r):
            out[l['name']] = (float(r.quantile(0.05)), float(r.median()), float(r.quantile(0.95)))
    return out


def _benchmark(args, lk, by_pol, lob1, files, gross, status):
    """Expected-loss keys (mean and conditional split) and the gross AAL share, by LOB, scaled to
    the same total as the final allocation."""
    status.update("Benchmark: building expected-loss lookups")
    lk_id = prepare_lookups(**{**args['lookups'], 'distortion': 'identity', 'progress': status.update})
    out = {}
    for name, method in (('EL Key, Mean Split', 'mean'), ('EL Key, Conditional Split', 'conditional')):
        status.update(f"Benchmark: {name}")
        _, bp, _, _ = run_allocation(**{**args['run'], 'lookups': lk_id, 'method': method,
                                        'progress': None, 'verbose': False})
        out[name] = bp.rename({'PREMIUM': name})
    status.update("Benchmark: AAL share")
    rates = gross.select(pl.col('EVENTID').cast(pl.Int64), 'RATE').unique('EVENTID')
    aal = (pl.scan_parquet(files).select(pl.col('EVENTID').cast(pl.Int64), pl.col('POLICYID').cast(pl.Int64),
                                         pl.col('PERSPVALUE').cast(pl.Float64))
           .join(rates.lazy(), on='EVENTID')
           .group_by('POLICYID').agg((pl.col('RATE') * pl.col('PERSPVALUE')).sum().alias('AAL')).collect())
    total = by_pol['PREMIUM'].sum()
    aal = aal.with_columns((pl.col('AAL') / pl.col('AAL').sum() * total).alias('AAL Share')).drop('AAL')
    cmp = aal
    for df in list(out.values()) + [by_pol.rename({'PREMIUM': 'Final'})]:
        cmp = cmp.join(df, on='POLICYID', how='full', coalesce=True)
    cols = ['AAL Share', 'EL Key, Mean Split', 'EL Key, Conditional Split', 'Final']
    return (cmp.with_columns(pl.col(cols).fill_null(0)).join(lob1, on='POLICYID', how='left')
            .with_columns(pl.col('LOBNAME').fill_null('(no LOB)'))
            .group_by('LOBNAME').agg(pl.col(cols).sum())
            .with_columns((pl.col('Final') / pl.col('AAL Share')).alias('Final / AAL Share'))
            .sort('Final', descending=True))


def _diag_rows(items):
    return pl.DataFrame([{'Item': k, 'Value': None if v is None else (f"{v:,.6g}" if isinstance(v, float) else
                                                                      f"{v:,}" if isinstance(v, int) else str(v)),
                          'Note': n} for k, v, n in items], schema={'Item': pl.Utf8, 'Value': pl.Utf8, 'Note': pl.Utf8})


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------
def _runPrepare(analysis, p, status):
    g, pr = p['settings'], p['prepare']
    out = _path(g[G_ELTFOLDER])
    renames = parse_renames(pr.get(P_RENAMES))
    target = int(_number(pr.get(P_TARGET), 3_000_000))
    if _text(pr.get(P_SOURCETYPE), 'Parquet Files') == 'Database Query':
        manifest = prepare_event_buckets(None, out, target, renames, progress=status.update,
                                         query=dict(connection=pr[P_CONNECTION], query=pr[P_QUERY]),
                                         keep_raw=_bool(pr.get(P_KEEPRAW), False))
    else:
        files = sorted(glob.glob(os.path.join(_path(pr[P_FOLDER]), _text(pr.get(P_PATTERN), '*.parquet'))))
        manifest = prepare_event_buckets(files, out, target, renames, progress=status.update)
    analysis.manifest = manifest
    log = manifest.select(pl.col('File').alias('Output'), pl.lit(out).alias('Path'), pl.col('Rows'))
    diag = _diag_rows([('Run type', 'Prepare Data', ''), ('Files written', manifest.height, out),
                       ('Rows', int(manifest['Rows'].sum()), 'Same as read (checked)'),
                       ('Events', int(manifest['Events'].sum()), ''),
                       ('Largest file (rows)', int(manifest['Rows'].max()), 'Whole events, so files vary around the target')])
    _writeResultTables(analysis, {'OutputLog': log, 'Diagnostics': diag})


def _runAllocate(analysis, p, status):
    t0 = time.time()
    g = p['settings']
    flag = lambda k, d: _bool(g.get(k), d)
    folder = _path(g[G_ELTFOLDER])
    pattern = _text(g.get(G_ELTPATTERN), 'ev_bucket_*.parquet')
    files = sorted(glob.glob(os.path.join(folder, pattern)))
    outdir = _path(g[G_OUTPUT])
    os.makedirs(outdir, exist_ok=True)

    # ----- data -----
    data = {}
    for r in _rows(p[SPEC_SOURCES]):
        status.update(f"Reading {r[S_NAME]}")
        data[r[S_NAME]] = readDataset(r)
    net_elt = flag(G_ELTNET, False)
    net = data['BU ELT Net']
    gross = net if net_elt else data['BU ELT Gross']
    elt_id_type = pl.read_parquet_schema(files[0]).get('POLICYID')
    if elt_id_type is None:
        raise ValueError(f"{os.path.basename(files[0])} has no POLICYID column; run Prepare Data to build the files")
    ids = _match_policy_ids({'Policy LOB': data['Policy LOB'], 'Exposure': data.get('Exposure')}, elt_id_type)
    pol_lob, exposure = ids['Policy LOB'], ids['Exposure']
    lob1 = pol_lob.select('POLICYID', 'LOBNAME').unique('POLICYID')

    # data checks that don't stop the run
    def warn(spec, msg):
        MYLOGGER.log(DATA, f"{spec}: {msg}", extra={'spec': spec})
    lobs_elt = set(net['LOBNAME'].unique().to_list())
    extra = sorted(set(lob1['LOBNAME'].unique().to_list()) - lobs_elt)
    if extra:
        warn('Policy LOB', f"LOB(s) not in the BU ELT Net, so their policies get no inuring or per-risk "
                           f"adjustment and fall outside any LOB-specific subject: {', '.join(map(str, extra))}")
    regions_elt = set(net['REGION'].unique().to_list())
    for r in _rows(p[SPEC_SUBJECTS]):
        if not _blank(r.get(SUB_LOB)) and r[SUB_LOB] not in lobs_elt:
            warn('Subjects', f"{r[SUB_NAME]}: LOB '{r[SUB_LOB]}' is not in the BU ELT Net")
        bad = [x for x in _regions(r.get(SUB_REGIONS)) if x not in regions_elt]
        if bad:
            warn('Subjects', f"{r[SUB_NAME]}: region(s) not in the BU ELT Net: {', '.join(bad)}")

    # ----- programme -----
    subj = {}
    for r in _rows(p[SPEC_SUBJECTS]):
        subj.setdefault(r[SUB_NAME], []).append(('' if _blank(r.get(SUB_LOB)) else r[SUB_LOB], _regions(r.get(SUB_REGIONS))))
    subj = {k: make_subject([] if all(l == '' and not regs for l, regs in v) else v) for k, v in subj.items()}

    inur = [r for r in _rows(p[SPEC_INURING]) if _included(r)]
    if inur:
        tbl = pl.DataFrame([{'Stage': _number(r[L_STAGE]), 'Layer': r[L_LAYER], 'Retention': _number(r[L_RET]),
                             'Limit': _number(r[L_LIM]), 'Placement %': _number(r.get(L_PLACE), 1.0),
                             'Subject': r[L_SUBJ], MULTI_COL: _bool(r.get(L_MULTI), True)} for r in inur])
        stages = stages_from_table(tbl, subj)
    else:
        stages = []
    up = [r for r in _rows(p[SPEC_UPPER]) if _included(r)]
    upper = pl.DataFrame([{
        'Layer': r[L_LAYER], 'Retention': _number(r[L_RET]), 'Limit': _number(r[L_LIM]), 'Subject': r[L_SUBJ],
        'Premium': _number(r[L_PREM]), 'Placement %': _number(r.get(L_PLACE), 1.0),
        'Basis': _text(r.get(L_BASIS)), 'Reinstatements': _number(r.get(L_REINST), 0.0),
        'Reinstatement Rate': _number(r.get(L_RRATE), 1.0), 'Agg Limit': _number(r.get(L_AGGLIM)),
        'Agg Deductible': _number(r.get(L_AGGDED), 0.0), 'Distortion': _text(r.get(L_DIST)),
        'Theta': _number(r.get(L_THETA)), MULTI_COL: _bool(r.get(L_MULTI), True)} for r in up],
        schema_overrides={'Basis': pl.Utf8, 'Distortion': pl.Utf8, 'Agg Limit': pl.Float64, 'Theta': pl.Float64})

    # ----- run -----
    lookups_args = dict(exposure=exposure, gross_cells=gross, cells=net, stages=stages, upper_layers=upper,
                        subj=subj, policy_lob=pol_lob,
                        sharing=_text(g.get(G_SHARING), 'conditional').lower(),
                        distortion=_text(g.get(G_DISTORTION), 'wang').lower(),
                        basis=_text(g.get(G_BASIS), 'occurrence').lower(),
                        include_reinstatement_premium=flag(G_RP, True),
                        n_x=int(_number(g.get(G_GRID), 500)), verbose=False, progress=status.update)
    lk = prepare_lookups(**lookups_args)
    run_args = dict(folder=folder, exposure=None, gross_cells=None, cells=None, stages=None, upper_layers=None,
                    subj=subj, pattern=pattern, policy_lob=pol_lob, lookups=lk,
                    method=_text(g.get(G_SPLIT), 'conditional').lower(),
                    clamp_negative=flag(G_CLAMP, False), positive_split=flag(G_BOUNDED, True),
                    verbose=False, progress=status.update)
    by_pl, by_pol, info, diag = run_allocation(**run_args)

    # ----- checks -----
    status.update("Checking results")
    ratios = _ratio_check(lk, files[0])
    for layer, (lo, med, hi) in ratios.items():
        if abs(med - 1) > RATIO_TOLERANCE:
            warn('Results', f"{layer}: policy net means are {med:.1%} of the BU x region net means (median over "
                            f"events in {os.path.basename(files[0])}). The policy ELT and the BU ELTs may not "
                            f"match (per-risk applied twice? policies missing?)")
    if diag['rows_no_lob']:
        warn('Results', f"{diag['rows_no_lob']:,} policy ELT rows have a POLICYID not in Policy LOB and were dropped")
    if diag['rows_no_exposure']:
        warn('Results', f"{diag['rows_no_exposure']:,} rows of region-sensitive LOBs had no exposure with TIV > 0 "
                        f"and were dropped ({diag['mean_no_exposure'] / max(diag['mean_in'], 1e-300):.2%} of mean loss)")
    if diag['label_policies_multi_region']:
        warn('Results', f"{diag['label_policies_multi_region']:,} policies flagged single-region have exposure in "
                        f"more than one region; each was placed in its largest-TIV region")
    if diag['events_spanning_files']:
        warn('Results', f"{diag['events_spanning_files']:,} events span several policy ELT files, so the run used a "
                        f"disk cache; run Prepare Data to avoid it")
    for r in info.iter_rows(named=True):
        if r['negative_share_before_clamp'] > 1e-9 and not (flag(G_BOUNDED, True) or flag(G_CLAMP, False)):
            warn('Results', f"{r['Layer']}: negative contributions are {r['negative_share_before_clamp']:.2%} of the "
                            f"layer (switch on Bounded Split)")
        if abs(r['policy_ALLOC_total'] / r['calibration_target_100pct'] - 1) > 0.01:
            warn('Results', f"{r['Layer']}: policy contributions total {r['policy_ALLOC_total']:,.0f} against a "
                            f"calibration target of {r['calibration_target_100pct']:,.0f}; some events' policies "
                            f"may be missing from the policy ELT files")

    # ----- outputs -----
    status.update("Writing results")
    layer_sum = _layer_summary(info, by_pl)
    lob_sum = _lob_summary(by_pl, lob1, info)
    bench = (_benchmark({'lookups': lookups_args, 'run': run_args}, lk, by_pol, lob1, files, gross, status)
             if flag(G_BENCHMARK, False) else None)
    items = [('Run type', 'Allocate', ''), ('Run name', p['analysisname'], ''),
             ('Policy ELT files', len(files), f"{folder} ({pattern})"),
             ('Policy ELT rows read', diag['rows_in'], 'Rows for events with weight in some layer'),
             ('Rows with no LOB', diag['rows_no_lob'], 'POLICYID not in Policy LOB; dropped'),
             ('Rows with no exposure', diag['rows_no_exposure'], 'Region-sensitive LOBs with no TIV > 0; dropped'),
             ('Policy-events split across regions', diag['rows_split_multi_region'], ''),
             ('Splits using TIV only', diag['rows_tiv_fallback'], 'Event showed no loss in the policy\'s regions'),
             ('Events spanning files', diag['events_spanning_files'], '0 for event-grouped files (one pass, no disk cache)'),
             ('Policies placed in largest region', diag['label_policies_multi_region'], 'Flagged single-region but with several'),
             ('LOBs split by region', ', '.join(sorted(lk['split_lobs'])) if lk['split_lobs'] is not None else 'all', ''),
             ('LOBs given a region only', ', '.join(sorted(lk['label_lobs'])) if lk['label_lobs'] is not None else 'all others', '')]
    for layer, (lo, med, hi) in ratios.items():
        items.append((f'Policies / cells, {layer}', med, f'Median over events in the first file (5th-95th pct {lo:.3f}-{hi:.3f}); should be close to 1'))
    items += [('Total allocated', float(by_pol['PREMIUM'].sum()), 'Sum of placed premiums'),
              ('Policies allocated', by_pol.height, ''), ('Run time (s)', round(time.time() - t0, 1), '')]
    diag_tbl = _diag_rows(items)

    written = []
    def save(df, name, csv=True, parquet=True):
        for ext, ok in (('parquet', parquet), ('csv', csv)):
            if ok:
                path = os.path.join(outdir, f"{name}.{ext}")
                (df.write_parquet if ext == 'parquet' else df.write_csv)(path)
                written.append({'Output': f"{name}.{ext}", 'Path': path, 'Rows': df.height})
    save(by_pl, 'premium_by_policy_layer')
    save(by_pol, 'premium_by_policy')
    save(layer_sum, 'layer_summary', parquet=False)
    save(lob_sum, 'lob_summary', parquet=False)
    save(diag_tbl, 'diagnostics', parquet=False)
    if bench is not None:
        save(bench, 'benchmark', parquet=False)
    log = pl.DataFrame(written)

    analysis.results = dict(by_policy_layer=by_pl, by_policy=by_pol, layer_info=info, diagnostics=diag,
                            layer_summary=layer_sum, lob_summary=lob_sum, benchmark=bench, lookups=lk,
                            ratio_check=ratios)
    tables = {'LayerSummary': layer_sum, 'LOBSummary': lob_sum, 'Diagnostics': diag_tbl, 'OutputLog': log}
    tables['Benchmark'] = bench if bench is not None else pl.DataFrame()
    _writeResultTables(analysis, tables)
    MYLOGGER.info(f"Allocated {by_pol['PREMIUM'].sum():,.2f} to {by_pol.height:,} policies")


def modelSpecificAnalysisSteps(analysis, *args):
    """Run Prepare Data or Allocate. On failure sets analysis.error, which main.py reports (the
    platform does not otherwise surface errors from this hook)."""
    p = analysis.preppedspecs
    status = getCurrentStatus()
    run_type = _text(p['settings'].get(G_RUNTYPE), 'Allocate')
    try:
        if run_type == 'Prepare Data':
            _runPrepare(analysis, p, status)
        else:
            _runAllocate(analysis, p, status)
    except Exception as e:
        MYLOGGER.exception(f'{run_type} run failed')
        analysis.error = f"{run_type} failed: {e}"
