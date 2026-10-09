# modelkit/npz.py
"""
Simulation chunk arrays (.npz): save, accumulate, combine across losim_<n>
chunk folders, and convert to a wide DataFrame.
"""

import re
from pathlib import Path

import numpy as np
import polars as pl
from .common import get_logger

MYLOGGER = get_logger('modelkit.npz')


def save_npz(path: Path, **arrays):
    """
    Saves arrays to NPZ using float32 where possible.
    Overwrites existing file. Uncompressed for performance —
    all npz files are transient and deleted after the run.
    """
    payload = {}
    for k, v in arrays.items():
        if isinstance(v, np.ndarray) and np.issubdtype(v.dtype, np.floating):
            payload[k] = v.astype(np.float32, copy=False)
        else:
            payload[k] = v
    np.savez(path, **payload)

def add_to_npz(path: Path, **add_arrays):
    """Load existing arrays from NPZ (if exists), add elementwise, overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = np.load(path)
        merged = {}
        for k, v in add_arrays.items():
            if k in data.files:
                if data[k].shape != v.shape:
                    data.close()
                    raise ValueError(
                        f"Shape mismatch for '{k}' in {path}: file {data[k].shape} vs add {v.shape}"
                    )
                merged[k] = data[k] + v
            else:
                merged[k] = v.copy()
        data.close()
    else:
        merged = {k: v.copy() for k, v in add_arrays.items()}
    save_npz(path, **merged)

def losim_number(p: Path) -> int:
    m = re.search(r"losim_(\d+)$", p.name)
    return int(m.group(1)) if m else 0

def combine_npz_across_chunks(root_dir: Path, out_dir: Path):
    root_dir = Path(root_dir)
    out_dir  = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_chunk_dirs = sorted(
        [p for p in root_dir.iterdir() if p.is_dir() and p.name.startswith("losim_")],
        key=losim_number
    )
    if not all_chunk_dirs:
        raise FileNotFoundError(f"No losim_* folders found under {root_dir}")

    # Collect non-empty chunk dirs and their file lists
    chunk_dirs = []
    files_by_dir = {}
    for d in all_chunk_dirs:
        npz_files = sorted(d.glob("*.npz"))
        if npz_files:
            chunk_dirs.append(d)
            files_by_dir[d] = [p.name for p in npz_files]

    if not chunk_dirs:
        return

    template_files = sorted({name for names in files_by_dir.values() for name in names})
    if not template_files:
        return

    for fname in template_files:
        # Single pass — read all chunks at once
        all_keys = set()
        chunk_data = []  # list of dicts: key -> array

        for d in chunk_dirs:
            f = d / fname
            if not f.exists():
                chunk_data.append(None)
                continue
            with np.load(f, allow_pickle=False) as z:
                if not z.files:
                    chunk_data.append(None)
                    continue
                chunk_data.append({k: np.asarray(z[k]).ravel().astype(np.float32, copy=False)
                                   for k in z.files})
                all_keys.update(z.files)

        if not all_keys:
            continue

        # A chunk missing this file is zero-filled to that chunk's own length,
        # read from a sibling npz - the last chunk is usually shorter, so the
        # first chunk's length is the wrong guess.
        chunk_lens = []
        for d, c in zip(chunk_dirs, chunk_data):
            if c is not None:
                chunk_lens.append(next(iter(c.values())).size)
                continue
            for sibling in files_by_dir[d]:
                with np.load(d / sibling, allow_pickle=False) as z:
                    if z.files:
                        chunk_lens.append(np.asarray(z[z.files[0]]).size)
                        break
            else:
                raise ValueError(f"Cannot size zero-fill for {fname}: no arrays in {d}")

        payload = {}
        for k in sorted(all_keys):
            parts = [
                c[k] if (c is not None and k in c)
                else np.zeros(n, dtype=np.float32)
                for c, n in zip(chunk_data, chunk_lens)
            ]
            payload[k] = np.concatenate(parts)

        np.savez(out_dir / fname, **payload)

def npz_folder_to_wide_df(folder) -> pl.DataFrame:
    folder_path = Path(folder)
    files = sorted(folder_path.glob("*.npz"))
    if not files:
        return pl.DataFrame()

    df = pl.DataFrame()
    expected_len = None
    for fp in files:
        with np.load(fp, allow_pickle=False) as z:
            cols = {}
            for k in z.files:
                arr = np.asarray(z[k]).ravel()
                if expected_len is None:
                    expected_len = arr.size
                elif arr.size != expected_len:
                    raise ValueError(
                        f"Length mismatch in {fp.name} key '{k}': {arr.size} vs expected {expected_len}"
                    )
                cols[k] = arr
        chunk_df = pl.DataFrame(cols)
        df = chunk_df if df.is_empty() else df.hstack(chunk_df)

    return df
