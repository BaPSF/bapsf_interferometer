# Handoff: live analysis + EPICS IOC for `bapsf_interferometer`

Repository `BaPSF/bapsf_interferometer`, branch `refactor/linux-epics-daq`. Updated 2026-10-08. This file is the
implementation spec for the remaining commits. It implements the decisions in `docs/ARCHITECTURE.md` (D1–D15) and does
not repeat their reasons. A question neither file answers goes to the user before code is written. The user supplies,
with this file, `ARCHITECTURE_DECISIONS.md` and `c4-test-fixes.patch`.

- **Local agent (Windows):** writes and pushes each commit to `origin/refactor/linux-epics-daq`, pulling first. Never
  force-push; no pull request unless asked. It runs no tests unless the user asks; the cloud session tests every pushed commit.
- **Cloud test session:** for each pushed commit, the user sends `test <sha> (Cn)`. It runs the full suite on Linux and
  that commit's scenario on synthetic data, then reports. It pushes nothing. Linux-only tests (unix sockets, acquisition
  timers, the IOC scenarios) are verified there, not before the push.
- **Real-data gates** (the user's lab-PC checks, §5) are deferred until the Linux machine with the `.trc` recordings is
  connected. Mark each stage "pending real-data check"; do no `.trc`-dependent work before then.

## 1. Done so far

| Commit | What |
|---|---|
| 1620cfd C1 | `_handle_shot` fans out to a list of outputs, failures isolated (outages keyed by `id()`) |
| 73d7c67 C2, b6d8c6c C2a/C2b | `streamer.payload.shot_from_variables` (C4r: moved to `interf_archive.decode`); `interf_analysis.analyze_shot` per port; `interf_sim.synthetic`; flat or too-short channels marked missing |
| 3119997 C3, f26c882 C3a | `diag_ioc/link.py`: `IocLink` (latest-wins sender) and `LinkListener`; `INTERF_IOC_LINK`, `interf_sim --ioc-link`, `interf_sim.listen`; unix paths over 107 bytes rejected |
| 3ca9aca C3b, 60e4dca C3c/C3d | `streamer.adios_io.iter_steps`/`read_step` (BP5 and BP4, open files end at the last step); C4r moved them to `interf_archive` and restored `streamer/` to 642429a |
| 32f40cc C4 | `diag_ioc` host: TOML config, `ModuleHost`, `ShotPipeline`, `StatusRecords` (`STAT:*`), devIocStats, `diag-ioc@.service`, `tests/ioc_harness.py` |

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

**lab_scopes v0.4.1** (the branch pins v0.4.0 until C4r). `LeCroyWavedesc(b).wd` holds the 346-byte WAVEDESC fields. The
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
- `rec.set(value, severity=, alarm=, timestamp=)` is thread-safe. On a Passive record it only stores the value; on an
  I/O Intr record it processes the record and its FLNK chain. With `TSE=-2`, always pass `timestamp` (epoch s).
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

Two commits remain before Stage 3: **C4r** revises the C1–C4 work to the architecture review, and **C5** adds the IOC
module. Each part below is a section of one of them; the cloud session runs each commit's tests after it is pushed.

**C4r `refactor: apply the architecture review to the live path`.** Parts (a)–(d):

**(a) C4 test fixes** (`c4-test-fixes.patch`, supplied with this file):
- In `test_invalid_configs_name_the_problem`, the key `"unknown top-level"` becomes `"top level: unknown keys"`, the
  text the code produces.
- `IocProcess.__exit__` in `tests/ioc_harness.py` closes `self.process.stdout`, which removes a `ResourceWarning` per IOC.

**(b) Docs.** Add `ARCHITECTURE_DECISIONS.md` as `docs/ARCHITECTURE.md` and this file as `docs/HANDOFF_live_epics.md`,
replacing the old handoff.

**(c) Decouple the live link from streamer (D6, D12).**
- New `diag_ioc/framing.py`: `send_arrays`, `receive_message`, `send_end`, copied from `streamer/socket_protocol.py`
  without compression or authentication.
- New `diag_ioc/network.py`: `ipv4_network`, `create_tcp_listener`, `accept_from_allowed_network`, copied from
  `streamer/network_access.py`.
- `IocLink(address, encode, …)`: `encode(item) -> {name: array}` becomes required (`diag_ioc` cannot import `interf_*`).
  Remove the link's own `seq` counter and the `seq` argument. `shot_number` replaces it (D14).
- New `interf_payload.py`, copied from `streamer/payload.py`:
  - `encode(shot)` writes the envelope `schema_version` (2), `shot_date`, `shot_number`, `shot_time`, `time_source`,
    `host_time`, `critical_path_s` and `missing_json`, then the `lecroy_*`/`rigol_*` arrays as today. The shot fields
    come from part (d).
  - `decode(variables)` returns a dataclass that `analyze_shot` accepts, and raises `ValueError` on an unknown version.
- `interf_main`, `interf_sim` and `interf_sim.listen` use `interf_payload`.
- Tests:
  - the link tests pass;
  - an encode/decode round trip;
  - after importing `diag_ioc`, `interf_payload` and (from C5) `interf_ioc` in a subprocess, no `streamer` module is
    loaded;
  - `--raw-output` ADIOS files are unchanged.

**(d) Shot identity and trigger time (D14, D15).** Pin lab_scopes to v0.4.1 in pyproject and the README. New
`interf_shot.py`, with the zone as a constant `America/Los_Angeles` (no setting); `RawShot` gains `shot_date`,
`shot_number`, `shot_time` and `time_source` (`streamer` ignores them, so ADIOS is unchanged). In `ShotResult`, these four
replace `shot_index`.
- `trigger_time(wd, host_time) -> (shot_time, time_source)`:
  - `t = wavedesc_trigger_timestamp(wd)` on the first LeCroy channel's WAVEDESC.
  - When `t + 3600` has the same LA wall-clock time as `t` (the repeated fall-back hour), use whichever of `t` and
    `t + 3600` is closer to `host_time`.
  - No LeCroy data, or `t is None`, gives `(host_time, "host")`; otherwise `"trigger"`.
  - Log a throttled warning when `host_time − shot_time` is negative or above `INTERF_TRIG_LAG_MAX_S`. As built:
    `trigger_time` is a pure conversion; a per-run `ShotIdentifier(counter, trig_lag_max_s)` calls it and the
    counter, does the lag check, and owns the throttled logging. `_handle_shot` calls `identifier.identify(shot)`.
- `ShotCounter(state_path).next(shot_time) -> (shot_date, shot_number)`:
  - `shot_date` is the America/Los_Angeles date of `shot_time` as YYYYMMDD, and the number restarts at 0 when it changes.
  - Write the state atomically (temp file + `os.replace`) after each shot. On a restart the same day, continue from
    last + 1.
  - An unreadable state file is logged and counting starts at 0.
- `_handle_shot` assigns the fields after the re-arm and before the outputs. It never raises; on failure it uses
  `host_time` and the in-memory counter. The log line adds `shot <date>-<number>`. `analyze_shot` copies the fields
  into `ShotResult` when present.
- Settings:
  - `INTERF_SHOT_STATE`, default `~/data/state/shot_counter.json` (state, kept out of the rotated log directory;
    `interf_sim` uses its own log directory);
  - `INTERF_TRIG_LAG_MAX_S`, default 5, where 0 disables the check. `interf_sim` sets 0, since replays carry old
    trigger times.
- Tests:
  - PST and PDT;
  - the fall-back hour;
  - the `host` fallback;
  - the counter: increment, same-day restart, restart at LA midnight, corrupt state;
  - `interf_sim.synthetic.make_wavedesc` writes LA-local fields (it uses `time.gmtime` today, matching the old `timegm`).

**C5 `feat: interferometer IOC module` (D2, D4, D5, D7, D8, D11).** Includes the read-only access file and the version
pins (parts at the end). Files: `interf_ioc.py`, `deploy/diag-ioc/interferometer.toml`, `diag_ioc/host.py`,
`diag_ioc/module.py`, `deploy/systemd/diag-ioc@.service`, `tests/ioc_harness.py`, `tests/dummy_ioc_module.py`,
`tests/test_interf_ioc.py`. The PV names are the D4 list. Records:

| PV | Record |
|---|---|
| `SHOT:NUM` | longIn, I/O Intr, TSE −2: chain head, set last |
| `SHOT:DATE`, `SHOT:LOST` | longIn, Passive, TSE −2, `MDEL=-1` |
| `SHOT:TIME_SOURCE`, `SHOT:TIME_STR` | stringIn, Passive, TSE −2 |
| `SHOT:HOST_TIME` (PREC 6), `SHOT:CRIT_PATH` | aIn, Passive, TSE −2 |
| `SHOT:MISSING` | longStringIn 1024, Passive, TSE −2: each acquisition reason once, then port reasons that add something |
| `<port>:TIME_ARRAY`, `:PHASE`, `:NE` | WaveformIn f64, NELM `max_points`, Passive, TSE −2 |
| `<port>:NE_MEAN` | aIn, Passive, TSE −2, `MDEL=-1`; NaN + INVALID when NaN |
| `<port>:CAL`, `<port>:FREQ` (GHz), `CFG:NE_WINDOW_MS` (2 elements) | set once in `start()`; module option `ne_window_ms`, default `[5.0, 10.0]` |

- Ports come from `[[module.options.ports]]` (`name`, `scope`, `ref`, `plasma`, `freq_hz`; default
  `interf_analysis.PORTS`), validated at construction. PVs are created per configured port.
- Chain: `SHOT:NUM → DATE → TIME_SOURCE → HOST_TIME → TIME_STR → CRIT_PATH → LOST → MISSING → <port>:TIME_ARRAY →
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
  - Create `STAT:PUBLISHED` (longIn), `SHOT:ACK` (the ack pattern), `STAT:ACK_TIMEOUTS` (longIn) and `STAT:TRIG_LAG`
    (aIn, s, `host_time − shot_time`).
  - `DiagnosticModule.create_records(builder)` now returns its chain's last record (or `None` for a module without a
    chain); update the docstring and `tests/dummy_ioc_module.py`. The host sets `tail.FLNK = ack` before
    `LoadDatabase()`.
  - Before each `publish`, wait until the ack equals the last published count (an `Event` set from `on_update`). The
    wait times out at 2 s, then it logs, increments `STAT:ACK_TIMEOUTS` and proceeds. Then increment and set
    `STAT:PUBLISHED`, and call `publish`.
  - Write `<ioc name>.db` with `WriteRecords` at every start into the new optional `[ioc] db_dir` (default: the config
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
- PVA tests (skip without IPv6 and p4p): `<P>:SHOT` matches the shot, and a monitor gets one consistent update per shot.
- Cloud scenario: `diag_ioc` plus `interf_sim --ioc-link` on synthetic `.trc` files.
  - Read the D4 list over CA.
  - `STAT:STALE` goes MAJOR after the simulator stops.
  - Kill and restart the IOC mid-run: acquisition continues, and the link logs the outage and recovery.

**C5, read-only access (D8).** Add `deploy/diag-ioc/readonly.acf` (reads for all, writes for none), installed via the
new optional `[ioc] acf` path. Making `CFG:NE_WINDOW_MS` writable from the screen is a later change, not part of C5. Test: a CA put is rejected. On an IPv6 machine, check that PVA puts are
rejected too, and say so in the commit body.

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
- the Files/Configuration tables, including `INTERF_SHOT_STATE` and `INTERF_TRIG_LAG_MAX_S`, and the fixed LA zone;
- the module contract, and how to add a diagnostic (another `[[module]]`, or another `diag-ioc@<name>`);
- networking: Phoebus on the IOC machine over loopback, interface address lists, several IOCs on one host, and a
  gateway for the outside group if it is on another subnet;
- that only the PVA group is atomic, while a CA client that falls behind can drop or tear a shot;
- operations: run as the non-root `diag-ioc` user, and keep the scope clock NTP-synced.

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
