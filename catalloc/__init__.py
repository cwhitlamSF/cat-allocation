"""
catalloc: policy-level allocation of cat tower premium from RMS ELTs, without simulation.

    distortion  event weights for a layer: distortion-calibrated (Wang / PH / identity),
                occurrence or aggregate basis; the policy split within an event
    inuring     subjects, inuring towers (co-recovery sharing), per-risk ratios, regions
    driver      chunked runner over event-grouped policy ELT files:
                prepare_lookups(), run_allocation()
    prepare     builds the event-grouped policy ELT files from Parquet chunks or a query
"""
