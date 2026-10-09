"""
cca_tables.py
=============
The CCA tables, defined once. Used by tools/build_workbooks.py to build CCA_Template.xlsx and
CCA_ConfigSpecs.xlsx (so the spec definitions always match the template), and by CCA_Functions.py
for the names of the settings and columns.

Input tables list their columns as (name, data type, default, required, dropdown list, width, note).
Data types are Spec Data Formats types (Utf8, Float64, Boolean). Required columns are
"Exclude if Null": rows missing them are dropped by the spec reader.

Info tables (General, Prepare Data) are Information / Value pairs; `settings` lists their rows as
(Information, default, dropdown list, note).

Result tables are written by the run (not read as specs); their columns are fixed, so the run can
fill them in an open workbook (xlwings) or a closed one (modelkit.excel_tablewriter).
"""

# ---------------------------------------------------------------------------------------------
# Choices
# ---------------------------------------------------------------------------------------------
RUN_TYPES = ['Allocate', 'Prepare Data']
DATASETS = ['BU ELT Gross', 'BU ELT Net', 'Exposure', 'Policy LOB']
SOURCETYPES = ['Parquet', 'CSV', 'Excel Table', 'Database Query']
PREP_SOURCETYPES = ['Parquet Files', 'Database Query']
DISTORTIONS = ['wang', 'ph', 'identity']
BASES = ['occurrence', 'aggregate']
SHARINGS = ['conditional', 'prorata']
SPLITS = ['conditional', 'mean']

LISTS = {
    'lst_RunTypes': RUN_TYPES,
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
G_RUNNAME, G_RUNTYPE, G_OUTPUT = 'Run Name', 'Run Type', 'Output Folder'
G_ELTFOLDER, G_ELTPATTERN, G_ELTNET = 'Policy ELT Folder', 'Policy ELT File Pattern', 'Policy ELT Is Net Of Per-Risk'
G_DISTORTION, G_BASIS, G_SHARING, G_SPLIT = 'Distortion', 'Basis', 'Inuring Sharing', 'Policy Split'
G_BOUNDED, G_CLAMP, G_RP = 'Bounded Split', 'Clamp Negatives', 'Include Reinstatement Premium'
G_BENCHMARK, G_GRID, G_NOTES = 'Benchmark Comparison', 'Grid Points', 'Notes'

GENERAL_SETTINGS = [
    (G_RUNNAME, 'Cat cost allocation', None, ''),
    (G_RUNTYPE, 'Allocate', 'lst_RunTypes', 'Allocate, or Prepare Data to build the event-grouped policy ELT files'),
    (G_OUTPUT, 'output', None, 'Folder for result files; relative to this workbook\'s folder'),
    (G_ELTFOLDER, 'ev_buckets', None, 'Event-grouped policy ELT files (Prepare Data writes them here)'),
    (G_ELTPATTERN, 'ev_bucket_*.parquet', None, 'Which files in the folder to read'),
    (G_ELTNET, 'FALSE', 'lst_Boolean', 'TRUE if the policy ELT is already net of per-risk (BU ELT Net is then used for both)'),
    (G_DISTORTION, 'wang', 'lst_Distortions', 'wang; ph (proportional hazards, a sensitivity); identity (expected-loss key)'),
    (G_BASIS, 'occurrence', 'lst_Bases', 'occurrence (layer OEP) or aggregate (annual recoveries); a layer can override it'),
    (G_SHARING, 'conditional', 'lst_Sharings', 'How inuring recoveries are shared: conditional (co-recovery) or prorata'),
    (G_SPLIT, 'conditional', 'lst_Splits', 'How an event\'s weight is split across its policies'),
    (G_BOUNDED, 'TRUE', 'lst_Boolean', 'Bound the conditional split so no policy gets a negative share'),
    (G_CLAMP, 'FALSE', 'lst_Boolean', 'Alternative: zero negatives within each event and rescale the rest'),
    (G_RP, 'TRUE', 'lst_Boolean', 'Aggregate basis: calibrate to premium + expected reinstatement premium'),
    (G_BENCHMARK, 'FALSE', 'lst_Boolean', 'Also run the expected-loss keys and AAL share, for the Benchmark sheet (about 3x the run time)'),
    (G_GRID, '500', None, 'Grid points for calibration'),
    (G_NOTES, None, None, ''),
]

P_SOURCETYPE, P_FOLDER, P_PATTERN = 'Source Type', 'Input Folder', 'Input File Pattern'
P_CONNECTION, P_QUERY, P_TARGET = 'Connection', 'Query', 'Target Rows Per File'
P_RENAMES, P_KEEPRAW = 'Column Renames', 'Keep Query Chunks'

PREPARE_SETTINGS = [
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
      (L_MULTI, 'Boolean', 'TRUE', False, 'lst_Boolean', 14, 'FALSE: no policy in the subject has exposure in more than one region (no split needed)'),
      (L_INCLUDE, 'Boolean', 'TRUE', False, 'lst_Boolean', 9, ''),
  ]),

  dict(spec=SPEC_UPPER, sheet='Upper Layers', table='UpperLayers', keys='Layer', columns=[
      (L_LAYER, 'Utf8', None, True, None, 14, 'The layers whose premium is allocated'),
      (L_RET, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_LIM, 'Float64', None, True, None, 14, 'Per occurrence'),
      (L_SUBJ, 'Utf8', None, True, None, 12, 'A Subject from the Subjects sheet'),
      (L_PREM, 'Float64', None, True, None, 14, '100% upfront premium'),
      (L_PLACE, 'Float64', 1.0, False, None, 11, 'Fraction between 0 and 1; Premium x Placement % is allocated'),
      (L_BASIS, 'Utf8', None, False, 'lst_Bases', 12, 'Blank = General setting'),
      (L_REINST, 'Float64', 0.0, False, None, 9, 'Aggregate basis'),
      (L_RRATE, 'Float64', 1.0, False, None, 11, 'Aggregate basis: fraction of premium per full reinstatement'),
      (L_AGGLIM, 'Float64', None, False, None, 13, 'Aggregate basis: default Limit x (1 + Reinstatements)'),
      (L_AGGDED, 'Float64', 0.0, False, None, 12, 'Aggregate basis'),
      (L_DIST, 'Utf8', None, False, 'lst_Distortions', 11, 'Blank = General setting'),
      (L_THETA, 'Float64', None, False, None, 9, 'Fixed parameter (skips calibration)'),
      (L_MULTI, 'Boolean', 'TRUE', False, 'lst_Boolean', 14, 'As for inuring layers'),
      (L_INCLUDE, 'Boolean', 'TRUE', False, 'lst_Boolean', 9, ''),
  ]),
]

BY_TABLE = {t['table']: t for t in TABLES}
BY_SPEC = {t['spec']: t for t in TABLES}

# ---------------------------------------------------------------------------------------------
# Result tables: (sheet, table, [(column, width)])
# ---------------------------------------------------------------------------------------------
RESULT_TABLES = [
  dict(sheet='Layer Summary', table='LayerSummary', columns=[
      ('Layer', 14), ('Basis', 11), ('Distortion', 10), ('Theta', 9), ('Premium 100%', 14),
      ('Placement', 11), ('Placed Premium', 14), ('Calibration Target 100%', 16),
      ('Expected Loss 100%', 15), ('Load', 7), ('P(Attach)', 9), ('P(Exhaust)', 10),
      ('Agg Limit', 13), ('Exp. Reinstatement Premium 100%', 16), ('Allocated', 14),
      ('Policies', 10), ('Events', 9), ('Bounded Rows', 11), ('Negative Share Before Fix', 12)]),
  dict(sheet='LOB Summary', table='LOBSummary', columns=[
      ('LOBNAME', 16), ('Layer', 14), ('Premium', 14), ('Share of Layer', 12), ('Policies', 10)]),
  dict(sheet='Benchmark', table='Benchmark', columns=[
      ('LOBNAME', 16), ('AAL Share', 14), ('EL Key, Mean Split', 16), ('EL Key, Conditional Split', 16),
      ('Final', 14), ('Final / AAL Share', 12)]),
  dict(sheet='Diagnostics', table='Diagnostics', columns=[('Item', 36), ('Value', 18), ('Note', 90)]),
  dict(sheet='Output Log', table='OutputLog', columns=[('Output', 30), ('Path', 70), ('Rows', 12)]),
]
RESULT_BY_TABLE = {t['table']: t for t in RESULT_TABLES}
