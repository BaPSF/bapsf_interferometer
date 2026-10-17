# bapsf_interferometer

Acquisition and analysis for the BaPSF microwave interferometers at ports 20, 29, and 40.

> **Refactor in progress:** branch `refactor/linux-epics-daq`.
> Acquisition now reads both scopes directly over Ethernet on Linux and stops once it has the raw samples. Nothing is written to disk but the log and the shot counter, and no phase is computed in the loop. With `INTERF_IOC_LINK` set, each shot also goes to the live analysis IOC, which publishes it to EPICS. The HDF5 layout below is for files written by the previous acquisition, which ran on Windows and read LeCroy `.trc` files.

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

- Each LeCroy capture produces a shot line and then a timing line, written to stdout and to `$INTERF_LOG_DIR/interf_acquire.log`. The default directory is `~/data/log`, and the log rotates at midnight, keeping 30 days. A shot line gives:
  - the shot ID, `shot <YYYYMMDD>-<number>`;
  - host time, in Los Angeles time like every time on the line;
  - LeCroy trigger time, and Δt since the previous capture (≈ 3 s steady, ≈ 6 s when a shot is skipped, "-" after a shot with no trigger time);
  - points per channel;
  - time from capture detection to LeCroy re-arm;
  - any missing scope, with the reason.
- The timing line, always INFO, is `timing <YYYYMMDD>-<number> (ms) | <step> <ms> | ...`: the steps of the loop pass that captured the shot, in the order they run, each in ms with one decimal (`time.perf_counter()`), and `-` for a step that did not run. Grep `interf_main: timing` for them. The shot counter resumes from shot lines only, and the timings are not sent over the link.
  - `open`: connecting to the LeCroy;
  - `prepare`: reading its trigger mode and arming it if needed, then `armed`, or `not armed` when it was already SINGLE or a capture was pending;
  - `wait`: from the end of `prepare` to capture detection;
  - `C1` … `C4` (`LECROY_CHANNELS`): each channel's read on its own, which includes the scope's processing and the network transfer; `-` for a channel that failed or was not read;
  - `rigol`: the Rigol read, then `tried`, `skipped` (backoff after a failure) or `disabled` (`RIGOL_IP` empty);
  - `rearm`: the LeCroy re-arm; `-` when it was not re-armed (a stop request or a LeCroy link error);
  - `crit`: the shot line's `path` (capture detection to re-arm), in ms;
  - `close`: closing the LeCroy connection, after the re-arm;
  - `identify`: assigning the shot identity; `outputs`: every output's `write()` (an `IocLink` only queues the shot);
  - `cycle`: from the previous capture detection to this one.
- Shot identity ([interf_shot.py](interf_shot.py)): the shot time is the LeCroy trigger time from the WAVEDESC (keep the scope clock NTP-synced), or the host time when a shot has none. A shot is its Los Angeles date plus a number from 0 each day; a background thread saves the counter after every shot, so a restart the same day continues. A gap in the numbers is a lost shot. A host time before the trigger time, or more than `INTERF_TRIG_LAG_MAX_S` after it, is logged as a clock problem.
- A pause in triggers, or a LeCroy that keeps failing to connect or respond, is logged when it starts, then every 5 min, then once more when capture resumes. After a LeCroy error the next attempt waits `LECROY_RETRY_INTERVAL`.
- **Ctrl-C** (or SIGTERM from systemd): a shot already being read is finished and logged. The LeCroy is then set to NORM, the Rigol is sent `:RUN` (normally already running; not when disabled), and every connection is closed. A **second** Ctrl-C exits immediately, without restoring the trigger modes.

### Configuration

Each variable is read from the environment at startup; unset means the default. The first three are in [interf_main.py](interf_main.py), `INTERF_TRIG_LAG_MAX_S` in [interf_shot.py](interf_shot.py), the rest in [interf_raw.py](interf_raw.py). The time zone of trigger times and shot dates is always America/Los_Angeles, not a setting.

| Variable | Default | Meaning |
|---|---|---|
| `INTERF_LOG_DIR` | `~/data/log` | Log directory |
| `INTERF_IOC_LINK` | unset | `unix:/path` or `tcp:host:port` of a `diag_ioc` listener. When set, every shot is also sent there for live analysis; unset, acquisition runs exactly as before |
| `INTERF_SHOT_STATE` | `~/data/state/shot_counter.json` | Shot counter state (date and last number). Keep it on a local disk and outside `INTERF_LOG_DIR`, whose rotated files are deleted. At start, counting resumes after the later of this file and the last shot line in `interf_acquire.log` and its newest rotated copy. An unreadable file is logged as a warning; only if the logs hold no shot either can a running day repeat numbers |
| `INTERF_TRIG_LAG_MAX_S` | 5 s | Largest normal host time − trigger time; outside 0 to this, a clock problem is logged. 0 disables the check |
| `LECROY_IP` | `10.10.10.10` | **Placeholder; set it** |
| `LECROY_CHANNELS` | `C1,C2,C3,C4` | Channels read, comma-separated. The first one carries the sweep counter used to detect a fresh capture |
| `LECROY_TIMEOUT` | 5 s | VICP socket timeout |
| `LECROY_RETRY_INTERVAL` | 5 s | Wait before retrying after a LeCroy error |
| `TRIGGER_TIMEOUT` | 10 s | How long to wait for a trigger before logging a pause and retrying |
| `RIGOL_IP`, `RIGOL_REF_CH`, `RIGOL_PLA_CH` | `192.168.7.63`, `C1`, `C2` | Rigol address and channels. `RIGOL_IP=` (empty) disables the Rigol: it is never contacted, and every shot has `missing["rigol"]` = `disabled (RIGOL_IP empty)`, so P40 is published as missing with that reason |
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

With `INTERF_IOC_LINK`, the output is an `IocLink` ([diag_ioc/link.py](diag_ioc/link.py)). Its `write()` only queues the shot; a background thread encodes it with [interf_payload.py](interf_payload.py) (schema 2: the shot identity, then the raw arrays) and sends it. It is latest-wins: it keeps at most the 2 newest unsent shots, never blocks acquisition, and does not resend a shot lost during an outage (the raw archive is the complete record). An outage is logged when it starts, every 5 min, and on recovery with the number of shots not delivered; reconnection is retried every 5 s. Each delivered shot logs, from the sender thread, `IOC link <address> timing (ms) | encode <ms> | send <ms> | sent <count>`; a reconnect before the send is not counted in it.

## Live analysis IOC

`diag_ioc` runs [interf_ioc.py](interf_ioc.py), which analyzes every shot from the link with `analyze_shot` and serves the results as EPICS PVs over CA and PVA, under the prefix `LAPD:DIAG:INTERF`. The PV list is D4 in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

```bash
pip install -e '.[ioc,clients]' -c constraints.txt   # constraints.txt pins the tested EPICS stack
python -m diag_ioc --config my_interferometer.toml   # a copy of deploy/diag-ioc/interferometer.toml
INTERF_IOC_LINK=unix:/run/diag-ioc/interferometer/link.sock LECROY_IP=<address> python interf_main.py
```

- [deploy/diag-ioc/interferometer.toml](deploy/diag-ioc/interferometer.toml) is the production config, run by `diag-ioc@interferometer` from `/etc/diag-ioc/` with `readonly.acf` beside it. For a trial run, copy it and change the prefix, `listen` and `db_dir`.
- Every PV of a shot carries the shot time as its timestamp. `SHOT:NUM` is set last, and the PVA group `<prefix>:SHOT` updates once per shot with every value of it. A CA client that falls behind can miss or mix values of a shot; only the group is atomic.
- A port without data (P40 when the Rigol is missing, which is every shot replayed from LeCroy-only `.trc` files) gets empty arrays with INVALID severity, and `SHOT:MISSING` says why.
- Module options: `max_points` (2048; a 1,000,002-sample record gives 1953 points per port, so none are dropped), `ne_window_ms` (default 5 to 10 ms), `plasma_length_m` (0.4) and `ports` (default P20, P29, P40). `<port>:CAL`, `<port>:FREQ` and `CFG:NE_WINDOW_MS` show them; a change needs an IOC restart.
- At every start the IOC writes `<config name>.db`, listing every record, into `[ioc] db_dir`. `[ioc] acf` installs an access file; `readonly.acf` makes every PV read-only over CA and PVA.
- Each publish waits, at most 2 s, until the previous shot's records have all processed (`SHOT:ACK` reaches `STAT:PUBLISHED`), so no update mixes two shots. `STAT:ACK_TIMEOUTS` counts the waits that timed out and should stay 0.
- Each published shot logs one INFO line to the IOC log (stdout; the journal under systemd): `module interferometer timing <YYYYMMDD>-<number> (ms) | decode <ms> | P20 <ms> | P29 <ms> | P40 <ms> | ack wait <ms> | publish <ms>`. `decode` turns the link message into arrays, each port is its own analysis, `ack wait` is the wait above, and `publish` sets the records. A shot whose analysis or publish failed logs none. `STAT:ANALYSIS_S` is unchanged: the whole of analysis, ack wait and publish.

## Offline simulation

`interf_sim/` runs `interf_main` and `interf_raw` on recorded LeCroy `.trc` files, and contacts no scope. It replaces only the two lab_scopes driver classes that `interf_raw` imports, so a change to `interf_main` or `interf_raw` takes effect in the simulation without editing it.

```bash
python -m interf_sim --limit 20    # from the repo root; --help lists the options
python -m interf_sim --limit 20 --repeat-traces  # cycle available traces into 20 synthetic shots
python -m interf_sim --raw-output raw.bp --limit 20  # write complete raw shots to BP5
python -m interf_sim --ioc-link unix:/tmp/interf.sock --limit 20  # also send shots to a live-analysis listener
python -m interf_sim --loop --ioc-link unix:/run/diag-ioc/interferometer/link.sock  # until Ctrl-C or SIGTERM; as diag-ioc, which owns the socket
```

`python -m interf_sim.listen ADDRESS [--analyze] [--ne-window-ms START STOP]` is a development listener for `--ioc-link` and `INTERF_IOC_LINK`. It prints one line per shot received, and with `--analyze` adds each port's points and density, or the reason the port is missing. Use `tcp:127.0.0.1:PORT` where unix sockets are unavailable.

- The `.trc` directory is `TRC_DIR` in [interf_sim/trc_replay.py](interf_sim/trc_replay.py) (`D:/data/raw data` on the lab PC). On Linux, edit that line or pass `--trc-dir`.
- Shots play in trigger-time order, because the file counter wraps. Indexing reads one header per shot: 5–20 s for 29k shots, depending on the disk cache. Each machine trigger, every `--period` s (default 3; 0 = as fast as files load), serves the next shot.
- `--repeat-traces` requires `--limit`. It cycles the indexed shots until it has exactly that many entries, assigning consecutive counters from the first selected counter. The channel dictionaries refer to the original `.trc` files, so their recorded trigger timestamps do not change: each cycle repeats the trigger times and runs back in time. For long runs use `--loop`.
- `--loop` serves the selected shots (`--start-shot`, `--limit`) pass after pass until Ctrl-C or SIGTERM, then stops as acquisition does and prints the link summary. Every pass looks like new shots:
  - Pass k serves every WAVEDESC with its trigger time moved k × `--loop-offset` seconds later. The files are not changed.
  - The default offset is the shots' trigger-time span plus their mean spacing, so each pass starts about one shot interval after the last ends. An offset no larger than the span is refused, since it would put trigger times out of order.
  - Shot numbers continue from pass to pass. When the shifted times cross midnight in Los Angeles, the shot date advances and numbering restarts at 0, as in acquisition (and `SHOT:LOST` restarts its baseline).
  - Numbering always starts at 0 when the simulator starts, so a second run on the same date repeats the first run's IDs.
  - `--loop` refuses `--raw-output` and `--repeat-traces`.
- There is no Rigol: every shot has `missing["rigol"]`.
- When the shots run out (never with `--loop`), the simulator sends SIGINT, so the Ctrl-C stop and release path runs. The log goes to `interf_sim/log/` and rotates by size, keeping at most about 250 MB, because a `--loop` or `--period 0` run can log far more per day than acquisition.
- Transfers take only the file read time, so `critical_path_s` and the timing line's channel reads are shorter than on the scopes. The logged `dt` follows the recorded trigger times, not `--period`.
- Shot dates are the recording's. Shot numbers start at 0 on every run and are not saved (`INTERF_SHOT_STATE` is not used), so runs are repeatable and never add to acquisition's numbering. The trigger-lag check is off, since recorded trigger times are older than the replay.
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
| [diag_ioc/](diag_ioc/) | Diagnostic IOC host (pythonSoftIOC; Linux). `python -m diag_ioc --config FILE.toml` serves each configured module's records over CA and PVA, plus `STAT:*` link and analysis status, the publish ack (`SHOT:ACK`, `STAT:PUBLISHED`, `STAT:ACK_TIMEOUTS`) and the devIocStats health records. Modules subclass `module.DiagnosticModule` (`create_records`, which returns its chain's last record, `analyze`, `publish`) and build records with `records.py`. `link.py` is the latest-wins shot link from acquisition (`IocLink`) to the IOC (`LinkListener`, which refuses a message announcing more than 1 GiB, about 80× today's shot), with its own framing (`framing.py`) and allow-list (`network.py`), so the package never imports `streamer/`; `outage.py` logs a persisting failure when it starts, every 5 min, and on recovery. Importing the package never loads softioc, so acquisition can use the link. `pip install -e '.[ioc]' -c constraints.txt` |
| [interf_ioc.py](interf_ioc.py) | The interferometer's `diag_ioc` module: link message -> `analyze_shot` -> the D4 PVs and the PVA group `<prefix>:SHOT` |
| [deploy/diag-ioc/](deploy/diag-ioc/) | `interferometer.toml`, the production IOC config, and `readonly.acf`, its read-only access file |
| [deploy/systemd/](deploy/systemd/) | System units, run as the `diag-ioc` account: `diag-ioc@.service` (one IOC per `/etc/diag-ioc/<name>.toml`) and `interf-acquire.service` (acquisition), with `acquire.env.example`; install steps in their headers |
| [constraints.txt](constraints.txt) | Exact versions of the EPICS stack (`softioc`, `epicscorelibs`, `pvxslibs`, `p4p`, `pyepics`), for `pip install -c` |
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
