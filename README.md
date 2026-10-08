# bapsf_interferometer

Acquisition and analysis for the BaPSF microwave interferometers at ports 20, 29, and 40.

> **Refactor in progress:** branch `refactor/linux-epics-daq`.
> Acquisition now reads both scopes directly over Ethernet on Linux and stops once it has the raw samples. Nothing is written to disk but the log and the shot counter, no phase is computed in the loop, and nothing is published to EPICS yet. The HDF5 layout below is for files written by the previous acquisition, which ran on Windows and read LeCroy `.trc` files.

Requires Python 3.11 or later (developed on 3.14). Acquisition runs on **Linux only**, because the Rigol deadline uses `signal.setitimer`. `pip install .` installs the dependencies, including [`lab-scopes`](https://github.com/hjia94/lab_scopes) `v0.4.1`, which provides both scope drivers and reads the LeCroy trigger time as America/Los_Angeles (on Windows it also installs `tzdata`).

## Acquisition

| Scope | Ports | Channels | Link |
|---|---|---|---|
| LeCroy (trigger master) | 20 (288 GHz), 29 (282 GHz) | C1/C2 = port 20 ref/plasma, C3/C4 = port 29 ref/plasma | VICP over TCP |
| Rigol DHO804, `192.168.7.63` | 40 (288 GHz) | C1 = ref, C2 = plasma | SCPI, TCP port 5555; 12-bit WORD reads |

The LeCroy trigger-out is hard-wired to the Rigol trigger input, so the Rigol triggers on every LeCroy trigger.
- The LeCroy is armed for one trigger per shot. It emits no further trigger-out until it is re-armed.
- The Rigol free-runs in AUTO sweep, as on `main`. Per shot it is stopped, read, and resumed, before the LeCroy is re-armed.

So both records hold the same shot. The one exception is an AUTO-forced Rigol acquisition landing between the trigger and the stop; that is pending a bench test. The same-shot rules are in the [interf_raw.py](interf_raw.py) docstring.

### Running

```bash
LECROY_IP=<address> python interf_main.py
```

- Each LeCroy capture produces one log line, written to stdout and to `$INTERF_LOG_DIR/interf_acquire.log`. The default directory is `~/data/log`, and the log rotates at midnight, keeping 30 days. A line gives:
  - the shot ID, `shot <YYYYMMDD>-<number>`;
  - host time, in Los Angeles time like every time on the line;
  - LeCroy trigger time, and Δt since the previous capture (≈ 3 s steady, ≈ 6 s when a shot is skipped, "-" after a shot with no trigger time);
  - points per channel;
  - time from capture detection to LeCroy re-arm;
  - any missing scope, with the reason.
- Shot identity ([interf_shot.py](interf_shot.py)): the shot time is the LeCroy trigger time from the WAVEDESC (keep the scope clock NTP-synced), or the host time when a shot has none. A shot is its Los Angeles date plus a number from 0 each day; the counter is saved after every shot, so a restart the same day continues. A gap in the numbers is a lost shot. A host time before the trigger time, or more than `INTERF_TRIG_LAG_MAX_S` after it, is logged as a clock problem.
- A pause in triggers, or a LeCroy that keeps failing to connect or respond, is logged when it starts, then every 5 min, then once more when capture resumes. After a LeCroy error the next attempt waits `LECROY_RETRY_INTERVAL`.
- **Ctrl-C** (or SIGTERM from systemd): a shot already being read is finished and logged. The LeCroy is then set to NORM, the Rigol is sent `:RUN` (normally already running), and every connection is closed. A **second** Ctrl-C exits immediately, without restoring the trigger modes.

### Configuration

Each variable is read from the environment at startup; unset means the default. The first three are in [interf_main.py](interf_main.py), `INTERF_TRIG_LAG_MAX_S` in [interf_shot.py](interf_shot.py), the rest in [interf_raw.py](interf_raw.py). The time zone of trigger times and shot dates is always America/Los_Angeles, not a setting.

| Variable | Default | Meaning |
|---|---|---|
| `INTERF_LOG_DIR` | `~/data/log` | Log directory |
| `INTERF_IOC_LINK` | unset | `unix:/path` or `tcp:host:port` of a `diag_ioc` listener. When set, every shot is also sent there for live analysis; unset, acquisition runs exactly as before |
| `INTERF_SHOT_STATE` | `~/data/state/shot_counter.json` | Shot counter state (date and last number), kept apart from the logs. An unreadable file is logged and counting starts at 0 |
| `INTERF_TRIG_LAG_MAX_S` | 5 s | Largest normal host time − trigger time; outside 0 to this, a clock problem is logged. 0 disables the check |
| `LECROY_IP` | `10.10.10.10` | **Placeholder; set it** |
| `LECROY_CHANNELS` | `C1,C2,C3,C4` | Channels read, comma-separated. The first one carries the sweep counter used to detect a fresh capture |
| `LECROY_TIMEOUT` | 5 s | VICP socket timeout |
| `LECROY_RETRY_INTERVAL` | 5 s | Wait before retrying after a LeCroy error |
| `TRIGGER_TIMEOUT` | 10 s | How long to wait for a trigger before logging a pause and retrying |
| `RIGOL_IP`, `RIGOL_REF_CH`, `RIGOL_PLA_CH` | `192.168.7.63`, `C1`, `C2` | Rigol address and channels |
| `RIGOL_RETRY_INTERVAL` | 100 shots | How long the Rigol is skipped after a connect, deadline, or read failure |
| `RIGOL_CONNECT_TIMEOUT` | 1 s | TCP connect timeout |
| `RIGOL_OPERATION_TIMEOUT` | 2.5 s | Hard deadline for one Rigol connect, stop, both channel reads, and run (and for `:RUN` on exit). Keep the Rigol at ≤ 1M points: a WORD read takes about 0.77 s per 1M-point channel |

A Rigol operation that hangs is cut off by `SIGALRM` at its deadline, so it cannot hold up the LeCroy. After a cut-off read, the Rigol can stay stopped until its next successful read, as on `main`.

### Output

`interf_raw.acquire_shot(state, stop_requested)` returns `None` when nothing was captured (a pause, a LeCroy error, or a stop request), or a `RawShot` with these fields:

- `lecroy`: `{ch: (int16 samples, 346-byte WAVEDESC)}`;
- `rigol`: `{ch: (uint16 12-bit codes, calibration metadata dict)}`. The Rigol has no header block, so voltages are `(code - y_origin - y_reference) * y_increment`;
- `missing`: `{"lecroy" | "rigol": reason}`. A missing Rigol has an empty data dict. `missing["lecroy"]` lists failed channels, and `lecroy` still holds the channels that were read;
- `host_time` and `critical_path_s`;
- `shot_date`, `shot_number`, `shot_time` and `time_source` (`trigger` or `host`): `None` from `acquire_shot`, then set by `interf_main` before the outputs. The raw archive does not store them.

`interf_main.main(outputs)` assigns each shot's identity, writes the shot to every output, then logs it. A failing output is logged (traceback when it starts failing, a count every 5 min, a line on recovery) and never stops the other outputs or the loop. The command-line entry point passes no output unless `INTERF_IOC_LINK` is set.

With `INTERF_IOC_LINK`, the output is an `IocLink` ([diag_ioc/link.py](diag_ioc/link.py)). Its `write()` only queues the shot; a background thread encodes it with [interf_payload.py](interf_payload.py) (schema 2: the shot identity, then the raw arrays) and sends it. It is latest-wins: it keeps at most the 2 newest unsent shots, never blocks acquisition, and does not resend a shot lost during an outage (the raw archive is the complete record). An outage is logged when it starts, every 5 min, and on recovery with the number of shots not delivered; reconnection is retried every 5 s.

## Offline simulation

`interf_sim/` runs `interf_main` and `interf_raw` on recorded LeCroy `.trc` files, and contacts no scope. It replaces only the two lab_scopes driver classes that `interf_raw` imports, so a change to `interf_main` or `interf_raw` takes effect in the simulation without editing it.

```bash
python -m interf_sim --limit 20    # from the repo root; --help lists the options
python -m interf_sim --limit 20 --repeat-traces  # cycle available traces into 20 synthetic shots
python -m interf_sim --raw-output raw.bp --limit 20  # write complete raw shots to BP5
python -m interf_sim --ioc-link unix:/tmp/interf.sock --limit 20  # also send shots to a live-analysis listener
```

`python -m interf_sim.listen ADDRESS [--analyze] [--ne-window-ms START STOP]` is a development listener for `--ioc-link` and `INTERF_IOC_LINK`. It prints one line per shot received, and with `--analyze` adds each port's points and density, or the reason the port is missing. Use `tcp:127.0.0.1:PORT` where unix sockets are unavailable.

- The `.trc` directory is `TRC_DIR` in [interf_sim/trc_replay.py](interf_sim/trc_replay.py) (`D:/data/raw data` on the lab PC). On Linux, edit that line or pass `--trc-dir`.
- Shots play in trigger-time order, because the file counter wraps. Indexing reads one header per shot: 5–20 s for 29k shots, depending on the disk cache. Each machine trigger, every `--period` s (default 3; 0 = as fast as files load), serves the next shot.
- `--repeat-traces` requires `--limit`. It cycles the indexed shots until it has exactly that many entries, assigning consecutive counters from the first selected counter. The channel dictionaries refer to the original `.trc` files, so their recorded trigger timestamps do not change.
- There is no Rigol: every shot has `missing["rigol"]`.
- When the shots run out, the simulator sends SIGINT, so the Ctrl-C stop and release path runs. The log goes to `interf_sim/log/`.
- Transfers take only the file read time, so `critical_path_s` is shorter than on the scopes. The logged `dt` follows the recorded trigger times, not `--period`.
- Shot dates are the recording's, and the shot counter state is `interf_sim/log/shot_counter.json` (`INTERF_SHOT_STATE` is not used), so a replay never adds to acquisition's numbering. The trigger-lag check is off, since recorded trigger times are older than the replay.
- `--raw-output` enables the integrated output package. It accepts a direct ADIOS output, an encrypted socket connection file, or a remote server configuration. See [streamer/README.md](streamer/README.md) for the payload and consumer commands.

Without recorded shots, generate synthetic ones with a known phase (a Gaussian bump of about 6 rad), then replay them with `--trc-dir`:

```bash
python -m interf_sim.synthetic /tmp/synth --shots 20   # --help lists samples, dt, IF, period, noise
python -m interf_sim.synthetic /tmp/dead --shots 5 --flat C2   # C2 dead: P20 is reported missing ("C2 flat (no signal)")
python -m interf_sim --trc-dir /tmp/synth --period 0
```

`analyze_shot` reports a port missing, with its reason, when a channel is absent, flat (every code identical, e.g. a dead input), or too short for the 5 leading windows the phase offset needs. A disconnected input that still delivers noise is not detected yet; that needs a signal-to-noise check calibrated on real scope data.

Downstream code can take the same `RawShot` objects that `acquire_shot` returns:

```python
from interf_sim.trc_replay import ReplayLeCroy, iter_shots, trc_shots

for shot in iter_shots(ReplayLeCroy(trc_shots()[:10])):
    samples, wavedesc = shot.lecroy["C1"]
```

`FakeLeCroyScope` subclasses the real `LeCroyScope`, so decoding and validation are the driver's own. It raises `SimFault`, which `except Exception` does not catch, in two cases:
- `interf_raw` calls a driver method the fake does not override and that method reaches the scope. Override the method in `FakeLeCroyScope`, keeping the lab_scopes signature.
- A capture is read twice (same-shot rule 3).

## Files

| File | Purpose |
|---|---|
| [interf_main.py](interf_main.py) | Acquisition entry point: loop, logging, and Ctrl-C/SIGTERM handling |
| [interf_raw.py](interf_raw.py) | Same-shot raw acquisition from the LeCroy and the Rigol |
| [interf_analysis.py](interf_analysis.py) | Phase extraction (`phase_from_raw`, which uses the cross-spectral density; `phase_from_hilbert`, which is slower), `get_calibration_factor`, and `analyze_shot`, which turns one raw shot into per-port phase and density (P20, P29, P40) |
| [interf_shot.py](interf_shot.py) | Shot time (LeCroy trigger time, Los Angeles zone, host-time fallback) and the persisted per-day shot counter |
| [interf_payload.py](interf_payload.py) | Live-link message of one shot (`encode` for `IocLink`, `decode` for the IOC); independent of `streamer/` |
| [interf_archive.py](interf_archive.py) | Reads the `--raw-output` ADIOS archive back shot by shot (`iter_steps`, `read_step`, `decode`) for offline reanalysis with `analyze_shot`. Kept outside `streamer/`, which another group maintains |
| [interf_file.py](interf_file.py) | HDF5 schema and writers for the daily interferometer file (previous acquisition) |
| [interf_sim/](interf_sim/) | Offline simulation: scope fakes that replay `.trc` files, `synthetic.py`, which generates shots with a known phase, and `listen.py`, a development listener for the IOC link |
| [diag_ioc/](diag_ioc/) | Diagnostic IOC host (pythonSoftIOC; Linux). `python -m diag_ioc --config FILE.toml` serves each configured module's records over CA and PVA, plus `STAT:*` link and analysis status and the devIocStats health records. Modules subclass `module.DiagnosticModule` (`create_records`, `analyze`, `publish`) and build records with `records.py`. `link.py` is the latest-wins shot link from acquisition (`IocLink`) to the IOC (`LinkListener`, which refuses a message announcing more than 1 GiB, about 80× today's shot), with its own framing (`framing.py`) and allow-list (`network.py`), so the package never imports `streamer/`; `outage.py` logs a persisting failure when it starts, every 5 min, and on recovery. Importing the package never loads softioc, so acquisition can use the link. `pip install -e '.[ioc]'` |
| [deploy/systemd/](deploy/systemd/) | `diag-ioc@.service`: one IOC instance per `/etc/diag-ioc/<name>.toml` (user and paths are placeholders) |
| [docs/](docs/) | `ARCHITECTURE.md`: decisions D1–D15 for the live analysis and EPICS IOC; `HANDOFF_live_epics.md`: the commit-by-commit plan |
| [streamer/](streamer/) | Buffered ADIOS/socket raw output, metadata encoding, consumer, security, compression, and remote restart support |

The live GUI will be re-implemented under EPICS. The HDF5 readers and the datarun merge scripts may return later on this branch.

## Existing HDF5 data (previous acquisition)

Each daily file is named `interferometer_data_YYYY-MM-DD.hdf5`. Its groups each contain one dataset per shot, named by its timestamp in seconds since the Unix epoch:

| Group | Contents | Unit |
|---|---|---|
| `phase_p20/`, `phase_p29/`, `phase_p40/` | Phase per port. The group attributes give the microwave frequency and the calibration factor, which assumes a 40 cm plasma path | rad |
| `time_array/` | LeCroy time base | ms |
| `time_array_p40/` | Rigol time base, separate from `time_array` | ms |

- `phase_p40` datasets have per-shot attributes `rigol_missing` and `rigol_missing_reason`. When the Rigol was down, the dataset is a zero-filled placeholder.
- Files from before port 40 was added have only `phase_p20`, `phase_p29`, and `time_array`.
- Timestamps mark when the LeCroy saved its C4 file, which can be slightly later than the shot.
