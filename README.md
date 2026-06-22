# bapsf_interferometer

Acquisition, analysis, storage, and plotting for the BaPSF microwave interferometers (ports 20, 29, and 40).

> **Branch:** `main` is the current diagnostic-PC version and carries the port-40 (Rigol) acquisition path. The Rigol driver is provided by the [`lab-scopes`](https://github.com/hjia94/lab_scopes) package (`from lab_scopes.rigol import RigolDHO800`), pinned in [pyproject.toml](pyproject.toml); run `pip install --pre .` to install it.

## How it works

1. Raw interferometer signals feed three scopes:
   - **LeCroy** — ports 20 (288 GHz) and 29 (282 GHz). Runs in "Save Waveform → Wrap" mode, continuously writing traces to a folder shared on DAQ NET (mounted as `I:\` on the diagnostic PC).
   - **Rigol DHO804** (`192.168.7.63`, `C1` = ref, `C2` = plasma) — port 40 (288 GHz). Runs in AUTO trigger; per shot it is stopped, both channels are read over a TCP socket (port 5555, via the `lab_scopes` `RigolDHO800` driver), then resumed.
2. The diagnostic PC ([interf_main.py](interf_main.py)) detects each new LeCroy `.trc`, pulls the matching Rigol shot, computes phase shifts ([interf_raw.py](interf_raw.py)), and appends to a daily HDF5 file ([interf_file.py](interf_file.py)).
3. Old scope traces are deleted to keep disks clean ([interf_cleanup.py](interf_cleanup.py)).
4. A Raspberry Pi in the main lab runs a live plotting GUI ([interf_GUI.py](interf_GUI.py)) by reading the shared HDF5.
5. After a run, interferometer data is merged into the datarun HDF5 ([interf_merge_datarun.py](interf_merge_datarun.py)).

## Files

### Core
| File | Purpose |
|---|---|
| [interf_main.py](interf_main.py) | Diagnostic-PC entry point: monitor LeCroy, read Rigol, analyze, save |
| [interf_raw.py](interf_raw.py) | Phase extraction (cross-correlation in use; Hilbert available but slower) and density calibration |
| [interf_file.py](interf_file.py) | HDF5 schema, writes, and metadata |
| [interf_read.py](interf_read.py) | Read phase/time arrays by date and timestamp; example plotting |

### Support
| File | Purpose |
|---|---|
| [interf_GUI.py](interf_GUI.py) | Live density plots on the lab Raspberry Pi |
| [interf_plot.py](interf_plot.py) | Shared plotting helpers and styles |
| [interf_cleanup.py](interf_cleanup.py) | Delete processed scope traces; log activity |
| [interf_merge_datarun.py](interf_merge_datarun.py) | Merge interferometer data into datarun HDF5 (timestamp matching, auto-detection of channels, per-channel and per-shot attrs, single-file or whole-folder batch with log file) |

### Utilities
| File | Purpose |
|---|---|
| [read_scope_data.py](read_scope_data.py) | Read LeCroy `.trc` (binary) and `.txt` (ASCII) files |
| [LeCroy_Scope_Header.py](LeCroy_Scope_Header.py) | Decode LeCroy binary headers; build time arrays |
| [read_hdf5.py](read_hdf5.py) | Read LAPD datarun HDF5 via bapsflib (probe motion, digitizer signals, run sequence) |
| [cpu_temp.py](cpu_temp.py) | Raspberry Pi CPU temperature monitor |

## Data structure

### Standalone interferometer file: `interferometer_data_YYYY-MM-DD.hdf5`

**Root attributes:** `created`, `description`.

**Groups** (each contains datasets keyed by timestamp):

| Group | Description | `unit` | Per-group attrs |
|---|---|---|---|
| `phase_p20/` | Phase, port 20 | rad | `Microwave frequency (Hz)` = 288e9, `calibration factor (m^-3/rad)` |
| `phase_p29/` | Phase, port 29 | rad | `Microwave frequency (Hz)` = 282e9, `calibration factor (m^-3/rad)` |
| `phase_p40/` | Phase, port 40 (Rigol) | rad | `Microwave frequency (Hz)` = 288e9, `calibration factor (m^-3/rad)` |
| `time_array/` | Time base for LeCroy channels | ms | — |
| `time_array_p40/` | Time base for Rigol, independent of `time_array` | ms | — |

> By default the Rigol phase is **not** resampled onto the LeCroy grid (`INTERPOLATE_RIGOL = False` in [interf_main.py](interf_main.py)): the raw Rigol time/phase are stored, so `time_array_p40` has its own sample count and length, generally different from `time_array`. Set `INTERPOLATE_RIGOL = True` to resample the Rigol phase onto the LeCroy time grid via `np.interp`, in which case `time_array_p40` matches `time_array`.

`phase_p40` datasets carry per-shot `rigol_missing` (bool) and `rigol_missing_reason` (str) when the Rigol was unreachable; the array is then a zero-filled placeholder.

> Timestamps mark when the scope saved the trace's last channel (C4), so there can be a small delay vs. the actual shot.

### Datarun file after merge

Merged data lives under `diagnostics/interferometer/`:

```
diagnostics/interferometer/
├── phase_p20/{shot_number}     # rad
├── phase_p29/{shot_number}     # rad
├── phase_p40/{shot_number}     # rad   (new-format files only)
├── time_array/{shot_number}    # ms    (LeCroy)
└── time_array_p40/{shot_number}# ms    (Rigol; new-format files only)
```

Each subgroup carries the same per-channel attrs as in the standalone file; calibration factors assume a 40 cm plasma path. `phase_p40` datasets preserve the `rigol_missing` / `rigol_missing_reason` per-shot attrs.

**Example — read shot 5:**
- `diagnostics/interferometer/phase_p20/5`
- `diagnostics/interferometer/phase_p29/5`
- `diagnostics/interferometer/phase_p40/5` *(if present)*
- `diagnostics/interferometer/time_array/5`
- `diagnostics/interferometer/time_array_p40/5` *(if present)*

**Naming:** datasets are keyed by shot number starting at 1. A missing shot number means no interferometer data for that shot. For new-format files, a shot may have `phase_p20`/`phase_p29`/`time_array` but no `phase_p40`/`time_array_p40` if the Rigol was down.

**Backward compatibility:** legacy interferometer files (pre-port-40) only have `phase_p20`, `phase_p29`, `time_array`. The merger auto-detects which subgroups exist and only creates those, so merging an old file produces the original three-group layout.

### Merge process

`interf_merge_datarun.py`:

1. Pulls timestamps for completed shots from the datarun sequence. Only rows whose message originates from the SIS DAQ (`SIS crate` or `SIS 3302`) are counted, so sequences that also log `bmotion` (or other) rows per shot do not double-count shots. Each shot's real shot number is parsed from the message and used as the dataset name, so dataset identity tracks the run sequence even when shot numbers have gaps.
2. Matches them to interferometer timestamps (±1 s tolerance) via binary search.
3. Copies matched data into the structure above, preserving per-channel group attrs and per-shot dataset attrs (e.g., `rigol_missing`).
4. **Defaults to merging only the first and last completed shot** as a fast sanity check that keeps the file small. Pass `all_shots=True` to merge every shot:

   ```python
   merge_interferometer_data(datarun_path, interf_path, all_shots=True)
   ```
5. The datarun file is opened and closed once per shot, so an interruption leaves all already-merged shots safely flushed to disk. The interferometer file is opened via a context manager, so an unexpected error mid-merge cannot leak the handle.

### Batch merge (whole folder)

`merge_folder(datarun_dir, interf_dir, all_shots=False)` runs the merge for every `.hdf5` datarun file in a folder:

```python
from interf_merge_datarun import merge_folder
merge_folder(r"D:\data\LAPD\Mar26", r"D:\data\LAPD\interferometer_samples", all_shots=True)
```

- **Interferometer file lookup:** parses a `YYYY-MM-DD` date out of each datarun filename and looks for `interferometer_data_<date>.hdf5` in `interf_dir`, falling back to the previous day, then the next day, if the same-day file is absent. If the filename has no date, the datarun file's **creation time** (real ctime on Windows) is used as a fallback and the log marks that file with a `[ctime]` tag.
- **Per-file isolation:** missing interferometer files, zero-match runs, and corrupt files are caught and logged; the batch continues.
- **Progress output:** one fixed-width `[i/N] filename  STATUS  detail` line per file. Status verbs are `OK` (with shot count), `EMPTY` (interferometer file found but no shot matched), `SKIP` (no date or no interferometer file on disk), `ERROR`. A final tally summarises `ok / empty / skipped / error / total`.
- **Log file:** `interf_merge_log.txt` is written into `datarun_dir` (fixed filename, overwritten on each run, flushed per line so partial batches still leave a usable log). The log mirrors the terminal output and is bracketed by start/finish timestamps.
- **Return value:** a `{datarun_path: status_string}` dict so callers can drive downstream automation.

## Update 2026-04-21 — port 40 added

- Third interferometer at **port 40** (288 GHz, 40 cm plasma path) on a Rigol DHO804 at `192.168.7.63` (`C1` = ref, `C2` = plasma).
- Per-shot flow in `interf_main.py`: detect LeCroy `.trc` → submit a Rigol `stop` → read-both-channels → `run` job on a worker thread (overlapping the LeCroy work, not serialized in front of it) → read all four LeCroy channels via the multiprocessing pool → run the `phase_from_raw` analyses in parallel. The Rigol read is bounded by `RIGOL_OPERATION_TIMEOUT` (2.5 s, measured from `stop()`); a stalled scope is dropped for that shot.
- Schema added `phase_p40` and `time_array_p40`. By default (`INTERPOLATE_RIGOL = False`) the raw Rigol phase and its own time base are stored as-is, so `time_array_p40` is independent of `time_array`. With `INTERPOLATE_RIGOL = True`, the Rigol phase is bounds/sanity-checked by `interpolate_rigol_phase()` and resampled onto the LeCroy grid via `np.interp` (falling back to raw Rigol data if the time base fails validation).
- The Rigol driver comes from the `lab_scopes` package (`from lab_scopes.rigol import RigolDHO800`), pinned in [pyproject.toml](pyproject.toml).
- If the Rigol is unreachable or errors out, the LeCroy pipeline keeps writing; `phase_p40` becomes a zero-filled placeholder with `rigol_missing=True` and a reason string. Reconnect retries every `RIGOL_RETRY_INTERVAL` (100) shots.
- `interf_GUI.py` and `interf_plot.py` show a third (P40) trace; older HDF5 files without `phase_p40` still load.
- Added `_worker_init()` as the multiprocessing pool initializer to suppress `SIGINT` in workers. On Windows, Ctrl-C is broadcast to every console process; without this, workers raised `KeyboardInterrupt` mid-task and stalled `pool.join()`. The main process keeps full Ctrl-C handling.
