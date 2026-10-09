"""
main.py
===========
Entry point for the analytics tool platform.

This script can be launched in three ways:

1. **From Excel (via xlwings macro):** Excel calls ``main.main()``
   directly using ``xlwings.Book.caller()``. The workbook is already open;
   its local path comes from ``modelkit.paths.localPath`` (which maps
   OneDrive/SharePoint addresses to the synced folder), and
   ``CONNECTIONTYPE`` is set to 0 automatically.

2. **As a standalone Python script (or frozen .exe):** The user is presented
   with a dropdown dialog to choose between opening the spec file in Excel
   (connection type 1), running without opening Excel (connection type 1.1),
   or opening the browser version (connection type 2).

3. **Panel local (connection type 2):** Opens the Panel app in a local
   browser window. Supports developer mode, which returns the Panel object
   for interactive inspection.

Connection types
----------------
0
    Called from Excel. The workbook is already open via ``xlwings``.
1
    Called from Python. The spec file is opened in Excel via ``xlwings``.
    Also used as a fallback when connection type 1.1 is requested but the
    file is ``.xlsb``.
1.1
    Called from Python. Excel is not opened; ``python-calamine`` is used
    instead. Reads ``.xlsx``/``.xlsm`` of any size.

2
    Panel local mode. Opens the app in a local browser window.

Developer mode
--------------
Set ``DEVELOPERMODE = True`` at the module level to enable interactive use
from a Jupyter notebook or Python REPL. In developer mode:

- ``main()`` returns the ``Analysis`` or Panel object rather than discarding it.
- ``SOURCEFILE`` can be pre-set before calling ``main()`` to skip the file dialog.
- The script entry point captures the return value as ``result``.

Model configuration
-------------------
``MODELTYPE`` and ``MODELPREFIX`` are imported from ``config.py`` in the
tool's repo root.
"""

import sys
import os
from pathlib import Path
from modelkit.excel_xlwings import use_xlwings, openWorkbook
from modelkit.dialogs import selectAnalysisFile, tkinterSelectFromList
from modelkit.paths import localPath
from modelkit.status import RunStatus, ExcelStatus, runContext
import _myLogging
import xlwings as xw

# Determine root paths — must handle both frozen (PyInstaller) and script modes
if getattr(sys, 'frozen', False):
    # Running as packaged exe
    # sys.executable = path to the .exe
    # sys._MEIPASS   = temp folder where bundled files are extracted
    _exe_dir   = os.path.dirname(sys.executable)      # folder containing the .exe
    _meipass   = sys._MEIPASS                          # extracted bundle contents
    _root      = _exe_dir                              # tool root = exe location
    sys.path.insert(0, _meipass)                       # _misc, _myLogging etc live here
    sys.path.insert(0, os.path.join(_meipass, 'panel'))
    from config import MODELTYPE, MODELPREFIX,INCLUDEPANEL,DEVELOPERMODE
    CONFIGFILE = os.path.join(_meipass, f'{MODELPREFIX}_config.ini')
else:
    # Running as script from shared-lib/
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, os.path.join(_root, 'shared-lib'))
    sys.path.insert(0, _root)
    _meipass = None
    from config import MODELTYPE, MODELPREFIX,INCLUDEPANEL,DEVELOPERMODE
    CONFIGFILE = os.path.join(_root, f'{MODELPREFIX}_config.ini')

# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

# Extra line in the end-of-run message; a tool can set its own in config.py.
try:
    from config import SUCCESSNOTE_EXCEL
except ImportError:
    SUCCESSNOTE_EXCEL = "Data Model will update after OK."
try:
    from config import SUCCESSNOTE_NOEXCEL
except ImportError:
    SUCCESSNOTE_NOEXCEL = "Update data model after opening Excel file."

# Set by main() once the connection type is determined. None until then.
CONNECTIONTYPE=None
_myLogging.setup_logging(modelprefix=MODELPREFIX)

# Full path to the spec file (Excel workbook or gzip). Set by main() after
# the file dialog, or from the calling workbook's local path.
SOURCEFILE=None

# The open xlwings Book object. Set when called from Excel (type 0) or when
# a file is opened via xlwings (type 1). None when using calamine (type 1.1).
BOOK=None

# Folder for data_warnings.csv: the same folder as Model.log (see _myLogging).
RUNFOLDER = Path(rf"C:\{MODELPREFIX}")

def main():
    """
    Determine the connection type, load the spec file, and run the analysis
    or launch the Panel app.

    This function is the single entry point for all launch modes. It:

    1. Detects whether it was called from Excel (``xw.Book.caller()``
       succeeds) or from Python (falls back to the tkinter mode dialog).
    2. For Excel/Python modes (0, 1, 1.1): imports ``_analysis`` and
       ``modelFunctions``, constructs the Analysis object, and reports
       success or failure.
    3. For Panel local mode (2): imports ``modelPanelSetup`` and ``panel``,
       and opens the app in a local browser window.

    Globals mutated
    ---------------
    CONNECTIONTYPE, BOOK, SOURCEFILE
        All three may be updated during execution.

    Returns
    -------
    Analysis, Panel, or None
        Returns the Analysis or Panel object only when ``DEVELOPERMODE`` is
        True. Returns ``None`` in all other cases (including on user
        cancellation).
    """
    global CONNECTIONTYPE,BOOK,SOURCEFILE,DEVELOPERMODE
    
    # Import the Analysis module (needed for RunAnalysis)
    import _analysis as Analysis    
    
    def RunAnalysis(book,sourcefile):
        """
        Construct the Analysis object and report success or failure.

        Parameters
        ----------
        book : xlwings.Book or CalamineWorkbook or None
            Open workbook, or None when using calamine without opening Excel.
        sourcefile : str
            Full path to the spec file.

        Returns
        -------
        Analysis or None
            The Analysis object if ``DEVELOPERMODE`` is True, else None.
        """
        # Excel: status bar during the run, data warnings written to Data Issues,
        # runstatus set, message box at the end. Without a workbook: console + message box.
        if book is not None and CONNECTIONTYPE in [0, 1]:
            status = ExcelStatus(book, MODELPREFIX, success_note=SUCCESSNOTE_EXCEL)
        else:
            status = RunStatus(MODELPREFIX, success_note=SUCCESSNOTE_NOEXCEL,
                               message_box=True)

        analysis = None
        try:
            with runContext(status, RUNFOLDER):
                status.update("Loading specs and running analysis")
                analysis= Analysis.Analysis(CONNECTIONTYPE,MODELTYPE,MODELPREFIX,book,sourcefile,configfile=CONFIGFILE)
                if len(analysis.error)>0:
                    raise RuntimeError(f"Model Update Failed: {analysis.error}")
        except Exception:
            # runContext has already shown the error (and set runstatus in Excel)
            if DEVELOPERMODE:
                return analysis
            sys.exit(1)

        if DEVELOPERMODE:
            return analysis     

    def run_analysis_and_return_if_dev(book, sourcefile):
        """Run analysis, optionally return object if in developer mode."""
        result = RunAnalysis(book, sourcefile)
        return result if DEVELOPERMODE else None
    
    # ------------------------------------------------------------------
    # Step 1: Determine connection type.
    # If xw.Book.caller() succeeds we were launched from an Excel macro.
    # If it raises (e.g. not called from Excel) fall back to the dialog.
    # ------------------------------------------------------------------
                
    try:
        BOOK=xw.Book.caller()
        BOOK.app.calculate()
        SOURCEFILE=localPath(BOOK.fullname)
        CONNECTIONTYPE=0
    except Exception:
        options=["Open Excel File", "Run Analysis on Closed Excel File"]
        if INCLUDEPANEL:
            options.append("Open Browser Version")
            sys.path.insert(0, os.path.join(_root, 'shared-lib', 'panel'))                
        mode=tkinterSelectFromList(options=options)
        if mode=="Open Excel File":
            CONNECTIONTYPE=1  
        elif mode=="Run Analysis on Closed Excel File":
            CONNECTIONTYPE=1.1
        elif mode=="Open Browser Version":
            CONNECTIONTYPE=2

    # ------------------------------------------------------------------
    # Step 2: Import model modules and define the RunAnalysis helper.
    # Imports are deferred here so that importing main itself (e.g.
    # in tests or notebooks) doesn't trigger the full module load.
    # ------------------------------------------------------------------
    if CONNECTIONTYPE in [0,1,1.1]:
        # Called from Excel

        # ------------------------------------------------------------------
        # Step 3: Dispatch based on connection type.
        # ------------------------------------------------------------------
        if CONNECTIONTYPE == 0:
            # Called from Excel — book and sourcefile are already set.
            return run_analysis_and_return_if_dev(BOOK, SOURCEFILE)

        elif CONNECTIONTYPE in [1,1.1]:
            # Called from Python — show file dialog to select the spec file.
            SOURCEFILE = selectAnalysisFile()
            if not SOURCEFILE:
                return

            # Promote 1.1 to 1 for .xlsb files, whose tables python-calamine
            # cannot read. File size no longer matters: calamine parses lazily.
            if use_xlwings(CONNECTIONTYPE, SOURCEFILE):
                CONNECTIONTYPE=1
                BOOK=openWorkbook(SOURCEFILE)
                if BOOK is None:
                    return
                return run_analysis_and_return_if_dev(BOOK, SOURCEFILE)
            else:
                # Connection type 1.1: use calamine, no Excel instance needed.
                BOOK=None
                return run_analysis_and_return_if_dev(BOOK,SOURCEFILE)
    elif CONNECTIONTYPE == 2:
        # A tool with its own browser app names its module in config.py (PANELMODULE); the module's
        # launch() opens it. Otherwise the spec-driven Panel app (_PanelSetup) is used.
        try:
            from config import PANELMODULE
        except ImportError:
            PANELMODULE = None
        if PANELMODULE:
            import importlib
            return importlib.import_module(PANELMODULE).launch(developer=DEVELOPERMODE)

        # Panel local mode — opens the app in a local browser window.
        import _PanelSetup as aPanel
        import panel as pn

        # One config file for Excel and Panel; the Panel app picks out its tables via [Panel Flags].
        # The "<prefix>_Panel" model prefix is kept: the Panel app uses it for its /app folder.
        app = aPanel.Panel(CONNECTIONTYPE, MODELTYPE, f"{MODELPREFIX}_Panel", configfile=CONFIGFILE)
        if DEVELOPERMODE:
            return app
        else:
            app.view().show()

if __name__ == "__main__":
    if not DEVELOPERMODE:
        main()
    else:
        # In developer mode, capture the Analysis or Panel object for interactive use.
        SOURCEFILE=None
        result=main()