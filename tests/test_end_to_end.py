"""
End to end through the platform (shared-lib main.main), as the exe's "Run Analysis on Closed Excel
File" does, on the example workbook:

  1. one click: Prepare Data, then five runs (Base, PH, aggregate basis, pro rata sharing, mean split)
  2. runs only (Prepare Data off), reading the files written in 1
  3. one run failing: the others still finish and the failure is on the Run Summary
  4. a workbook with planted mistakes: all reported, nothing run

Results are compared with the engine called directly on the same data.

Runs in a temporary copy of the repo (the platform writes its log to C:\\CCA, which off Windows is a
folder named 'C:\\CCA' in the working directory).
"""
import copy, glob, json, os, shutil, subprocess, sys, tempfile

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
fail = os.environ.get('CCA_FAIL_RUN')
if fail:                                        # make one run fail, to test that the others carry on
    import CCA_Functions as F
    real = F._runAllocation
    def patched(analysis, p, inp, run, status, results):
        if run['name'] == fail:
            raise RuntimeError('planted failure')
        return real(analysis, p, inp, run, status, results)
    F._runAllocation = patched
a = main.main()
res = getattr(a, 'results', None) or {}
out = {'error': getattr(a, 'error', 'no analysis'), 'runs': {}}
for name, r in res.items():
    r['by_policy'].write_parquet(os.path.join(sys.argv[2], name + '.parquet'))
    out['runs'][name] = {'total': float(r['by_policy']['PREMIUM'].sum()),
                         'ratio': {k: v[1] for k, v in r['ratio_check'].items()},
                         'bench': r['benchmark'] is not None, 'lookups': id(r['lookups'])}
if getattr(a, 'manifest', None) is not None:
    out['files'] = a.manifest.height
print('RESULT ' + json.dumps(out))
'''
open('run_once.py', 'w').write(RUN)


def run(book, env=None):
    outdir = tempfile.mkdtemp()
    p = subprocess.run([sys.executable, 'run_once.py', book, outdir], capture_output=True, text=True,
                       env={**os.environ, **(env or {})})
    line = [l for l in p.stdout.splitlines() if l.startswith('RESULT ')]
    if not line:
        print(p.stdout[-3000:], p.stderr[-3000:])
        raise SystemExit('run failed')
    return json.loads(line[0][7:]), outdir


def table(book, name):
    from python_calamine import CalamineWorkbook
    t = CalamineWorkbook.from_path(book, load_tables=True).get_table_by_name(name)
    return [list(t.columns)] + t.to_python()


sys.path[:0] = [root, os.path.join(root, 'shared-lib'), os.path.join(root, 'tools')]
import polars as pl
from build_workbooks import build_template, EXAMPLE, VBA_PROJECT
from make_example_data import make
make()
placed = sum(row[4] * row[5] for row in EXAMPLE['UpperLayers'])
runs = [r[0] for r in EXAMPLE['SpecifyRuns'] if r[1] == 'TRUE']

# 1. Prepare Data and five runs in one click (the example workbook as built, with its VBA)
build_template('example.xlsm', EXAMPLE, vba=VBA_PROJECT)
r, out1 = run('example.xlsm')
files = sorted(glob.glob('example_data/ev_buckets/ev_bucket_*.parquet'))
print(f"1. Prepare Data + runs: error {r['error']!r}; {r.get('files')} files written ({len(files)} on disk)")
for name in runs:
    x = r['runs'][name]
    print(f"   {name:<17} total {x['total']:>13,.2f} (placed {placed:,.2f}); policies/cells "
          f"{min(x['ratio'].values()):.4f}-{max(x['ratio'].values()):.4f}; benchmark {x['bench']}")
print(f"   lookups shared by Base and Mean split: {r['runs']['Base']['lookups'] == r['runs']['Mean split']['lookups']}; "
      f"distinct lookups: {len({x['lookups'] for x in r['runs'].values()})} for {len(runs)} runs")
summ = table('example.xlsm', 'RunSummary')
print("   Run Summary:", [(row[0], row[1]) for row in summ[1:]])
for t in ('LayerSummary', 'LOBSummary', 'Benchmark', 'Diagnostics', 'OutputLog'):
    rows = table('example.xlsm', t)
    print(f"   {t}: {len(rows) - 1} rows; runs {sorted({row[0] for row in rows[1:]})}")
print("   output folder:", sorted(os.listdir('example_data/output')))
z = __import__('zipfile').ZipFile('example.xlsm')
print("   VBA still in the workbook after the run:", 'xl/vbaProject.bin' in z.namelist())

# 2. the engine directly on the same data, for Base (conditional) and Mean split
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
for name, kw, method in [('Base', {}, 'conditional'), ('Mean split', {}, 'mean'),
                         ('PH sensitivity', {'distortion': 'ph'}, 'conditional'),
                         ('Aggregate basis', {'basis': 'aggregate'}, 'conditional'),
                         ('Pro rata sharing', {'sharing': 'prorata'}, 'conditional')]:
    lk = prepare_lookups(exp.select('POLICYID', 'REGION', 'TIV'), gross, net, stages, up, subj,
                         exp.select('POLICYID', 'LOBNAME'), verbose=False, **kw)
    _, ref, _, _ = run_allocation('example_data/ev_buckets', None, None, None, None, None, subj,
                                  pattern='ev_bucket_*.parquet', lookups=lk, method=method, verbose=False)
    got = pl.read_parquet(os.path.join(out1, name + '.parquet'))
    c = ref.join(got, on='POLICYID', how='full', coalesce=True, suffix='_APP').fill_null(0)
    print(f"2. {name:<17} through the workbook vs engine directly: max |diff| per policy "
          f"{(c['PREMIUM'] - c['PREMIUM_APP']).abs().max():.2e}")
allp = pl.read_parquet('example_data/output/premium_by_policy_all_runs.parquet')
print(f"   premium_by_policy_all_runs: {allp.height} policies x {allp.width - 1} runs; column sums equal placed: "
      f"{all(abs(allp[n].sum() - placed) < 1e-6 for n in runs)}")

# 3. runs only, and one of them failing
ex = copy.deepcopy(EXAMPLE)
ex['PrepareSpecs']['Run Prepare Data'] = 'FALSE'
build_template('runs.xlsm', ex, vba=VBA_PROJECT)
r, _ = run('runs.xlsm', env={'CCA_FAIL_RUN': 'PH sensitivity'})
summ = table('runs.xlsm', 'RunSummary')
print(f"3. Runs only, PH sensitivity made to fail: {len(r['runs'])} runs finished; Run Summary "
      f"{[(row[0], row[1]) for row in summ[1:]]}")
print("   error:", r['error'].replace('\n', ' | '))

# 4. planted mistakes: all reported, nothing run
bad = copy.deepcopy(EXAMPLE)
bad['PrepareSpecs']['Run Prepare Data'] = 'maybe'
bad['SpecifyRuns'] = [['Base', 'TRUE', 'wangg'], ['base', 'TRUE'], ['A/B', 'TRUE', None, 'annual'],
                      [None, 'TRUE', 'ph'], ['Coarse', 'TRUE', None, None, None, None, 'perhaps', None, None, None, 10]]
bad['DataSources'] = [r_ for r_ in bad['DataSources'] if r_[0] != 'Exposure']
bad['InuringLayers'][0] = [1, 'FHCF', 5e6, 20e6, 90, 'FHCF', 'FALSE', 'TRUE']
bad['UpperLayers'] = bad['UpperLayers'] + [['U4', 1e6, 2e6, 'NOPE', -5, 1.0]]
build_template('bad.xlsm', bad, vba=VBA_PROJECT)
r, _ = run('bad.xlsm')
print("4. Broken workbook:", r['error'])
print("   runs done:", len(r['runs']))
