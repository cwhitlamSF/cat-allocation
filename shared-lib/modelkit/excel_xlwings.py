# modelkit/excel_xlwings.py
"""
Excel workbook operations via xlwings (needs Excel).
For reading saved files without Excel, see excel_calamine.
"""

import xlwings as xw
import os
import pandas as pd
import polars as pl
from pathlib import Path
from typing import Optional
from .common import get_logger
from .dialogs import showMessageBox
from .paths import localPath

MYLOGGER = get_logger('modelkit.excel_xlwings')

def use_xlwings(conntype: int, filepath: str) -> bool:
    """
    Determine if we should open a file with xlwings (vs python-calamine).

    xlwings is used when:
    - Connection type is 1 (always open in Excel)
    - Connection type is 1.1 AND the file is .xlsb

    python-calamine is used when:
    - Connection type is 1.1 AND the file is .xlsx/.xlsm, at any size

    Parameters
    ----------
    conntype : int
        Connection type (0, 1, 1.1, 2, 3).
    filepath : str
        Full path to the file.

    Returns
    -------
    bool
        True if should use xlwings, False if should use python-calamine.
    """
    if conntype == 1:
        return True

    if conntype == 1.1:
        # calamine reads table (ListObject) definitions from the xlsx family
        # only, so .xlsb still has to go through Excel. Size is not a factor:
        # calamine parses lazily, so a 189MB workbook opens in ~0.02s.
        return os.path.splitext(filepath)[1].lower() == '.xlsb'

    return False

def getWorkbookByFullPath(full_path: str) -> Optional[xw.Book]:
    """
    Return the workbook for `full_path`, reusing it if already open in Excel.

    A workbook matching only on filename is rejected unless its 'Model Path'
    sheet's 'fullname' range confirms it is the same file, so that a
    similarly-named workbook open from another folder is never used by
    mistake. Returns None (after telling the user why) on any mismatch.

    Parameters
    ----------
    full_path : str
        Full path to .xlsx, .xlsm, or .xlsb file.

    Returns
    -------
    xlwings.Book or None
        Open workbook object, or None if it could not be resolved.
    """
    MYLOGGER.debug(f'Opening workbook: {full_path}')
    target_path = str(full_path).strip()
    filename = Path(full_path).name.lower()

    apps = list(xw.apps)

    for app in apps:
        for wb in app.books:
            if wb.name.lower() != filename:
                continue

            try:
                # Recalculate to ensure formulas/named ranges are updated
                wb.app.calculate()
            except Exception as e:
                showMessageBox("Status", f"Failed to recalculate '{wb.name}': {e}")
                return None

            try:
                model_sheet = wb.sheets["Model Path"]
            except Exception:
                showMessageBox(
                    "Status",
                    f"A workbook named '{wb.name}' is already open, but it does not contain the 'Model Path' sheet."
                )
                return None

            try:
                defined_path = model_sheet.range("fullname").value
            except Exception:
                showMessageBox(
                    "Status",
                    f"A workbook named '{wb.name}' is already open, but the 'fullname' range is missing or invalid."
                )
                return None

            if not defined_path:
                showMessageBox(
                    "Status",
                    f"A workbook named '{wb.name}' is already open, but the 'fullname' range is empty."
                )
                return None

            defined_path_norm = os.path.normpath(str(defined_path).strip())
            target_path_norm = os.path.normpath(str(target_path).strip())

            if defined_path_norm.lower() == target_path_norm.lower():
                return wb

            showMessageBox(
                "Status",
                f"A workbook named '{wb.name}' is already open, but its full path does not match the requested file.\n\n"
                f"Open:      {defined_path_norm}\n"
                f"Requested: {target_path_norm}"
            )
            return None

    # Not open anywhere, so open it
    try:
        app = apps[0] if apps else xw.App(visible=True, add_book=False)
        return app.books.open(full_path)
    except Exception as e:
        MYLOGGER.error(f'Failed to open workbook {full_path}: {e}')
        showMessageBox("Status", f"Failed to open workbook '{full_path}': {e}")
        return None

def closeWorkbook(book: 'xw.Book', save: bool = False) -> None:
    """Close a workbook without saving by default."""
    if book:
        book.close(save=save)
        MYLOGGER.debug(f'Closed workbook')

def copyTableToSht(xlbook,connectionType,df, shtName, tblName):
    if connectionType==1:
        try:
            if df is None:
                xlbook.sheets[shtName].tables[tblName].data_body_range.clear()
                return

            if isinstance(df, pl.DataFrame):
                df=pd.DataFrame(df.to_dicts())

            if hasattr(df,"shape") and df.shape[0]>0:
                xlbook.sheets[shtName].tables[tblName].update(df, index=False)
            else:
                xlbook.sheets[shtName].tables[tblName].data_body_range.clear()
        except:
            pass
    elif connectionType==2:
        from openpyxl.utils import range_boundaries, get_column_letter
        # NOTE: this branch clears the table and checks the width but does not yet
        # write df's rows into the table.
        ws = xlbook[shtName]

        if tblName not in ws.tables:
            raise KeyError(f"Table '{tblName}' not found on sheet '{shtName}'")

        tbl = ws.tables[tblName]
        min_col, min_row, max_col, max_row = range_boundaries(tbl.ref)
        table_ncols = max_col - min_col + 1

        # Normalize df to pandas DataFrame
        if df is None:
            df = pd.DataFrame()
        elif isinstance(df, pl.DataFrame):
            df = pd.DataFrame(df.to_dicts())
        elif not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        # Clear existing data body (keep header)
        if max_row >= min_row + 1:
            for r in range(min_row + 1, max_row + 1):
                for c in range(min_col, max_col + 1):
                    ws.cell(row=r, column=c).value = None

        # Empty df -> shrink table to header-only and done
        if df.shape[0] == 0:
            tbl.ref = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{min_row}"
            return

        # Enforce width match (safer)
        if df.shape[1] != table_ncols:
            raise ValueError(
                f"DataFrame has {df.shape[1]} cols but table '{tblName}' has {table_ncols} cols "
                f"(from ref {tbl.ref})."
            )


def clearTableOnSht(xlbook, connectionType, shtName, tblName):
    """
    Clears the table's data body only (keeps headers).
    For openpyxl, also shrinks the table range to header-only.
    """
    if connectionType == 1:
        try:
            xlbook.sheets[shtName].tables[tblName].data_body_range.clear()
        except:
            pass

    elif connectionType == 2:
        from openpyxl.utils import range_boundaries, get_column_letter
        ws = xlbook[shtName]
        if tblName not in ws.tables:
            raise KeyError(f"Table '{tblName}' not found on sheet '{shtName}'")

        tbl = ws.tables[tblName]
        min_col, min_row, max_col, max_row = range_boundaries(tbl.ref)

        # Clear existing data body (keep header)
        if max_row >= min_row + 1:
            for r in range(min_row + 1, max_row + 1):
                for c in range(min_col, max_col + 1):
                    ws.cell(row=r, column=c).value = None

        # Shrink table ref to header-only
        tbl.ref = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(max_col)}{min_row}"


def refresh_data_model(wb: xw.Book, timeout_seconds: int = 300) -> None:
    app = wb.app

    # Optional: reduce UI noise
    app.display_alerts = True
    app.screen_updating = False

    try:
        # Refresh workbook connections / queries first
        wb.api.RefreshAll()

        # Wait for async OLEDB / OLAP queries to finish
        app.api.CalculateUntilAsyncQueriesDone()

        # Then force a full model refresh/reprocess
        wb.api.Model.Refresh()

        # And wait again in case that kicked off async work
        app.api.CalculateUntilAsyncQueriesDone()

        # Final calc pass
        app.calculate()

    finally:
        app.screen_updating = True


def findOpenWorkbook(full_path: str) -> Optional[xw.Book]:
    """
    Return the xlwings Book if `full_path` is open in a running Excel instance, else None.

    Unlike getWorkbookByFullPath, this never opens the file, shows no messages and
    does not need the 'Model Path' sheet. Matches on the full local path; when Excel
    reports a OneDrive/SharePoint URL, it is converted with paths.localPath. Only if
    that fails does it fall back to matching the file name. Any failure (no Excel,
    COM error) counts as not open.
    """
    try:
        apps = list(xw.apps)
    except Exception:
        return None

    target_path = os.path.normcase(os.path.abspath(full_path))
    target_name = Path(full_path).name.lower()
    for app in apps:
        try:
            books = list(app.books)
        except Exception:
            continue
        for bk in books:
            try:
                bk_full = localPath(bk.api.FullName)
            except Exception:
                bk_full = ""
            if bk_full:
                if os.path.normcase(os.path.abspath(bk_full)) == target_path:
                    return bk
            elif bk.name.lower() == target_name:
                return bk
    return None


def openWorkbook(full_path: str) -> Optional[xw.Book]:
    """
    Return the workbook for `full_path`: the open copy if Excel already has it
    (see findOpenWorkbook), otherwise open it. Replaces getWorkbookByFullPath
    without needing the 'Model Path' sheet. Returns None, after telling the
    user, if it can't be opened.
    """
    bk = findOpenWorkbook(full_path)
    if bk is not None:
        return bk
    try:
        apps = list(xw.apps)
        app = apps[0] if apps else xw.App(visible=True, add_book=False)
        return app.books.open(full_path)
    except Exception as e:
        MYLOGGER.error(f'Failed to open workbook {full_path}: {e}')
        showMessageBox("Status", f"Failed to open workbook '{full_path}': {e}", icon="error")
        return None


def _waitForDiskWrite(full_path: str, mtime_before: float, timeout: float = 15) -> bool:
    """
    After an Excel save, wait until the file on disk has actually changed.

    For OneDrive files Excel may upload first and let the sync client update the
    local copy, so the save can land on disk a few seconds late.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if os.path.getmtime(full_path) > mtime_before:
                time.sleep(0.5)  # let the write finish
                return True
        except OSError:
            pass
        time.sleep(0.5)
    return False


def confirmWorkbookSaved(full_path: str) -> bool:
    """
    Before reading a workbook from disk, make sure unsaved edits aren't being missed.

    If the workbook is open in Excel with unsaved changes, asks the user to save
    and continue, continue with the last saved version, or cancel.

    Returns True to continue, False to cancel.
    """
    from .dialogs import askYesNoCancel, askOkCancel

    bk = findOpenWorkbook(full_path)
    if bk is None:
        return True
    try:
        if bk.api.Saved:
            return True
    except Exception:
        return True  # can't tell; carry on with the file on disk

    answer = askYesNoCancel(
        "Unsaved changes",
        f"{os.path.basename(full_path)} is open in Excel with unsaved changes.\n\n"
        "Only the saved file is read, so unsaved edits will be ignored.\n\n"
        "Yes  -  save the workbook now and continue\n"
        "No  -  continue with the last saved version\n"
        "Cancel  -  stop",
    )
    if answer is None:
        return False
    if answer:
        mtime_before = os.path.getmtime(full_path)
        bk.save()
        if not _waitForDiskWrite(full_path, mtime_before):
            return askOkCancel(
                "Save not detected",
                "Excel saved the workbook, but the file on disk hasn't updated yet "
                "(OneDrive may still be syncing).\n\n"
                "OK to continue anyway, Cancel to stop.",
            )
    return True
