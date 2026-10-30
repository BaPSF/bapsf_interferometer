# Handoff: live analysis + EPICS IOC for `bapsf_interferometer`

Repository `BaPSF/bapsf_interferometer`, branch `refactor/linux-epics-daq`. Updated 2026-10-18. This file is the
implementation spec for the remaining commits. It implements the decisions in `docs/ARCHITECTURE.md` (D1–D17) and does
not repeat their reasons. A question neither file answers goes to the user before code is written.

**Single source:** this file is `docs/HANDOFF_live_epics.md` on `refactor/linux-epics-daq`, next to `docs/ARCHITECTURE.md`.
Every session (the local coding agent and `test-on-Bobby`) pulls the branch and reads both before any work. Copies
elsewhere (the project's shared folder, an agent's memory) are not authoritative. Doc changes land only as commits by
the local agent; other sessions send them to the user as patch files.

- **Local agent (Windows):** writes and pushes each commit to `origin/refactor/linux-epics-daq`, pulling first. Never
  force-push; no pull request unless asked. It runs no tests unless the user asks, and each commit message lists what
  it ran and what it did not.
- **`test-on-Bobby`** (the Linux lab machine with the `.trc` recordings; the only test session since 2026-10-11): for
  each pushed commit, runs the full suite (the PVA tests included), that commit's scenario on real `.trc` files and
  the real-data checks (§5), then reports. It pushes nothing. Linux-only tests (unix sockets, acquisition
  timers, the IOC) are verified there, not before the push. The cloud test session is retired, and no new synthetic
  data generators are needed; the unit tests keep using `interf_sim.synthetic`.
- Bobby's old procServ IOC (prefix `Interferometer:Port31:`, default CA 5064 and PVA 5075) was removed by its admin on
  2026-10-11. Test IOCs still run on `ioc_harness.isolated_epics_env()` (private ports, loopback only), so they never
  meet a deployed IOC.

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
| 73ccb44 | Docs synced to the latest review; `docs/` on this branch is the single source |
| 465b76a C4s | `ShotCounter` writes its state on a daemon thread (latest wins) and resumes from the later of the state file and the acquisition log; the replay persists no counter |
| b8ecf40 C5 (5928659, pushed before review, was reverted by 3b3e143) | `interf_ioc` module (D4 PVs, chain, PVA group `<P>:SHOT`); host `SHOT:ACK` wait, `STAT:PUBLISHED`/`ACK_TIMEOUTS`, `<name>.db` export to `[ioc] db_dir`, `[ioc] acf` with `readonly.acf`; `create_records` returns the chain tail; `constraints.txt`; `ioc_harness.monitor_all` |
| 442c405 C6 | `interf_store` (PVA client of `<P>:SHOT`, own process, daily HDF5 in the legacy layout; missing P20/P29 as NaN); `<port>:MISSING` PVs; `interf_file` `lecroy_missing=`/`shot=`, `ShotExists`; `interf-store.service` as `diag-ioc`; `p4p` in `clients`; `ioc_harness.ChildProcess` |

**Tested on Linux with real recordings** (`test-on-Bobby`, 2026-10-11): C4s `465b76a` and C5 `b8ecf40`. 119 of 119
tests pass, PVA tests included. The 55 recorded shots ran through the real IOC (`interf_sim --ioc-link`), with an IOC
SIGKILL and restart mid-run. No code changes were needed.

Facts from testing that still matter:
- The recordings (`test-on-Bobby`, 2026-10-11): 216 `.trc` files, 55 shots, LeCroy HDO4104 channels C1–C4 only. No
  Rigol traces, so P40 is missing on every replayed shot (empty arrays, INVALID, the reason in `SHOT:MISSING`).
  Known gaps: shot 82435 has only C2 (P20 and P29 missing), 82437 has no C4 (P29 missing), and 82436 is absent.
- The record: 1,000,002 int16 samples at 0.1 µs (100 ms). `P20:NE` returns 1953 points, so `max_points = 2048` keeps
  them all, and a 2048-point float64 waveform (16 KiB) fits `ca_max_array_bytes = 65536`.
- Density: the 5–10 ms mean is about 4.2e18 m⁻³ on P20 and 4.35e18 m⁻³ on P29 (within 4 % of each other). Analysis
  takes about 0.064 s per real shot on Bobby. (Synthetic shots on other machines took 0.18–0.25 s at 1M samples per
  channel and 2.35 s at 10M; `scipy.fft.rfft` with `workers=-1` in `correlation_spectrogram` gives the same peaks faster.)
- `SHOT:LOST` does not count shots missed while the IOC was down (D4): the restart showed only as a `SHOT:NUM` gap,
  12 to 17, and in the link's outage log.
- `interf_sim` persists no shot counter since C4s, so a test of counter persistence across runs needs a driver outside
  the repo.
- The EPICS libraries bind some UDP sockets on all interfaces at random high ports, even with every address list set
  to loopback. Harmless; C8's deployment notes say so.
- Installed and working on Bobby: softioc 4.7.2, epicscorelibs 7.0.10.99.0.2, pvxslibs 1.5.3, p4p 4.3.0, pyepics 3.5.10,
  lab-scopes 0.4.1 (`constraints.txt` pins the EPICS ones). The pin is now lab-scopes 0.4.2 (`n_channels=`, TCP_NODELAY).
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
    `ShotIdentifier(counter, trig_lag_max_s).identify(shot)` (never raises) and `ShotCounter(state_path, log_paths=())`
    (C4s: `None` persists nothing; `close()` writes the last state).
  - `ShotResult` carries `shot_date`, `shot_number`, `shot_time` and `time_source` (None where the shot has none).
  - `interf_raw_reader.iter_steps`/`read_step`/`decode` (the module was `interf_archive` until the D17 change, as in the
    commit rows above; `RawOutputShot` was `ArchiveShot`) read the ADIOS raw output (streamer's schema 1, or 2 with PNG's
    numbers, D16; no shot identity). It imports `streamer.payload` for that schema, which is fine offline; the live path
    must not.

## 2. Conventions

- Tabs in `interf_*.py`, `interf_sim/`, `tests/`, `diag_ioc/` and new modules. `streamer/` keeps its own style (each
  file's indent, its names) and changes only with the streaming team's permission (D6).
- Short docstrings; comments explain why; `log = logging.getLogger(__name__)`. Acquisition settings come from
  environment variables through `interf_raw._env`-style helpers.
- Tests: stdlib `unittest` only (no pytest), `python -m unittest discover -s tests -v`. Tests that need optional
  packages use `@unittest.skipUnless`.
- Python ≥ 3.11. The layout is flat: add every new module to `[tool.setuptools] py-modules`, and every new package to
  `packages`, in the same commit.
- Conventional commit prefixes (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`), with a short body saying why.
  Keep the README "Files" and "Configuration" tables current.
- Setup: `pip install -e '.[ioc,clients]' -c constraints.txt` (`clients` includes `p4p` since C6).
- `interf_main`'s main thread owns SIGALRM and the LeCroy re-arm. Anything added there is O(1), never blocks, never
  raises into the loop, and never imports EPICS, except the optional PNG CA monitor (`interf_png`, D16).

## 3. API facts (verified; rely on these)

**lab_scopes v0.4.2** (v0.4.1 pinned since C4r; v0.4.2 adds `LeCroyScope(..., n_channels=)`, which skips channel
detection, and sets TCP_NODELAY on the VICP socket). `LeCroyWavedesc(b).wd` holds the 346-byte WAVEDESC fields. The
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
`socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)`. PVA tests skip when it fails.

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

Done: C4r, the docs sync (73ccb44, step 1), C4s (465b76a) and C5 (b8ecf40), the last two tested on Linux with real
recordings (§1), and C6 (442c405, as built below). On Bobby the suite at 442c405 ran 143 of 150 passed, 6 skipped, and
1 failed from a test bug in `test_interf_store`, fixed after 442c405. The local agent's next step is **C7**.

`test-on-Bobby` runs each commit's tests after it is pushed.

**C4s `fix: keep the shot counter's disk write off the acquisition thread` (D14). Done**; kept as the record of what it
does. Files (plus `tests/test_streamer.py`, whose counter now persists nothing): `interf_shot.py`,
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

**C5 `feat: interferometer IOC module` (D2, D4, D5, D7, D8, D11). Done**; kept as the record of what it does.
Includes the read-only access file and the version pins (parts at the end). Files: `interf_ioc.py`,
`deploy/diag-ioc/interferometer.toml`, `diag_ioc/host.py`, `diag_ioc/module.py`, `deploy/systemd/diag-ioc@.service`,
`tests/ioc_harness.py`, `tests/dummy_ioc_module.py`, `tests/test_interf_ioc.py`; as built also `diag_ioc/__main__.py`
(resolves `db_dir` and `acf` against the config file's directory), `diag_ioc/records.py` (`passive()`, which
`scalar`/`waveform` now build on), `tests/test_diag_ioc.py`, `tests/test_interf_payload.py`, and
`tests/test_ioc_link.py` (from `test-on-Bobby`: unix socket tests make their temporary directory in `/tmp`, since a
long TMPDIR pushed the socket path over 107 bytes). The PV names are the D4 list. Records:

| PV | Record |
|---|---|
| `SHOT:NUM` | longIn, I/O Intr, TSE −2: chain head, set last |
| `SHOT:DATE`, `SHOT:LOST` | longIn, Passive, TSE −2, `MDEL=-1` |
| `SHOT:TIME_SOURCE`, `SHOT:TIME_STR` | stringIn, Passive, TSE −2, `MPST="Always"` (a stringin posts only on change by default) |
| `SHOT:HOST_TIME` (PREC 6), `SHOT:CRIT_PATH`, `STAT:TRIG_LAG` (s, `host_time − shot_time`) | aIn, Passive, TSE −2 |
| `SHOT:MISSING` | longStringIn 1024, Passive, TSE −2: each acquisition reason once, then port reasons that add something |
| `<port>:TIME_ARRAY`, `:PHASE`, `:NE` | WaveformIn f64, NELM `max_points`, Passive, TSE −2 |
| `<port>:NE_MEAN` | aIn, Passive, TSE −2, `MDEL=-1`; NaN + INVALID when NaN |
| `<port>:MISSING` (added in C6) | longStringIn 1024, Passive, TSE −2: the port's own reason (`PortResult.missing`), `""` when it has data; no alarm |
| `<port>:CAL`, `<port>:FREQ` (GHz), `CFG:NE_WINDOW_MS` (2 elements) | fixed: `initial_value=` from config in `create_records` (not in the chain); module option `ne_window_ms`, default `[5.0, 10.0]` |

- Module options, validated at construction (unknown keys are errors): `ports`, `max_points` (default 2048, see §1),
  `ne_window_ms`, `plasma_length_m` (default 0.4).
- Ports come from `[[module.options.ports]]` (`name`, `scope`, `ref`, `plasma`, `freq_hz`; default
  `interf_analysis.PORTS`). PVs are created per configured port; the group field is the lower-case name.
- Chain: `SHOT:NUM → DATE → TIME_SOURCE → HOST_TIME → TIME_STR → CRIT_PATH → STAT:TRIG_LAG → LOST → MISSING → <port>:TIME_ARRAY →
  PHASE → NE → MISSING → NE_MEAN → … → <last port>:NE_MEAN → SHOT:ACK` (24 records before the ack).
- Group `<P>:SHOT`:
  - `""` (meta) and `num` from `SHOT:NUM`;
  - `date`, `time_source`, `host_time`, `lost`, `missing` (plain);
  - `<port>.time_array`, `.phase` and `.ne` (scalar), and `.missing` and `.ne_mean` (plain);
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
    `STAT:PUBLISHED`, and call `publish`. As built: `host.PublishAck`, a context manager that `ShotPipeline` takes as
    `gate` and wraps each `publish` in; a `publish` that raises releases the wait, since no chain ran and no ack will come.
  - Write `<name>.db` (the config file's name, e.g. `interferometer.db`; `__main__` passes the path to `run`) with
    `WriteRecords` at every start into the new optional `[ioc] db_dir` (default: the config
    file's directory). The systemd unit adds `StateDirectory=diag-ioc`, and the example config sets
    `db_dir = "/var/lib/diag-ioc"`, because the non-root IOC cannot write `/etc/diag-ioc`. As built: written to a
    temporary file and renamed; a failure is logged and the IOC still starts.
- `ioc_harness.monitor_all(names, callback, timeout)` implements the pyepics flush rule (§3). `IocProcess` no longer
  passes `EPICS_CA_MAX_ARRAY_BYTES` to the IOC, so the config's `ca_max_array_bytes` applies, as deployed.
- CA tests (they run on `test-on-Bobby`):
  - every data PV equals `analyze_shot` of the same shot;
  - every timestamp equals `shot_time`;
  - a shot without a trigger time shows `TIME_SOURCE` = `host`;
  - a Rigol-missing shot gives empty P40 arrays, INVALID, and the reason in `SHOT:MISSING`;
  - `SHOT:LOST` counts gaps and resets;
  - back-to-back publishing leaves `SHOT:ACK == STAT:PUBLISHED` and `STAT:ACK_TIMEOUTS == 0`;
  - `monitor_all` sees exactly one update per member per shot for 5 shots;
  - a `caget` of `P20:NE` returns the full array (as built: a 1,000,002-sample shot, 1953 points, under the deployed
    `ca_max_array_bytes`).
- Extend C4r's `test_the_live_path_loads_no_streamer_module` to import `interf_ioc` too.
- PVA tests (skip without IPv6 and p4p): `<P>:SHOT` matches the shot, and a monitor gets one consistent update per shot.
- Scenario (`test-on-Bobby`): `diag_ioc` plus `interf_sim --ioc-link` on the real `.trc` recordings (P40 missing on
  every shot, since they hold no Rigol traces).
  - Read the D4 list over CA.
  - `STAT:STALE` goes MAJOR after the simulator stops.
  - Kill and restart the IOC mid-run: acquisition continues, and the link logs the outage and recovery.

**C5, read-only access (D8).** Add `deploy/diag-ioc/readonly.acf` (reads for all, writes for none), installed via the
new optional `[ioc] acf` path. Making `CFG:NE_WINDOW_MS` writable from the screen is a later change, not part of C5.
Test: a CA put is rejected. On an IPv6 machine, check that PVA puts are rejected too, and say so in the commit body.
As built: the CA test checks that the access right itself is refused (DISP=1 alone leaves it granted), and
`test_pva_puts_are_rejected` covers PVA, including `IOC:HEARTBEAT` (DISP=0); it runs only where IPv6 works.

**C5, version pins (D11).** Add `constraints.txt` with exact `softioc`, `epicscorelibs`, `pvxslibs` and
`p4p` versions as tested, and document `-c constraints.txt` in the README. As built: Bobby's versions (§1), plus
`pyepics`.

**C6 `feat: store analyzed shots in the daily HDF5 file` (D9). Done**; kept as the record of what it does. Files:
`interf_store.py`, `interf_file.py`, `deploy/systemd/interf-store.service`, `tests/test_interf_store.py`; add `p4p` to
the `clients` extra and the script `interf-store`. As built also `interf_ioc.py` and `tests/test_interf_ioc.py`
(`<port>:MISSING`), `tests/ioc_harness.py`, `tests/test_interf_payload.py` and the `diag-ioc@.service` install line.
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
- As built:
  - The separate-process rule and the HDF5 keys are in D9; the unit is the third of D10.
  - The IOC publishes each port's own reason as `<port>:MISSING` (C5 record table), in the chain after `<port>:NE` and
    in the group as `<port>.missing`. `missing_reason` (P20, P29) and `rigol_missing_reason` (P40) hold it;
    `SHOT:MISSING` (the whole shot's text, cut at 1023 bytes) is not used by the store. `create_sourcefile_dataset`
    writes a missing P20/P29 phase as NaN of `len(t_ms)` (D9); P40 stays empty. The `shot` attributes go on the
    `phase_p20` dataset.
  - A duplicate is `interf_file.ShotExists` (a `ValueError`). An update with `date` or `timeStamp` 0 (the IOC's state
    before its first shot after a start, sent to every new or reconnected monitor) is not a shot.
  - `interf-store.service`: `diag-ioc`, `StateDirectory=interferometer`, `INTERF_DATA_DIR=/var/lib/interferometer/hdf5`
    (`interf_store` creates `hdf5`), `EnvironmentFile=-/etc/interferometer/store.env`, `After=` only on the IOC,
    `Restart=always`, `TimeoutStopSec=30`. `interf_store.STOP_TIMEOUT_S` is 20 s, under that, so the exit summary is
    logged before SIGKILL.
  - `shot_time` is the group's top-level `timeStamp`. softioc stores it as whole s plus truncated ns, which rounds back
    to the same float64, so the dataset name equals `str()` of the trigger time.
  - `<port>.missing` is read as a string or as a NUL-terminated char array, since which one QSRV serves for a
    `longStringIn` group field is not yet checked (`test-on-Bobby` reports it).
  - Disconnects, a full queue (100 shots) and failed writes use `diag_ioc.outage.Outage`. The callback ignores the
    `Disconnected` that p4p sends before the first connection. Each stored shot is logged.
  - `tests/test_interf_store.py` also has `RecordedShotTests` for `test-on-Bobby` (skipped without the `.trc` files in
    `interf_sim.trc_replay.TRC_DIR`). It runs the real IOC, `interf_store` and `python -m interf_sim --ioc-link` on
    every recorded shot, then checks every stored shot against `analyze_shot` of the same replayed shot. It also
    checks the known gaps (82435: P20 and P29 missing; 82437: P29 missing; P40 missing on every shot).
  - Gaps use `interf_ioc.LostShots`, the rule of `SHOT:LOST`. `create_sourcefile_dataset` no longer prints a line
    per shot; the store logs one.
  - `tests/ioc_harness.py`: `ChildProcess` (which `IocProcess` now extends) runs the store, and
    `interferometer_config` and `publish` moved there from `tests/test_interf_ioc.py`.

**PNG `feat: PNG run and shot numbers in the raw archive` (D16). Committed, not yet tested.** Files:
`interf_png.py` (new), `interf_main.py`, `interf_raw.py`, `interf_archive.py`, `pyproject.toml`, `diag_ioc/__init__.py`,
`docs/` (ACQUISITION, LIVE_IOC, README), `deploy/systemd/acquire.env.example`, `tests/test_interf_png.py` (new) and
`tests/test_interf_analysis.py`. No change to `streamer/`, `interf_payload`, the IOC, `interf_store` or HDF5.
- `interf_png.PngMonitor(run_pv, shot_pv)` as D16 describes (disconnects and reconnects through `Outage`). `latest()`
  returns a new `{"run", "run_time", "shot", "shot_time"}` (None per value not yet received); `flush()`. Neither raises.
- `interf_main.main()`: a `PngMonitor` when `INTERF_PNG_RUN_PV` and `INTERF_PNG_SHOT_PV` are both set (a failure is
  logged and acquisition goes on without PNG); `acquire_shot(..., read_png=png.latest)`; `png.flush()` after each
  handled shot. The shot line ends with `png <run>-<shot> lag <s>` (`lag` = `host_time` − PNG shot time), or `png -`.
  `INTERF_RAW_OUTPUT` adds streamer's `RawOutput` beside the `IocLink`, as `interf_sim` does.
- `interf_raw._consume(..., read_png=None)` calls it right after `host_time`; an exception gives None. `RawShot.png`.
- `interf_raw_reader.decode` reads schema 1 (`png` None) and 2 (`png` dict; -1/NaN read as None). Raw output schema 2 is
  streamer's numbering, unrelated to the live link's `interf_payload` schema 2.
- **For the streaming team** (their code, not edited here): `streamer.payload.shot_variables` should add four scalars
  from `RawShot.png`: `png_run` (int64), `png_run_time` (float64), `png_shot` (int64) and `png_shot_time` (float64), with
  a missing `png` or None value written as -1 (ints) or NaN (floats), and raise `SCHEMA_VERSION` to 2. Until then raw
  output stays schema 1 and PNG's numbers are only in the acquisition log's shot lines.
- Tests (written, not run): `PngMonitor` against a fake `epics` module; the shot line's PNG part; `decode` of schema 2.
- Scenario (`test-on-Bobby`): with PNG's IOC up, the shot lines' `lag` stays near 0–0.2 s (or about 3 s); stop PNG's IOC
  mid-run, and acquisition goes on with the disconnect and the reconnect logged once each.

**Raw output never holds up acquisition (D17). Committed, not yet tested.** Two commits, so the streamer one
can be handed back to the streaming team on its own.
- A (`streamer/`, with the streaming team's permission, J 2026-10-29): `raw_output.py` (`error`), `raw_output_io.py`
  (the mode by destination, the `error` status), `arguments.py`, `remote_consumer_io.py` (`abort()`, which also stops
  relaunching), `consumer.py` (`--keep-listening`, keepalive), `single_socket_io.py` (keepalive), `network_access.py`
  (`enable_keepalive`), `consumer_output.py` (names through the shared `unused_path`), `timing_log.py`
  (`try_record`), `README.md`, `tests/test_streamer.py`. D17 lists what each change does.
- B (ours): `interf_main.py` (`_start_raw_output`, logging set up before the outputs, the `raw output` outage log
  after each shot, the stop order and counts), `docs/` (ARCHITECTURE D6, D9, D16, D17; ACQUISITION; LIVE_IOC; this
  entry), `deploy/systemd/acquire.env.example`. No change to `interf_sim` beyond the new CLI flags, `interf_payload`,
  the IOC, `interf_store` or HDF5. The PNG fields in `streamer.payload` stay a separate change (PNG entry above).
- The wording "raw output" (J, 2026-10-30), in B: `interf_archive.py` is renamed `interf_raw_reader.py` and
  `ArchiveShot` is `RawOutputShot` (`iter_steps`, `read_step` and `decode` are unchanged); comments and docstrings in
  `interf_analysis.py`, `interf_payload.py`, `interf_raw.py`, `diag_ioc/link.py`, `interf_sim/__main__.py`; test class
  and method names in `tests/test_interf_analysis.py` and `tests/test_interf_payload.py`; `pyproject.toml`;
  `README.md`. The commit rows in §1 and the PNG entry's commit label keep the old name, as history.
- Not run: any test, and nothing live. Syntax checks only.
- Tests (written, not run), in `tests/test_streamer.py`: `write()` returns at once while a fake output's opening hangs;
  a write error is retried on a new output with no error to the caller; directory rotation with an injected clock
  never reuses a name; an existing path gets `-1`; `close(timeout)` returns within the timeout with a hanging fake and
  reports unsent shots; `settings_from_env`.
- Scenario (`test-on-Bobby`):
  - the unit tests;
  - live, a local folder (`INTERF_RAW_OUTPUT=/var/lib/interferometer/raw/`, `INTERF_RAW_OUTPUT_FILE_S=300`): restart
    acquisition twice and check that no file is overwritten, a new file starts every 5 min, and the stop takes under
    30 s;
  - live, network: a consumer started by hand with `--keep-listening`. Stop it mid-run, then start it again. The IOC
    and HDF5 must keep getting a shot every 3 s, and raw output must resume within one retry (`io.open` after
    `io.error` in the timing log). The acquisition log shows `raw output failed` once and `raw output recovered`
    once;
  - live, network: cut the network between Bobby and the consumer (or drop its packets) while acquisition idles
    between shots; the consumer drops the connection within about a minute (keepalive) and accepts the producer's
    reconnect;
  - throughout, the timing line's `outputs` value stays under 1 ms.

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
  gateway for the outside group if it is on another subnet; the EPICS libraries still bind some UDP sockets on all
  interfaces at random high ports with every address list set to loopback, which is harmless;
- that only the PVA group is atomic, while a CA client that falls behind can drop or tear a shot;
- operations: the D10 system units as the `diag-ioc` account, keep the scope clock NTP-synced, and keep
  `INTERF_SHOT_STATE` and `INTERF_LOG_DIR` on a local disk (not a network filesystem), with the state file outside the
  log directory (the acquisition unit sets them under `/var/lib` and `/var/log`).

## 5. Real-data checks (`test-on-Bobby`, connected 2026-10-11)

- **IPv6 probe** (§3) on the IOC machine, which also runs Phoebus.
- **After C4r**, replaying real recordings:
  - shot IDs carry the recordings' LA dates;
  - trigger times match the scope times `interf_main` logs.
- **After C5** (`diag_ioc` plus `interf_sim --ioc-link` on real `.trc` files), passed 2026-10-11 (§1):
  - plausible density, and no missing P20 or P29 on good shots (P40 is always missing: no Rigol traces recorded);
  - the PVA tests. They replace the PVA bench re-run, which is dropped; the CA bench verdict (keep pythonSoftIOC) stands.
- **After C8**, with the IOC, `interf_store` and Phoebus running, and `--raw-output raw.bp`:
  - HDF5 dataset names equal the trigger times recomputed from the BP WAVEDESCs;
  - then run live through `interf-acquire.service` (D10). That first live run also checks acquisition's
    `/var/log/interferometer` and `/var/lib/interferometer`, which the replay does not use.
- **Set in C5** from the real record length (§1): `max_points = 2048`, `ca_max_array_bytes = 65536`.
- **D10 system units**, tried on the lab machine 2026-10-12 with the 55-shot replay: all checks passed (D10 has the
  list). Install steps are in each unit's header.
- **Closed 2026-10-18:** empty P40 arrays. The main-branch writer has stored empty P40 arrays with `rigol_missing=True`
  since `33ca795` (2026-05-12), so the store's empty arrays match the legacy files.
