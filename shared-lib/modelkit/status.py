# modelkit/status.py
"""
Run status for whichever front end started the run.

    RunStatus      console / log only (scripts, tests, unattended runs)
    ExcelStatus    Excel status bar during the run; message box at the end;
                   data warnings written to the workbook
    PanelStatus    a Panel text/progress widget in the browser app

Model code calls status.update("...") at each stage. runContext() wraps a run:
it routes DATA warnings to <output folder>/data_warnings.csv, makes the status
current (so fullLogging reaches it), and calls finish() or fail() at the end.

    with runContext(ExcelStatus(book), output_dir) as status:
        status.update("Loading specs")
        ...

xlwings and panel are only imported inside the classes that need them, so this
module is safe to import in the browser app and in builds without Excel.
"""

import logging
import time
from contextlib import contextmanager
from pathlib import Path

from .common import get_logger
from .logs import DATA, DataWarningCSV, readDataWarnings, DATA_WARNINGS_FILE

MYLOGGER = get_logger('modelkit.status')


def _icon(warnings, failed):
    if failed:
        return "error"
    return "warning" if warnings is not None and warnings.height else "info"


class RunStatus:
    """Console/log status. Base class for the Excel and Panel versions."""

    def __init__(self, label: str = "Run", success_note: str = "", message_box: bool = False):
        self.label = label
        self.success_note = success_note      # extra line in the end-of-run message on success
        self.message_box = message_box        # also show the end-of-run message in a message box
        self.started = time.time()
        self.output_dir = None

    # ----- called by model code -----
    def update(self, msg: str) -> None:
        MYLOGGER.info(msg)
        self._show(msg)

    def finish(self, output_dir=None) -> None:
        output_dir = output_dir or self.output_dir
        warnings = readDataWarnings(output_dir) if output_dir else None
        n = 0 if warnings is None else warnings.height
        msg = f"{self.label} complete in {self._elapsed()}."
        if self.success_note:
            msg += f" {self.success_note}"
        if n:
            msg += f"\n\n{n} data warning{'s' if n != 1 else ''}. " + self._warnings_hint(output_dir)
        MYLOGGER.info(msg.replace("\n\n", " "))
        self._done(msg, warnings, failed=False)

    def fail(self, error: BaseException) -> None:
        msg = (f"{self.label} stopped after {self._elapsed()}.\n\n"
               f"{type(error).__name__}: {error}")
        MYLOGGER.error(msg.replace("\n\n", " "))
        self._done(msg, None, failed=True)

    # ----- overridden by front ends -----
    def _show(self, msg: str) -> None:
        print(msg)

    def _done(self, msg: str, warnings, failed: bool) -> None:
        print(msg)
        if self.message_box:
            from .dialogs import showMessageBox
            showMessageBox(f"{self.label} {'failed' if failed else 'complete'}", msg,
                           icon=_icon(warnings, failed))

    def _warnings_hint(self, output_dir) -> str:
        return f"See {Path(output_dir) / DATA_WARNINGS_FILE}."

    # ----- helpers -----
    def _elapsed(self) -> str:
        s = int(time.time() - self.started)
        return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


class ExcelStatus(RunStatus):
    """
    Status bar in the calling workbook's Excel window; message box at the end.

    If warnings_sheet/warnings_range are given, the data warnings are written
    down from that named range at the end of the run (replacing earlier ones),
    as fullLogging did with 'Data Issues' / 'startdatawarnings'.
    """

    def __init__(self, book, label: str = "Run", success_note: str = "",
                 warnings_sheet: str = "Data Issues", warnings_range: str = "startdatawarnings",
                 result_range: str = "runstatus",
                 result_text=("Model Update Successful", "Model Update Failed")):
        super().__init__(label, success_note)
        self.book = book
        self.warnings_sheet = warnings_sheet
        self.warnings_range = warnings_range
        self.result_range = result_range      # named range given a final success/failure text (None to skip)
        self.result_text = result_text

    def _show(self, msg):
        try:
            self.book.app.status_bar = f"{self.label}: {msg}"
        except Exception as e:
            MYLOGGER.debug(f"Status bar update failed: {e}")

    def _warnings_hint(self, output_dir):
        if self.warnings_sheet:
            return f"See the {self.warnings_sheet} sheet."
        return super()._warnings_hint(output_dir)

    def _done(self, msg, warnings, failed):
        from .dialogs import showMessageBox
        try:
            self.book.app.status_bar = False          # give the status bar back to Excel
        except Exception:
            pass
        if warnings is not None and self.warnings_sheet and self.warnings_range:
            self._write_warnings(warnings)
        if self.warnings_sheet and self.result_range:
            try:
                self.book.sheets[self.warnings_sheet].range(self.result_range).value = \
                    self.result_text[1 if failed else 0]
            except Exception as e:
                MYLOGGER.debug(f"Could not write {self.result_range}: {e}")
        showMessageBox(f"{self.label} {'failed' if failed else 'complete'}", msg, icon=_icon(warnings, failed))

    def _write_warnings(self, warnings):
        try:
            rng = self.book.sheets[self.warnings_sheet].range(self.warnings_range)
            if rng.value is not None:                  # clear earlier warnings below the start cell
                last = rng.end("down") if rng.offset(1, 0).value is not None else rng
                rng.sheet.range(rng, last).clear_contents()
            messages = warnings.get_column("Message").to_list()
            if messages:
                rng.options(transpose=True).value = messages
        except Exception as e:
            MYLOGGER.warning(f"Could not write data warnings to {self.warnings_sheet}!{self.warnings_range}: {e}")


class PanelStatus(RunStatus):
    """Shows status in a Panel widget (e.g. pn.pane.Markdown or pn.widgets.StaticText)."""

    def __init__(self, widget, label: str = "Run", success_note: str = ""):
        super().__init__(label, success_note)
        self.widget = widget

    def _set(self, text):
        def apply():
            if hasattr(self.widget, "object"):
                self.widget.object = text
            else:
                self.widget.value = text
        try:
            import panel as pn
            pn.state.execute(apply)                    # safe when the model runs on a worker thread
        except Exception:
            apply()

    def _show(self, msg):
        self._set(f"{self.label}: {msg}")

    def _done(self, msg, warnings, failed):
        self._set(msg.replace("\n\n", " "))


# ----- the current run's status (used by fullLogging) -----
_current = RunStatus()


def getCurrentStatus() -> RunStatus:
    return _current


def setCurrentStatus(status: RunStatus) -> RunStatus:
    """Make `status` current; returns the previous one."""
    global _current
    previous, _current = _current, status
    return previous


@contextmanager
def runContext(status: RunStatus, output_dir):
    """
    Run a model with `status`: DATA warnings go to <output_dir>/data_warnings.csv,
    `status` is current for fullLogging, and finish()/fail() is called at the end.
    Exceptions are reported through status.fail() and then re-raised.
    """
    status.output_dir = Path(output_dir)
    handler = DataWarningCSV(output_dir)
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    if root.getEffectiveLevel() > DATA:      # e.g. root still at the default WARNING
        root.setLevel(DATA)                  # let DATA records through (INFO stays filtered)
    previous = setCurrentStatus(status)
    try:
        yield status
    except BaseException as e:
        status.fail(e)
        raise
    else:
        status.finish(output_dir)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()
        setCurrentStatus(previous)


def fullLogging(logger, level, msg):
    """
    Compatibility with the old _misc.fullLogging:
      'debug' -> logger.debug
      'info'  -> logger.info + status update (Excel status bar / Panel widget)
      'data'  -> DATA level: data_warnings.csv (when inside runContext) and the log
      'warning' / 'error' -> logger.warning / logger.error
    """
    level = level.lower()
    if level == "debug":
        logger.debug(msg)
    elif level == "info":
        logger.info(msg)
        _current._show(msg)
    elif level == "data":
        logger.log(DATA, msg)
    elif level in ("warning", "error"):
        logger.log(logging.WARNING if level == "warning" else logging.ERROR, msg)
