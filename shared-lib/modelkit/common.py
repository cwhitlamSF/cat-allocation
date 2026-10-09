# modelkit/common.py
"""
Common constants, enums, and logging configuration.
Shared across all modelkit modules.
"""

import logging
import polars as pl
from typing import Dict

# Polars type mapping (used across modules)
DTYPES_CONVERT: Dict[str, pl.DataType] = {
    'Boolean': pl.Boolean,
    'Utf8': pl.Utf8,
    'Utf8Trim.0': pl.Utf8,
    'Date': pl.Utf8,
    'Float64RoundToInt': pl.Float64,
    'Float64': pl.Float64,
    'Int64': pl.Int64
}


# Section names in MODELPREFIX_config.ini (written by config_creator, read by the model).
# Defined once here so the writer and the readers can't drift apart.
CONFIG_KEYS_SUFFIX   = ' Keys'          # companion section restoring original-case keys, e.g. [Main Menu Keys]
CONFIG_KEY_COLUMNS   = 'Key Columns'
CONFIG_VALUE_COLUMNS = 'Value Columns'
CONFIG_PANEL_FLAGS   = 'Panel Flags'
CONFIG_SOURCE_TABLES = 'Source Tables'   # Excel table name -> Spec Key, for Default Source lookups


# list_difference and list_dropped also live in modelkit.lists (with the other list helpers);
# these copies stay here because common is imported by every module and lists imports common.
def list_difference(list1, list2):
    #returns sorted difference between two lists
    return sorted(list(set(list1).symmetric_difference(set(list2))))


def list_dropped(list1, list2):
    #returns sorted list1 with items in list2 removed
    return sorted(list(set(list1) - set(list2)))

# Logger configuration (all modules use this)
def get_logger(module_name: str) -> logging.Logger:
    """
    Get a logger for the given module.

    No level is set here: records propagate to the root handlers installed by
    _myLogging.setup_logging, and setting a level on the child would filter
    debug records out before they ever reach them.
    """
    return logging.getLogger(module_name)

MYLOGGER = get_logger('modelkit')
