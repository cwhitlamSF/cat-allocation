"""
cca_tables.py
=============
The CCA tables, defined once. Used by tools/build_workbooks.py to build CCA_Template.xlsx and
CCA_ConfigSpecs.xlsx (so the spec definitions always match the template), and by CCA_Functions.py
for the names of the settings and columns.

Input tables list their columns as (name, data type, default, required, dropdown list, width, note).
Data types are Spec Data Formats types (Utf8, Float64). TRUE/FALSE columns are read as text and checked
by CCA_Functions, so a mistyped value is reported (the spec reader's Boolean type would turn it into a
blank, i.e. the default). Required columns are "Exclude if Null": rows missing them are dropped by the
spec reader. Run Name is not marked required for that reason: a run row without a name is reported.

Specify Runs has one row per run (its settings are columns), so one Run can do several runs.

Info tables (General, Prepare Data) are Information / Value pairs; `settings` lists their rows as
(Information, default, dropdown list, note).

Result tables are written by the run (not read as specs); their columns are fixed, so the run can
fill them in an open workbook (xlwings) or a closed one (modelkit.excel_tablewriter).
"""

# ---------------------------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------------------------
DATASETS = ['BU ELT Gross', 'BU ELT Net', 'Exposure', 'Policy LOB']
SOURCETYPES = ['Parquet', 'CSV', 'Excel Table', 'Database Query']
PREP_SOURCETYPES = ['Parquet Files', 'Database Query']
DISTORTIONS = ['wang', 'ph', 'identity']
BASES = ['occurrence', 'aggregate']
SHARINGS = ['conditional', 'prorata']
SPLITS = ['conditional', 'mean']

LISTS = {
    'lst_Datasets': DATASETS,
    'lst_SourceTypes': SOURCETYPES,
    'lst_PrepSourceTypes': PREP_SOURCETYPES,
    'lst_Distortions': DISTORTIONS,
    'lst_Bases': BASES,
    'lst_Sharings': SHARINGS,
    'lst_Splits': SPLITS,
    'lst_Boolean': ['TRUE', 'FALSE'],
}

# ---------------------------------------------------------------------------------------------
# Settings in the info tables: (Information, default, dropdown list, note)
# ---------------------------------------------------------------------------------------------
G_OUTPUT = 'Output Folder'
G_ELTFOLDER, G_ELTPATTERN, G_ELTNET = 'Policy ELT Folder', 'Policy ELT File Pattern', 'Policy ELT Is Net Of Per-Risk'
G_NOTES = 'Notes'

GENERAL_SETTINGS = [
    (G_OUTPUT, 'output', None, 'Folder for result files (one subfolder per run); relative to this workbook\'s folder'),
    (G_ELTFOLDER, 'ev_buckets', None, 'Event-grouped policy ELT files (Prepare Data writes them here)'),
    (G_ELTPATTERN, 'ev_bucket_*.parquet', None, 'Which files in the folder to read'),
    (G_ELTNET, 'FALSE', 'lst_Boolean', 'TRUE if the policy ELT is already net of per-risk (BU ELT Net is then used for both)'),
    (G_NOTES, None, None, ''),
]

P_RUN, P_SOURCETYPE, P_FOLDER, P_PATTERN = 'Run Prepare Data', 'Source Type', 'Input Folder', 'Input File Pattern'
P_CONNECTION, P_QUERY, P_TARGET = 'Connection', 'Query', 'Target Rows Per File'
P_RENAMES, P_KEEPRAW = 'Column Renames', 'Keep Query Chunks'

PREPARE_SETTINGS = [
    (P_RUN, 'FALSE', 'lst_Boolean', 'TRUE: build the event-grouped policy ELT files first (once per RMS run); the runs on Specify Runs follow'),
    (P_SOURCETYPE, 'Parquet Files', 'lst_PrepSourceTypes', 'Where the raw policy ELT is'),
    (P_FOLDER, None, None, 'Parquet Files: folder of the raw chunks (any grouping)'),
    (P_PATTERN, '*.parquet', None, 'Parquet Files: which files to read'),
    (P_CONNECTION, None, None, 'Database Query: SQLAlchemy URL; use ${VAR} for secrets'),
    (P_QUERY, None, None, 'Database Query: SQL returning EVENTID, POLICYID, PERSPVALUE, STDDEVI, STDDEVC'),
    (P_TARGET, '3000000', None, 'Rows per output file (whole events, so files vary around this)'),
    (P_RENAMES, None, None, 'Old=New pairs, comma-separated, e.g. PERSPLOSS=PERSPVALUE (case is ignored anyway)'),
    (P_KEEPRAW, 'FALSE', 'lst_Boolean', 'Database Query: keep the downloaded chunks in <folder>/_raw'),
]

# ---------------------------------------------------------------------------------------------
# Input tables
# ---------------------------------------------------------------------------------------------
SPEC_GENERAL, SPEC_PREPARE, SPEC_SOURCES = 'General', 'Prepare Data', 'Data Sources'
SPEC_SUBJECTS, SPEC_INURING, SPEC_UPPER = 'Subjects', 'Inuring Layers', 'Upper Layers'

S_NAME, S_TYPE, S_PATH, S_SHEET, S_TABLE = 'Dataset Name', 'Source Type', 'File Path', 'Sheet', 'Table Name'
S_DELIM, S_CONN, S_QUERY, S_RENAMES = 'Delimiter', 'Connection', 'Query', 'Column Renames'

SUB_NAME, SUB_LOB, SUB_REGIONS = 'Subject', 'LOBNAME', 'Regions'

SPEC_RUNS = 'Specify Runs'
R_NAME, R_INCLUDE, R_DISTORTION, R_BASIS = 'Run Name', 'Include', 'Distortion', 'Basis'
R_SHARING, R_SPLIT, R_BOUNDED, R_CLAMP = 'Inuring Sharing', 'Policy Split', 'Bounded Split', 'Clamp Negatives'
R_RP, R_BENCHMARK, R_GRID = 'Include Reinstatement Premium', 'Benchmark Comparison', 'Grid Points'

L_STAGE, L_LAYER, L_RET, L_LIM, L_PLACE, L_SUBJ = 'Stage', 'Layer', 'Retention', 'Limit', 'Placement', 'Subject'
L_MULTI, L_INCLUDE, L_PREM = 'Multiple Regions Per Policy', 'Include', 'Premium'
L_BASIS, L_REINST, L_RRATE = 'Basis', 'Reinstatements', 'Reinstatement Rate'
L_AGGLIM, L_AGGDED, L_DIST, L_THETA = 'Agg Limit', 'Agg Deductible', 'Distortion', 'Theta'

TABLES = [
  dict(spec=SPEC_GENERAL, sheet='General', table='GeneralSpecs', keys='', info=True,
       columns=[('Information', 'Utf8', None, True, None, 30, ''), ('Value', 'Utf8', None, False, None, 45, ''),
                ('Note', 'Utf8', None, False, None, 90, '')],
       settings=GENERAL_SETTINGS),

  dict(spec=SPEC_PREPARE, sheet='Prepare Data', table='PrepareSpecs', keys='', info=True,
       columns=[('Information', 'Utf8', None, True, None, 30, ''), ('Value', 'Utf8', None, False, None, 60, ''),
                ('Note', 'Utf8', None, False, None, 80, '')],
       settings=PREPARE_SETTINGS),

  dict(spec=SPEC_RUNS, sheet='Specify Runs', table='SpecifyRuns', keys='Run Name', columns=[
      (R_NAME, 'Utf8', None, False, None, 22, 'Names the run on the result sheets and its subfolder of the Output Folder (required)'),
      (R_INCLUDE, 'Utf8', 'TRUE', False, 'lst_Boolean', 9, ''),
      (R_DISTORTION, 'Utf8', 'wang', False, 'lst_Distortions', 11, 'wang; ph (proportional hazards); identity (expected-loss key)'),
      (R_BASIS, 'Utf8', 'occurrence', False, 'lst_Bases', 12, 'occurrence (layer OEP) or aggregate (annual recoveries)'),
      (R_SHARING, 'Utf8', 'conditional', False, 'lst_Sharings', 12, 'Inuring recoveries: conditional (co-recovery) or prorata'),
      (R_SPLIT, 'Utf8', 'conditional', False, 'lst_Splits', 12, 'How an event\'s weight is split across its policies'),
      (R_BOUNDED, 'Utf8', 'TRUE', False, 'lst_Boolean', 10, 'Bound the conditional split so no policy gets a negative share'),
      (R_CLAMP, 'Utf8', 'FALSE', False, 'lst_Boolean', 10, 'Alternative: zero negatives within each event and rescale'),
      (R_RP, 'Utf8', 'TRUE', False, 'lst_Boolean', 13, 'Aggregate basis: calibrate to premium + expected reinstatement premium'),
      (R_BENCHMARK, 'Utf8', 'FALSE', False, 'lst_Boolean', 12, 'Also the expected-loss keys and AAL share (Benchmark sheet; about 3x the run time)'),
      (R_GRID, 'Float64', 500.0, False, None, 9, 'Grid points for calibration'),
      ('Comments', 'Utf8', None, False, None, 30, 'Not used by the tool'),
  ]),

  dict(spec=SPEC_SOURCES, sheet='Data Sources', table='DataSources', keys='Dataset Name', columns=[
      (S_NAME, 'Utf8', None, True, 'lst_Datasets', 16, 'BU ELT Gross / Net: EVENTID, LOBNAME, REGION (or COUNTRY + STATE), RATE, PERSPVALUE, STDDEVI, STDDEVC, EXPVALUE'),
      (S_TYPE, 'Utf8', None, True, 'lst_SourceTypes', 15, 'Exposure: POLICYID, REGION (or COUNTRY + STATE), TIV. Policy LOB: POLICYID, LOBNAME'),
      (S_PATH, 'Utf8', None, False, None, 45, 'Full path, or relative to this workbook\'s folder'),
      (S_SHEET, 'Utf8', None, False, None, 12, 'Excel Table: only if two sheets have a table with the same name'),
      (S_TABLE, 'Utf8', None, False, None, 16, 'Excel Table: the table name'),
      (S_DELIM, 'Utf8', None, False, None, 10, 'CSV: default comma'),
      (S_CONN, 'Utf8', None, False, None, 40, 'Database: SQLAlchemy URL; use ${VAR} for secrets'),
      (S_QUERY, 'Utf8', None, False, None, 45, 'Database: SQL query'),
      (S_RENAMES, 'Utf8', None, False, None, 30, 'Old=New pairs, e.g. PolicyID=POLICYID (case is ignored anyway)'),
  ]),

  dict(spec=SPEC_SUBJECTS, sheet='Subjects', table='Subjects', keys='', columns=[
      (SUB_NAME, 'Utf8', None, True, None, 18, 'Name used by the layer tables; one row per LOB in the subject'),
      (SUB_LOB, 'Utf8', None, False, None, 18, 'Blank = every LOB'),
      (SUB_REGIONS, 'Utf8', None, False, None, 40, 'COUNTRY_STATE codes, comma-separated (e.g. US_FL); blank = every region. Don\'t list all regions: leave it blank'),
      ('Comments', 'Utf8', None, False, None, 30, 'Not used by the tool'),
  ]),

  dict(spec=SPEC_INURING, sheet='Inuring Layers', table='InuringLayers', keys='', columns=[
      (L_STAGE, 'Float64', None, True, None, 8, 'Order of application; same stage + same subject = one tower'),
      (L_LAYER, 'Utf8', None, True, None, 16, ''),
      (L_RET, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_LIM, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_PLACE, 'Float64', 1.0, False, None, 11, 'Fraction between 0 and 1'),
      (L_SUBJ, 'Utf8', None, True, None, 14, 'A Subject from the Subjects sheet'),
      (L_MULTI, 'Utf8', 'TRUE', False, 'lst_Boolean', 14, 'FALSE: no policy in the subject has exposure in more than one region (no split needed)'),
      (L_INCLUDE, 'Utf8', 'TRUE', False, 'lst_Boolean', 9, ''),
  ]),

  dict(spec=SPEC_UPPER, sheet='Upper Layers', table='UpperLayers', keys='Layer', columns=[
      (L_LAYER, 'Utf8', None, True, None, 14, 'The layers whose premium is allocated'),
      (L_RET, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_LIM, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_SUBJ, 'Utf8', None, True, None, 12, 'A Subject from the Subjects sheet'),
      (L_PREM, 'Float64', None, True, None, 14, '100% upfront premium'),
      (L_PLACE, 'Float64', 1.0, False, None, 11, 'Fraction between 0 and 1; Premium x Placement % is allocated'),
      (L_BASIS, 'Utf8', None, False, 'lst_Bases', 12, 'Blank = the run\'s Basis; a value here applies in every run'),
      (L_REINST, 'Float64', 0.0, False, None, 9, 'Aggregate basis'),
      (L_RRATE, 'Float64', 1.0, False, None, 11, 'Aggregate basis: fraction of premium per full reinstatement'),
      (L_AGGLIM, 'Float64', None, False, None, 13, 'Aggregate basis: default Limit x (1 + Reinstatements)'),
      (L_AGGDED, 'Float64', 0.0, False, None, 12, 'Aggregate basis'),
      (L_DIST, 'Utf8', None, False, 'lst_Distortions', 11, 'Blank = the run\'s Distortion; a value here applies in every run'),
      (L_THETA, 'Float64', None, False, None, 9, 'Fixed parameter (skips calibration)'),
      (L_MULTI, 'Utf8', 'TRUE', False, 'lst_Boolean', 14, 'As for inuring layers'),
      (L_INCLUDE, 'Utf8', 'TRUE', False, 'lst_Boolean', 9, ''),
  ]),
]

BY_TABLE = {t['table']: t for t in TABLES}
BY_SPEC = {t['spec']: t for t in TABLES}

# ---------------------------------------------------------------------------------------------
# Result tables: (sheet, table, [(column, width)])
# ---------------------------------------------------------------------------------------------
RESULT_TABLES = [
  dict(sheet='Run Summary', table='RunSummary', columns=[
      ('Run Name', 22), ('Status', 10), ('Distortion', 10), ('Basis', 11), ('Inuring Sharing', 12),
      ('Policy Split', 11), ('Bounded Split', 9), ('Clamp Negatives', 9), ('Include Reinstatement Premium', 12),
      ('Grid Points', 8), ('Placed Premium', 14), ('Allocated', 14), ('Policies', 10), ('Run Time (s)', 9),
      ('Message', 70)]),
  dict(sheet='Layer Summary', table='LayerSummary', columns=[
      ('Run Name', 22), ('Layer', 14), ('Basis', 11), ('Distortion', 10), ('Theta', 9), ('Premium 100%', 14),
      ('Placement', 11), ('Placed Premium', 14), ('Calibration Target 100%', 16),
      ('Expected Loss 100%', 15), ('Load', 7), ('P(Attach)', 9), ('P(Exhaust)', 10),
      ('Agg Limit', 13), ('Exp. Reinstatement Premium 100%', 16), ('Allocated', 14),
      ('Policies', 10), ('Events', 9), ('Bounded Rows', 11), ('Negative Share Before Fix', 12)]),
  dict(sheet='LOB Summary', table='LOBSummary', columns=[
      ('Run Name', 22), ('LOBNAME', 16), ('Layer', 14), ('Premium', 14), ('Share of Layer', 12), ('Policies', 10)]),
  dict(sheet='Benchmark', table='Benchmark', columns=[
      ('Run Name', 22), ('LOBNAME', 16), ('AAL Share', 14), ('EL Key, Mean Split', 16), ('EL Key, Conditional Split', 16),
      ('Final', 14), ('Final / AAL Share', 12)]),
  dict(sheet='Diagnostics', table='Diagnostics', columns=[('Run Name', 22), ('Item', 36), ('Value', 18), ('Note', 90)]),
  dict(sheet='Output Log', table='OutputLog', columns=[('Run Name', 22), ('Output', 30), ('Path', 70), ('Rows', 12)]),
]
RESULT_BY_TABLE = {t['table']: t for t in RESULT_TABLES}
