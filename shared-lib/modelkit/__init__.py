# modelkit/__init__.py
"""
Shared modelling toolkit (formerly _misc_submodules).

Import from the module that owns the function, e.g.
``from modelkit.excel_xlwings import getWorkbookByFullPath``.

Modules
-------
common          logging, DTYPES_CONVERT, config section-name constants
paths           OneDrive/SharePoint URL -> local path; resource paths
dialogs         message boxes, Yes/No/Cancel, file and option pickers
excel_xlwings   live workbooks through Excel (needs xlwings)
excel_calamine  read tables from saved .xlsx/.xlsm files; no Excel needed
specs           config .ini -> tables; loading and formatting spec tables
frames          Polars DataFrame/LazyFrame helpers
stats           summary statistics, VaR/TVaR, return periods
npz             simulation chunk arrays
files           folders, file lists, parquet I/O
lists           list / comma-string / dict helpers
logs            DATA log level and data_warnings.csv
status          run status: Excel status bar, Panel widget, end-of-run message

Deliberately empty: re-exporting everything here would make importing any
one module pull in the dependencies of all of them (xlwings for a caller
that only wanted calamine), and the export list would have to be kept in
sync by hand. The file itself is kept so this stays a regular package
rather than a namespace package, which PyInstaller collects more reliably.
"""
