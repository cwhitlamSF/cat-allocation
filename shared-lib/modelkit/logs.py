# modelkit/logs.py
"""
Data warnings: a DATA log level and a handler that records DATA messages in
<output folder>/data_warnings.csv.

Model code logs warnings normally:

    from modelkit.logs import DATA
    log.log(DATA, "State XY not in rate table; defaulted to 1.0", extra={"spec": "State Factors"})

The CSV is independent of how the run was started: Excel imports it at the end
of the run, the browser viewer shows it as a table, and it stays with the outputs.
"""

import csv
import datetime as dt
import logging
from pathlib import Path

import polars as pl

DATA = 25                                   # between INFO (20) and WARNING (30)
logging.addLevelName(DATA, "DATA")

DATA_WARNINGS_FILE = "data_warnings.csv"
DATA_WARNING_FIELDS = ["Time", "Source", "Spec", "Message"]


class DataWarningCSV(logging.Handler):
    """Append DATA-level records to <folder>/data_warnings.csv (created with a header row)."""

    def __init__(self, folder, overwrite: bool = True):
        super().__init__(level=DATA)
        self.path = Path(folder) / DATA_WARNINGS_FILE
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow(DATA_WARNING_FIELDS)

    def emit(self, record):
        if record.levelno != DATA:
            return
        try:
            with open(self.path, "a", newline="", encoding="utf-8-sig") as f:
                csv.writer(f).writerow([
                    dt.datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
                    record.name,
                    getattr(record, "spec", ""),
                    record.getMessage(),
                ])
        except Exception:
            self.handleError(record)


def readDataWarnings(path) -> pl.DataFrame:
    """Read a data_warnings.csv (or the folder containing it). Empty frame if there is none."""
    path = Path(path)
    if path.is_dir():
        path = path / DATA_WARNINGS_FILE
    if not path.exists():
        return pl.DataFrame({c: [] for c in DATA_WARNING_FIELDS}, schema={c: pl.Utf8 for c in DATA_WARNING_FIELDS})
    return pl.read_csv(path, infer_schema_length=0, encoding="utf8-lossy")


def countDataWarnings(path) -> int:
    return readDataWarnings(path).height
