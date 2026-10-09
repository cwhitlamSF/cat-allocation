"""
modelAnalysis.py
================
Core analysis module for the Experience Rating Tool.

This module defines the ``Analysis`` class, which is the central object of the
tool. It is responsible for:

  - Locating and loading the model configuration (``.ini``) file.
  - Reading spec data from an Excel workbook (via xlwings or calamine),
    a pre-prepared gzip/parquet file, or starting from a blank state.
  - Parsing and type-coercing each spec table according to the format rules
    defined in the config.
  - Delegating initial cleaning (``initialCleanSpecs``), spec preparation
    (``createPreppedSpecs``), and model-specific post-processing steps
    (``modelSpecificAnalysisSteps``) to ``modelFunctions``.

Search for "MODEL-SPECIFIC" to find the sections most likely to need editing
when adapting this module for a new model.

Connection types
----------------
The ``connectiontype`` parameter controls how the tool interacts with Excel and
where it looks for the spec file:

  0   — Called from Excel (via xlwings ``Book.caller()``). The workbook is
        already open; no file dialog is shown.
  1   — Called from Python; spec file is opened in Excel via xlwings.
  1.1 — Called from Python; Excel is not opened (calamine used instead),
        unless the file is ``.xlsb``, in which case it falls back to
        connection type 1.
  3   — Panel/web app mode; behaves like type 1 for Excel interaction but
        receives its path from the panel object rather than a file dialog.
  2+  — Panel/web app mode with gzip or blank specs; no Excel interaction.
"""

#### SEARCH "MODEL-SPECIFIC" TO FIND MODEL-SPECIFIC CODE ####

import xlwings as xw
import os
import polars as pl
import logging
import sys
import importlib

MYLOGGER = logging.getLogger(__name__)
MYLOGGER.setLevel(logging.INFO)

#config.ini is in parent folder of this file
_here       = os.path.dirname(os.path.abspath(__file__))  # shared-lib/
_tool_root  = os.path.dirname(_here)                       # rsa/ or experience-rating/
sys.path.insert(0, _tool_root)
from config import MODELPREFIX,DEVELOPERMODE
from modelkit.specs import (configparser_to_dict, configTables, loadModelSpecs, specSourcesTable,
                            specFormatsTable, specFormatRules, assertStringFormats, panelSpecKeys,
                            COL_SPEC, COL_HEADERS)
from modelkit.excel_xlwings import findOpenWorkbook, openWorkbook
from modelkit.excel_calamine import isTrue
from modelkit.files import fromGzipParquet
mFns = importlib.import_module(f'{MODELPREFIX}_Functions')
if DEVELOPERMODE:
    from IPython.display import display

class Analysis():
    """
    Loads, validates, and prepares all spec data for a single model run.

    The constructor performs the full initialisation pipeline: config lookup,
    spec file detection, spec loading, format coercion, and model-specific
    post-processing. If any step fails, ``self.error`` is set to a descriptive
    message and the constructor returns early. Callers should always check
    ``analysis.error`` before using the object.

    Attributes
    ----------
    error : str
        Empty string on success; a human-readable message on failure.
    specfile : str
        Full path to the spec file (Excel or gzip), or ``''`` for blank specs.
    connectiontype : int or float
        Connection type used for this run (see module docstring).
    fileext : str
        Normalised file extension of the spec file: ``'xls'``, ``'gzip'``,
        or ``''`` (blank).
    filesize : int
        Size in bytes of the spec file, or 0 if no file was provided.
    book : xlwings.Book or CalamineWorkbook or None
        The open workbook object, or ``None`` if not using Excel.
    xlconnectiontype : int
        Excel connection sub-type: 1 = xlwings, 2 = calamine, 0 = none.
    initialspecs : dict
        Spec tables keyed by spec name (the Information column of Model Spec
        Sources): from the model file, its Default Source, or empty.
    specSources : polars.DataFrame
        Where each spec in ``initialspecs`` came from (Spec, Source, Detail,
        Rows, Problems). See ``modelkit.specs.loadModelSpecs``.
    configTables : dict
        Every spec section of the config as a table, keyed by Spec Key.
    preppedspecs : dict
        Cleaned and enriched spec tables produced by ``prepSpecs()``, plus
        metadata keys ``'specfile'``, ``'fileext'``, and ``'error'``.
    configdict : dict
        Full parsed contents of the ``.ini`` config file.
    specpath : str
        Directory of the spec file (Windows path separators), set when a
        spec file is present.
    analysispath : str
        Output directory for this run, set during model-specific steps.
    resultpath : str
        ``Results`` subdirectory inside ``analysispath``, set during
        model-specific steps.
    """

    def __init__(self, connectiontype, modeltype, modelprefix, book=None, specfile='', configfile=None, panel=None):
        """
        Initialise the Analysis object and run the full spec-loading pipeline.

        Parameters
        ----------
        connectiontype : int or float
            Controls how the tool interacts with Excel and finds the spec file.
            See the module docstring for the full list of values.
        modeltype : str
            Human-readable model name, stored for reference (e.g. used in
            logging or UI messages).
        modelprefix : str
            Short prefix identifying the model (e.g. ``'EXPER'``). Used to
            locate the config file (``<modelprefix>_config.ini``) and set
            the log folder.
        book : xlwings.Book or CalamineWorkbook, optional
            An already-open workbook (``connectiontype == 0``: the calling
            workbook). Defaults to ``None``.
        specfile : str, optional
            Full path to the spec file (Excel workbook or gzip/parquet).
            If empty, the tool will either show a file dialog (local mode)
            or start with blank specs (panel mode). Defaults to ``''``.
        panel : object, optional
            Panel app object passed in when running in web/panel mode
            (``connectiontype >= 2``). Supplies ``panel.analysispath`` for
            the output directory. Defaults to ``None``.
        """
        self.error = ""
        self.specfile = specfile if specfile else ''
        self.connectiontype = connectiontype
        self.fileext = ''
        self.filesize = 0
        self.initialspecs = {}
        self.specSources = pl.DataFrame()
        self.configTables = {}
        self.book = book
        self.xlconnectiontype = 0
        self.modeltype = modeltype
        MYLOGGER.info('Starting Analysis Class initialization')
        MYLOGGER.debug('Trying to connect to config file')

        # ------------------------------------------------------------------
        # Load the .ini config file.
        # ------------------------------------------------------------------
        if not (configfile and os.path.exists(configfile)):
            self.error = "Unable to find config file"
            return

        MYLOGGER.debug(configfile)
        self.configdict = configparser_to_dict(configfile)
        if isinstance(self.configdict, str):          # configparser_to_dict returns an error message
            self.error = self.configdict
            return

        # Every spec section of the config as a table, keyed by Spec Key
        # (e.g. self.configTables['Model Spec Sources']).
        try:
            self.configTables = configTables(self.configdict)
        except Exception as e:
            self.error = f"Error converting config sections to tables: {e}"
            return

        # ------------------------------------------------------------------
        # SECTION 1: WORK OUT WHERE THE SPECS COME FROM
        #
        # fileext is normalised to one of: 'xls', 'gzip', or '' (blank).
        # ------------------------------------------------------------------
        if self.specfile != '':
            try:
                self.fileext = os.path.splitext(self.specfile)[1]
                self.filesize = os.path.getsize(self.specfile)
            except Exception as e:
                self.error = f"Error getting file info for {self.specfile}: {e}"
                return

        # Normalise Excel extensions to a single 'xls' token, and decide
        # whether to use xlwings (forcexlwings=True) or calamine.
        # xlwings is required when connectiontype==1 (explicit), or when the
        # file is already open in Excel.
        if self.fileext.lower() in ['.xlsx', '.xlsm', '.xlsb']:
            if self.connectiontype == 1 or self.fileext.lower() == '.xlsb':
                self.forcexlwings = True
            else:
                try:
                    self.forcexlwings = findOpenWorkbook(self.specfile) is not None
                except Exception:
                    self.forcexlwings = False
            self.fileext = 'xls'
        elif self.fileext == '.gzip':
            self.fileext = 'gzip'
        else:
            self.fileext = ''

        if self.fileext == 'xls':
            self.specpath = os.path.dirname(self.specfile).replace("/", "\\")

            if self.connectiontype in [1, 1.1, 3]:
                if self.forcexlwings == True:
                    # Use the workbook open in Excel, or open it.
                    if self.book is None:
                        MYLOGGER.debug('Opening spec file with xlwings')
                        self.book = openWorkbook(self.specfile)
                        if self.book is None:
                            MYLOGGER.error(f"Error opening Excel file: {self.specfile}")
                            self.error = "Unable to open Excel file."
                            return
                    self.xlconnectiontype = 1
                else:
                    # Read the closed workbook with python-calamine (no Excel
                    # instance needed). Unlike openpyxl it also copes with this
                    # workbook's pivot caches, which openpyxl cannot parse.
                    try:
                        from python_calamine import CalamineWorkbook
                        self.book = CalamineWorkbook.from_path(self.specfile, load_tables=True)
                        self.xlconnectiontype = 2
                    except Exception as e:
                        MYLOGGER.error(f"Error opening Excel file: {self.specfile}: {e}")
                        self.error = "Unable to open Excel file with python-calamine."
                        return
            else:
                # connectiontype 0: book was passed in by the Excel caller.
                self.xlconnectiontype = 1

        elif self.fileext == 'gzip':
            # Load the pre-prepared gzip/parquet bundle produced by a
            # previous run. This avoids re-opening Excel and is the default
            # mode for the panel/web app.
            MYLOGGER.debug('Connection type is 2')
            self.specpath = os.path.dirname(self.specfile).replace("/", "\\")
            try:
                self.initialspecs = fromGzipParquet(self.specfile, 'initialspecs')
            except Exception as e:
                MYLOGGER.error(f"Error opening Gzip file: {self.specfile}: {e}")
                self.error = "Unable to open Gzip file."
                return

        elif self.fileext == '':
            # No spec file provided. Allowed only in panel/web app mode
            # (connectiontype >= 2): specs start from their defaults (or empty).
            MYLOGGER.debug('Blank specs')
            if self.connectiontype >= 2:
                self.specfile = ''
                self.book = None
                self.xlconnectiontype = 0
            else:
                self.error = "No valid spec file provided."
                return

        # ------------------------------------------------------------------
        # SECTION 2: LOAD THE SPEC TABLES
        #
        # Excel or blank: loadModelSpecs resolves each spec in Model Spec
        # Sources from the model file, its Default Source in the config, or
        # an empty table, and formats it per Spec Data Formats. Problems
        # (missing tables, duplicate keys) are logged as data warnings.
        # gzip: the tables were saved already formatted; only string
        # formats are re-asserted.
        # ------------------------------------------------------------------
        try:
            if self.fileext == 'gzip':
                self.formatGzipSpecs()
            else:
                xlbook = self.book if self.fileext == 'xls' else None
                self.initialspecs, self.specSources = loadModelSpecs(
                    self.configdict, xlbook, self.xlconnectiontype or 2, logger=MYLOGGER)
        except Exception as e:
            MYLOGGER.exception("Unable to load specs")
            self.error = f"Unable to load specs: {e}"
            return

        # Delegate cleaning and enrichment to prepSpecs, then finalise.
        self.prepSpecs()

        if self.error != '':
            return

        self.preppedspecs.update({"specfile": self.specfile})
        self.preppedspecs.update({"fileext": self.fileext})

        # MODEL-SPECIFIC: run any additional post-processing steps defined in
        # modelFunctions (e.g. building summary tables, writing CSVs).
        try:
            MYLOGGER.debug('Trying to connect to model specific analysis steps')
            mFns.modelSpecificAnalysisSteps(self, connectiontype, panel)
            MYLOGGER.info('Successfully completed Analysis Class initialization')
        except:
            pass

    def formatGzipSpecs(self):
        """Re-assert string formats on specs loaded from a gzip bundle."""
        formats = specFormatsTable(self.configdict)
        for row in specSourcesTable(self.configdict).rows(named=True):
            key = row[COL_SPEC]
            if key in self.initialspecs:
                dictFormats = specFormatRules(formats, key)[0]
                try:
                    self.initialspecs[key] = assertStringFormats(
                        self.initialspecs[key], isTrue(row.get(COL_HEADERS)), dictFormats)
                except Exception:
                    pass

    def prepSpecs(self):
        """
        Clean and enrich ``initialspecs`` to produce ``preppedspecs``.

        This method is called automatically at the end of ``__init__``. It:

        1. Runs ``mFns.initialCleanSpecs`` on the raw spec tables when the
           source is an Excel file. This step performs lightweight,
           non-destructive cleaning (trimming, type normalisation) without
           adding or removing columns, so the result can still serve as the
           authoritative source data for any data-entry forms.

        2. Copies ``initialspecs`` to ``preppedspecs`` and delegates the main
           transformation logic to ``mFns.createPreppedSpecs``, which builds
           the fully enriched spec tables used by the model calculations.

        3. Promotes the config sections flagged Panel = True (``[Panel Flags]``)
           into ``preppedspecs`` as plain dicts keyed by Spec Key, without the
           ``'panel'`` metadata key.

        On failure, ``self.error`` is set and the method returns early.
        Callers (i.e. ``__init__``) check ``self.error`` after calling this
        method.

        Side effects
        ------------
        Sets ``self.preppedspecs``.
        May set ``self.error``.
        """
        if self.fileext == 'xls':
            mFns.initialCleanSpecs(self.initialspecs, self.specpath)

        self.preppedspecs = self.initialspecs.copy()
        self.preppedspecs['error'] = ''
        mFns.createPreppedSpecs(self.preppedspecs)
        if self.preppedspecs['error'] != '':
            self.error = self.preppedspecs['error']
            return

        # Promote Panel-flagged config sections into preppedspecs, keyed by
        # Spec Key, as plain dicts with the 'panel' metadata key removed.
        for key in panelSpecKeys(self.configdict):
            if key in self.configdict:
                self.preppedspecs[key] = {k: v for k, v in self.configdict[key].items() if k != 'panel'}