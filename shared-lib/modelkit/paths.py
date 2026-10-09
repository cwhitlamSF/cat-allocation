# modelkit/paths.py
"""
Local file paths for workbooks that Excel reports as OneDrive/SharePoint URLs.

Sources, tried in order (longest URL prefix wins):
  1. OneDrive sync client's provider entries
       HKCU\\Software\\SyncEngines\\Providers\\OneDrive\\*  (UrlNamespace -> MountPoint)
     Covers OneDrive for Business and synced SharePoint libraries.
  2. OneDrive account entries
       HKCU\\Software\\Microsoft\\OneDrive\\Accounts\\*      (cid / UserFolder)
     Covers personal OneDrive (https://d.docs.live.net/<cid>/...).
  3. Personal OneDrive environment variables (OneDriveConsumer, OneDrive)
     for d.docs.live.net URLs, if the registry has no matching entry.

Replaces patching xlwings' fullname conversion. Windows only.
Use oneDriveDiagnostics() to see what this computer's sync client reports.
"""

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse
from .common import get_logger

MYLOGGER = get_logger('modelkit.paths')

_PROVIDERS_KEY = r"Software\SyncEngines\Providers\OneDrive"
_ACCOUNTS_KEY  = r"Software\Microsoft\OneDrive\Accounts"
_PERSONAL_HOST = "d.docs.live.net"

# shared-lib folder (this file is shared-lib/modelkit/paths.py); resource paths are relative to it,
# as they were when these functions lived in shared-lib/_misc.py
_SHARED_LIB = Path(__file__).resolve().parent.parent


def _registry_subkeys(path):
    """Yield (subkey_name, {value_name: value}) under HKCU\\path. Empty off Windows."""
    try:
        import winreg
    except ImportError:
        return
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return
    i = 0
    while True:
        try:
            name = winreg.EnumKey(root, i)
        except OSError:
            break
        i += 1
        values = {}
        try:
            with winreg.OpenKey(root, name) as k:
                j = 0
                while True:
                    try:
                        vname, vdata, _ = winreg.EnumValue(k, j)
                    except OSError:
                        break
                    values[vname] = vdata
                    j += 1
        except OSError:
            continue
        yield name, values


def _provider_mounts():
    for _, v in _registry_subkeys(_PROVIDERS_KEY):
        if v.get("UrlNamespace") and v.get("MountPoint"):
            yield v["UrlNamespace"], v["MountPoint"], "provider"


def _account_mounts():
    for name, v in _registry_subkeys(_ACCOUNTS_KEY):
        cid, folder = v.get("cid"), v.get("UserFolder")
        if cid and folder:                       # personal account
            yield f"https://{_PERSONAL_HOST}/{cid}/", folder, f"account:{name}"


def _env_mounts(fullname):
    """Personal OneDrive folder from environment variables, for a d.docs.live.net URL."""
    u = urlparse(fullname)
    parts = u.path.strip("/").split("/", 1)
    if u.netloc.lower() != _PERSONAL_HOST or not parts[0]:
        return
    prefix = f"https://{u.netloc}/{parts[0]}/"
    for var in ("OneDriveConsumer", "OneDrive"):
        folder = os.environ.get(var)
        if folder:
            yield prefix, folder, f"env:{var}"


def _mounts(fullname=""):
    """All (url_prefix, local_folder, source) candidates, longest prefix first."""
    found = list(_provider_mounts()) + list(_account_mounts()) + list(_env_mounts(fullname))
    cleaned = [(u.rstrip("/") + "/", m, s) for u, m, s in found]
    return sorted(cleaned, key=lambda m: len(m[0]), reverse=True)


def isUrl(path: str) -> bool:
    return str(path).lower().startswith(("http://", "https://"))


def localPath(fullname: str) -> str:
    """
    Return a local file path for a workbook path or OneDrive/SharePoint URL.

    A local path is returned unchanged. A URL is mapped through the OneDrive
    sync client's registry entries (then personal-OneDrive environment
    variables); raises FileNotFoundError if no synced local copy is found.
    """
    if not isUrl(fullname):
        return fullname
    tried = []
    for url, mount, source in _mounts(fullname):
        if fullname.lower().startswith(url.lower()):
            rest = unquote(fullname[len(url):]).replace("/", os.sep)
            local = os.path.join(mount, rest)
            if os.path.exists(local):
                MYLOGGER.debug(f"Mapped {fullname} -> {local} ({source})")
                return local
            tried.append(local)
    detail = ("\n\nTried:\n  " + "\n  ".join(tried)) if tried else \
             "\n\nNo synced OneDrive location matches this address."
    raise FileNotFoundError(
        f"Can't find a local synced copy of:\n{fullname}{detail}\n\n"
        "Check that the folder is synced with OneDrive on this computer.")


def oneDriveDiagnostics(fullname: str = "") -> None:
    """Print every URL-prefix -> folder mapping found on this computer (for troubleshooting)."""
    for url, mount, source in _mounts(fullname):
        match = "  <- matches" if fullname and fullname.lower().startswith(url.lower()) else ""
        print(f"[{source}]\n  {url}\n  -> {mount}{match}")


def resource_path(relative_path=""):
    # Works for both PyInstaller and normal dev environment
    if getattr(sys, 'frozen', False):  # PyInstaller sets this
        base_path = Path(sys._MEIPASS)
    else:
        base_path = _SHARED_LIB
    return os.path.join(base_path, relative_path)  #


def panel_resource_path(relative_path=""):
    # Works for both PyInstaller and normal dev environment
    if getattr(sys, 'frozen', False):  # PyInstaller sets this
        base_path = os.path.join(Path(sys._MEIPASS), "panel")
    else:
        base_path = os.path.join(_SHARED_LIB, 'panel')
    return os.path.join(base_path, relative_path)  #
