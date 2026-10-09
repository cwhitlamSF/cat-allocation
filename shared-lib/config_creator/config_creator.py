# Purpose: Create config file based on current version of Excel spec format
# Will create config file in same folder as Excel file named MODELPREFIX_config.ini.
# Panel and non-Panel specs go in the same file; the [Panel Flags] section says which are which.
# Reads the saved workbook from disk with calamine; Excel does not need to be open.
import os
import sys
import re
import configparser

_here       = os.path.dirname(os.path.abspath(__file__))  # shared-lib/config_creator/
_shared_lib = os.path.dirname(_here)                       # shared-lib/
_tool_root  = os.path.dirname(_shared_lib)                 # tool folder

sys.path.insert(0, _shared_lib)
sys.path.insert(0, _tool_root)

from modelkit.common import (CONFIG_KEYS_SUFFIX, CONFIG_KEY_COLUMNS, CONFIG_VALUE_COLUMNS,
                             CONFIG_PANEL_FLAGS, CONFIG_SOURCE_TABLES)
from modelkit.specs import (COL_SPEC, COL_SHEET, COL_TABLE, COL_KEYS, COL_DEFAULT,
                            COL_INFO, COL_HEADERS)
from modelkit.excel_calamine import openWorkbookReader, readTableAsStrings, isTrue
from modelkit.dialogs import showMessageBox, selectAnalysisFile


SPEC_TABLE = 'Tbl_specDefinition'
SPEC_KEY   = 'Spec Key'

_RESERVED = {n.lower() for n in (CONFIG_KEY_COLUMNS, CONFIG_VALUE_COLUMNS, CONFIG_PANEL_FLAGS, CONFIG_SOURCE_TABLES)}

# Excel tables that define the model's input specs (cross-checked when both are present)
INPUT_SPECS_TABLE  = 'Tbl_dataInputSpecs'
SPEC_FORMATS_TABLE = 'Tbl_specDataFormats'

# Letters, digits, underscores and spaces; must start with a letter.
# Leading/trailing spaces are stripped before this check.
_SPEC_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_ ]*$")


def getColList(colstring):
    return [x.strip() for x in colstring.split(",")]


def getKeys(collist, x):
    if len(collist) > 1:
        return '|'.join(x[collist].values.tolist())
    return x[collist[0]]


def getVals(collist, x):
    if len(collist) > 1:
        return tuple(x[collist].values.tolist())
    return x[collist[0]]


def _validate_spec_keys(specs):
    """Strip Spec Keys and stop with a clear message if any are missing, malformed or duplicated."""
    if SPEC_KEY not in specs.columns:
        raise ValueError(f"Column '{SPEC_KEY}' not found in {SPEC_TABLE}.")
    specs[SPEC_KEY] = specs[SPEC_KEY].str.strip()
    problems = []
    for i, key in specs[SPEC_KEY].items():
        if key in ("", "None"):
            problems.append(f"row {i + 1}: Spec Key is blank")
        elif key.lower() in _RESERVED:
            problems.append(f"row {i + 1}: '{key}' is reserved for a summary section in the config")
        elif key.lower().endswith(CONFIG_KEYS_SUFFIX.lower()):
            problems.append(f"row {i + 1}: '{key}' can't end in '{CONFIG_KEYS_SUFFIX.strip()}' "
                            "(it would clash with another spec's companion section)")
        elif not _SPEC_KEY_PATTERN.match(key):
            problems.append(f"row {i + 1}: '{key}' - use letters, digits, underscores and spaces only, "
                            "starting with a letter")
    dupes = specs.loc[specs[SPEC_KEY].duplicated(keep=False), SPEC_KEY]
    for key in sorted(set(dupes) - {"", "None"}):
        rows = ", ".join(str(i + 1) for i in dupes[dupes == key].index)
        problems.append(f"'{key}' is used more than once (rows {rows})")
    if problems:
        raise ValueError(f"Problems with Spec Key in {SPEC_TABLE}:\n  " + "\n  ".join(problems))


def _blank(x):
    return x is None or str(x).strip() in ("", "None", "nan")


def _check_input_specs(reader, specs):
    """
    Cross-check Tbl_dataInputSpecs against Tbl_specDataFormats and Tbl_specDefinition.
    Returns (errors, warnings). Skipped if either table isn't listed in Tbl_specDefinition.
    """
    by_table = {str(t): row for t, row in zip(specs['Source Table'], specs.to_dict('records'))}
    if INPUT_SPECS_TABLE not in by_table or SPEC_FORMATS_TABLE not in by_table:
        return [], []
    errors, warnings = [], []

    # Tbl_dataInputSpecs' own row must carry the columns the spec reader needs into the config
    row = by_table[INPUT_SPECS_TABLE]
    carried = set(getColList(row['Key Column (s)']) + getColList(row['Value Column (s)']))
    needed = [COL_SPEC, COL_SHEET, COL_TABLE, COL_KEYS, COL_DEFAULT, COL_INFO, COL_HEADERS]
    lost = [c for c in needed if c not in carried]
    if lost:
        errors.append(f"{SPEC_TABLE}, {row[SPEC_KEY]} row: add {', '.join(lost)} to Key/Value Column (s)")

    inputs = readTableAsStrings(reader, INPUT_SPECS_TABLE).to_dicts()
    formats = readTableAsStrings(reader, SPEC_FORMATS_TABLE).to_dicts()
    cols_by_spec = {}
    for f in formats:
        cols_by_spec.setdefault(f['Spec Table'], []).append(f['Column'])

    names = [r[COL_SPEC] for r in inputs]
    for r in inputs:
        spec = r[COL_SPEC]
        is_info = isTrue(r.get(COL_INFO))
        keys = [] if _blank(r.get(COL_KEYS)) else getColList(r[COL_KEYS])
        if not keys and is_info:
            keys = ['Information']
        cols = cols_by_spec.get(spec)
        if not cols:
            errors.append(f"{spec}: no rows in {SPEC_FORMATS_TABLE}")
        else:
            missing = [k for k in keys if k not in cols]
            if missing:
                errors.append(f"{spec}: key column(s) {', '.join(missing)} not in {SPEC_FORMATS_TABLE}")
        default = r.get(COL_DEFAULT)
        if _blank(r.get(COL_TABLE)) and _blank(default):
            errors.append(f"{spec}: no Table Name and no Default Source, so there is nowhere to read it from")
        if not _blank(default):
            if default not in by_table:
                errors.append(f"{spec}: Default Source {default} is not a Source Table in {SPEC_TABLE}")
            else:
                d = by_table[default]
                dcols = getColList(d['Key Column (s)']) + getColList(d['Value Column (s)'])
                missing = [k for k in keys if k not in dcols]
                if missing:
                    errors.append(f"{spec}: Default Source {default} has no {', '.join(missing)} column")
    unknown = sorted(set(cols_by_spec) - set(names))
    if unknown:
        warnings.append(f"{SPEC_FORMATS_TABLE} has rows for specs not in {INPUT_SPECS_TABLE} "
                        f"(ignored): {', '.join(unknown)}")
    return errors, warnings


def create_config(fullname, model_prefix):
    """
    Build MODELPREFIX_config.ini from the spec workbook.
    Returns (output path, number of specs, warnings). Raises ValueError listing any errors.
    """
    config = configparser.ConfigParser()
    filepath = os.path.dirname(fullname).replace(os.sep, "/") + "/"
    reader = openWorkbookReader(fullname)

    # Import table of dictionaries that contains all specs
    tblDictSpecs = readTableAsStrings(reader, SPEC_TABLE).to_pandas()
    _validate_spec_keys(tblDictSpecs)
    errors, warnings = _check_input_specs(reader, tblDictSpecs)
    if errors:
        raise ValueError(f"Problems with {INPUT_SPECS_TABLE}:\n  " + "\n  ".join(errors))

    for _, spec in tblDictSpecs.iterrows():
        tempSourceTbl = readTableAsStrings(reader, spec['Source Table']).to_pandas()
        tempListKeyCols = getColList(spec['Key Column (s)'])
        tempListValCols = getColList(spec['Value Column (s)'])
        tempSourceTbl['keys'] = tempSourceTbl.apply(lambda x: getKeys(tempListKeyCols, x), axis=1)
        tempSourceTbl['vals'] = tempSourceTbl.apply(lambda x: getVals(tempListValCols, x), axis=1)
        ref = spec[SPEC_KEY]
        config[ref] = dict(zip(tempSourceTbl["keys"], tempSourceTbl["vals"]))
        config[ref + CONFIG_KEYS_SUFFIX] = dict(zip(tempSourceTbl["keys"], tempSourceTbl["keys"]))
        # In-section flag kept for existing code; new code should read [Panel Flags].
        config[ref]['panel'] = "True" if isTrue(spec['Panel']) else "False"

    specKeys = tblDictSpecs[SPEC_KEY]
    config[CONFIG_KEY_COLUMNS]                        = dict(zip(specKeys, tblDictSpecs['Key Column (s)']))
    config[CONFIG_KEY_COLUMNS + CONFIG_KEYS_SUFFIX]   = dict(zip(specKeys, specKeys))
    config[CONFIG_VALUE_COLUMNS]                      = dict(zip(specKeys, tblDictSpecs['Value Column (s)']))
    config[CONFIG_VALUE_COLUMNS + CONFIG_KEYS_SUFFIX] = dict(zip(specKeys, specKeys))
    config[CONFIG_PANEL_FLAGS]                        = {key: ("True" if isTrue(flag) else "False")
                                                         for key, flag in zip(specKeys, tblDictSpecs['Panel'])}
    config[CONFIG_PANEL_FLAGS + CONFIG_KEYS_SUFFIX]   = dict(zip(specKeys, specKeys))
    # Excel table name -> Spec Key, so a Default Source can name the table
    config[CONFIG_SOURCE_TABLES]                      = dict(zip(tblDictSpecs['Source Table'], specKeys))
    config[CONFIG_SOURCE_TABLES + CONFIG_KEYS_SUFFIX] = dict(zip(tblDictSpecs['Source Table'], tblDictSpecs['Source Table']))

    outfile = f'{filepath}{model_prefix}_config.ini'
    with open(outfile, 'w') as configfile:
        config.write(configfile)
    return outfile, len(tblDictSpecs), warnings


if __name__ == "__main__":
    from config import MODELPREFIX
    from modelkit.excel_xlwings import confirmWorkbookSaved

    fullname = selectAnalysisFile(
        filetypes=[("Excel File", "*.xlsx;*.xlsm")]
    )
    if fullname and confirmWorkbookSaved(fullname):
        try:
            outfile, n_specs, warnings = create_config(fullname, MODELPREFIX)
        except Exception as e:
            showMessageBox("Config not created",
                           f"Creating the config from {os.path.basename(fullname)} failed:\n\n"
                           f"{type(e).__name__}: {e}",
                           icon="error")
            raise
        note = ("\n\nWarnings:\n" + "\n".join(warnings)) if warnings else ""
        showMessageBox("Config created",
                       f"Created {os.path.basename(outfile)} from {n_specs} spec tables.\n\n"
                       f"Folder: {os.path.dirname(os.path.abspath(outfile))}{note}",
                       icon="warning" if warnings else "info")
