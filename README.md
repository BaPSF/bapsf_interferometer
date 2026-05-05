# bapsf_interferometer

Acquisition, analysis, storage, and plotting for the BaPSF microwave interferometers (ports 20, 29, and 40).

> **Branches:** `main` is the legacy Raspberry Pi (Linux) version, unused since 2024-07-16. **Use `diagnostic-pc`** — it runs on both PC and RP and is current.

## How it works

1. Raw interferometer signals feed three scopes:
   - **LeCroy** — ports 20 (282 GHz) and 29 (288 GHz). Runs in "Save Waveform → Wrap" mode, continuously writing traces to a folder shared on DAQ NET.
   - **Rigol DHO** (`192.168.7.63`) — port 40 (288 GHz). Runs in AUTO trigger; traces are read in-memory over telnet.
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
| [interf_merge_datarun.py](interf_merge_datarun.py) | Merge interferometer data into datarun HDF5 (timestamp matching, auto-detection of channels, per-channel and per-shot attrs) |

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
| `phase_p40/` | Phase, port 40 (Rigol; resampled onto LeCroy time grid) | rad | `Microwave frequency (Hz)` = 288e9, `calibration factor (m^-3/rad)` |
| `time_array/` | Time base for LeCroy channels | ms | — |
| `time_array_p40/` | Time base for Rigol (currently identical to `time_array`, kept separate so they can diverge in the future) | ms | — |

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

1. Pulls timestamps for completed shots from the datarun file.
2. Matches them to interferometer timestamps (±1 s tolerance).
3. Copies matched data into the structure above, preserving per-channel group attrs and per-shot dataset attrs (e.g., `rigol_missing`).
4. **Defaults to merging only the first and last completed shot** as a fast sanity check that keeps the file small. Pass `all_shots=True` to merge every shot:

   ```python
   merge_interferometer_data(datarun_path, interf_path, all_shots=True)
   ```
5. The datarun file is opened and closed once per shot, so an interruption leaves all already-merged shots safely flushed to disk.

## Update 2026-04-21 — port 40 added

- Third interferometer at **port 40** (288 GHz, 40 cm plasma path) on a Rigol DHO at `192.168.7.63` (CH1 = ref, CH2 = plasma).
- Per-shot flow in `interf_main.py`: detect LeCroy `.trc` → send `:STOP` to Rigol → read both Rigol channels over telnet → send `:RUN` → read all four LeCroy channels via the multiprocessing pool → run all three `phase_from_raw` analyses in parallel.
- Schema added `phase_p40` and `time_array_p40`. The Rigol phase is bounds/sanity-checked, then resampled onto the LeCroy grid via `np.interp`, so all phase traces and both time arrays share the same length per shot.
- If the Rigol is unreachable or errors out, the LeCroy pipeline keeps writing; `phase_p40` becomes a zero-filled placeholder with `rigol_missing=True` and a reason string. Reconnect retries every 100 shots.
- `interf_GUI.py` and `interf_plot.py` show a third (P40) trace; older HDF5 files without `phase_p40` still load.
- Added `_worker_init()` as the multiprocessing pool initializer to suppress `SIGINT` in workers. On Windows, Ctrl-C is broadcast to every console process; without this, workers raised `KeyboardInterrupt` mid-task and stalled `pool.join()`. The main process keeps full Ctrl-C handling.
