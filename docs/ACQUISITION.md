# Acquisition

[interf_main.py](../interf_main.py) and [interf_raw.py](../interf_raw.py): read both scopes over Ethernet, name and time each shot, and hand it to the outputs. Overview in the [README](../README.md).

Acquisition stops once it has the raw samples. It writes nothing to disk but its log, the shot counter and, with `INTERF_RAW_OUTPUT`, the raw archive, and computes no phase in the loop. It runs on **Linux only**, because the Rigol deadline uses `signal.setitimer`. Both scope drivers come from [`lab-scopes`](https://github.com/hjia94/lab_scopes) `v0.4.2`, which reads the LeCroy trigger time as America/Los_Angeles (on Windows, `pip install .` also installs `tzdata`).

## Scopes

| Scope | Ports | Channels | Link |
|---|---|---|---|
| LeCroy (trigger master) | 20 (288 GHz), 29 (282 GHz) | C1/C2 = port 20 ref/plasma, C3/C4 = port 29 ref/plasma | VICP over TCP |
| Rigol DHO804, `192.168.7.63` | 40 (288 GHz) | C1 = ref, C2 = plasma | SCPI, TCP port 5555; 12-bit WORD reads |

The LeCroy trigger-out is hard-wired to the Rigol trigger input, so the Rigol triggers on every LeCroy trigger.
- The LeCroy is armed for one trigger per shot. It emits no further trigger-out until it is re-armed.
- The Rigol free-runs in AUTO sweep, as on `main`. Per shot it is stopped, read, and resumed, before the LeCroy is re-armed.

So both records hold the same shot. The one exception is an AUTO-forced Rigol acquisition landing between the trigger and the stop; that is pending a bench test. The same-shot rules are in the [interf_raw.py](../interf_raw.py) docstring.

## Running

```bash
LECROY_IP=<address> python interf_main.py
```

- Each LeCroy capture produces a shot line and then a timing line, written to stdout and to `$INTERF_LOG_DIR/interf_acquire.log`. The default directory is `~/data/log`, and the log rotates at midnight, keeping 30 days. A shot line gives:
  - the shot ID, `shot <YYYYMMDD>-<number>`;
  - host time, in Los Angeles time like every time on the line;
  - LeCroy trigger time, and Δt since the previous capture (≈ 3 s steady, ≈ 6 s when a shot is skipped, "-" after a shot with no trigger time);
  - points per channel;
  - time from capture detection to LeCroy re-arm;
  - any missing scope, with the reason;
  - last, `png <run>-<shot> lag <s>`: PNG's run and shot numbers copied at capture detection, and `host_time` minus the time PNG wrote that shot number; `png -` without PNG or before its first shot number. A lag near 0–0.2 s means the same shot, and about 3 s means PNG's previous shot (D16).
- The timing line, always INFO, is `timing <YYYYMMDD>-<number> (ms) | <step> <ms> | ...`: the steps of the loop pass that captured the shot, in the order they run, each in ms with one decimal (`time.perf_counter()`), and `-` for a step that did not run. Grep `interf_main: timing` for them. The shot counter resumes from shot lines only, and the timings are not sent over the link.
  - `open`: connecting to the LeCroy;
  - `prepare`: reading its trigger mode and arming it if needed, then `armed`, or `not armed` when it was already SINGLE or a capture was pending;
  - `wait`: from the end of `prepare` to capture detection;
  - `onoff`: asking the LeCroy whether each `LECROY_CHANNELS` entry is displayed (`<ch>:TRACE?`), once per shot. A channel that is off is not read, and is missing as `trace off`. If a query fails, times out or gets a reply other than ON/OFF, no channel is read, the first channel's reason says why (`on/off check failed: ...`), and the LeCroy is not re-armed;
  - `C1` … `C4` (`LECROY_CHANNELS`): each channel's read on its own, which includes the scope's processing and the network transfer; `-` for a channel that is off, failed or was not read;
  - `rigol`: the Rigol read, then `tried`, `skipped` (backoff after a failure) or `disabled` (`RIGOL_IP` empty);
  - `rearm`: the LeCroy re-arm; `-` when it was not re-armed (a stop request or a LeCroy link error);
  - `crit`: the shot line's `path` (capture detection to re-arm), in ms;
  - `close`: closing the LeCroy connection, after the re-arm;
  - `identify`: assigning the shot identity; `outputs`: every output's `write()` (an `IocLink` only queues the shot);
  - `cycle`: from the previous capture detection to this one.
- Shot identity ([interf_shot.py](../interf_shot.py)): the shot time is the LeCroy trigger time from the WAVEDESC (keep the scope clock NTP-synced), or the host time when a shot has none. A shot is its Los Angeles date plus a number from 0 each day; a background thread saves the counter after every shot, so a restart the same day continues. A gap in the numbers is a lost shot. A host time before the trigger time, or more than `INTERF_TRIG_LAG_MAX_S` after it, is logged as a clock problem.
- A pause in triggers, or a LeCroy that keeps failing to connect or respond, is logged when it starts, then every 5 min, then once more when capture resumes. After a LeCroy error the next attempt waits `LECROY_RETRY_INTERVAL`.
- **Ctrl-C** (or SIGTERM from systemd): a shot already being read is finished and logged. The LeCroy is then set to NORM, the Rigol is sent `:RUN` (normally already running; not when disabled), and every connection is closed. A **second** Ctrl-C exits immediately, without restoring the trigger modes.

## Configuration

Each variable is read from the environment at startup; unset means the default. The first five rows are in [interf_main.py](../interf_main.py), `INTERF_TRIG_LAG_MAX_S` in [interf_shot.py](../interf_shot.py), `INTERF_DATA_DIR` in [interf_store.py](../interf_store.py) (not acquisition), the rest in [interf_raw.py](../interf_raw.py). The time zone of trigger times and shot dates is always America/Los_Angeles, not a setting.

| Variable | Default | Meaning |
|---|---|---|
| `INTERF_LOG_DIR` | `~/data/log` | Log directory |
| `INTERF_IOC_LINK` | unset | `unix:/path` or `tcp:host:port` of a `diag_ioc` listener. When set, every shot is also sent there for live analysis; unset, acquisition runs exactly as before |
| `INTERF_PNG_RUN_PV`, `INTERF_PNG_SHOT_PV` | unset | PNG's CA PVs, e.g. `PNG:RunNumber` and `PNG:ShotNumber`. With both set, acquisition monitors them with pyepics (the `clients` extra) and copies their latest values and timestamps into every shot (`RawShot.png`, the shot line, the raw archive once streamer writes them; D16). At start it waits at most 5 s for both first values, then goes on. If PNG's IOC is down, acquisition runs normally and shots keep the last values. If PNG's IOC is on another subnet, set `EPICS_CA_ADDR_LIST` to its address too. One of the two alone is logged and ignored |
| `INTERF_RAW_OUTPUT` | unset | The raw ADIOS archive (streamer's `RawOutput`, as `interf_sim --raw-output`): a direct ADIOS path, written beside the IOC link. Its timing log is `raw_output.jsonl` in `INTERF_LOG_DIR`, appended to and never rotated. A socket destination is not supported here (no private key is passed). Unset, no archive |
| `INTERF_SHOT_STATE` | `~/data/state/shot_counter.json` | Shot counter state (date and last number). Keep it on a local disk and outside `INTERF_LOG_DIR`, whose rotated files are deleted. At start, counting resumes after the later of this file and the last shot line in `interf_acquire.log` and its newest rotated copy. An unreadable file is logged as a warning; only if the logs hold no shot either can a running day repeat numbers |
| `INTERF_TRIG_LAG_MAX_S` | 5 s | Largest normal host time − trigger time; outside 0 to this, a clock problem is logged. 0 disables the check |
| `INTERF_DATA_DIR` | `~/data/interferometer` | `interf_store`'s directory of the daily HDF5 files, created if missing; `interf-store.service` sets `/var/lib/interferometer/hdf5`. `--data-dir` overrides it |
| `LECROY_IP` | `10.10.10.10` | **Placeholder; set it** |
| `LECROY_CHANNELS` | `C1,C2,C3,C4` | Channels read, comma-separated. The first one carries the sweep counter used to detect a fresh capture. Each must be one of `C1` … `C<LECROY_N_CHANNELS>`; otherwise acquisition stops at startup with an error naming the bad entries, since a channel the scope lacks would cost a `LECROY_TIMEOUT` at every connection and every shot |
| `LECROY_TIMEOUT` | 5 s | VICP socket timeout |
| `LECROY_N_CHANNELS` | 4 | Input channels on the LeCroy: 2, 4 or 8 (anything else stops acquisition at startup). Passed to the driver so it skips its channel-count probe, which costs a full `LECROY_TIMEOUT` per connection on the HDO4104 |
| `LECROY_RETRY_INTERVAL` | 5 s | Wait before retrying after a LeCroy error |
| `TRIGGER_TIMEOUT` | 10 s | How long to wait for a trigger before logging a pause and retrying |
| `RIGOL_IP`, `RIGOL_REF_CH`, `RIGOL_PLA_CH` | `192.168.7.63`, `C1`, `C2` | Rigol address and channels. `RIGOL_IP=` (empty) disables the Rigol: it is never contacted, and every shot has `missing["rigol"]` = `disabled (RIGOL_IP empty)`, so P40 is published as missing with that reason |
| `RIGOL_RETRY_INTERVAL` | 100 shots | How long the Rigol is skipped after a connect, deadline, or read failure |
| `RIGOL_CONNECT_TIMEOUT` | 1 s | TCP connect timeout |
| `RIGOL_OPERATION_TIMEOUT` | 2.5 s | Hard deadline for one Rigol connect, stop, both channel reads, and run (and for `:RUN` on exit). Keep the Rigol at ≤ 1M points: a WORD read takes about 0.77 s per 1M-point channel |

A Rigol operation that hangs is cut off by `SIGALRM` at its deadline, so it cannot hold up the LeCroy. After a cut-off read, the Rigol can stay stopped until its next successful read, as on `main`.

## Output

`interf_raw.acquire_shot(state, stop_requested)` returns `None` when nothing was captured (a pause, a LeCroy error, or a stop request), or a `RawShot` with these fields:

- `lecroy`: `{ch: (int16 samples, 346-byte WAVEDESC)}`;
- `rigol`: `{ch: (uint16 12-bit codes, calibration metadata dict)}`. The Rigol has no header block, so voltages are `(code - y_origin - y_reference) * y_increment`;
- `missing`: `{"lecroy" | "rigol": reason}`. A missing Rigol has an empty data dict. `missing["lecroy"]` lists failed channels, and `lecroy` still holds the channels that were read;
- `host_time` and `critical_path_s`;
- `png`: PNG's `{"run", "run_time", "shot", "shot_time"}` copied right after `host_time`, None per value not yet received, or `None` without PNG (D16). A value can be unchanged from earlier shots; a reader matches it to this shot when `abs(host_time - shot_time) < 0.3 s`;
- `shot_date`, `shot_number`, `shot_time` and `time_source` (`trigger` or `host`): `None` from `acquire_shot`, then set by `interf_main` before the outputs. The raw archive does not store them.

`interf_main.main(outputs)` assigns each shot's identity, writes the shot to every output, then logs it. A failing output is logged (traceback when it starts failing, a count every 5 min, a line on recovery) and never stops the other outputs or the loop. The command-line entry point passes a `RawOutput` when `INTERF_RAW_OUTPUT` is set and an `IocLink` when `INTERF_IOC_LINK` is set, and closes them on exit.

With `INTERF_IOC_LINK`, the output is an `IocLink` ([diag_ioc/link.py](../diag_ioc/link.py)). Its `write()` only queues the shot; a background thread encodes it with [interf_payload.py](../interf_payload.py) (schema 2: the shot identity, then the raw arrays) and sends it. It is latest-wins: it keeps at most the 2 newest unsent shots, never blocks acquisition, and does not resend a shot lost during an outage (completeness is the raw archive's job, D9). An outage is logged when it starts, every 5 min, and on recovery with the number of shots not delivered; reconnection is retried every 5 s. Each delivered shot logs, from the sender thread, `IOC link <address> timing (ms) | encode <ms> | send <ms> | sent <count>`; a reconnect before the send is not counted in it.
