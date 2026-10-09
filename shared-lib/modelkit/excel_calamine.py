# modelkit/excel_calamine.py
"""
Read Excel tables (ListObjects) from a saved workbook with calamine (via fastexcel).

No Excel or xlwings needed, so this works in the Excel-launched exe and the
browser app alike. Reads the file as last saved on disk; unsaved edits in an
open workbook are not seen (see excel_xlwings.confirmWorkbookSaved).

Requires fastexcel >= 0.12 for table support; >= 0.20 recommended.
.xlsx/.xlsm only: calamine does not read table definitions from .xlsb.
"""

import fastexcel
import polars as pl
from .common import get_logger

MYLOGGER = get_logger('modelkit.excel_calamine')


def openWorkbookReader(full_path) -> fastexcel.ExcelReader:
    """Open a workbook for reading. Accepts a path or the file's bytes. Reuse it for every table."""
    MYLOGGER.debug(f'Opening workbook reader: {full_path if isinstance(full_path, str) else "<bytes>"}')
    return fastexcel.read_excel(full_path)


def readTableAsStrings(reader: fastexcel.ExcelReader, table_name: str) -> pl.DataFrame:
    """
    Read an Excel table by name, every cell as a string.

    Normalises calamine's string forms to match what xlwings + astype(str) produced:
    booleans 'true'/'false' -> 'True'/'False', empty cells -> 'None'.
    Numbers keep calamine's form (a whole number reads as '1', not xlwings' '1.0').
    """
    df = reader.load_table(table_name, dtypes="string").to_polars()
    return df.with_columns(
        pl.col(pl.String)
          .replace({"true": "True", "false": "False"})
          .fill_null("None")
    )


def isTrue(value) -> bool:
    """Interpret a flag cell: Excel TRUE, text 'TRUE'/'true'/'Yes', or 1 all count as True."""
    return str(value).strip().lower() in ("true", "yes", "y", "1", "1.0")
