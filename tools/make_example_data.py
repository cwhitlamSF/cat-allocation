"""
Writes a small synthetic book to example_data/ for CCA_Example.xlsx:

  bu_elt_gross.parquet   BU x region ELT, gross of per-risk (COUNTRY and STATE columns, no REGION)
  bu_elt_net.parquet     the same, net of per-risk (REGION column)
  exposure.csv           PolicyID, LOBNAME, REGION, TIV: one row per policy x region; also used as
                         the policy -> LOB map
  raw_policy_elt/        the gross policy ELT in shuffled chunks, with columns PolicyID and Loss
                         (Prepare Data renames them and groups the rows by event)

HO policies each sit in one state (so the FHCF layer can be flagged single-region); CO policies
can span several states. Deterministic (fixed seed).

    python tools/make_example_data.py
"""
import os
import shutil

import numpy as np
import polars as pl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'example_data')


def make(out=OUT, n_pol=400, n_ev=500, seed=5):
    rng = np.random.default_rng(seed)
    regions = ["US_FL", "US_GA", "US_SC", "US_NC", "US_TX", "US_LA"]
    exp_rows, lob = [], {}
    for p in range(1, n_pol + 1):
        lob[p] = "HO" if rng.random() < 0.55 else "CO"
        k = 1 if lob[p] == "HO" else int(rng.integers(1, 4))
        for r in rng.choice(regions, k, replace=False):
            exp_rows.append((p, lob[p], str(r), float(rng.lognormal(16, 1))))
    exposure = pl.DataFrame(exp_rows, schema=["PolicyID", "LOBNAME", "REGION", "TIV"], orient="row")

    rate = rng.gamma(0.5, 0.004, n_ev)
    rows = []
    for e in range(1, n_ev + 1):
        start = int(rng.integers(0, len(regions)))
        hit = {regions[(start + k) % len(regions)]: rng.lognormal(-5.5, 1.2) for k in range(int(rng.integers(1, 4)))}
        for p, l, r, tiv in exp_rows:
            if r in hit and rng.random() < 0.8:
                m = tiv * hit[r] * rng.lognormal(0, 0.5)
                rows.append((e, p, l, r, m, m * rng.uniform(.3, .9), m * rng.uniform(.2, .6)))
    truth = pl.DataFrame(rows, orient="row", schema=["EVENTID", "POLICYID", "LOBNAME", "REGION",
                                                     "PERSPVALUE", "STDDEVI", "STDDEVC"])
    rms = [pl.col("PERSPVALUE").sum(), (pl.col("STDDEVI") ** 2).sum().sqrt().alias("STDDEVI"),
           pl.col("STDDEVC").sum()]
    policy_elt = truth.group_by(["EVENTID", "POLICYID"]).agg(rms)
    rate_df = pl.DataFrame({"EVENTID": np.arange(1, n_ev + 1), "RATE": rate})
    gross = (truth.group_by(["EVENTID", "LOBNAME", "REGION"]).agg(rms)
             .with_columns((pl.col("PERSPVALUE") * 8).alias("EXPVALUE"))
             .join(rate_df, on="EVENTID").sort(["EVENTID", "LOBNAME", "REGION"]))
    f = pl.Series(rng.uniform(0.7, 1.0, gross.height))
    net = gross.with_columns(*(pl.col(c) * f for c in ["PERSPVALUE", "STDDEVC", "STDDEVI", "EXPVALUE"]))

    if os.path.exists(out):
        for name in ("raw_policy_elt", "ev_buckets", "output"):
            shutil.rmtree(os.path.join(out, name), ignore_errors=True)
    os.makedirs(os.path.join(out, "raw_policy_elt"), exist_ok=True)
    (gross.with_columns(pl.col("REGION").str.split_exact("_", 1).struct.rename_fields(["COUNTRY", "STATE"])
                        .alias("_S")).unnest("_S").drop("REGION")
     .write_parquet(os.path.join(out, "bu_elt_gross.parquet")))
    net.write_parquet(os.path.join(out, "bu_elt_net.parquet"))
    exposure.write_csv(os.path.join(out, "exposure.csv"))
    shuffled = policy_elt.sample(fraction=1.0, shuffle=True, seed=1).rename({"POLICYID": "PolicyID",
                                                                             "PERSPVALUE": "Loss"})
    for i, part in enumerate(shuffled.iter_slices(n_rows=25_000)):
        part.write_parquet(os.path.join(out, "raw_policy_elt", f"chunk_{i:03d}.parquet"))
    return dict(policy_elt=policy_elt, gross=gross, net=net, exposure=exposure)


if __name__ == '__main__':
    d = make()
    print(f"Wrote example_data/: {d['policy_elt'].height:,} policy-event rows, "
          f"{d['gross'].height:,} BU x region cells, {d['exposure'].height:,} exposure rows")
