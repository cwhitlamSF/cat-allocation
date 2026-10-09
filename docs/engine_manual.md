# Cat Cost Allocation: Engine Manual

> For running the tool from Excel, see the README. This manual covers using the engine (`catalloc`) directly from Python, e.g. in a notebook. Modules: `oep_cotvar.py` is now `catalloc/distortion.py`, so import `from catalloc.driver import prepare_lookups, run_allocation`.

How to run the policy-level allocation of cat tower premium: the inputs it needs, how to call it, what comes out, how to check it, and what to do when something goes wrong. The method itself is described in the technical document; this manual is about using the code.

---

## 1. What the code does

For each layer of the upper towers, the code allocates the layer's **placed upfront premium** (100% premium × placement) to individual policies. It works from RMS ELTs, without simulation:

1. **Per-risk.** Policy losses are moved to a net-of-per-risk basis using BU-level net/gross ratios for each event.
2. **Regions.** Policies in region-sensitive lines of business get a region (or are split across regions) from the exposure data.
3. **Inuring.** The inuring towers (lower towers, the FHCF proxy) are applied in stage order. Each tower's recovery is shared among the policies in its subject by co-recovery.
4. **Event weights.** For each upper layer, each event gets a weight: its contribution to the layer, with extra weight on rare severe outcomes. The weighting comes from a distortion (Wang by default) calibrated so that the weights add up to the layer's 100% premium. This can be done on an **occurrence** basis (layer OEP) or an **aggregate** basis (annual recoveries after reinstatements and aggregate terms).
5. **Policy split.** Each event's weight is split across the policies it hits, with the **conditional** split (by default bounded so no policy gets a negative share).
6. **Scaling.** Contributions are summed per policy and scaled so each layer's policies add up to that layer's placed premium.

---

## 2. Setup

**Modules.** Keep these three in one folder and always replace them together:

| File | Contents |
|---|---|
| `catalloc/distortion.py` | Event weights (distortion, calibration, occurrence and aggregate bases), policy split |
| `catalloc/inuring.py` | Subjects, inuring towers, per-risk ratios, region handling |
| `catalloc/driver.py` | Chunked runner: `prepare_lookups`, `run_allocation` |

The test scripts (`test_*.py`) are optional. They rerun the validation checks.

**Packages:** `polars`, `numpy`, `scipy`, `pyarrow`.

**After copying in new module files, restart the kernel.** Re-importing in a running session can keep the old version. To check which version is loaded:

```python
import inspect
from catalloc import distortion as oep_cotvar, driver
print("basis" in inspect.signature(oep_cotvar.layer_event_weights).parameters,
      "stream" in inspect.signature(driver.run_allocation).parameters)      # both True
```

---

## 3. Inputs

### 3.1 BU × region ELTs

These are two Polars DataFrames, one gross of per-risk and one net of per-risk, each with one row per event × LOB × region.

| Column | Meaning |
|---|---|
| `EVENTID` | RMS event ID |
| `LOBNAME` | Business unit |
| `REGION` | `COUNTRY_STATE`, e.g. `US_FL` |
| `RATE` | Annual event rate |
| `PERSPVALUE` | Mean loss |
| `STDDEVI`, `STDDEVC` | Independent and correlated SD |
| `EXPVALUE` | Exposed value (used as the loss bound) |

**Include every peril the towers cover** (HU, SCS, and any others) in one table. Event IDs must be unique across perils. The occurrence OEP and the annual loss both run across all perils, so running perils separately gives the wrong answer.

### 3.2 Policy ELT (event-grouped Parquet files)

The policy ELT is a folder of Parquet files, **grouped by event**, so that every row for a given event is in one file. Each file has:

| Column | Notes |
|---|---|
| `EVENTID`, `POLICYID` | Exact names, upper case. Int32 is fine. |
| `PERSPVALUE`, `STDDEVI`, `STDDEVC` | Float32 is fine. Values are upcast to 64-bit internally. |

Other columns (RATE, EXPVALUE, LOB, POLICYNUM) aren't needed and aren't read.

The code assumes the policy ELT is **gross of per-risk**. If yours is already net, pass the net BU × region ELT as **both** `gross_cells` and `cells`. That sets every per-risk ratio to 1 (see section 4).

With event-grouped files, the run uses a single pass and writes nothing to disk. Files of 2–5M rows work well. More files don't increase memory use, but larger files do.

### 3.3 Policy → LOB map (`policy_lob`)

This is a table with `POLICYID` and `LOBNAME`. Duplicate rows (for example, one per location) are tolerated by the driver. A policy can't have two different LOBs. **LOB names must match the ELTs exactly**, including capitalisation and trailing spaces.

> When you join this map onto results yourself, use `pol_lob.unique("POLICYID")` first. Otherwise every result row is repeated once per row of the map, and totals are inflated.

### 3.4 Exposure

This is a table with `POLICYID`, `REGION` and `TIV`, at any grain (it's summed to policy × region). It's only needed if some subject restricts regions, such as FHCF (HO in Florida). Policies with zero or null TIV in every region are dropped from region-sensitive lines and counted in `diag["rows_no_exposure"]`.

### 3.5 Subjects

A subject is a dictionary of named subject definitions, each built with `make_subject`:

```python
from catalloc.inuring import make_subject
subj = {
    "ALL":  make_subject([]),                       # every LOB, every region
    "HO":   make_subject([("HO", [])]),             # HO, all regions
    "FHCF": make_subject([("HO", ["US_FL"])]),      # HO in Florida only
    "PROP": make_subject([("HO", []), ("CO", [])]), # several LOBs
}
```

- **Empty list:** every LOB, every region.
- **Blank LOB (`""`):** every LOB.
- **Empty region list:** every region.
- **Listing regions** restricts that LOB to those regions, and makes the LOB region-sensitive. Don't list all regions explicitly; use an empty list instead.

### 3.6 Inuring layers

One row per inuring layer:

| Column | Meaning |
|---|---|
| `Stage` | Order of application, ascending. Layers in the same stage with the same subject form one tower. |
| `Layer` | Name |
| `Retention`, `Limit` | Per occurrence |
| `Placement %` | Fraction in (0, 1] |
| `Subject` | Key into `subj` |
| `Multiple Regions Per Policy` | Optional. `False` means no policy in the subject has exposure in more than one region, so each policy simply takes its region with no split. Blank means True. |

```python
from catalloc.inuring import stages_from_table
stages = stages_from_table(inuring_df, subj)
```

Towers in one stage must have non-overlapping subjects.

### 3.7 Upper layers (the layers being allocated)

One row per layer:

| Column | Required | Meaning |
|---|---|---|
| `Layer` | yes | Name |
| `Retention`, `Limit` | yes | Per occurrence |
| `Subject` | yes | Key into `subj` |
| `Premium` | yes | **100%** upfront premium (calibrates the weights) |
| `Placement %` | no | Fraction in (0, 1]; default 1. Placed premium = Premium × Placement % is what's allocated. |
| `Basis` | no | `occurrence` or `aggregate`; overrides the `basis` argument |
| `Reinstatements` | no | Number of reinstatements; default 0 |
| `Reinstatement Rate` | no | Fraction of premium per full reinstatement; default 1.0 (100%) |
| `Agg Limit` | no | Annual aggregate limit; default Limit × (1 + Reinstatements) |
| `Agg Deductible` | no | Annual aggregate deductible; default 0 |
| `Distortion` | no | `wang`, `ph` or `identity`; overrides the `distortion` argument for this layer |
| `Theta` | no | Fixed distortion parameter (skips calibration) |
| `Multiple Regions Per Policy` | no | As for inuring layers |

> A `Distortion` or `Theta` column overrides the function arguments. If two runs that should differ give identical results, check for these columns first.

---

## 4. Running

### 4.1 Build the lookups once

The lookups hold everything that doesn't depend on the policy files: per-risk ratios, inuring tower tables, region data, and each layer's calibrated event weights. Building them is the slow step (about a minute for HU; longer for SCS, which has more events).

```python
from catalloc.driver import prepare_lookups, run_allocation

lk = prepare_lookups(exposure, gross_bu_elt, net_bu_elt, stages, upper_layers, subj, pol_lob)
#   policy ELT already net of per-risk?  use  net_bu_elt, net_bu_elt  for the two ELTs
```

Settings fixed when the lookups are built: the two ELTs, exposure, stages, upper layers, subjects, `policy_lob`, `distortion`, `basis`, `sharing`, `split_lobs`, `include_reinstatement_premium`, `n_x`. **Changing any of them means rebuilding the lookups.** When `lookups=` is passed, `run_allocation` ignores its own ELT, exposure and stage arguments.

> Keep `prepare_lookups` and the runs that use it in the same notebook cell, so a changed input always rebuilds the lookups. Stale lookups have been the most common cause of confusing results.

To check what a set of lookups contains:

```python
print([(l["name"], l["info"]["basis"], l["info"]["distortion"], l["info"]["theta"]) for l in lk["layers"]])
```

### 4.2 Trial run, then full run

```python
folder = r"C:\...\ev_buckets"

by_pl, by_pol, layer_info, diag = run_allocation(
    folder, exposure, None, None, None, None, subj,
    pattern="ev_bucket_00[0-1].parquet",     # two files for a trial
    policy_lob=pol_lob, lookups=lk)

by_pl, by_pol, layer_info, diag = run_allocation(
    folder, exposure, None, None, None, None, subj,
    pattern="ev_bucket_*.parquet",           # every file
    policy_lob=pol_lob, lookups=lk)
```

A trial on a few files only covers some events, so its per-policy numbers are partial. Use it to check that the code runs and the diagnostics look sensible, not to judge the allocation.

When the run starts it prints one of:
- `one pass per file, nothing written to disk`: the expected message for event-grouped files.
- `N events span files: two passes with a disk cache`: some events appear in more than one file. The run still works, but writes a compressed cache to the temp folder.

### 4.3 Main options

| Option | Where | Default | Meaning |
|---|---|---|---|
| `distortion` | `prepare_lookups` | `"wang"` | `"wang"`, `"ph"` (proportional hazards, a sensitivity), `"identity"` (expected-loss key) |
| `basis` | `prepare_lookups` | `"occurrence"` | `"occurrence"` (layer OEP) or `"aggregate"` (annual recoveries after aggregate terms) |
| `include_reinstatement_premium` | `prepare_lookups` | `True` | Aggregate basis: calibrate to 100% premium + expected reinstatement premium |
| `sharing` | `prepare_lookups` | `"conditional"` | Inuring recoveries shared by co-recovery (`"conditional"`) or the same percentage for every policy (`"prorata"`) |
| `split_lobs` | `prepare_lookups` | `"auto"` | Which LOBs need regions, read from the subjects. `"all"` or a list overrides. |
| `method` | `run_allocation` | `"conditional"` | Policy split within an event: `"conditional"` or `"mean"` (by mean loss) |
| `positive_split` | `run_allocation` | `True` | Bound the conditional split so no contribution is negative. `False` gives the raw linear split. |
| `clamp_negative` | `run_allocation` | `False` | Alternative fix: zero out negatives within each event and rescale the rest. Needs event-grouped files. |
| `stream` | `run_allocation` | auto | One pass when events don't span files; `False` forces the disk cache |
| `n_x`, `n_jobs` | `prepare_lookups` | 500, all cores | Grid size and threads for calibration |

### 4.4 Occurrence vs aggregate basis

- **Occurrence:** the distortion acts on the probability that the layer's loss from the worst event in a year exceeds each level (the OEP). This is right when each loss occurrence is what matters.
- **Aggregate:** the distortion acts on the probability that the year's total recoveries exceed each level (the AEP), after the aggregate limit (Limit × (1 + reinstatements)) and any aggregate deductible. The calibration target is the 100% premium plus the expected reinstatement premium. A year's cost is shared among its occurrences in proportion to their layer losses.

The two differ only when the aggregate actually binds: working layers, few reinstatements, frequent losses. When the aggregate is tight, weight shifts somewhat from the most severe events toward frequent ones. With one reinstatement at 100% on upper layers, the bases give very similar results.

The basis can be set per layer with the `Basis` column.

---

## 5. Outputs

`run_allocation` returns `(by_policy_layer, by_policy, layer_info, diag)`.

### 5.1 `by_policy_layer`

| Column | Meaning |
|---|---|
| `POLICYID`, `Layer` | |
| `ALLOC` | Policy's contribution on the 100% basis (the layer's contributions add to about its calibration target) |
| `SHARE` | Policy's share of the layer (adds to 1 per layer) |
| `PREMIUM` | Allocated placed premium = SHARE × Premium × Placement % |

### 5.2 `by_policy`

`POLICYID` and `PREMIUM`, summed over layers. The total equals Σ Premium × Placement % across layers.

### 5.3 `layer_info` (one row per layer)

| Column | Meaning |
|---|---|
| `basis`, `distortion`, `theta` | What was used |
| `calibration_target_100pct` | Occurrence: the 100% premium. Aggregate: the 100% premium + expected reinstatement premium. |
| `premium_100pct`, `placement`, `placed_premium` | Premium terms; `placed_premium` is what's allocated |
| `expected_loss_100pct` | Occurrence: expected layer loss. Aggregate: expected annual recovery. |
| `load_ratio` | Calibration target ÷ expected loss |
| `P_attach`, `P_exhaust` | Probability of attaching / exhausting (aggregate basis: of the annual aggregate) |
| `agg_limit`, `expected_reinstatement_premium_100pct` | Aggregate basis only |
| `policy_ALLOC_total` | Sum of policy contributions |
| `negative_rows_before_clamp`, `negative_share_before_clamp` | Negative contributions (0 with the default bounded split) |
| `bounded_rows` | Policy-event rows where the bound changed the split |
| `negative_policies` | Policies with a negative total in the layer |
| `events`, `dropped_share` | Events with weight; share dropped as negligible |

### 5.4 `diag`

| Key | Meaning |
|---|---|
| `rows_in` | Policy-event rows read (for events with weight in some layer) |
| `rows_no_lob` | Rows whose policy isn't in `policy_lob` (dropped) |
| `rows_split_lobs`, `rows_label_lobs` | Rows in LOBs that are split by region / given a region only |
| `rows_no_exposure`, `mean_no_exposure` | Region-sensitive rows dropped for lack of exposure with TIV > 0 |
| `rows_split_multi_region` | Policy-events split across more than one region |
| `rows_tiv_fallback` | Splits that fell back to TIV-only weights (event showed no loss in the policy's regions) |
| `duplicate_policy_event_rows` | Should be 0 |
| `events_spanning_files` | 0 for event-grouped files |
| `label_policies_multi_region` | Policies flagged single-region whose exposure shows several regions (placed in their largest-TIV region) |

---

## 6. Checking the results

### 6.1 Totals

```python
print(by_pl.group_by("Layer").agg(pl.col("PREMIUM").sum()).sort("Layer"))
print(layer_info.select("Layer", "placed_premium", "calibration_target_100pct", "policy_ALLOC_total"))
```

- Each layer's `PREMIUM` total should equal `placed_premium` exactly.
- On a full run, `policy_ALLOC_total` should be close to `calibration_target_100pct`. A gap means some events' policy rows aren't in the files.

### 6.2 Diagnostics

- `rows_no_lob` and `rows_no_exposure` should be 0 or explainable.
- `duplicate_policy_event_rows` should be 0.
- `negative_share_before_clamp` should be 0 with the default settings.

### 6.3 Do the policy totals match the cells?

For each event and layer, the policy means after per-risk and inuring should add up to the BU × region net mean that the event weights were built on. Check one file:

```python
from catalloc.driver import collapse_chunk
from catalloc.inuring import subject_event_elt
files = sorted(glob.glob(os.path.join(folder, "ev_bucket_*.parquet")))
res, _ = collapse_chunk(pl.read_parquet(files[0]), lk)
for k, l in enumerate(lk["layers"]):
    pol = res.group_by("EVENTID").agg(pl.col(f"M_{k}").sum().alias("POLICIES"))
    cel = subject_event_elt(lk["net_cells"], l["subject"]).select("EVENTID", pl.col("PERSPVALUE").alias("CELLS"))
    r = pol.join(cel, on="EVENTID").with_columns((pl.col("POLICIES") / pl.col("CELLS")).alias("RATIO"))
    print(l["name"], r["RATIO"].quantile(0.05), r["RATIO"].median(), r["RATIO"].quantile(0.95))
```

Ratios should be close to 1. A consistent offset points to per-risk being applied twice (a net policy ELT run with a gross BU ELT), or to policies missing from the policy ELT.

### 6.4 Benchmark ladder

Compare the allocation by LOB (and state) against simpler keys. Each step isolates one effect.

```python
lk_id = prepare_lookups(exposure, gross_bu_elt, net_bu_elt, stages, upper_layers, subj, pol_lob,
                        distortion="identity")
pat = "ev_bucket_*.parquet"
runs = {"EL_MEAN": (lk_id, "mean"), "EL_COND": (lk_id, "conditional"), "WANG_COND": (lk, "conditional")}
out = {}
for name, (L, m) in runs.items():
    _, bp, _, _ = run_allocation(folder, exposure, None, None, None, None, subj, pattern=pat,
                                 policy_lob=pol_lob, lookups=L, method=m, verbose=False)
    out[name] = bp.rename({"PREMIUM": name})

files = sorted(glob.glob(os.path.join(folder, pat)))
rates = gross_bu_elt.select("EVENTID", "RATE").unique("EVENTID").cast({"EVENTID": pl.Int64})
aal = (pl.scan_parquet(files)
       .select(pl.col("EVENTID").cast(pl.Int64), pl.col("POLICYID").cast(pl.Int64), "PERSPVALUE")
       .join(rates.lazy(), on="EVENTID")
       .group_by("POLICYID").agg((pl.col("RATE") * pl.col("PERSPVALUE")).sum().alias("AAL"))
       .collect())
total = out["WANG_COND"]["WANG_COND"].sum()
aal = aal.with_columns((pl.col("AAL") / pl.col("AAL").sum() * total).alias("AAL_SHARE")).drop("AAL")

cmp = aal
for df in out.values():
    cmp = cmp.join(df, on="POLICYID", how="full", coalesce=True)
lob1 = pol_lob.unique("POLICYID").cast({"POLICYID": pl.Int64})
cmp = cmp.fill_null(0).join(lob1, on="POLICYID", how="left")
print(cmp.group_by("LOBNAME").agg(pl.col("AAL_SHARE", "EL_MEAN", "EL_COND", "WANG_COND").sum())
         .sort("WANG_COND", descending=True))
```

| Step | What changes |
|---|---|
| `AAL_SHARE` → `EL_MEAN` | Programme structure: attachments, subjects, per-risk and inuring. Usually the largest move: attritional-heavy LOBs give share to LOBs driving the large events. |
| `EL_MEAN` → `EL_COND` | The conditional split: policies whose losses move with the event total take more. Should be modest. |
| `EL_COND` → `WANG_COND` | Tail weighting: modest on working layers, larger on upper layers. |

A LOB moving in a direction its exposure can't explain is where to look first.

---

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `... got an unexpected keyword argument ...` | Module versions don't match. Replace all three modules together and restart the kernel. |
| Two runs that should differ give identical results | The same `lookups` were reused (the run then ignores its ELT and option arguments), or a `Distortion`/`Theta` column overrode the argument. Rebuild the lookups; check the columns. |
| `Cannot calibrate ...: target ... outside the attainable range` | Premium doesn't fit the layer: check it's the 100% layer premium (not tower, not placed), in the same units as the ELT, with the right subject. The message gives the expected loss and implied rate on line. |
| `No files matching ...` | Pattern doesn't match the file names, e.g. `ev_bucket_*.parquet` vs `ev_chunk_*`. |
| Large `rows_no_exposure` | Policies in region-sensitive LOBs with no TIV > 0 in exposure, null `REGION`, or LOBs unintentionally made region-sensitive by a subject listing regions. Check `lk["split_lobs"]`, `lk["label_lobs"]`. |
| Large `rows_no_lob` | Policies in the ELT missing from `policy_lob`. |
| Totals inflated after your own join | `policy_lob` has repeated rows. Use `pol_lob.unique("POLICYID")`. |
| `unable to find column RATE` in your own code | The bucket files don't carry RATE. Take it from the BU ELT (`gross_bu_elt.select("EVENTID","RATE").unique("EVENTID")`). |
| Disk filling up | Old runs (or runs whose events span files) left caches. Delete `catalloc_*` folders in the temp directory when nothing is running (below). Event-grouped files avoid the cache. |
| Windows: `user-mapped section open` when replacing a file | The file is still memory-mapped. Read with `pl.read_parquet(f, memory_map=False)`, `del` the frame, `gc.collect()`, then replace. |

```python
import glob, os, shutil, tempfile
for d in glob.glob(os.path.join(tempfile.gettempdir(), "catalloc_*")):
    shutil.rmtree(d, ignore_errors=True)
```

---

## 8. Limitations and assumptions

- **Per-risk is applied at BU level.** Every policy in a LOB gets that LOB's event-level net/gross ratio. Policy-level per-risk differences aren't captured until a gross policy ELT is used.
- **Regional split** uses TIV × the event's damage rate in each region. The coastal vs inland split within a state isn't available from state-level ELTs.
- **FHCF** is approximated as an occurrence layer on HO in Florida.
- **The conditional split is a linear approximation.** It models a policy's loss as a straight line in the event total, with slopes from RMS's correlated and independent SDs. In tests on a synthetic comonotone book with some very volatile policies, it was less accurate than the mean split on middle layers. Compare `EL_MEAN` and `EL_COND` by LOB on real data before relying on the difference between them.
- **The bounded split** fixes negative contributions with the smallest change to the split, keeping each event's total. Policies at the bound get no share of that event.
- **Aggregate basis** assumes events occur as independent Poisson processes (RMS's assumption) and pools all perils. A year's cost is shared among its occurrences in proportion to their layer losses.
- **Allocated amount** is the placed upfront premium. On the aggregate basis, the expected reinstatement premium enters the calibration (it's part of the price of the recoveries) but isn't itself allocated. It's reported in `layer_info` if you want to allocate it too, using the same shares.

---

## 9. Validation (test scripts)

| Script | What it checks |
|---|---|
| `test_distortion.py` | Identity distortion reproduces expected layer loss; Wang/PH parameters and event weights match Monte Carlo |
| `test_aggregate.py` | Aggregate basis: identity with no aggregate limit equals occurrence; event weights match 1M simulated years, including when the aggregate binds |
| `test_inuring.py`, `test_corecovery.py` | Inuring moments and co-recovery sharing |
| `test_split.py`, `test_multiflag.py` | Regional split and the Multiple Regions flag |
| `test_driver.py`, `test_basis_driver.py` | Chunked driver equals the in-memory calculation (to about 1e-11), including mixed occurrence/aggregate layers |
| `test_policy_lob.py` | `policy_lob` join, Int32/Float32 files, reused lookups |
| `test_clamp.py`, `test_bounded.py` | Negative handling: clamp, bounded split, one-pass vs cached runs |
