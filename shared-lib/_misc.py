# _misc.py
"""
TEMPORARY compatibility module. Everything that was here now lives in modelkit:

    dialogs         file pickers, message boxes, option picker
    paths           resource_path, panel_resource_path (and OneDrive localPath)
    excel_xlwings   getWorkbookByFullPath, copyTableToSht, clearTableOnSht, refresh_data_model
    specs           configparser_to_dict, convertDictToTable, load_spec_table_to_df, applyFormats, ...
    frames          Polars helpers (validate_polars_schema, concat_normalize_*, pl_isclose, clip, ...)
    stats           calcSummaryStats, summarizeByGroup, getReturnPeriod
    npz             simulation chunk arrays
    files           folder and parquet helpers
    lists           list / comma-string / dict helpers
    status          fullLogging (now routed to the run status and data_warnings.csv)

Existing `import _misc` code keeps working through the imports below. Change it to
import from modelkit directly, then delete this file.
"""
import logging
import os
import sys

#config.ini is in parent folder of this file
_here       = os.path.dirname(os.path.abspath(__file__))  # shared-lib/
_tool_root  = os.path.dirname(_here)                       # rsa/ or experience-rating/
sys.path.insert(0, _tool_root)
from config import DEVELOPERMODE   # kept for old code; modelkit itself does not import the model's config
if DEVELOPERMODE:
    from IPython.display import display

from modelkit.common import DTYPES_CONVERT as DTYPESCONVERT
from modelkit.dialogs import (
    selectAnalysisFile_PanelVersion,
    selectAnalysisFile_LocalVersion,
    showMessageBox,
    tkinterSelectFromList,
)
from modelkit.paths import (
    resource_path,
    panel_resource_path,
)
from modelkit.excel_xlwings import (
    getWorkbookByFullPath,
    copyTableToSht,
    clearTableOnSht,
    refresh_data_model,
)
from modelkit.specs import (
    convertDictToTable,
    createSpecCleanInfo,
    load_spec_table_to_df,
    applyFormats,
    assertStringFormats,
    configparser_to_dict,
    recapitalizeConfigDictKey,
)
from modelkit.frames import (
    firstRowToDict,
    stringToFloat,
    clip,
    dfReplaceNanNone,
    pl_isclose,
    _gt_zero,
    normalize_lazy,
    concat_normalize_lazy,
    concat_normalize_collect,
    df_to_nested_dict,
    filterDataFrameColumns,
    colListWithSuffix,
    dropSuffixFromList,
    validate_polars_schema,
)
from modelkit.stats import (
    getReturnPeriod,
    summarizeByGroup,
    _tvar_include_var_mass_partition,
    calcSummaryStats,
)
from modelkit.npz import (
    save_npz,
    add_to_npz,
    losim_number,
    combine_npz_across_chunks,
    npz_folder_to_wide_df,
)
from modelkit.files import (
    empty_dir,
    createFolderIfNot,
    deleteFolderIfExists,
    deleteFilesFromFolder,
    getFromParquet,
    fromGzipParquet,
    saveToParquet,
    getFileList,
    getFileNameFromPath,
)
from modelkit.lists import (
    flagValueIfNotNullNorInList,
    list_intersection_sls,
    list_intersection_sll,
    list_to_string,
    list_intersection_sss,
    list_intersection_ssl,
    list_intersection,
    list_difference,
    list_dropped,
    list_added,
    uniqueList,
    getDictValue,
    concatenateDictListVals,
    keysAreDicts,
    create_consistent_groups,
)
from modelkit.status import (
    fullLogging,
)

MYLOGGER = logging.getLogger(__name__)
MYLOGGER.setLevel(logging.INFO)


def deleteFilesFromFolderExclLALAE(folder):
    # RSA-specific: keeps the LALAE parquet outputs. Now a call to the general version.
    deleteFilesFromFolder(folder, keep_suffixes=("- lalae.parquet", "- summary lalae.parquet"))
