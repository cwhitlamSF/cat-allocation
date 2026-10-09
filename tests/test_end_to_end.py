"""
End to end through the platform (shared-lib main.main), as the exe's "Run Analysis on Closed Excel
File" does: Prepare Data, then Allocate, on the example workbook; then a workbook with planted
mistakes. Results are compared with the engine called directly on the same data.

Runs in a temporary copy of the repo (the platform writes its log to C:\\CCA, which off Windows is a
folder named 'C:\\CCA' in the working directory).
"""
import copy, glob, os, shutil, subprocess, sys, tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
work = tempfile.mkdtemp()
root = os.path.join(work, 'cca')
shutil.copytree(REPO, root, ignore=shutil.ignore_patterns('.git', '__pycache__', 'output', 'ev_buckets'))
os.chdir(root)

RUN = r'''
import os, sys, json
sys.path[:0] = ['shared-lib', '.']
import main
main.tkinterSelectFromList = lambda options, **k: "Run Analysis on Closed Excel File"
main.selectAnalysisFile = lambda *a, **k: os.path.abspath(sys.argv[1])
main.DEVELOPERMODE = True
a = main.main()
res = getattr(a, 'results', None) or {}
out = {'error': getattr(a, 'error', 'no analysis'), 'total': None}
if res:
    bp = res['by_policy']
    bp.write_parquet(sys.argv[2])
    out['total'] = float(bp['PREMIUM'].sum())
    out['ratio'] = {k: v[1] for k, v in res['ratio_check'].items()}
    out['bench'] = res['benchmark'] is not None
if getattr(a, 'manifest', None) is not None:
    out['files'] = a.manifest.height
print('RESULT ' + json.dumps(out))
'''
open('run_once.py', 'w').write(RUN)

def run(book, out='result.parquet'):
    p = subprocess.run([sys.executable, 'run_once.py', book, out], capture_output=True, text=True)
    line = [l for l in p.stdout.splitlines() if l.startswith('RESULT ')]
    if not line:
        print(p.stdout[-3000:], p.stderr[-3000:])
        raise SystemExit('run failed')
    import json
    return json.loads(line[0][7:])

sys.path[:0] = [root, os.path.join(root, 'shared-lib'), os.path.join(root, 'tools')]
import polars as pl
from build_workbooks import build_template, EXAMPLE
from make_example_data import make
make()

# 1. Prepare Data
ex = copy.deepcopy(EXAMPLE)
ex['GeneralSpecs']['Run Type'] = 'Prepare Data'
build_template('prep.xlsx', ex)
r = run('prep.xlsx')
files = sorted(glob.glob('example_data/ev_buckets/ev_bucket_*.parquet'))
print(f"1. Prepare Data: error {r['error']!r}; {r.get('files')} files written; on disk {len(files)}")

# 2. Allocate (the example workbook as built, with Benchmark on)
build_template('alloc.xlsx', EXAMPLE)
r = run('alloc.xlsx', 'alloc.parquet')
placed = sum(row[4] * row[5] for row in EXAMPLE['UpperLayers'])
print(f"2. Allocate: error {r['error']!r}; total {r['total']:,.2f} vs placed {placed:,.2f}; "
      f"policies/cells median {r['ratio']}; benchmark {r['bench']}")

# result tables written into the closed workbook
from python_calamine import CalamineWorkbook
wb = CalamineWorkbook.from_path('alloc.xlsx', load_tables=True)
for t in ('LayerSummary', 'LOBSummary', 'Diagnostics', 'Benchmark', 'OutputLog'):
    print(f"   {t}: {len(wb.get_table_by_name(t).to_python())} rows")
print("   LayerSummary first row:", wb.get_table_by_name('LayerSummary').to_python()[0][:8])
print("   outputs:", sorted(os.path.basename(f) for f in glob.glob('example_data/output/*')))

# 3. the engine directly on the same data (no workbook, no hooks)
from catalloc.inuring import make_subject, stages_from_table
from catalloc.driver import prepare_lookups, run_allocation
gross = pl.read_parquet('example_data/bu_elt_gross.parquet').with_columns(
    pl.concat_str(['COUNTRY', 'STATE'], separator='_').alias('REGION')).drop('COUNTRY', 'STATE')
net = pl.read_parquet('example_data/bu_elt_net.parquet')
exp = pl.read_csv('example_data/exposure.csv').rename({'PolicyID': 'POLICYID'})
subj = {'ALL': make_subject([]), 'HO': make_subject([('HO', [])]), 'CO': make_subject([('CO', [])]),
        'FHCF': make_subject([('HO', ['US_FL'])])}
inu = pl.DataFrame(EXAMPLE['InuringLayers'], schema=['Stage', 'Layer', 'Retention', 'Limit', 'Placement %',
                                                     'Subject', 'Multiple Regions Per Policy', 'Include'], orient='row')
stages = stages_from_table(inu.drop('Include'), subj)
up = pl.DataFrame([dict(Layer=u[0], Retention=u[1], Limit=u[2], Subject=u[3], Premium=u[4], **{'Placement %': u[5]},
                        Basis=u[6], Reinstatements=u[7]) for u in EXAMPLE['UpperLayers']],
                  schema_overrides={'Basis': pl.Utf8})
lk = prepare_lookups(exp.select('POLICYID', 'REGION', 'TIV'), gross, net, stages, up, subj,
                     exp.select('POLICYID', 'LOBNAME'), verbose=False)
_, ref, _, _ = run_allocation('example_data/ev_buckets', None, None, None, None, None, subj,
                              pattern='ev_bucket_*.parquet', lookups=lk, verbose=False)
got = pl.read_parquet('alloc.parquet')
c = ref.join(got, on='POLICYID', how='full', coalesce=True, suffix='_APP').fill_null(0)
print(f"3. Through the workbook vs engine directly: max |diff| per policy {(c['PREMIUM'] - c['PREMIUM_APP']).abs().max():.2e}")

# 4. a workbook with planted mistakes: all reported, nothing run
bad = copy.deepcopy(EXAMPLE)
bad['GeneralSpecs'].update({'Distortion': 'wangg', 'Bounded Split': 'maybe'})
bad['DataSources'] = [r for r in bad['DataSources'] if r[0] != 'Exposure'] + [['Exposures', 'CSV', 'x.csv']]
bad['DataSources'][0] = ['BU ELT Gross', 'Parquet', 'example_data/missing.parquet']
bad['InuringLayers'][0] = [1, 'FHCF', 5e6, 20e6, 90, 'FHCF', 'FALSE', 'TRUE']             # placement as a percent
bad['UpperLayers'] = bad['UpperLayers'] + [['U4', 1e6, 2e6, 'NOPE', -5, 1.0, 'annual', 1.5]]
build_template('bad.xlsx', bad)
r = run('bad.xlsx')
print("4. Broken workbook:", r['error'])
