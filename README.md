# bapsf_interferometer

Acquisition and analysis for the BaPSF microwave interferometers at ports 20, 29, and 40.

> **Refactor in progress:** branch `refactor/linux-epics-daq`; the plan is in [docs/refactor_step1.md](docs/refactor_step1.md).
> Acquisition now reads both scopes directly over Ethernet on Linux and stops once it has the raw samples. Nothing is written to disk, no phase is computed in the loop, and nothing is published to EPICS yet. The HDF5 tools below read files written by the previous acquisition, which ran on Windows and read LeCroy `.trc` files.

Requires Python 3.11 or later (developed on 3.14). Acquisition runs on **Linux only**, because the Rigol deadline uses `signal.setitimer`. `pip install .` installs the dependencies, including [`lab-scopes`](https://github.com/hjia94/lab_scopes) `v0.4.0`, which provides both scope drivers.

## Acquisition

| Scope | Ports | Channels | Link |
|---|---|---|---|
| LeCroy (trigger master) | 20 (288 GHz), 29 (282 GHz) | C1/C2 = port 20 ref/plasma, C3/C4 = port 29 ref/plasma | VICP over TCP |
| Rigol DHO804, `192.168.7.63` | 40 (288 GHz) | C1 = ref, C2 = plasma | SCPI, TCP port 5555; 12-bit WORD reads |

The LeCroy trigger-out is hard-wired to the Rigol trigger input, so the Rigol triggers on every LeCroy trigger.
- The LeCroy is armed for one trigger per shot. It emits no further trigger-out until it is re-armed.
- The Rigol free-runs in AUTO sweep, as on `main`. Per shot it is stopped, read, and resumed, before the LeCroy is re-armed.

So both records hold the same shot. The one exception is an AUTO-forced Rigol acquisition landing between the trigger and the stop; that is pending a bench test. The rules are in the plan.

### Running

```bash
LECROY_IP=<address> python interf_main.py
```

- Each LeCroy capture produces one log line, written to stdout and to `$INTERF_LOG_DIR/interf_acquire.log`. The default directory is `~/data/log`, and the log rotates at midnight, keeping 30 days. A line gives:
  - host time;
  - LeCroy trigger time on the scope's clock, and Δt since the previous capture (≈ 3 s steady, ≈ 6 s when a shot is skipped, "-" after a shot with no LeCroy data);
  - points per channel;
  - time from capture detection to LeCroy re-arm;
  - any missing scope, with the reason.
- A pause in triggers is logged when it starts, then every 5 min, then once more when capture resumes.
- **Ctrl-C** (or SIGTERM from systemd): a shot already being read is finished and logged. The LeCroy is then set to NORM, the Rigol is sent `:RUN` (normally already running), and every connection is closed. A **second** Ctrl-C exits immediately, without restoring the trigger modes.

### Configuration

Every constant at the top of [interf_raw.py](interf_raw.py) can be overridden by an environment variable of the same name.

| Variable | Default | Meaning |
|---|---|---|
| `LECROY_IP` | `10.10.10.10` | **Placeholder; set it** |
| `LECROY_CHANNELS` | `C1,C2,C3,C4` | Channels read. The first one carries the sweep counter used to detect a fresh capture |
| `LECROY_TIMEOUT` | 5 s | VICP socket timeout |
| `LECROY_RETRY_INTERVAL` | 5 s | Wait before retrying after a LeCroy error |
| `TRIGGER_TIMEOUT` | 10 s | How long to wait for a trigger before logging a pause and retrying |
| `RIGOL_IP`, `RIGOL_REF_CH`, `RIGOL_PLA_CH` | `192.168.7.63`, `C1`, `C2` | Rigol address and channels |
| `RIGOL_RETRY_INTERVAL` | 100 shots | How long the Rigol is skipped after a connect, deadline, or read failure |
| `RIGOL_CONNECT_TIMEOUT` | 1 s | TCP connect timeout |
| `RIGOL_OPERATION_TIMEOUT` | 2.5 s | Hard deadline for one Rigol stop/read/run (and for `:RUN` on exit). Keep the Rigol at ≤ 1M points: a WORD read takes about 0.77 s per 1M-point channel |

A Rigol operation that hangs is cut off by `SIGALRM` at its deadline, so it cannot hold up the LeCroy. After a cut-off read, the Rigol can stay stopped until its next successful read, as on `main`.

### Output

`interf_raw.acquire_shot(state, stop_requested)` returns `None` when nothing was captured, or a `RawShot` with these fields:

- `lecroy`: `{ch: (int16 samples, 346-byte WAVEDESC)}`;
- `rigol`: `{ch: (uint16 12-bit codes, calibration metadata dict)}`. The Rigol has no header block, so voltages are `(code - y_origin - y_reference) * y_increment`;
- `missing`: `{"lecroy" | "rigol": reason}`. A missing Rigol has an empty data dict. `missing["lecroy"]` lists failed channels, and `lecroy` still holds the channels that were read;
- `host_time` and `critical_path_s`.

`interf_main.py` currently logs each shot and discards it.

## Files

| File | Purpose |
|---|---|
| [interf_main.py](interf_main.py) | Acquisition entry point: loop, logging, and Ctrl-C/SIGTERM handling |
| [interf_raw.py](interf_raw.py) | Same-shot raw acquisition from the LeCroy and the Rigol |
| [interf_analysis.py](interf_analysis.py) | Phase extraction (`phase_from_raw`, which uses the cross-spectral density; `phase_from_hilbert`, which is slower) and `get_calibration_factor` |
| [interf_file.py](interf_file.py) | HDF5 schema and writers for the daily interferometer file |
| [interf_read.py](interf_read.py) | Read phase and time arrays by date and timestamp |
| [read_hdf5.py](read_hdf5.py) | Read LAPD datarun HDF5 through bapsflib |

The live GUI will be re-implemented under EPICS, and the datarun merge scripts will return later on this branch.

## Existing HDF5 data (previous acquisition)

Each daily file is named `interferometer_data_YYYY-MM-DD.hdf5`. Its groups each contain one dataset per shot, keyed by timestamp:

| Group | Contents | Unit |
|---|---|---|
| `phase_p20/`, `phase_p29/`, `phase_p40/` | Phase per port. The group attributes give the microwave frequency and the calibration factor, which assumes a 40 cm plasma path | rad |
| `time_array/` | LeCroy time base | ms |
| `time_array_p40/` | Rigol time base, separate from `time_array` | ms |

- `phase_p40` datasets have per-shot attributes `rigol_missing` and `rigol_missing_reason`. When the Rigol was down, the dataset is a zero-filled placeholder.
- Files from before port 40 was added have only `phase_p20`, `phase_p29`, and `time_array`.
- Timestamps mark when the LeCroy saved its C4 file, which can be slightly later than the shot.
