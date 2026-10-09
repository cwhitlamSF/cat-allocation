# modelkit/specs.py
"""
Turning spec definitions and config sections into tables.

Formerly split across _misc_submodules/validation.py and dataframes.py.
The spec reader (model file / Default Source / empty-table resolution)
will live here too.
"""

import configparser
import pandas as pd
import polars as pl
from typing import Dict, List, Any, Tuple
from .common import (get_logger, DTYPES_CONVERT, CONFIG_KEYS_SUFFIX, CONFIG_KEY_COLUMNS,
                     CONFIG_VALUE_COLUMNS, CONFIG_SOURCE_TABLES, CONFIG_PANEL_FLAGS)
from .lists import list_difference, list_dropped

MYLOGGER = get_logger('modelkit.specs')

DTYPESCONVERT = DTYPES_CONVERT   # old name used by the spec-formatting functions

# Config sections whose values must stay as strings (no True/None/number conversion).
# 'specDataFormats' is the old Python Reference name, kept until old configs are regenerated.
_STRING_ONLY_SECTIONS = {'Spec Data Formats', 'specDataFormats'}


def _splitStoredValues(values, n, conventions=True):
    """
    Split config values written as str(tuple) by config_creator into n fields.

    Parsed with ast.literal_eval, so commas, quotes and backslashes inside a
    field survive. Falls back to the original approach (strip the brackets,
    drop quotes, split on commas) for values that don't parse as an n-tuple.
    Fields are stripped; with conventions=True, ` becomes ' and ; becomes ,
    (the workbook conventions convertDictToTable has always applied).
    """
    import ast
    rows = []
    for v in values:
        fields = None
        if v is not None:
            try:
                parsed = ast.literal_eval(str(v).strip())
                if isinstance(parsed, tuple) and len(parsed) == n:
                    fields = [None if x is None else str(x) for x in parsed]
            except (ValueError, SyntaxError):
                pass
            if fields is None:
                text = str(v).strip()[1:-1].replace("'", "")
                fields = text.split(",")[:n]
                fields += [None] * (n - len(fields))
        else:
            fields = [None] * n
        if conventions:
            fields = [None if f is None else f.replace("`", "'").strip().replace(";", ",") for f in fields]
        else:
            fields = [None if f is None else f.strip() for f in fields]
        rows.append(fields)
    return rows


def validateSpecStructure(spec_dict: Dict, expected_keys: List[str]) -> Tuple[bool, str]:
    """
    Validate that a spec dictionary contains all expected keys.
    
    Parameters
    ----------
    spec_dict : Dict
        Spec dictionary to validate.
    expected_keys : List[str]
        Keys that must be present.
    
    Returns
    -------
    Tuple[bool, str]
        (is_valid, error_message)
    """
    missing = [k for k in expected_keys if k not in spec_dict]
    if missing:
        error_msg = f'Missing spec keys: {", ".join(missing)}'
        MYLOGGER.error(error_msg)
        return False, error_msg
    return True, ""

def recapitalizeConfigDictKey(_configdict: Dict, _key: str) -> Dict:
    """
    Restore original capitalisation to the keys of a config section.

    configparser lowercases option names, so each section `<key>` is paired
    with a `<key> Keys` section (CONFIG_KEYS_SUFFIX) mapping the lowercased name back to the
    original. Entries with no mapping are passed through unchanged.
    """
    try:
        temp = _configdict[_key]

        tempkeys = _configdict[_key + CONFIG_KEYS_SUFFIX]

        result = {}
        tempkeydiff = list_difference(list(temp.keys()), list(tempkeys.keys()))

        for key2, val2 in tempkeys.items():
            result[val2] = temp[key2]

        for key3 in tempkeydiff:
            result[key3] = temp[key3]
    except KeyError:
        # No companion section: fall back to the section as-is (lowercased keys)
        MYLOGGER.debug(f"No '{_key}{CONFIG_KEYS_SUFFIX}' section; keys stay lowercased")
        result = _configdict[_key]
    return result


def convertDictToTable(configdict: Dict, dictname: str, src_dict: Dict) -> pl.DataFrame:
    """
    Convert a Python dictionary into a Polars DataFrame for spec ingestion.
    
    Parameters
    ----------
    configdict : Dict
        Configuration dictionary with Key Columns and Value Columns sections.
    dictname : str
        Name of the dictionary (used to look up config).
    src_dict : Dict
        The dictionary to convert.
    
    Returns
    -------
    pl.DataFrame
        Frame with the composite key split into the key columns and the value
        split into the value columns, per the `Key Columns`/`Value Columns`
        config sections.
    """
    MYLOGGER.debug(f'Converting dict to table: {dictname}')

    mapper = pl.DataFrame([
        {'keys': x, 'values': y} for x, y in src_dict.items()
    ])

    vals = pl.Series(src_dict.keys()).to_frame("keys").join(
        mapper, on="keys", how="left"
    ).to_series(1)

    dictkeycols = recapitalizeConfigDictKey(configdict, CONFIG_KEY_COLUMNS)
    dictvalcols = recapitalizeConfigDictKey(configdict, CONFIG_VALUE_COLUMNS)
    keycollist = [x.strip() for x in dictkeycols[dictname].split(",")]
    valcollist = [x.strip() for x in dictvalcols[dictname].split(",")]

    TblKeys = (pl.Series(src_dict.keys())
        .to_frame("keys")
        .with_columns(pl.col("keys").fill_null('None').cast(pl.Utf8))
        .with_columns(
            [
                pl.col("keys")
                .str.split_exact("|", len(keycollist))
                .struct.rename_fields(keycollist)
                .alias("fields"),
            ]
        ).unnest("fields").drop("keys"))

    TblVals = vals.to_frame("values")

    if len(valcollist) > 1:
        # Values are stored as str(tuple); see _splitStoredValues.
        rows = _splitStoredValues(TblVals.get_column("values").to_list(), len(valcollist))
        TblVals = pl.DataFrame(rows, schema={c: pl.Utf8 for c in valcollist}, orient="row")
    else:
        TblVals = TblVals.rename({"values": valcollist[0]})

    finalvalcollist = list_dropped(valcollist, keycollist)
    if len(finalvalcollist) > 0:
        result = TblKeys.hstack(TblVals.select(finalvalcollist))
    else:
        result = TblKeys

    MYLOGGER.debug('Finished convertDictToTable')
    return result

def coerceToType(value: Any, target_dtype: str) -> Any:
    """
    Coerce a value to a target Polars data type.
    
    Parameters
    ----------
    value : Any
        Value to coerce.
    target_dtype : str
        Target type key from DTYPES_CONVERT.
    
    Returns
    -------
    Any
        Coerced value, or original if coercion fails.
    """
    if target_dtype not in DTYPES_CONVERT:
        return value
    
    polars_type = DTYPES_CONVERT[target_dtype]
    try:
        return pl.Series([value]).cast(polars_type)[0]
    except Exception as e:
        MYLOGGER.warning(f'Coercion failed for {value} to {target_dtype}: {e}')
        return value


def configparser_to_dict(configFilename):

    MYLOGGER.debug(f'Starting configparser_to_dict: {configFilename}')
    try:
        config_dict = {}

        config = configparser.ConfigParser()
        config.read(configFilename)

        for section in config.sections():
            MYLOGGER.debug(section)
            config_dict[section] = {}
            if section not in _STRING_ONLY_SECTIONS:
                for key, value in config.items(section):
                    # Now try to convert back to original types if possible
                    if value in ['True',True]:
                        value = True
                    elif value in ['False',False]:
                        value = False
                    elif value in ['None',None]:
                        value = None
                    elif isinstance(value, str):
                        try:
                            if '.' in value:
                                value = float(value)
                            else:
                                value = int(value)
                        except:
                            pass
                    config_dict[section][key] = value
            else:
                config_dict[section]=dict(config.items(section))

        # Now drop root section if present
        config_dict.pop('root', None)

        #Recapitalize
        tempdict={}
        for key in config_dict.keys():
            if not key.endswith(CONFIG_KEYS_SUFFIX):
                tempdict[key]=recapitalizeConfigDictKey(config_dict,key)

        return tempdict
    except:
        return "Error Creating Config: "+configFilename


def createSpecCleanInfo(fromdict):
    mapper=pl.DataFrame([{'keys':x, 'values':y} for x,y in fromdict.items()])

    vals=pl.Series(fromdict.keys()).to_frame("keys").join(mapper, on="keys",how="left").to_series(1)

    TblKeys=pl.Series(fromdict.keys()).to_frame("keys").with_columns(
        [
            pl.col("keys")
            .str.split_exact("|", 1)
            .struct.rename_fields(["Table", "Column"])
            .alias("fields"),
        ]
    ).unnest("fields").drop("keys")

    rows=_splitStoredValues(vals.to_list(), 3, conventions=False)
    TblVals=pl.DataFrame(rows, schema={c: pl.Utf8 for c in ["Data Type", "Default Value", "Exclude if Null"]}, orient="row")

    return TblKeys.hstack(TblVals)


def readSpecTableRaw(xlbook,connectionType,shtName,tableName,hasHeader):
    """Read a spec table from the model workbook as a pandas DataFrame, unformatted.
    connectionType: 1=xlwings, 2=python-calamine (workbook opened with load_tables=True)."""
    if type(hasHeader)==str:
        if hasHeader=='True':
            hasHeader=True
        else:
            hasHeader=False

    if connectionType==2:
        # calamine addresses tables by name across the whole workbook, so
        # shtName is unused here. to_python() returns the data rows only -
        # the header row is never included and comes from .columns instead.
        tbl = xlbook.get_table_by_name(tableName)
        data_rows = tbl.to_python()
        if hasHeader:
            columnlist = [str(x) for x in tbl.columns]
            result = pd.DataFrame(data_rows, columns=columnlist)
            result[columnlist] = result[columnlist].astype(str)
        else:
            result=pd.DataFrame(data_rows)
            result.rename(columns={0:'Information',1:'Value'}, inplace=True)
    else:
        result = (
            xlbook.sheets[shtName]
            .range(tableName + "[[#All]]")
            .options(pd.DataFrame, index=False,header=hasHeader)
            .value)
        if hasHeader==False:
            result.rename(columns={0:'Information',1:'Value'}, inplace=True)

    return result


def formatSpecTable(result,formatSchema={},defaultValues={},excludeIfNull={},tableName=""):
    """Blanks to null, then exclude-if-null, default values and data types (Spec Data Formats).
    Accepts a pandas or polars DataFrame; returns polars. Failed casts leave the column unchanged."""
    if isinstance(result, pl.DataFrame):
        result = pd.DataFrame(result.to_dict(as_series=False))
    #Try formatting dataframe
    result = result.astype(str)
    result=result.replace({'None':None,'nan':None,'NaT':None,'NaN':None,'':None,' ':None})
    result=result.astype(object).where(pd.notna(result), None)   # pandas 3 keeps blanks as NaN; make them None
    result = pl.DataFrame(result.to_dict(orient="list"))

    actualcols=result.columns
    lowercasecols=[x for x in actualcols]
    result.columns=lowercasecols
    if bool(excludeIfNull)==True:
        MYLOGGER.debug(f"Excluding nulls for {excludeIfNull}")
        for key,val in excludeIfNull.items():
            result=result.filter(pl.col(key).is_not_null())
        MYLOGGER.debug(f"Excluding nulls for {excludeIfNull}")
    if bool(defaultValues)==True:
        MYLOGGER.debug(f"Filling nulls with default values: {defaultValues}")
        for key,val in defaultValues.items():
            result=result.with_columns(pl.col(key).fill_null(val))
    if bool(formatSchema)==True:
        for key,val in formatSchema.items():
            MYLOGGER.debug(f"Formatting {key} to {val}")
            if val=='Boolean':
                try:
                    result=result.with_columns(pl.when(pl.col(key)=='True')
                                           .then(pl.lit(True))
                                           .when(pl.col(key)=='False')
                                           .then(False)
                                           .cast(pl.Boolean)
                                           .alias(key))
                except:
                    MYLOGGER.debug(f"Failed to cast {key} to Boolean")
                    pass
            elif val=='Date':
                try:
                    result=result.with_columns(pl.col(key).str.replace(" 00:00:00", "").cast(DTYPESCONVERT[val]).str.strptime(pl.Date,format=('%Y-%m-%d'), strict=False))
                except:
                    MYLOGGER.debug(f"Failed to cast {key} to Date")
                    pass
            elif val=='Float64RoundToInt':
                try:
                    result=result.with_columns(pl.col(key).cast(DTYPESCONVERT[val]).round(0).cast(pl.Int64))
                except:
                    MYLOGGER.debug(f"Failed to cast {key} to Float64RoundToInt")
                    pass
            elif val=='Utf8Trim.0':
                try:
                    # cast first: an all-blank column arrives as dtype Null, which string operations reject
                    result=result.with_columns(pl.col(key).cast(pl.Utf8).str.replace(r'\.0$',''))
                except:
                    MYLOGGER.debug(f"Failed to trim {key} with Utf8Trim.0")
                    pass
            else:
                try:
                    result=result.with_columns(pl.col(key).cast(DTYPESCONVERT[val]))
                except:
                    MYLOGGER.debug(f"Failed to cast {key} to {DTYPESCONVERT[val]}")
                    pass

    result.columns=actualcols
    MYLOGGER.debug(f"Ending load_spec_table_to_df: {tableName}")
    return result


def load_spec_table_to_df(xlbook,connectionType,shtName, tableName,hasHeader,formatSchema={},defaultValues={},excludeIfNull={}):
    #connectionType: 1=xlwings, 2=python-calamine
    #read in table from excel and convert to dataframe and format columns
    MYLOGGER.debug(f"Starting load_spec_table_to_df: {tableName}")
    result = readSpecTableRaw(xlbook,connectionType,shtName,tableName,hasHeader)
    return formatSpecTable(result,formatSchema,defaultValues,excludeIfNull,tableName)


def applyFormats(df,formatSchema={},defaultValues={},excludeIfNull={}):
#This function is specifically to clean spec tables that were loaded from the config file (rather than from an excel spec file)
    if isinstance(df,pl.DataFrame):
        result=pd.DataFrame(df.to_dicts())
    else:
        result=df
    result = result.astype(str)
    result=result.replace({'None':None,'nan':None,'NaT':None,'NaN':None,'':None,' ':None})
    result=result.astype(object).where(pd.notna(result), None)   # pandas 3 keeps blanks as NaN; make them None
    result = pl.DataFrame(result.to_dict(orient="list"))
    actualcols=result.columns
    lowercasecols=[x for x in actualcols]
    result.columns=lowercasecols

    if bool(excludeIfNull)==True:
        for key,val in excludeIfNull.items():
            result=result.filter(pl.col(key).is_not_null())

    if bool(defaultValues)==True:
        for key,val in defaultValues.items():
            result=result.with_columns(pl.col(key).fill_null(val))

    if bool(formatSchema)==True:
        for key,val in formatSchema.items():
            if val=='Boolean':
                result=result.with_columns(pl.when(pl.col(key)=='True')
                                           .then(pl.lit(True))
                                           .when(pl.col(key)=='False')
                                           .then(False)
                                           .cast(pl.Boolean)
                                           .alias(key))
            elif val=='Date':
                result=result.with_columns(pl.col(key).cast(DTYPESCONVERT[val]).str.strptime(pl.Date,format=('%Y-%m-%d'), strict=False))
            elif val=='Float64RoundToInt':
                result=result.with_columns(pl.col(key).cast(DTYPESCONVERT[val]).round(0).cast(pl.Int64))
            elif val=='Utf8Trim.0':
                try:
                    # cast first: an all-blank column arrives as dtype Null, which string operations reject
                    result=result.with_columns(pl.col(key).cast(pl.Utf8).str.replace(r'\.0$',''))
                except:
                    pass
            else:
                result=result.with_columns(pl.col(key).cast(DTYPESCONVERT[val]))

    result.columns=actualcols
    return result


def assertStringFormats(df,hasHeader,formatSchema):
    #Try formatting dataframe

    if type(hasHeader)==str:
        if hasHeader=='True':
            hasHeader=True
        else:
            hasHeader=False

    if hasHeader==False:
        return df
    else:
        result=df
        try:
            for key,val in formatSchema.items():
                if val in ['Utf8','Utf8Trim.0']:
                    try:
                        result=result.with_columns(pl.col(key).cast(pl.Utf8).alias(key))
                    except:
                        pass
        except:
            pass
    return result


# =============================================================================================
# Spec reader: resolve every model input table from the model file, its Default Source, or an
# empty table, as defined by Tbl_dataInputSpecs ("Model Spec Sources") and
# Tbl_specDataFormats ("Spec Data Formats") in the config.
# =============================================================================================

SPEC_SOURCES_SECTION = 'Model Spec Sources'     # Spec Key of Tbl_dataInputSpecs
SPEC_FORMATS_SECTION = 'Spec Data Formats'      # Spec Key of Tbl_specDataFormats

# Column names in Tbl_dataInputSpecs
COL_SPEC, COL_SHEET, COL_TABLE = 'Information', 'Sheet Name', 'Table Name'
COL_KEYS, COL_DEFAULT = 'Key Column (s)', 'Default Source'
COL_INFO, COL_HEADERS = 'Flag Info Table', 'Table Has Headers'

# Polars types of an empty table, matching what formatSpecTable produces for each Data Type
EMPTY_TABLE_DTYPES = {
    'Boolean': pl.Boolean, 'Utf8': pl.Utf8, 'Utf8Trim.0': pl.Utf8, 'Date': pl.Date,
    'Float64RoundToInt': pl.Int64, 'Float64': pl.Float64, 'Int64': pl.Int64,
}

# Where a spec table came from
SOURCE_MODEL, SOURCE_DEFAULT, SOURCE_EMPTY = 'model file', 'default', 'empty'


def _blank(x) -> bool:
    return x is None or str(x).strip() in ('', 'None', 'nan')


def _true(x) -> bool:
    return str(x).strip().lower() in ('true', 'yes', 'y', '1', '1.0')


def configSpecKeys(config):
    """Spec Keys of every spec section in the config, in Tbl_specDefinition order
    (summary sections such as Key Columns and the ' Keys' companions excluded)."""
    return list(config.get(CONFIG_KEY_COLUMNS, {}).keys())


def panelSpecKeys(config):
    """Spec Keys flagged Panel = True in the config's [Panel Flags] section."""
    flags = config.get(CONFIG_PANEL_FLAGS, {})
    return [k for k, v in flags.items() if v is True or _true(v)]


def configTable(config, section):
    """One config section rebuilt as a table (the in-section 'panel' flag dropped)."""
    return _sectionAsTable(config, section)


def configTables(config):
    """{Spec Key: table} for every spec section in the config."""
    return {k: _sectionAsTable(config, k) for k in configSpecKeys(config) if k in config}


def specSourcesTable(config):
    """Model Spec Sources (Tbl_dataInputSpecs) as a table."""
    return _sectionAsTable(config, SPEC_SOURCES_SECTION)


def specFormatsTable(config):
    """Spec Data Formats as a table: Table, Column, Data Type, Default Value, Exclude if Null."""
    return _specFormats(config)


def _sectionAsTable(config, section):
    """A config section rebuilt as a table (the in-section 'panel' flag dropped)."""
    data = {k: v for k, v in config[section].items() if str(k).lower() != 'panel'}
    return convertDictToTable(config, section, data)


def _specFormats(config):
    """Spec Data Formats as a table: Table, Column, Data Type, Default Value, Exclude if Null."""
    data = {k: v for k, v in config[SPEC_FORMATS_SECTION].items() if str(k).lower() != 'panel'}
    return createSpecCleanInfo(data)


def specFormatRules(formats: pl.DataFrame, spec: str):
    """(formatSchema, defaultValues, excludeIfNull, ordered column list) for one spec."""
    rows = formats.filter(pl.col('Table') == spec).to_dicts()
    schema = {r['Column']: r['Data Type'] for r in rows if not _blank(r['Data Type'])}
    defaults = {r['Column']: r['Default Value'] for r in rows if not _blank(r['Default Value'])}
    exclude = {r['Column']: True for r in rows if _true(r['Exclude if Null'])}
    return schema, defaults, exclude, [r['Column'] for r in rows]


def emptySpecTable(columns, schema) -> pl.DataFrame:
    """No rows; the spec's columns with the types formatting would give them."""
    return pl.DataFrame(schema={c: EMPTY_TABLE_DTYPES.get(schema.get(c), pl.Utf8) for c in columns})


def _restrict(rules, columns):
    return {k: v for k, v in rules.items() if k in columns}


def _resolveDefaultSource(config, name):
    """Spec Key of the config section holding Default Source `name` (an Excel table name or a Spec Key)."""
    if name in config:
        return name
    mapping = config.get(CONFIG_SOURCE_TABLES, {})
    for table, spec_key in mapping.items():
        if str(table).lower() == str(name).lower():
            return spec_key
    return None


def loadModelSpecs(config, xlbook=None, connectionType=2, logger=None):
    """
    Load every spec listed in Model Spec Sources (Tbl_dataInputSpecs).

    For each spec, in order:
      1. the model file's table, if Sheet/Table Name are given and it has data
         (rows that are entirely blank are ignored);
      2. otherwise the Default Source table from the config;
      3. otherwise an empty table with the columns and types in Spec Data Formats.
    Formatting (types, default values, exclude-if-null) comes from Spec Data Formats
    in every case. Key columns are then checked: present, and unique.

    Parameters
    ----------
    config : dict or str
        The config as returned by configparser_to_dict, or the .ini path.
    xlbook :
        The model workbook: an xlwings Book (connectionType=1) or a python-calamine
        CalamineWorkbook opened with load_tables=True (connectionType=2). None = no
        model file, so only defaults and empty tables.
    logger :
        Problems are logged at DATA level (so they reach data_warnings.csv inside
        runContext). Defaults to this module's logger.

    Returns
    -------
    (tables, sources)
        tables  : {spec name: polars DataFrame}
        sources : DataFrame with Spec, Source, Detail, Rows, Problems - which source
                  each table came from, for the run log / run folder.
    """
    from .logs import DATA
    log = logger or MYLOGGER
    if isinstance(config, str):
        config = configparser_to_dict(config)
        if isinstance(config, str):            # configparser_to_dict returns an error string
            raise ValueError(config)

    specs = _sectionAsTable(config, SPEC_SOURCES_SECTION)
    formats = _specFormats(config)
    tables, report = {}, []

    def warn(spec, msg):
        log.log(DATA, f"{spec}: {msg}", extra={'spec': spec})
        problems.append(msg)

    for row in specs.to_dicts():
        spec = row[COL_SPEC]
        problems = []
        is_info = _true(row.get(COL_INFO))
        has_headers = _true(row.get(COL_HEADERS))
        keys = [] if _blank(row.get(COL_KEYS)) else [k.strip() for k in str(row[COL_KEYS]).split(',')]
        if not keys and is_info:
            keys = ['Information']
        schema, defaults, exclude, columns = specFormatRules(formats, spec)

        df, source, detail = None, None, ''

        # 1. model file
        if xlbook is not None and not _blank(row.get(COL_TABLE)):
            sheet, table = row.get(COL_SHEET), row[COL_TABLE]
            try:
                raw = formatSpecTable(readSpecTableRaw(xlbook, connectionType, sheet, table, has_headers))
                raw = raw.filter(~pl.all_horizontal(pl.all().is_null()))
                if raw.height:
                    df = formatSpecTable(raw, _restrict(schema, raw.columns),
                                         _restrict(defaults, raw.columns), _restrict(exclude, raw.columns), table)
                    source, detail = SOURCE_MODEL, f"{sheet}!{table}"
            except Exception as e:
                warn(spec, f"table {table} not read from the model file ({type(e).__name__}: {e}); using default")

        # 2. Default Source from the config
        if df is None and not _blank(row.get(COL_DEFAULT)):
            name = row[COL_DEFAULT]
            section = _resolveDefaultSource(config, name)
            if section is None:
                warn(spec, f"Default Source {name} not found in the config")
            else:
                raw = _sectionAsTable(config, section)
                df = formatSpecTable(raw, _restrict(schema, raw.columns),
                                     _restrict(defaults, raw.columns), _restrict(exclude, raw.columns), name)
                source, detail = SOURCE_DEFAULT, name

        # 3. empty table
        if df is None:
            if not columns:
                columns = ['Information', 'Value'] if is_info else keys
                if not columns:
                    warn(spec, "no columns in Spec Data Formats; empty table has no columns")
            df = emptySpecTable(columns, schema)
            source, detail = SOURCE_EMPTY, ''

        # key checks
        missing = [k for k in keys if k not in df.columns]
        if missing:
            warn(spec, f"key column(s) not in table: {', '.join(missing)}")
        elif keys and df.height:
            dupes = df.filter(df.select(keys).is_duplicated()).select(keys).unique()
            if dupes.height:
                shown = '; '.join('|'.join('' if v is None else str(v) for v in r) for r in dupes.head(5).rows())
                warn(spec, f"{dupes.height} duplicated key(s) ({', '.join(keys)}): {shown}")

        tables[spec] = df
        report.append({'Spec': spec, 'Source': source, 'Detail': detail,
                       'Rows': df.height, 'Problems': '; '.join(problems)})

    sources = pl.DataFrame(report, schema={'Spec': pl.Utf8, 'Source': pl.Utf8, 'Detail': pl.Utf8,
                                           'Rows': pl.Int64, 'Problems': pl.Utf8})
    used_defaults = sources.filter(pl.col('Source') != SOURCE_MODEL).height
    MYLOGGER.info(f"Loaded {len(tables)} specs ({used_defaults} from defaults or empty)")
    return tables, sources
