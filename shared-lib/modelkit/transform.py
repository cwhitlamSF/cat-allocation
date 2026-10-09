# modelkit/transform.py
"""
Data transformation engine: read data sources, apply transformation steps,
write the resulting datasets.

The step functions (applyTransformationStep, describeTransformation, the filter
conditions, readCsv / readExcelTable) are the ones from _chartClasses, unchanged,
so the browser app and the Excel route share one implementation. They work on
pandas DataFrames.

On top of them this module adds what a batch run needs:
  - reading a source from a row of the Data Sources table (CSV, Excel table,
    Parquet, database query);
  - turning the typed columns of the Transformations / Filter Conditions /
    Aggregations tables into step parameters;
  - Join, Append and Group By steps (added here; the first two read a second dataset);
  - building every output dataset (an output can use another output as its source);
  - checks that report steps referring to missing columns, instead of ignoring them;
  - writing outputs to Parquet, CSV, DuckDB or Excel, as the Outputs table says.

Optional dependencies are imported only when used: sqlalchemy (database
queries), duckdb (DuckDB outputs), an Excel writer (openpyxl or xlsxwriter).
"""

import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET

import pandas as pd
from python_calamine import CalamineWorkbook

from .common import get_logger

MYLOGGER = get_logger('modelkit.transform')


#region File import helpers (CSV and Excel ListObjects). Used by the Import dialog and usable directly.
def _cellRef(ref):
    #'BC12' -> (row 12, col 55), both 1-based
    m = re.match(r'^([A-Za-z]+)(\d+)$', ref)
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


def _coerceColumns(df):
    #Values from calamine arrive as Python objects; turn numeric-looking columns into numbers
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            try:
                out[col] = pd.to_numeric(out[col])
            except (ValueError, TypeError):
                pass
    return out.infer_objects()


def readCsv(source, **readkwargs):
    #source is a path or bytes. Extra keyword arguments go to pandas.read_csv.
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    return pd.read_csv(source, **readkwargs)


def listExcelTables(source):
    #Lists the ListObjects (Excel tables) in an .xlsx/.xlsm workbook without needing Excel.
    #source is a path or bytes. Returns a list of dicts: sheet, table, ref, headerRowCount, totalsRowCount.
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    relid = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
    tables = []
    with zipfile.ZipFile(source) as z:
        names = set(z.namelist())
        workbook = ET.fromstring(z.read('xl/workbook.xml'))
        wbrels = {rel.get('Id'): rel.get('Target') for rel in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        for sheet in workbook.find('m:sheets', ns):
            target = wbrels.get(sheet.get(relid), '')
            sheetpath = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            relspath = os.path.dirname(sheetpath) + '/_rels/' + os.path.basename(sheetpath) + '.rels'
            if relspath not in names:
                continue
            for rel in ET.fromstring(z.read(relspath)):
                if not rel.get('Type', '').endswith('/table'):
                    continue
                target = rel.get('Target', '')
                if target.startswith('/'):        # absolute part name (written by openpyxl and others)
                    tablepath = target.lstrip('/')
                else:
                    tablepath = os.path.normpath(os.path.join(os.path.dirname(sheetpath), target)).replace('\\', '/')
                t = ET.fromstring(z.read(tablepath))
                tables.append({'sheet': sheet.get('name'),
                               'table': t.get('displayName') or t.get('name'),
                               'ref': t.get('ref'),
                               'headerRowCount': int(t.get('headerRowCount', 1)),
                               'totalsRowCount': int(t.get('totalsRowCount', 0))})
    return tables


def readExcelTable(source, table, sheet=None):
    #Reads one ListObject from an .xlsx/.xlsm workbook into a DataFrame. source is a path or bytes.
    #table is the table name (sheet is only needed if two sheets have a table with the same name).
    matches = [t for t in listExcelTables(source) if t['table'] == table and (sheet is None or t['sheet'] == sheet)]
    if not matches:
        raise ValueError(f"Table '{table}' not found in workbook")
    info = matches[0]
    if isinstance(source, (bytes, bytearray)):
        wb = CalamineWorkbook.from_filelike(io.BytesIO(source))
    else:
        wb = CalamineWorkbook.from_path(source)
    rows = wb.get_sheet_by_name(info['sheet']).to_python(skip_empty_area=False)
    start, end = info['ref'].split(':')
    r1, c1 = _cellRef(start)
    r2, c2 = _cellRef(end)
    block = [list(row[c1 - 1:c2]) + [None] * max(0, c2 - len(row)) for row in rows[r1 - 1:r2]]
    block = [row[:c2 - c1 + 1] for row in block]
    if info['totalsRowCount']:
        block = block[:-info['totalsRowCount']]
    if info['headerRowCount']:
        header = [str(h) if h not in (None, '') else f'Column{i + 1}' for i, h in enumerate(block[0])]
        block = block[1:]
    else:
        header = [f'Column{i + 1}' for i in range(c2 - c1 + 1)]
    df = pd.DataFrame(block, columns=header).replace({'': None})
    return _coerceColumns(df)
#endregion


#region Transformation engine. Pure functions so a pipeline can be applied, described and exported without any UI.
TRANSFORMATIONTYPES = ['Rename Column', 'Delete Columns', 'Combine Columns', 'Filter Data', 'Pivot Columns', 'Unpivot Columns',
                       'Join', 'Append', 'Group By']
# Join, Append read a second dataset: applyTransformationStep needs `resolve` (name -> DataFrame) for them
MULTIDATASETTYPES = ['Join', 'Append']
JOINTYPES = ['left', 'inner', 'right', 'full', 'anti']
GROUPBYFUNCTIONS = ['sum', 'mean', 'median', 'min', 'max', 'count', 'count rows', 'count distinct',
                    'first', 'last', 'std']

TEXTFILTEROPERATORS = ['equals', 'does not equal', 'contains', 'does not contain', 'starts with', 'ends with',
                       'is in list', 'is blank', 'is not blank']
NUMBERFILTEROPERATORS = ['=', '!=', '<', '<=', '>', '>=', 'between', 'is in list', 'is blank', 'is not blank']
NOVALUEFILTEROPERATORS = ['is blank', 'is not blank']
LISTFILTEROPERATORS = ['is in list']
PIVOTAGGREGATES = ['first', 'sum', 'mean', 'min', 'max', 'count']


def isNumericColumn(df, column):
    return (column in df.columns and pd.api.types.is_numeric_dtype(df[column])
            and not pd.api.types.is_bool_dtype(df[column]))


def filterOperatorsForColumn(df, column):
    #Numeric columns get comparison operators, everything else gets the text operators
    return list(NUMBERFILTEROPERATORS) if isNumericColumn(df, column) else list(TEXTFILTEROPERATORS)


def _splitValues(value):
    if isinstance(value, (list, tuple)):
        return list(value)
    return [v.strip() for v in str(value or '').split(',') if v.strip() != '']


def _conditionMask(df, condition):
    #Boolean mask for one filter condition
    column = condition.get('column')
    operator = condition.get('operator')
    value = condition.get('value')
    if column not in df.columns:
        raise ValueError(f"Filter column '{column}' is not in the data")
    series = df[column]
    blank = series.isna() | (series.astype('string').fillna('').str.strip() == '')

    if operator == 'is blank':
        return blank
    if operator == 'is not blank':
        return ~blank
    if operator == 'is in list':
        values = _splitValues(value)
        if isNumericColumn(df, column):
            values = [v for v in (pd.to_numeric(x, errors='coerce') for x in values) if not pd.isna(v)]
        return series.isin(values)
    if operator == 'between':
        parts = _splitValues(value)
        if len(parts) < 2:
            raise ValueError("'between' needs two values, for example: 10, 20")
        return pd.to_numeric(series, errors='coerce').between(pd.to_numeric(parts[0]), pd.to_numeric(parts[1]))
    if operator in ('=', '!=', '<', '<=', '>', '>='):
        numbers = pd.to_numeric(series, errors='coerce')
        threshold = pd.to_numeric(_splitValues(value)[0] if _splitValues(value) else value)
        if operator == '=':
            return numbers == threshold
        if operator == '!=':
            return numbers != threshold
        if operator == '<':
            return numbers < threshold
        if operator == '<=':
            return numbers <= threshold
        if operator == '>':
            return numbers > threshold
        return numbers >= threshold

    text = series.astype('string').fillna('')
    target = '' if value is None else str(value)
    if operator == 'equals':
        return text == target
    if operator == 'does not equal':
        return text != target
    if operator == 'contains':
        return text.str.contains(target, case=False, regex=False, na=False)
    if operator == 'does not contain':
        return ~text.str.contains(target, case=False, regex=False, na=False)
    if operator == 'starts with':
        return text.str.lower().str.startswith(target.lower(), na=False)
    if operator == 'ends with':
        return text.str.lower().str.endswith(target.lower(), na=False)
    raise ValueError(f"Unknown filter operator '{operator}'")


def applyFilterConditions(df, conditions):
    #Applies a list of conditions. Each condition after the first carries its own AND/OR joiner and, as in SQL,
    #AND binds tighter than OR: the conditions form OR-groups of AND-conditions.
    if not conditions:
        return df
    orgroups = []
    current = None
    for index, condition in enumerate(conditions):
        mask = _conditionMask(df, condition)
        if index == 0 or str(condition.get('joiner', 'AND')).upper() == 'AND':
            current = mask if current is None else (current & mask)
        else:
            orgroups.append(current)
            current = mask
    orgroups.append(current)
    combined = orgroups[0]
    for mask in orgroups[1:]:
        combined = combined | mask
    return df[combined]


def applyTransformationStep(df, transformationtype, parameters, resolve=None):
    #Applies one transformation to a DataFrame and returns the result. Columns that are named but no longer
    #present are ignored, so an earlier edit upstream cannot break the whole pipeline.
    #resolve(name) -> DataFrame supplies the other dataset(s) for Join and Append.
    p = parameters or {}
    if transformationtype in MULTIDATASETTYPES and resolve is None:
        raise ValueError(f"{transformationtype} needs a way to look up other datasets (resolve)")
    if transformationtype == 'Join':
        return joinDatasets(df, resolve(p.get('otherDataset')), p)
    if transformationtype == 'Append':
        return appendDatasets(df, [(name, resolve(name)) for name in p.get('otherDatasets', [])], p)
    if transformationtype == 'Group By':
        return groupBy(df, p.get('columns', []), p.get('aggregations', []))
    if transformationtype == 'Rename Column':
        column = p.get('column')
        newname = (p.get('newName') or '').strip()
        if column in df.columns and newname:
            return df.rename(columns={column: newname})
        return df

    if transformationtype == 'Delete Columns':
        return df.drop(columns=[c for c in p.get('columns', []) if c in df.columns])

    if transformationtype == 'Combine Columns':
        columns = [c for c in p.get('columns', []) if c in df.columns]
        if not columns:
            return df
        delimiter = p.get('delimiter') or ''
        ignorenulls = bool(p.get('ignoreNulls'))
        newcolumn = (p.get('newColumn') or 'Combined').strip()

        def combine(values):
            parts = ['' if pd.isna(v) else str(v) for v in values]
            if ignorenulls:
                parts = [part for part in parts if part != '']
            return delimiter.join(parts)

        combined = df[columns].apply(combine, axis=1)
        if p.get('removeSource'):
            df = df.drop(columns=columns)
        df = df.copy()
        df[newcolumn] = combined
        return df

    if transformationtype == 'Filter Data':
        return applyFilterConditions(df, p.get('conditions', []))

    if transformationtype == 'Unpivot Columns':
        columns = [c for c in p.get('columns', []) if c in df.columns]
        if not columns:
            return df
        return df.melt(id_vars=[c for c in df.columns if c not in columns],
                       value_vars=columns,
                       var_name=(p.get('namesColumn') or 'Attribute'),
                       value_name=(p.get('valuesColumn') or 'Value'))

    if transformationtype == 'Pivot Columns':
        namescolumn = p.get('namesColumn')
        valuescolumn = p.get('valuesColumn')
        if namescolumn not in df.columns or valuescolumn not in df.columns:
            return df
        index = [c for c in p.get('indexColumns', []) if c in df.columns and c not in (namescolumn, valuescolumn)]
        if not index:
            index = [c for c in df.columns if c not in (namescolumn, valuescolumn)]
        if not index:
            raise ValueError('Pivot needs at least one column to keep as the row index')
        pivoted = df.pivot_table(index=index, columns=namescolumn, values=valuescolumn,
                                 aggfunc=(p.get('aggregate') or 'first'))
        pivoted.columns = [str(c) for c in pivoted.columns]
        return pivoted.reset_index()

    raise ValueError(f"Unknown transformation type '{transformationtype}'")


def _keyText(series):
    """Join keys as text, with whole numbers written without '.0' (so 2023 matches '2023')."""
    def one(v):
        if pd.isna(v):
            return None
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v).strip()
    return series.map(one).astype('object')


def _joinKeys(p):
    """(keys in this dataset, keys in the other dataset). Other Columns defaults to the same names."""
    keys = list(p.get('columns', []))
    return keys, list(p.get('otherColumns') or []) or list(keys)


def joinDatasets(df, other, p):
    """Join `other` onto df. p: columns (keys in df), otherColumns (keys in other; default the same names),
    joinType (left, inner, right, full, anti), otherDataset (used to label clashing column names).
    Key columns of different types (e.g. numbers in one, text in the other) are compared as text."""
    keys, otherkeys = _joinKeys(p)
    # Keys pair up by position, so a missing key can't just be dropped: that would pair the
    # remaining keys with the wrong columns. Any problem with the keys skips the join.
    if (not keys or len(otherkeys) != len(keys) or any(k not in df.columns for k in keys)
            or any(k not in other.columns for k in otherkeys)):
        return df
    how = (p.get('joinType') or 'left').lower()
    label = p.get('otherDataset') or 'other'
    # Match on temporary copies of the keys (as text where the two sides' types differ), so the
    # output keeps its original columns and types.
    left, right = df.copy(), other.copy()
    joinkeys = [f'__dtxjoin{i}' for i in range(len(keys))]
    for jk, k, ok in zip(joinkeys, keys, otherkeys):
        same = left[k].dtype == right[ok].dtype
        left[jk] = left[k] if same else _keyText(left[k])
        right[jk] = right[ok] if same else _keyText(right[ok])

    if how == 'anti':
        matched = left[joinkeys].merge(right[joinkeys].drop_duplicates(), on=joinkeys,
                                       how='left', indicator=True)['_merge'].eq('both').to_numpy()
        return df[~matched]

    # The other dataset's key columns are kept aside and only used to fill keys for rows that
    # exist only in the other dataset (right and full joins)
    otherkeycols = [f'__dtxother{i}' for i in range(len(keys))]
    right = right.rename(columns=dict(zip(otherkeys, otherkeycols)))
    result = left.merge(right, on=joinkeys, how={'full': 'outer'}.get(how, how), suffixes=('', f' ({label})'))
    for k, ok in zip(keys, otherkeycols):
        result[k] = result[k].where(result[k].notna(), result[ok])
    return result.drop(columns=joinkeys + otherkeycols).reset_index(drop=True)


def appendDatasets(df, others, p):
    """Stack datasets: columns matched by name, missing columns left blank. p: sourceColumn (optional
    name of a column recording which dataset each row came from), sourceLabel (label for df's rows)."""
    column = p.get('sourceColumn')
    frames = [(p.get('sourceLabel') or 'current', df)] + list(others)
    parts = []
    for name, frame in frames:
        frame = frame.copy()
        if column:
            frame[column] = name
        parts.append(frame)
    return pd.concat(parts, ignore_index=True, sort=False)


def _sumOrBlank(series):
    # SQL-style: the sum of a group with no values is blank, not 0
    return series.sum(min_count=1)


_GROUPFUNCS = {'sum': _sumOrBlank, 'mean': 'mean', 'median': 'median', 'min': 'min', 'max': 'max', 'count': 'count',
               'count distinct': 'nunique', 'first': 'first', 'last': 'last', 'std': 'std'}


def groupBy(df, columns, aggregations):
    """One row per combination of `columns` (rows with blank keys form their own group). aggregations:
    list of {column, function, newName}; function from GROUPBYFUNCTIONS; 'count rows' needs no column.
    With no group columns the whole table is summarised in one row."""
    keys = [c for c in columns if c in df.columns]
    named = {}
    for a in aggregations:
        func, col = a.get('function'), a.get('column')
        if func == 'count rows':
            named[a.get('newName') or 'Rows'] = ('__rows', 'size')
        elif col in df.columns and func in _GROUPFUNCS:
            named[a.get('newName') or f'{col} ({func})'] = (col, _GROUPFUNCS[func])
    if not named:
        return df[keys].drop_duplicates().reset_index(drop=True) if keys else df
    data = df.assign(__rows=1)
    if keys:
        return data.groupby(keys, dropna=False, sort=True).agg(**named).reset_index()
    return pd.DataFrame({new: [data[col].agg(func)] for new, (col, func) in named.items()})


def describeFilterConditions(conditions):
    parts = []
    for index, condition in enumerate(conditions or []):
        text = f"{condition.get('column')} {condition.get('operator')}"
        if condition.get('operator') not in NOVALUEFILTEROPERATORS:
            value = condition.get('value')
            if isinstance(value, (list, tuple)):
                value = ', '.join(str(v) for v in value)
            text = f"{text} {value}"
        if index > 0:
            text = f"{str(condition.get('joiner', 'AND')).upper()} {text}"
        parts.append(text)
    return ' '.join(parts)


def describeTransformation(transformationtype, parameters):
    #One readable line summarising a transformation, for the record table and the Excel export
    p = parameters or {}
    if transformationtype == 'Rename Column':
        return f"{p.get('column')} renamed to {p.get('newName')}"
    if transformationtype == 'Delete Columns':
        return f"delete {', '.join(p.get('columns', []))}"
    if transformationtype == 'Combine Columns':
        text = f"{' + '.join(p.get('columns', []))} into {p.get('newColumn')} separated by '{p.get('delimiter', '')}'"
        if p.get('ignoreNulls'):
            text += ', blanks skipped'
        if p.get('removeSource'):
            text += ', source columns removed'
        return text
    if transformationtype == 'Filter Data':
        return f"keep rows where {describeFilterConditions(p.get('conditions', []))}"
    if transformationtype == 'Unpivot Columns':
        return (f"unpivot {', '.join(p.get('columns', []))} into {p.get('namesColumn')} / {p.get('valuesColumn')}")
    if transformationtype == 'Pivot Columns':
        index = p.get('indexColumns') or []
        text = f"pivot {p.get('namesColumn')} to columns, values from {p.get('valuesColumn')} ({p.get('aggregate')})"
        if index:
            text += f", rows keyed by {', '.join(index)}"
        return text
    if transformationtype == 'Join':
        keys = p.get('columns', [])
        other = p.get('otherColumns') or keys
        on = ', '.join(k if k == o else f'{k} = {o}' for k, o in zip(keys, other))
        return f"{p.get('joinType', 'left')} join {p.get('otherDataset')} on {on}"
    if transformationtype == 'Append':
        text = f"append {', '.join(p.get('otherDatasets', []))}"
        if p.get('sourceColumn'):
            text += f", source recorded in {p.get('sourceColumn')}"
        return text
    if transformationtype == 'Group By':
        aggs = ', '.join(f"{a.get('newName') or a.get('column')} = {a.get('function')}"
                         + (f"({a.get('column')})" if a.get('column') else '') for a in p.get('aggregations', []))
        by = ', '.join(p.get('columns', []))
        return f"group by {by or '(all rows)'}: {aggs}"
    return ''


def transformationColumnList(transformationtype, parameters):
    #The columns a transformation acts on, for the Columns field of the record
    p = parameters or {}
    if transformationtype == 'Rename Column':
        return [p['column']] if p.get('column') else []
    if transformationtype in ('Delete Columns', 'Combine Columns', 'Unpivot Columns'):
        return list(p.get('columns', []))
    if transformationtype == 'Filter Data':
        return [c.get('column') for c in p.get('conditions', []) if c.get('column')]
    if transformationtype == 'Pivot Columns':
        return [c for c in [p.get('namesColumn'), p.get('valuesColumn')] if c]
    if transformationtype == 'Join':
        return list(p.get('columns', []))
    if transformationtype == 'Group By':
        return list(p.get('columns', [])) + [a.get('column') for a in p.get('aggregations', []) if a.get('column')]
    return []
#endregion


#region Batch runs: typed spec tables -> datasets -> outputs
# ---------------------------------------------------------------------------------------------
# Column names of the input tables (as in the DTX template). Change them here if the template changes.
# ---------------------------------------------------------------------------------------------
SRC_NAME, SRC_TYPE, SRC_PATH, SRC_SHEET, SRC_TABLE = 'Dataset Name', 'Source Type', 'File Path', 'Sheet', 'Table Name'
SRC_DELIMITER, SRC_CONNECTION, SRC_QUERY, SRC_INCLUDE = 'Delimiter', 'Connection', 'Query', 'Include'

STEP_OUTPUT, STEP_NUMBER, STEP_SOURCE, STEP_TYPE = 'Output Dataset', 'Step', 'Source Dataset', 'Transformation Type'
STEP_COLUMNS, STEP_NEWNAME, STEP_DELIMITER = 'Columns', 'New Name', 'Delimiter'
STEP_IGNOREBLANKS, STEP_REMOVESOURCE = 'Ignore Blanks', 'Remove Source Columns'
STEP_NAMESCOL, STEP_VALUESCOL, STEP_INDEXCOLS, STEP_AGGREGATE = 'Names Column', 'Values Column', 'Index Columns', 'Aggregate'
STEP_OTHER, STEP_OTHERCOLS, STEP_JOINTYPE = 'Other Dataset', 'Other Columns', 'Join Type'

AGG_OUTPUT, AGG_STEP, AGG_NUMBER = 'Output Dataset', 'Step', 'Aggregation'
AGG_COLUMN, AGG_FUNCTION, AGG_NEWNAME = 'Column', 'Function', 'New Name'

COND_OUTPUT, COND_STEP, COND_NUMBER = 'Output Dataset', 'Step', 'Condition'
COND_JOINER, COND_COLUMN, COND_OPERATOR, COND_VALUE = 'Joiner', 'Column', 'Operator', 'Value'

OUT_NAME, OUT_DESTINATION, OUT_PATH, OUT_TABLE, OUT_INCLUDE = 'Output Dataset', 'Destination', 'File Path', 'Table Name', 'Include'

SOURCETYPES = ['CSV', 'Excel Table', 'Parquet', 'Database Query']
DESTINATIONS = ['Parquet', 'CSV', 'DuckDB', 'Excel']
JOINERS = ['AND', 'OR']

# A cell with only a space is read as blank, so delimiters that are whitespace are written as tokens
DELIMITER_TOKENS = {'(space)': ' ', '(tab)': '\t', '(none)': '', '(newline)': '\n'}


def _blank(x):
    if x is None:
        return True
    try:
        if pd.isna(x):
            return True
    except (TypeError, ValueError):
        pass
    return str(x).strip() in ('', 'None', 'nan')


def _text(x, default=''):
    return default if _blank(x) else str(x)


def _true(x, default=False):
    if _blank(x):
        return default
    if isinstance(x, bool):
        return x
    return str(x).strip().lower() in ('true', 'yes', 'y', '1', '1.0')


def _list(x):
    """Comma-separated cell -> list of names."""
    return [] if _blank(x) else [v.strip() for v in str(x).split(',') if v.strip()]


def _delimiter(x, default=''):
    if _blank(x):
        return default
    s = str(x)
    return DELIMITER_TOKENS.get(s.strip().lower(), s)


def _records(table):
    """Rows of a pandas or polars table as dicts (empty list for None)."""
    if table is None:
        return []
    if hasattr(table, 'to_dicts'):          # polars
        return table.to_dicts()
    return table.to_dict('records')


def resolvePath(path, base_folder=None):
    """Absolute local path: OneDrive/SharePoint URLs mapped to the synced folder, relative paths taken
    from base_folder (normally the folder of the workbook being run), ~ and %VAR% expanded."""
    from .paths import localPath, isUrl
    p = str(path).strip().strip('"')
    if isUrl(p):
        return localPath(p)
    p = os.path.expandvars(os.path.expanduser(p))
    if not os.path.isabs(p) and base_folder:
        p = os.path.join(base_folder, p)
    return os.path.normpath(p)


def _expandEnv(text):
    """${NAME} -> environment variable NAME, so passwords need not be stored in the workbook."""
    def repl(m):
        name = m.group(1)
        if name not in os.environ:
            raise ValueError(f"Environment variable {name} is not set (used in a connection string)")
        return os.environ[name]
    return re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}', repl, text)


# ----- sources -------------------------------------------------------------------------------
def readSource(row, base_folder=None):
    """Read one row of the Data Sources table into a pandas DataFrame."""
    name, kind = _text(row.get(SRC_NAME)), _text(row.get(SRC_TYPE))
    if kind == 'CSV':
        path = resolvePath(row.get(SRC_PATH), base_folder)
        return readCsv(path, sep=_delimiter(row.get(SRC_DELIMITER), ','))
    if kind == 'Excel Table':
        if _blank(row.get(SRC_TABLE)):
            raise ValueError(f"{name}: Excel Table sources need a Table Name")
        path = resolvePath(row.get(SRC_PATH), base_folder)
        sheet = None if _blank(row.get(SRC_SHEET)) else str(row.get(SRC_SHEET))
        return readExcelTable(path, str(row.get(SRC_TABLE)), sheet)
    if kind == 'Parquet':
        return pd.read_parquet(resolvePath(row.get(SRC_PATH), base_folder))
    if kind == 'Database Query':
        if _blank(row.get(SRC_CONNECTION)) or _blank(row.get(SRC_QUERY)):
            raise ValueError(f"{name}: Database Query sources need a Connection and a Query")
        try:
            import sqlalchemy
        except ImportError as e:
            raise ImportError("Database Query sources need the sqlalchemy package (and the database's driver)") from e
        engine = sqlalchemy.create_engine(_expandEnv(str(row.get(SRC_CONNECTION))))
        try:
            with engine.connect() as connection:
                return pd.read_sql(sqlalchemy.text(str(row.get(SRC_QUERY))), connection)
        finally:
            engine.dispose()
    raise ValueError(f"{name}: unknown Source Type '{kind}' (expected one of {', '.join(SOURCETYPES)})")


# ----- steps ---------------------------------------------------------------------------------
def stepParameters(step, conditions=(), aggregations=()):
    """Parameters for applyTransformationStep from one row of the Transformations table
    (plus, for Filter Data, its rows of the Filter Conditions table, and for Group By, its rows
    of the Aggregations table)."""
    kind = _text(step.get(STEP_TYPE))
    if kind == 'Join':
        return {'columns': _list(step.get(STEP_COLUMNS)),
                'otherDataset': _text(step.get(STEP_OTHER)),
                'otherColumns': _list(step.get(STEP_OTHERCOLS)),
                'joinType': _text(step.get(STEP_JOINTYPE), 'left').lower()}
    if kind == 'Append':
        return {'otherDatasets': _list(step.get(STEP_OTHER)),
                'sourceColumn': _text(step.get(STEP_NEWNAME)) or None}
    if kind == 'Group By':
        ordered = sorted(aggregations, key=lambda a: float(a.get(AGG_NUMBER) or 0))
        return {'columns': _list(step.get(STEP_COLUMNS)),
                'aggregations': [{'column': _text(a.get(AGG_COLUMN)) or None,
                                  'function': _text(a.get(AGG_FUNCTION)).lower(),
                                  'newName': _text(a.get(AGG_NEWNAME)) or None} for a in ordered]}
    if kind == 'Rename Column':
        columns = _list(step.get(STEP_COLUMNS))
        return {'column': columns[0] if columns else None, 'newName': _text(step.get(STEP_NEWNAME))}
    if kind == 'Delete Columns':
        return {'columns': _list(step.get(STEP_COLUMNS))}
    if kind == 'Combine Columns':
        return {'columns': _list(step.get(STEP_COLUMNS)),
                'newColumn': _text(step.get(STEP_NEWNAME), 'Combined'),
                'delimiter': _delimiter(step.get(STEP_DELIMITER), ''),
                'ignoreNulls': _true(step.get(STEP_IGNOREBLANKS)),
                'removeSource': _true(step.get(STEP_REMOVESOURCE))}
    if kind == 'Filter Data':
        ordered = sorted(conditions, key=lambda c: float(c.get(COND_NUMBER) or 0))
        return {'conditions': [{'joiner': _text(c.get(COND_JOINER), 'AND').upper(),
                                'column': _text(c.get(COND_COLUMN)),
                                'operator': _text(c.get(COND_OPERATOR)),
                                'value': None if _blank(c.get(COND_VALUE)) else str(c.get(COND_VALUE))}
                               for c in ordered]}
    if kind == 'Unpivot Columns':
        return {'columns': _list(step.get(STEP_COLUMNS)),
                'namesColumn': _text(step.get(STEP_NAMESCOL), 'Attribute'),
                'valuesColumn': _text(step.get(STEP_VALUESCOL), 'Value')}
    if kind == 'Pivot Columns':
        return {'namesColumn': _text(step.get(STEP_NAMESCOL)),
                'valuesColumn': _text(step.get(STEP_VALUESCOL)),
                'indexColumns': _list(step.get(STEP_INDEXCOLS)),
                'aggregate': _text(step.get(STEP_AGGREGATE), 'first')}
    raise ValueError(f"Unknown Transformation Type '{kind}' (expected one of {', '.join(TRANSFORMATIONTYPES)})")


def _stepKey(output, step):
    return (str(output), float(step))


def stepTable(steps, conditions=None, aggregations=None):
    """Transformations + Filter Conditions -> one row per step with parsed parameters, in the
    'transformation record' shape CreateChart uses (Transformation ID, Source Dataset,
    Transformation Type, Output Dataset, Columns, Details, Parameters as JSON)."""
    conds, aggs = {}, {}
    for c in _records(conditions):
        if not _blank(c.get(COND_OUTPUT)) and not _blank(c.get(COND_STEP)):
            conds.setdefault(_stepKey(c[COND_OUTPUT], c[COND_STEP]), []).append(c)
    for a in _records(aggregations):
        if not _blank(a.get(AGG_OUTPUT)) and not _blank(a.get(AGG_STEP)):
            aggs.setdefault(_stepKey(a[AGG_OUTPUT], a[AGG_STEP]), []).append(a)
    rows = []
    for s in _records(steps):
        if _blank(s.get(STEP_OUTPUT)) or _blank(s.get(STEP_TYPE)):
            continue
        number = s.get(STEP_NUMBER)
        key = _stepKey(s[STEP_OUTPUT], number if not _blank(number) else 0)
        params = stepParameters(s, conds.get(key, []), aggs.get(key, []))
        rows.append({'Transformation ID': 0 if _blank(number) else float(number),
                     'Source Dataset': _text(s.get(STEP_SOURCE)),
                     'Transformation Type': _text(s.get(STEP_TYPE)),
                     'Output Dataset': _text(s.get(STEP_OUTPUT)),
                     'Columns': ', '.join(c for c in transformationColumnList(s.get(STEP_TYPE), params) if c),
                     'Details': describeTransformation(s.get(STEP_TYPE), params),
                     'Parameters': json.dumps(params)})
    return pd.DataFrame(rows, columns=['Transformation ID', 'Source Dataset', 'Transformation Type',
                                       'Output Dataset', 'Columns', 'Details', 'Parameters'])


def _columnsUsed(kind, params):
    """Columns a step needs to find in its input (new names it creates are not included)."""
    if kind == 'Pivot Columns':
        return [c for c in [params.get('namesColumn'), params.get('valuesColumn')] + list(params.get('indexColumns') or []) if c]
    return [c for c in transformationColumnList(kind, params) if c]


class TransformRun:
    """
    Builds datasets from sources and steps.

        run = TransformRun(sources, stepTable(steps, conditions), warn=logger_function)
        df = run.dataset('Claims by Year')

    A dataset is a source, or an Output Dataset built by its steps in Step order. Each step reads
    its Source Dataset; a step whose Source Dataset is blank or the dataset being built continues
    from the previous step's result. Steps that name missing columns are reported through `warn`
    (the engine itself skips them), so a typo shows up in the run's data warnings.
    """

    def __init__(self, sources, steps, warn=None):
        self.sources = dict(sources)
        self.steps = steps
        self.warn = warn or (lambda dataset, msg: MYLOGGER.warning(f"{dataset}: {msg}"))
        self._built = {}

    def outputNames(self):
        return list(dict.fromkeys(self.steps['Output Dataset'])) if len(self.steps) else []

    def datasetNames(self):
        return list(dict.fromkeys(list(self.sources) + self.outputNames()))

    def dataset(self, name, _visiting=None):
        if name in self._built:
            return self._built[name]
        if name in self.sources:
            return self.sources[name]
        pipeline = self.steps[self.steps['Output Dataset'] == name].sort_values('Transformation ID', kind='stable')
        if not len(pipeline):
            raise ValueError(f"'{name}' is neither a data source nor an Output Dataset")
        visiting = set(_visiting or ())
        if name in visiting:
            raise ValueError(f"'{name}' is defined in terms of itself")
        visiting.add(name)

        running = None
        for _, row in pipeline.iterrows():
            source = row['Source Dataset']
            if (not source or source == name) and running is not None:
                base = running
            elif not source:
                raise ValueError(f"{name}, step {row['Transformation ID']:g}: the first step needs a Source Dataset")
            else:
                base = self.dataset(source, visiting)
            params = json.loads(row['Parameters'])
            kind = row['Transformation Type']
            missing = [c for c in _columnsUsed(kind, params) if c not in base.columns]
            if missing and kind != 'Join':                  # _checkJoin reports join keys
                self.warn(name, f"step {row['Transformation ID']:g} ({kind}): column(s) not found, step "
                                f"{'partly ' if len(missing) < len(_columnsUsed(kind, params)) else ''}skipped: "
                                f"{', '.join(missing)}")
            if kind == 'Filter Data':
                # applyFilterConditions raises on a missing column; drop those conditions instead
                params = dict(params, conditions=[c for c in params.get('conditions', []) if c.get('column') in base.columns])
            if kind == 'Join':
                self._checkJoin(name, row['Transformation ID'], base, params, visiting)
            if kind == 'Append':
                params = dict(params, sourceLabel=source or name)
            running = applyTransformationStep(base, kind, params, resolve=lambda n: self.dataset(n, visiting))
        self._built[name] = running
        return running

    def _checkJoin(self, name, stepid, base, params, visiting):
        """Warn when a join will be skipped (keys don't pair up, or a key is missing on either side),
        and about keys that repeat in the other dataset (each repeat duplicates the matching rows)."""
        other = self.dataset(params.get('otherDataset'), visiting)
        keys, otherkeys = _joinKeys(params)
        where = f"step {stepid:g} (Join {params.get('otherDataset')})"
        if not keys:
            self.warn(name, f"{where}: no key columns in Columns; step skipped")
            return
        if len(otherkeys) != len(keys):
            self.warn(name, f"{where}: Other Columns must list one key per column in Columns; step skipped")
            return
        missing = [k for k in keys if k not in base.columns]
        othermissing = [k for k in otherkeys if k not in other.columns]
        if missing or othermissing:
            parts = ([f"not in this dataset: {', '.join(missing)}"] if missing else []) + \
                    ([f"not in {params.get('otherDataset')}: {', '.join(othermissing)}"] if othermissing else [])
            self.warn(name, f"{where}: key column(s) {'; '.join(parts)}; step skipped")
            return
        if params.get('joinType', 'left') != 'anti':
            dupes = int(other.duplicated(subset=otherkeys).sum())
            if dupes:
                self.warn(name, f"{where}: {dupes} repeated key(s) in {params.get('otherDataset')}; "
                                f"matching rows are duplicated, one per match")


# ----- outputs -------------------------------------------------------------------------------
def _safeTableName(name):
    return re.sub(r'\W+', '_', str(name)).strip('_') or 'dataset'


def writeOutputs(datasets, outputs, base_folder=None):
    """
    Write datasets as the Outputs table says. `datasets` is {name: DataFrame} or a callable
    name -> DataFrame. Returns a DataFrame: Output Dataset, Destination, Path, Table, Rows, Columns.

    Parquet / CSV : File Path is a folder (file named after the dataset) or a .parquet / .csv file.
    DuckDB        : File Path is the .duckdb file; Table Name defaults to the dataset name.
    Excel         : File Path is an .xlsx file; Table Name is the sheet (default: the dataset name).
                    All Excel outputs to the same file are written together, replacing that file.
    """
    get = datasets if callable(datasets) else datasets.__getitem__
    written, excel_files = [], {}
    for o in _records(outputs):
        if _blank(o.get(OUT_NAME)) or not _true(o.get(OUT_INCLUDE), default=True):
            continue
        name, dest = str(o[OUT_NAME]), _text(o.get(OUT_DESTINATION), 'Parquet')
        df = get(name)
        target = _text(o.get(OUT_PATH))
        if dest in ('Parquet', 'CSV'):
            ext = '.parquet' if dest == 'Parquet' else '.csv'
            path = resolvePath(target or '.', base_folder)
            if not path.lower().endswith(ext):
                path = os.path.join(path, f"{name}{ext}")
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            if dest == 'Parquet':
                df.to_parquet(path, index=False)
            else:
                df.to_csv(path, index=False)
            written.append((name, dest, path, '', df))
        elif dest == 'DuckDB':
            if not target:
                raise ValueError(f"{name}: DuckDB outputs need a File Path (the .duckdb file)")
            try:
                import duckdb
            except ImportError as e:
                raise ImportError("DuckDB outputs need the duckdb package") from e
            path = resolvePath(target, base_folder)
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            table = _text(o.get(OUT_TABLE)) or _safeTableName(name)
            con = duckdb.connect(path)
            try:
                con.register('_dtx_frame', df)
                con.execute(f'CREATE OR REPLACE TABLE "{table}" AS SELECT * FROM _dtx_frame')
                con.unregister('_dtx_frame')
            finally:
                con.close()
            written.append((name, dest, path, table, df))
        elif dest == 'Excel':
            if not target:
                raise ValueError(f"{name}: Excel outputs need a File Path (the .xlsx file)")
            path = resolvePath(target, base_folder)
            sheet = (_text(o.get(OUT_TABLE)) or name)[:31]
            excel_files.setdefault(path, []).append((name, sheet, df))
        else:
            raise ValueError(f"{name}: unknown Destination '{dest}' (expected one of {', '.join(DESTINATIONS)})")

    for path, items in excel_files.items():
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with pd.ExcelWriter(path) as writer:
            for name, sheet, df in items:
                df.to_excel(writer, sheet_name=sheet, index=False)
                written.append((name, 'Excel', path, sheet, df))

    return pd.DataFrame([{'Output Dataset': n, 'Destination': d, 'Path': p, 'Table': t,
                          'Rows': df.shape[0], 'Columns': df.shape[1]} for n, d, p, t, df in written],
                        columns=['Output Dataset', 'Destination', 'Path', 'Table', 'Rows', 'Columns'])


def readSources(sources, base_folder=None, progress=None):
    """Read every included row of the Sources table. Returns {name: DataFrame}."""
    say = progress or (lambda msg: None)
    frames = {}
    for row in _records(sources):
        if _blank(row.get(SRC_NAME)) or not _true(row.get(SRC_INCLUDE), default=True):
            continue
        say(f"Reading {row[SRC_NAME]}")
        frames[str(row[SRC_NAME])] = readSource(row, base_folder)
    return frames


def outputDatasetNames(outputs):
    """Names of the datasets the included rows of the Outputs table ask for, in order, once each."""
    return list(dict.fromkeys(str(o[OUT_NAME]) for o in _records(outputs)
                              if not _blank(o.get(OUT_NAME)) and _true(o.get(OUT_INCLUDE), default=True)))


def buildDatasets(sources, steps, conditions=None, aggregations=None, base_folder=None, warn=None,
                  progress=None, names=None, frames=None):
    """
    Build datasets in memory; nothing is written.

    sources / steps / conditions / aggregations : the input tables (pandas or polars).
    names    : datasets to build now (default: every Output Dataset in the steps). Others can be
               built later, on demand, with run.dataset(name).
    frames   : sources already read, {name: DataFrame}, e.g. run.sources from an earlier run. When
               given, `sources` is not read again, so a browser app can rebuild after the steps
               change without re-reading files or re-running queries.
    warn(dataset, message), progress(message) : as for runTransformations.

    Returns (TransformRun, {name: DataFrame}). A TransformRun caches what it builds and does not
    notice later edits to the steps, so after an edit make a new one (passing frames=run.sources).
    """
    say = progress or (lambda msg: None)
    if frames is None:
        frames = readSources(sources, base_folder, progress)
    run = TransformRun(frames, stepTable(steps, conditions, aggregations), warn)
    if names is None:
        names = run.outputNames()
    datasets = {}
    for name in names:
        say(f"Building {name}")
        datasets[name] = run.dataset(name)
    return run, datasets


def runTransformations(sources, steps, conditions, outputs, base_folder=None, warn=None, progress=None,
                       aggregations=None):
    """
    Read every included source, build the datasets the Outputs table asks for, write them.

    sources / steps / conditions / outputs / aggregations : the input tables (pandas or polars).
    warn(dataset, message) : called for data problems that don't stop the run.
    progress(message)      : called at each stage (e.g. RunStatus.update).
    Returns (TransformRun, written) where written is the table from writeOutputs.

    Every dataset is built before anything is written, so a failing step leaves no partial outputs.
    """
    run, datasets = buildDatasets(sources, steps, conditions, aggregations, base_folder, warn, progress,
                                  names=outputDatasetNames(outputs))
    (progress or (lambda msg: None))("Writing outputs")
    written = writeOutputs(datasets, outputs, base_folder)
    return run, written
