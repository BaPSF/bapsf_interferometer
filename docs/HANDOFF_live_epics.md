# Handoff: live analysis + EPICS IOC for `bapsf_interferometer`

Repository `BaPSF/bapsf_interferometer`, branch `refactor/linux-epics-daq`. Updated 2026-10-11. This file is the
implementation spec for the remaining commits. It implements the decisions in `docs/ARCHITECTURE.md` (D1–D15) and does
not repeat their reasons. A question neither file answers goes to the user before code is written.

**Single source:** this file is `docs/HANDOFF_live_epics.md` on `refactor/linux-epics-daq`, next to `docs/ARCHITECTURE.md`.
Every session (the local coding agent, the cloud test agent and `test-on-Bobby`) pulls the branch and reads both before
any work. Copies elsewhere (the project's shared folder, an agent's memory) are not authoritative. Doc changes land only
as commits by the local agent; other sessions send them as patch files.

- **Local agent (Windows):** writes and pushes each commit to `origin/refactor/linux-epics-daq`, pulling first. Never
  force-push; no pull request unless asked. It runs no tests unless the user asks; the cloud session tests every pushed commit.
- **Cloud test session:** for each pushed commit, the user sends `test <sha> (Cn)`. It runs the full suite on Linux and
  that commit's scenario on synthetic data, then reports. It pushes nothing. Linux-only tests (unix sockets, acquisition
  timers, the IOC scenarios) are verified there, not before the push.
- **`test-on-Bobby`** (the Linux lab machine with the `.trc` recordings): runs the real-data checks (§5), the PVA tests
  and the PVA bench re-run, then reports. It pushes nothing.
- **Real-data gates** (§5) are deferred until `test-on-Bobby` is usable. Mark each stage "pending real-data check"; do
  no `.trc`-dependent work before then.

## 1. Done so far

| Commit | What |
|---|---|
| 1620cfd C1 | `_handle_shot` fans out to a list of outputs, failures isolated (outages keyed by `id()`) |
| 73d7c67 C2, b6d8c6c C2a/C2b | `streamer.payload.shot_from_variables` (C4r: moved to `interf_archive.decode`); `interf_analysis.analyze_shot` per port; `interf_sim.synthetic`; flat or too-short channels marked missing |
| 3119997 C3, f26c882 C3a | `diag_ioc/link.py`: `IocLink` (latest-wins sender) and `LinkListener`; `INTERF_IOC_LINK`, `interf_sim --ioc-link`, `interf_sim.listen`; unix paths over 107 bytes rejected |
| 3ca9aca C3b, 60e4dca C3c/C3d | `streamer.adios_io.iter_steps`/`read_step` (BP5 and BP4, open files end at the last step); C4r moved them to `interf_archive` and restored `streamer/` to 642429a |
| 32f40cc C4 | `diag_ioc` host: TOML config, `ModuleHost`, `ShotPipeline`, `StatusRecords` (`STAT:*`), devIocStats, `diag-ioc@.service`, `tests/ioc_harness.py` |
| 4ee26b8 C4r | Architecture review applied: `streamer/` restored to 642429a; `interf_archive` (offline archive reader); `diag_ioc/framing.py`, `diag_ioc/network.py`; `IocLink` needs an encoder and no longer numbers shots; `interf_payload` (schema 2); `interf_shot` (shot identity and trigger time); C4 test fixes; `docs/` |
| ebb0b9f | `LinkListener` holds an `flock` on `<path>.lock`, so a second listener on one unix socket path fails at start |

Facts from testing that still matter:
- Analysis takes 0.18–0.25 s per shot at 1M samples per channel, but 2.35 s at 10M. Measure on the IOC host once the
  real record length is known; `scipy.fft.rfft` with `workers=-1` in `correlation_spectrogram` gives the same peaks
  faster.
- `critical_path_s` is unchanged by the link (median about 4 ms with and without).
- Two listeners on one unix socket path (one config spelling it two ways, or two IOC processes) used to let the second
  silently take the socket, and stopping the first then removed it. Fixed after C4r: `LinkListener` holds
  `<path>.lock` (`flock`), so a second listener fails to start with EADDRINUSE. No `os.path.abspath` config check: it
  resolves `..` and `//` differently from the kernel.
- Run single test modules with `python -m unittest discover -s tests -p test_x.py -v`. The dotted form fails, because
  the tests import `ioc_harness` as a top-level module.
- A disconnected channel that still delivers noise is not caught. A signal-quality (SNR) check waits for real data.
- C4r APIs that later commits use:
  - `interf_payload.encode(shot)` / `decode(variables) -> DecodedShot`, which `analyze_shot` accepts; `decode` raises
    `ValueError` for a `schema_version` other than 2.
  - `interf_shot`: `SHOT_ZONE`, `local_time`, `clock`, `shot_date`, `shot_id`, `trigger_time(wd, host_time)`,
    `ShotIdentifier(counter, trig_lag_max_s).identify(shot)` (never raises) and `ShotCounter(state_path)`.
  - `ShotResult` carries `shot_date`, `shot_number`, `shot_time` and `time_source` (None where the shot has none).
  - `interf_archive.iter_steps`/`read_step`/`decode` read the ADIOS archive (streamer's schema 1, no shot identity).
    It imports `streamer.payload` for that schema, which is fine offline; the live path must not.

## 2. Conventions

- Tabs in `interf_*.py`, `interf_sim/`, `tests/`, `diag_ioc/` and new modules. Never edit `streamer/` (D6).
- Short docstrings; comments explain why; `log = logging.getLogger(__name__)`. Acquisition settings come from
  environment variables through `interf_raw._env`-style helpers.
- Tests: stdlib `unittest` only (no pytest), `python -m unittest discover -s tests -v`. Tests that need optional
  packages use `@unittest.skipUnless`.
- Python ≥ 3.11. The layout is flat: add every new module to `[tool.setuptools] py-modules`, and every new package to
  `packages`, in the same commit.
- Conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`), with a short body saying why.
  Keep the README "Files" and "Configuration" tables current.
- Setup: `pip install -e '.[ioc,clients]'` (`-c constraints.txt` from C5), plus `p4p` for PVA tests.
- `interf_main`'s main thread owns SIGALRM and the LeCroy re-arm. Anything added there is O(1), never blocks, never
  raises into the loop, and never imports EPICS.

## 3. API facts (verified; rely on these)

**lab_scopes v0.4.1** (pinned since C4r). `LeCroyWavedesc(b).wd` holds the 346-byte WAVEDESC fields. The
trigger time is `tt_second` (double), `tt_minute`, `tt_hours`, `tt_days`, `tt_months` and `tt_year`; `tt_year == 0` means
unset. These are the scope's wall-clock time with no zone. In v0.4.1, `lab_scopes.lecroy.wavedesc.wavedesc_trigger_timestamp(wd)`
reads them in `SCOPE_TIMEZONE` (`America/Los_Angeles`) with `zoneinfo` and returns epoch seconds, or `None` when unset
or unparseable. It never raises, and in the repeated fall-back hour it returns the first occurrence. (v0.4.0 used
`calendar.timegm`, which is wrong by 7–8 h.) Repack a changed WAVEDESC with `struct.pack(lab_scopes.lecroy.wavedesc.WAVEDESC_FMT, *wd)`.

**pythonSoftIOC (softioc 4.7.2).**
- Order: create every record, then `builder.LoadDatabase()`, then `softioc.iocInit(dispatcher)`. That runs once per
  process, so tests run the IOC in a subprocess. Set `EPICS_*` before importing softioc.
- `builder.SetDeviceName(prefix)` takes the prefix with no trailing colon. Constructors: `aIn`, `longIn`, `boolIn`,
  `stringIn`, `longStringIn(length=)`, `WaveformIn(length=, datatype=float)`, `longOut`; EPICS fields go in as keywords.
  In-records default to `SCAN='I/O Intr'`, so pass `SCAN='Passive'` for data records.
- `rec.set(value, severity=, alarm=, timestamp=)` is thread-safe. On a Passive record it only stores the value, and VAL
  changes only when the record next processes (checked on 4.7.2: a Passive `aIn` set after `iocInit` still reads 0); on an
  I/O Intr record it processes the record and its FLNK chain. Give fixed values as `initial_value=` at creation (PINI
  processes them at `iocInit`). With `TSE=-2`, always pass `timestamp` (epoch s).
  Waveforms assert `len <= NELM`, and `longStringIn` asserts the text length.
- Chains: `diag_ioc.records.chain(makers)` creates records last-to-first, passing `FLNK=<next>`. `rec.FLNK = other` after
  construction also works (checked on 4.7.2: `WriteRecords` shows the link). Use the existing `records.waveform`,
  `records.scalar`, `records.add_group` and `records.fit_text` helpers.
- Ack pattern (verified in the IOC bench, which stays outside the repo):
  `longOut(name, OMSL="closed_loop", DOL="<P>:STAT:PUBLISHED", DISP=1, always_update=True, on_update=cb)`. DOL without PP
  reads the stored value, DISP=1 turns CA puts into no-ops, and `on_update` runs after the whole chain has processed.
- `imports.install_pv_logging(acf_path)` installs an access-security file before `iocInit`.
- `builder.WriteRecords(path)` writes the loaded records as a `.db`.

**PVXS groups (`rec.add_info("Q:group", {...})`).** The group name is the full PV name. Field names may be dotted
(`"p20.ne"`). `+type` is `scalar` (value, alarm, timeStamp), `plain` (value only) or `meta` (alarm and timeStamp; `""`
puts them at the top level). Put `+trigger: "*"` only on the last data record of the chain; a group with no trigger posts
on every member change.

**PVA needs kernel IPv6.** Without it, the IOC serves CA only and creating a p4p `Context` raises. Probe:
`socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)`. PVA tests skip when it fails. The cloud has no IPv6.

**Clients.**
- p4p 4.3.0: `Context("pva", conf=…, useenv=False).monitor(name, cb, notify_disconnect=True)`. Read fields as
  `v["p20"]["ne"]["value"]`.
- pyepics 3.5.10, for tests and ad-hoc scripts: `epics.PV(name, auto_monitor=True, form="time", callback=cb)`. Make no CA
  calls in callbacks.
  - **Subscription race:** pyepics reports a PV connected before it subscribes, so the last monitor created can
    silently never start. After creating the monitors, call `epics.ca.flush_io()` until every PV has delivered its first
    value, or fail and name the ones that didn't. Drop values that arrive before the first shot.
- Isolated EPICS for tests (IOC and clients): free `EPICS_CA_SERVER_PORT`/`EPICS_CA_REPEATER_PORT`/`EPICS_PVA_SERVER_PORT`/
  `EPICS_PVA_BROADCAST_PORT`, `*_ADDR_LIST=127.0.0.1`, `*_AUTO_ADDR_LIST=NO`, `EPICS_CAS_INTF_ADDR_LIST` and
  `EPICS_PVAS_INTF_ADDR_LIST` set to 127.0.0.1, and a unique PV prefix per run.
- Phoebus `.bob`: `<display version="2.0.0">`; XY plot `<widget type="xyplot" version="3.0.0">` with
  `<traces><trace><x_pv>…</x_pv><y_pv>…</y_pv></trace></traces>`; macros are written `$(P)`.
  `org.phoebus.pv/default=pva` picks the protocol for PV names with no prefix.

## 4. Remaining commits

Every commit leaves `python -m unittest discover -s tests -v` green, updates pyproject for new modules, and updates the
README where users see a change.

C4r is done. The local agent's next steps, in order:
1. **Sync the docs (once).** Replace `docs/ARCHITECTURE.md` with the supplied `ARCHITECTURE_DECISIONS.md`, keeping the
   name `docs/ARCHITECTURE.md` (the code and README cite it), and `docs/HANDOFF_live_epics.md` with the supplied handoff.
   Commit as `docs: sync the architecture and handoff to the latest review` and push. Skip this step if `docs/` already
   has this text.
2. **C4s** fixes the shot counter after the C4r tests.
3. **C5** adds the IOC module.

The cloud session runs each commit's tests after it is pushed.

**C4s `fix: keep the shot counter's disk write off the acquisition thread` (D14).** Files: `interf_shot.py`,
`interf_main.py`, `interf_sim/trc_replay.py`, `tests/test_interf_shot.py`, `README.md`. Found by the C4r tests:
`ShotCounter._save` fsyncs on the acquisition thread (0.7 ms median on a fast disk, unbounded on a slow or network one),
and a corrupt state file on a running day repeats numbers already used.
- The state file stays where C4r put it: `INTERF_SHOT_STATE`, default `~/data/state/shot_counter.json`
  (`interf_main.py`). It must be on a local disk and outside `LOG_DIR` (`INTERF_LOG_DIR`, default `~/data/log`),
  whose rotated files are deleted after 30 days.
- `ShotCounter(state_path, log_paths=())`:
  - `next(shot_time)` stays in memory and O(1). It hands `(date, number)` to a daemon writer thread through a single
    latest-wins slot. That thread runs today's `_save` (temp file, fsync, `os.replace`, the `Outage` logging).
    `close()` writes the last state and stops the thread; `interf_main` calls it at exit on the counter it created.
  - `state_path=None` persists nothing. `interf_sim/trc_replay.py` passes `None`, since the simulator is only a test
    tool, so test runs are repeatable. Update its docstring and the README's simulator notes, which today name
    `interf_sim/log/shot_counter.json`.
  - At construction, read the state file, then scan `log_paths` once for the last `interf_main: shot <date>-<number> |`
    line (the `shot_id` format). `interf_main` passes `interf_acquire.log` and the newest rotated `interf_acquire.log.*` in
    `LOG_DIR`. Take the later date of the two sources, and for that date the larger number. `next()` continues from it
    as today.
  - A state file that exists but cannot be read is logged as a warning. If the log gives no number either, the
    warning says that numbers already used today may repeat.
  - Update the class docstring and the README's `INTERF_SHOT_STATE` row, which today say an unreadable file restarts
    counting at 0.
- Tests:
  - a corrupt state file recovered from the log;
  - a log ahead of the state file (a lost last write);
  - a log line from an earlier date only, and neither source present;
  - `state_path=None` writes no file;
  - `next()` returns while the writer is blocked (a stub that waits on an `Event`), and `close()` writes the last state.

**C5 `feat: interferometer IOC module` (D2, D4, D5, D7, D8, D11).** Includes the read-only access file and the version
pins (parts at the end). Files: `interf_ioc.py`, `deploy/diag-ioc/interferometer.toml`, `diag_ioc/host.py`,
`diag_ioc/module.py`, `deploy/systemd/diag-ioc@.service`, `tests/ioc_harness.py`, `tests/dummy_ioc_module.py`,
`tests/test_interf_ioc.py`. The PV names are the D4 list. Records:

| PV | Record |
|---|---|
| `SHOT:NUM` | longIn, I/O Intr, TSE −2: chain head, set last |
| `SHOT:DATE`, `SHOT:LOST` | longIn, Passive, TSE −2, `MDEL=-1` |
| `SHOT:TIME_SOURCE`, `SHOT:TIME_STR` | stringIn, Passive, TSE −2 |
| `SHOT:HOST_TIME` (PREC 6), `SHOT:CRIT_PATH`, `STAT:TRIG_LAG` (s, `host_time − shot_time`) | aIn, Passive, TSE −2 |
| `SHOT:MISSING` | longStringIn 1024, Passive, TSE −2: each acquisition reason once, then port reasons that add something |
| `<port>:TIME_ARRAY`, `:PHASE`, `:NE` | WaveformIn f64, NELM `max_points`, Passive, TSE −2 |
| `<port>:NE_MEAN` | aIn, Passive, TSE −2, `MDEL=-1`; NaN + INVALID when NaN |
| `<port>:CAL`, `<port>:FREQ` (GHz), `CFG:NE_WINDOW_MS` (2 elements) | fixed: `initial_value=` from config in `create_records` (not in the chain); module option `ne_window_ms`, default `[5.0, 10.0]` |

- Ports come from `[[module.options.ports]]` (`name`, `scope`, `ref`, `plasma`, `freq_hz`; default
  `interf_analysis.PORTS`), validated at construction. PVs are created per configured port.
- Chain: `SHOT:NUM → DATE → TIME_SOURCE → HOST_TIME → TIME_STR → CRIT_PATH → STAT:TRIG_LAG → LOST → MISSING → <port>:TIME_ARRAY →
  PHASE → NE → NE_MEAN → … → <last port>:NE_MEAN → SHOT:ACK`.
- Group `<P>:SHOT`:
  - `""` (meta) and `num` from `SHOT:NUM`;
  - `date`, `time_source`, `host_time`, `lost`, `missing` (plain);
  - `<port>.time_array`, `.phase` and `.ne` (scalar), and `.ne_mean` (plain);
  - `+trigger: "*"` on the last port's `NE_MEAN`.
- `analyze(variables)` is `interf_payload.decode` plus `analyze_shot(..., ports=, max_points=, ne_window_ms=)`.
- `publish(result)`:
  - Set every Passive record with `timestamp=shot_time`.
  - Missing ports get empty arrays and `INVALID_ALARM`/`READ_ALARM`.
  - `SHOT:TIME_STR` is `shot_time` in LA time as `YYYY-MM-DD HH:MM:SS.fff` (never the host's local zone).
  - Truncate text to fit.
  - `SHOT:LOST` adds gaps in `shot_number`. A new date, a decrease, and the first shot after IOC start reset its
    baseline.
  - `SHOT:NUM.set(shot_number, timestamp=shot_time)` comes last.
- Host (generic, every module):
  - Create `STAT:PUBLISHED` (longIn left at the default I/O Intr, no FLNK, so its VAL changes at `set()`; a Passive one
    would leave the ack reading the old count), `SHOT:ACK` (the ack pattern) and `STAT:ACK_TIMEOUTS` (longIn). `STAT:TRIG_LAG` is
    the module's (above), since only the module knows the shot's times.
  - `DiagnosticModule.create_records(builder)` now returns its chain's last record (or `None` for a module without a
    chain); update the docstring and `tests/dummy_ioc_module.py`. The host sets `tail.FLNK = ack` before
    `LoadDatabase()`.
  - Before each `publish`, wait until the ack equals the last published count (an `Event` set from `on_update`). The
    wait times out at 2 s, then it logs, increments `STAT:ACK_TIMEOUTS` and proceeds. Then increment and set
    `STAT:PUBLISHED`, and call `publish`.
  - Write `<name>.db` (the config file's name, e.g. `interferometer.db`; `__main__` passes the path to `run`) with
    `WriteRecords` at every start into the new optional `[ioc] db_dir` (default: the config
    file's directory). The systemd unit adds `StateDirectory=diag-ioc`, and the example config sets
    `db_dir = "/var/lib/diag-ioc"`, because the non-root IOC cannot write `/etc/diag-ioc`.
- `ioc_harness.monitor_all(names, callback, timeout)` implements the pyepics flush rule (§3).
- CA tests (they run in the cloud):
  - every data PV equals `analyze_shot` of the same shot;
  - every timestamp equals `shot_time`;
  - a shot without a trigger time shows `TIME_SOURCE` = `host`;
  - a Rigol-missing shot gives empty P40 arrays, INVALID, and the reason in `SHOT:MISSING`;
  - `SHOT:LOST` counts gaps and resets;
  - back-to-back publishing leaves `SHOT:ACK == STAT:PUBLISHED` and `STAT:ACK_TIMEOUTS == 0`;
  - `monitor_all` sees exactly one update per member per shot for 5 shots;
  - a `caget` of `P20:NE` returns the full array.
- Extend C4r's `test_the_live_path_loads_no_streamer_module` to import `interf_ioc` too.
- PVA tests (skip without IPv6 and p4p): `<P>:SHOT` matches the shot, and a monitor gets one consistent update per shot.
- Cloud scenario: `diag_ioc` plus `interf_sim --ioc-link` on synthetic `.trc` files.
  - Read the D4 list over CA.
  - `STAT:STALE` goes MAJOR after the simulator stops.
  - Kill and restart the IOC mid-run: acquisition continues, and the link logs the outage and recovery.

**C5, read-only access (D8).** Add `deploy/diag-ioc/readonly.acf` (reads for all, writes for none), installed via the
new optional `[ioc] acf` path. Making `CFG:NE_WINDOW_MS` writable from the screen is a later change, not part of C5.
Test: a CA put is rejected. On an IPv6 machine, check that PVA puts are rejected too, and say so in the commit body.

**C5, version pins (D11).** Add `constraints.txt` with exact `softioc`, `epicscorelibs`, `pvxslibs` and
`p4p` versions as tested, and document `-c constraints.txt` in the README.

**C6 `feat: store analyzed shots in the daily HDF5 file` (D9).** Files: `interf_store.py`, `interf_file.py`,
`deploy/systemd/interf-store.service`, `tests/test_interf_store.py`; add `p4p` to the `clients` extra and the script
`interf-store`.
- `interf_file.create_sourcefile_dataset(..., lecroy_missing=None, shot=None)`. The new arguments are additive:
  - missing P20/P29 get `missing`/`missing_reason` attributes;
  - P40 keeps `rigol_missing`/`rigol_missing_reason`;
  - `shot` adds `shot_date`, `shot_number` and `time_source` attributes.
- `interf_store` takes `--prefix` and `--data-dir` (default `$INTERF_DATA_DIR` or `~/data/interferometer`).
  - It monitors `<P>:SHOT` with p4p, and each update is one complete shot. The callback only copies the value onto a
    bounded queue, and a writer thread stores it.
  - `shot_from_group(value)` is pure.
  - The file is `interferometer_data_<shot_date as YYYY-MM-DD>.hdf5`, opened per shot with
    `h5py.File(path, "a", libver="latest")` after `init_hdf5_file`.
  - Dataset arguments:
    - `dataA`/`dataB`/`dataC` are the P20/P29/P40 phases;
    - `t_ms` is the P20 time array, or P29's if P20 is empty;
    - `t_ms_C` is the P40 time array;
    - `saved_time` is `shot_time`, so the dataset name is the trigger time.
  - A port whose PHASE severity is INVALID is missing.
  - A `ValueError` for an existing dataset is a duplicate after a reconnect, so skip it.
  - Log disconnects once per outage, and log gaps in `shot_number`.
  - On exit (SIGTERM drains the queue), log a summary of shots stored, duplicates skipped and gaps.
- Tests:
  - `shot_from_group`;
  - `store_shot` with plain dicts: the legacy layout, the dataset name, a skipped duplicate, two files across a date
    change, and the attributes.
  - PVA integration (skip without IPv6, so it runs on the Linux machine): 3 shots give 3 datasets, and a restart adds no
    duplicates.

**C7 `feat: Phoebus screens`.** Files: `phoebus/interf_overview.bob`, `phoebus/diag_ioc_health.bob`, `phoebus/settings.ini`,
`tests/test_phoebus_screens.py`.
- PV names carry no protocol prefix. Macros are `P` and `IOC`.
- Phoebus runs on the IOC machine. `settings.ini` sets the CA and PVA address lists to `127.0.0.1` with automatic
  address lists off, so the IOC's interface lists must include loopback (C8 says so).
- Overview (about 1280×800):
  - a "Density" tab: an XY plot of `$(P):<port>:TIME_ARRAY` against `:NE` for each port;
  - a "Phase" tab: the same with `PHASE`;
  - a side panel with `SHOT:DATE`, `SHOT:NUM`, `SHOT:TIME_STR`, `SHOT:TIME_SOURCE`, the `NE_MEAN` values,
    `CFG:NE_WINDOW_MS`, `SHOT:MISSING` (multi-line), LEDs for `STAT:CONNECTED`/`STAT:STALE`, `STAT:ANALYSIS_S`,
    `STAT:DROPPED`, `SHOT:LOST`, `STAT:TRIG_LAG` and `STAT:ACK_TIMEOUTS`;
  - alarm-sensitive borders, with the health screen embedded.
- The health screen shows `$(IOC):HEARTBEAT`, `UPTIME`, `IOC_CPU_LOAD`, `MEM_USED`, `CA_CLNT_CNT` and `RECORD_CNT`.
- Tests: the XML parses, and every substituted PV connects over CA on a test IOC.

**C8 `docs: live analysis, IOC and clients`.** Covers `README.md`, `diag_ioc/README.md` and `deploy/` examples:
- the architecture, PV table, services, EPICS variables, Phoebus setup and lab checklist;
- the Files/Configuration tables, including `INTERF_SHOT_STATE` (default `~/data/state/shot_counter.json`) and
  `INTERF_TRIG_LAG_MAX_S`, and the fixed LA zone;
- the module contract, and how to add a diagnostic (another `[[module]]`, or another `diag-ioc@<name>`);
- networking: Phoebus on the IOC machine over loopback, interface address lists, several IOCs on one host, and a
  gateway for the outside group if it is on another subnet;
- that only the PVA group is atomic, while a CA client that falls behind can drop or tear a shot;
- operations: run as the non-root `diag-ioc` user, keep the scope clock NTP-synced, and keep `INTERF_SHOT_STATE` and
  `INTERF_LOG_DIR` on a local disk (not a network filesystem), with the state file outside the log directory.

## 5. Real-data checks (user, on the Linux machine; deferred until it is connected)

- **IPv6 probe** (§3) on the IOC machine, which also runs Phoebus.
- **After C4r**, replaying real recordings:
  - shot IDs carry the recordings' LA dates;
  - trigger times match the scope times `interf_main` logs.
- **After C5** (`diag_ioc` plus `interf_sim --ioc-link` on real `.trc` files):
  - plausible density, and no missing port on good shots;
  - `pvget <P>:SHOT`;
  - the PVA tests;
  - the IOC bench with PVA (`ioc_bench.py run --quick` without `--no-pva`, from the project files, not the repo).
- **After C8**, with the IOC, `interf_store` and Phoebus running, and `--raw-output raw.bp`:
  - HDF5 dataset names equal the trigger times recomputed from the BP WAVEDESCs;
  - then run live with `INTERF_IOC_LINK=… python interf_main.py`.
- **Still open** (config; the user decides later):
  - the real record length, which sets `max_points` and `ca_max_array_bytes`;
  - the systemd user, group and paths.
- **Deferred by the user:** whether legacy HDF5 readers accept empty P40 arrays in place of zero-filled placeholders.
