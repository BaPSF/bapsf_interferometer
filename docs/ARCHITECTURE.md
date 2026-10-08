# EPICS live-analysis architecture: decisions

Repo `BaPSF/bapsf_interferometer`, branch `refactor/linux-epics-daq`, base head `32f40cc` (C4, `diag_ioc` host).
Status: **all 15 decisions ACCEPTED by J on 2026-10-08.** Both agents follow this file, and
`HANDOFF_live_epics.md` implements it commit by commit. Anything neither covers goes to J before code is written.

## 0. What exists at 32f40cc

```
interf_main (acquisition, Linux)
  └─ RawShot ─┬─ RawOutput (streamer/, ADIOS BP5)        -> complete raw archive
              └─ IocLink (diag_ioc/link.py, latest-wins) -> unix socket / loopback TCP
                                                              │
diag_ioc host (pythonSoftIOC, one process per config)  <──────┘
  ModuleHost: LinkListener -> ShotPipeline (mailbox + worker) -> module.analyze() -> module.publish()
  per module: <P>:STAT:*; per IOC: devIocStats; serves CA + PVA (QSRV2)
```

Both outputs currently encode with `streamer.payload.shot_variables`. D6 removes that dependency from the live
path. Each output keeps its own per-process counter (`streamer/raw_output.py:20`, `diag_ioc/link.py:80`), and
the record timestamps are `host_time`. D14 and D15 replace both.

## 1. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | IOC technology | pythonSoftIOC, standard record types only |
| D2 | Record definitions | Python `builder` in the module; host exports `<name>.db` at start |
| D3 | Process layout | One `diag-ioc@<name>` process per diagnostic |
| D4 | PV naming | Prefix `LAPD:DIAG:INTERF` (config value), fixed sub-tree below |
| D5 | Where analysis runs | In the IOC process, from raw shots over the link |
| D6 | Live data contract | Independent of streamer; encoding beside `interf_main`, socket code in `diag_ioc` |
| D7 | Per-shot consistency | One timestamp per shot, `SHOT:NUM` set last, PVA group `<P>:SHOT` |
| D8 | Outside group | Read-only access to documented PVs; nothing built for them |
| D9 | Source of record | ADIOS raw archive; IOC-fed HDF5 is best effort |
| D10 | Supervision | systemd template unit |
| D11 | Dependencies | Exact pins for `softioc`, `epicscorelibs`, `pvxslibs`, `p4p` |
| D12 | Repo layout | `diag_ioc` stays here until a 2nd diagnostic exists |
| D13 | Agent roles | Local agent writes code; cloud agent tests synthetically; real-data gates deferred |
| D14 | Shot identity | `shot_date` (YYYYMMDD, LA local) + `shot_number` from 0 each day, persisted |
| D15 | Shot time | LeCroy WAVEDESC trigger time (scope NTP-synced), float64 epoch seconds |

### D1. IOC technology: pythonSoftIOC
- A real EPICS 7 IOC (standard records, alarms, devIocStats, CA + PVA) with the analysis in the same process,
  so shot-atomic publishing and device timestamps come for free. Developed and run in production by Diamond
  (`softioc` on PyPI, bundles EPICS 7 + PVXS/QSRV2). Clients cannot tell it from a C IOC.
- Rejected: C IOC + Python `caput` client (about 15 puts per shot landing at different times, write access needed,
  two things to keep in sync); p4p/caproto/pcaspy (not a full IOC).
- Accepted risk: a niche choice, and EPICS base comes from `epicscorelibs` wheels (mitigated by D11).
- IOC bench (2026-10-08, synthetic, CA only; the report and scripts stay outside the repo):
  on identical records it beat C softIoc + Python writer on speed (10k points: 100 Hz vs 20 Hz max, p99 18 vs 35 ms at
  5 Hz) and matched or beat it on reliability. 100 % of its shots are matchable by one timestamp vs 0 %. C's only
  advantage is restarting the writer without restarting the PV server.
- Keeps a later move to a C IOC cheap: PV names are the only interface. `builder.WriteRecords()` gives the `.db`.
  Modules use only standard record types and fields (`ai`, `longin`, `waveform`, `stringin`, `lsi`, `bi`,
  `Q:group` info) with no `on_update` or other pythonSoftIOC-only features, and `analyze()` never touches
  records. One exception: the host's `SHOT:ACK` `on_update` (D7). After a move to a C IOC it would become a CA monitor on
  the same standard record. After such a move, the analysis would run as a separate writer process (the layout D5 rejects for now).
- The outside group's "don't create PVs in Python" comment does not change this (D8).

### D2. Record definitions in Python
- Each module builds its records with `builder` and `diag_ioc/records.py`: one source of truth for names, NELM and
  publish order. A hand-written `.db` would duplicate every name.
- The host writes the full database with `builder.WriteRecords()` to `<name>.db` at every start, so a current
  file listing every PV always exists. It goes in `[ioc] db_dir` (default: the config file's directory). The IOC runs
  as non-root (D10) and cannot write `/etc/diag-ioc`, so the systemd unit points it at its state directory.
- Lifetime: PVs exist while the IOC runs. After a crash, systemd restarts it within about 5 s and the data PVs are
  INVALID until the next shot. When acquisition stops, PVs keep the last shot and `STAT:STALE` goes MAJOR.
  Copies that survive our restarts are a consumer's own IOC with CA links.

### D3. One IOC process per diagnostic
- Isolates crashes, restarts and the GIL. The host can also group modules; do that only for tiny ones.
- With 2+ IOCs on one host, CA search needs care (shared UDP 5064): set an address list, use a per-IOC
  `EPICS_CA_SERVER_PORT`, or add a gateway. Handle it when the second IOC arrives.

### D4. PV list
`<P>` = `LAPD:DIAG:INTERF`. It is a config value, so code and tests never contain the literal. Later diagnostics
use `LAPD:DIAG:<NAME>`. Units and names are the stable interface; everything else may change.

| PV | Meaning |
|---|---|
| `<P>:SHOT:DATE` | shot date, YYYYMMDD, LA local (D14) |
| `<P>:SHOT:NUM` | shot number within the day, from 0 (D14); FLNK chain head, set last |
| `<P>:SHOT:TIME_SOURCE` | `trigger` or `host`: where the shot timestamp came from (D15) |
| `<P>:SHOT:HOST_TIME` | epoch s when the PC detected the capture; cross-check only |
| `<P>:SHOT:TIME_STR` | shot time as local text, for the screen |
| `<P>:SHOT:CRIT_PATH` | s, acquisition critical path (`critical_path_s`) |
| `<P>:SHOT:LOST` | running total of shots lost on the live link, from gaps in `SHOT:NUM`; a new day, a decrease, or the first shot after IOC start resets the baseline |
| `<P>:SHOT:MISSING` | which scope/channel is missing and why |
| `<P>:<port>:TIME_ARRAY` | ms, scope time axis (`<port>` = P20, P29, P40) |
| `<P>:<port>:PHASE` | rad |
| `<P>:<port>:NE` | m⁻³, path-averaged density |
| `<P>:<port>:NE_MEAN` | m⁻³, mean over the `CFG:NE_WINDOW_MS` window |
| `<P>:<port>:CAL` | m⁻³/rad, calibration factor; set once at start |
| `<P>:<port>:FREQ` | GHz, the port's interferometer frequency; set once at start |
| `<P>:CFG:NE_WINDOW_MS` | ms, (start, stop) averaging window, 2 elements; default (5, 10) from config, set at start; to become adjustable from the screen later (D8) |
| `<P>:STAT:CONNECTED`, `STALE`, `AGE_S`, `DROPPED`, `ANALYSIS_S`, `ERROR` | host health (exist in C4) |
| `<P>:SHOT:ACK` | internal: copies `STAT:PUBLISHED` when the chain finishes (D7); read-only, not in the group |
| `<P>:STAT:PUBLISHED` | shots published by this IOC process (monotonic, restarts at 0 with the IOC) |
| `<P>:STAT:ACK_TIMEOUTS` | publishes that went ahead after the 2 s ack timeout (D7); should stay 0 |
| `<P>:STAT:TRIG_LAG` | s, `host_time` − shot time; growth means lost NTP sync or detection lag |
| `<P>:IOC:*` | devIocStats |
| `<P>:SHOT` | PVA group of the shot's records |

`SHOT:SEQ` from the earlier handoff is dropped. `SHOT:LOST` counts shots lost on the link. `STAT:DROPPED` counts only
shots dropped by the IOC mailbox.

### D5. Analysis runs in the IOC process
- Raw shots are not PVs, so analysis must sit on the raw-data path (the link from `interf_main`). Phoebus,
  `interf_store` and other clients only consume results. Offline, `analyze_shot()` re-runs the same analysis on
  the ADIOS archive.
- Keeps acquisition's critical path to scope I/O only. Analysis can crash or restart without touching
  acquisition. The cost is about 12 MB per shot over a local socket. Acquisition and the IOC share a host (unix
  socket) or use an allow-listed loopback TCP link, since the link is unauthenticated.
- Rejected: a separate analysis process writing into an IOC (loses the single atomic update, needs write access).
- `analyze()` stays pure and picklable, so a process pool can be added later without touching modules.

### D6. Live link independent of streamer
- `streamer/` is maintained by a separate group. We never modify it, and nothing on the live path imports it.
- Interferometer side, beside `interf_main` (e.g. `interf_payload.py`): the encoder from `RawShot` to the link
  message (`lecroy_*`, `rigol_*` arrays) and its decoder for the IOC module. Copied from `streamer/payload.py`,
  not imported.
- Generic side, in `diag_ioc`: `IocLink`, `LinkListener`, and its own framing copy (JSON header + raw arrays, no
  compression or auth), plus its own `ipv4_network`/allow-list helpers in place of `streamer.network_access`.
- Every message starts with a common envelope: `schema_version`, `shot_date`, `shot_number`, `shot_time`,
  `time_source`, `host_time`, `critical_path_s`, `missing_json`, then the diagnostic arrays. A layout change bumps
  `schema_version`, and the decoder rejects unknown versions. The link carries raw samples, so live and offline
  analysis see the same input.
- The ADIOS format belongs to streamer, including its own per-process step counter. If the format changes, offline
  reanalysis (D9) needs a matching reader.

### D7. Per-shot consistency
- `publish()` sets every Passive record with `timestamp=shot_time` (TSE=-2), then sets the chain head `SHOT:NUM`
  last, so the FLNK chain processes once. The PVA group `<P>:SHOT` triggers on the chain's last data record:
  one consistent update per shot.
- **No overlap between shots (bench F1).** `set()` stores values at once, but the chain runs later on an EPICS
  thread. If shot N's chain is still running when shot N+1 is set, chain N publishes a mix of both (reproduced
  in-IOC, and silent). Fix, owned by the `diag_ioc` host so that every module gets it:
  - `SHOT:ACK` = `longOut(OMSL="closed_loop", DOL="<P>:STAT:PUBLISHED", DISP=1, always_update=True, on_update=…)`,
    FLNK'd from the chain's last record. DOL has no PP, so it reads the stored value. DISP=1 blocks CA puts.
  - `publish()` increments and sets `STAT:PUBLISHED` with the shot. Before setting the next shot, the host waits until
    the ack equals the last published count. The wait times out at 2 s, then logs, increments
    `STAT:ACK_TIMEOUTS` and proceeds.
  - The ack follows the IOC's own counter, not `SHOT:NUM`, because `SHOT:NUM` resets each day: a day ending at 0
    followed by a new day's 0 would pass the wait early.
  - Verified pattern: `repro/chain_overlap_ack.py` in the bench results (0 torn chains in 3000 back-to-back runs, versus
    1703 without the wait).
- **CA versus PVA delivery (bench F3).** When a CA client falls behind, the server merges its queued updates per
  channel, so the client can see a dropped, duplicated or torn shot. This is inherent to CA. CA clients match a
  shot's records by identical timestamp and expire incomplete shots. Only the PVA group is atomic under client stalls,
  so `interf_store` uses it (D9).
- Missing data is published as an empty array with INVALID alarm and the reason in `SHOT:MISSING`, never as
  fabricated zeros.

### D8. The outside group is a read-only client
- Read access to the D4 PVs over CA/PVA, nothing else: no write access, no code or design changes for them, no
  maintenance owed. Their suggestions are input, not requirements.
- Enforce with an access-security file that makes every record read-only, installed through
  `imports.install_pv_logging(acf_path)` before `iocInit` (softioc 4.7.2). Verify that it also covers
  PVA/QSRV2. If they are on another subnet, put a CA/PVA gateway in front.
- Phoebus runs on the IOC's own Linux machine (J, 2026-10-08), so our displays need no network access to the IOC.
- Planned, not in C5: making `CFG:NE_WINDOW_MS` adjustable from the Phoebus screen (J). The access file would then grant
  writes to that one record from the IOC machine only, and the new window would apply from the next shot. Until then
  the window is changed in the config, followed by an IOC restart.

### D9. Source of record
- The live path is latest-wins, so the IOC-fed HDF5 store (C6, `interf_store`) can miss shots. It stays a
  best-effort convenience in the legacy layout, with dataset names = `shot_time`, and logs gaps in `shot_number`.
- `interf_store` subscribes to the PVA group `<P>:SHOT` with p4p (J, 2026-10-08), so each shot arrives as one complete
  update. J has run the same PVA workflow on the Linux machine for another application (`bapsf_tx`). Its callback
  only copies; file writes happen on its own thread.
- The ADIOS raw archive is the record. Anything analyzed can be regenerated with `analyze_shot` over
  `interf_archive.iter_steps` (our reader; streamer only writes the archive). ADIOS carries no D14 fields. An archived shot is identified by its shot time, recomputed
  from the stored WAVEDESC with the D15 conversion, which also gives `shot_date`.

### D10. Supervision: systemd
- `deploy/systemd/diag-ioc@.service` (exists): one instance per `/etc/diag-ioc/<name>.toml`, restart on failure.
- Runs as the non-root `diag-ioc` user (already in the unit), never as root. As root, EPICS locks all memory in RAM
  (about 316 MiB vs 62 MiB idle in the bench).

### D11. Pin the EPICS stack
- Exact versions of `softioc`, `epicscorelibs`, `pvxslibs` and (for C6) `p4p` in a constraints file (pyproject's `softioc>=4.7.2` is only a
  floor). Upgrades are deliberate and tested.

### D12. Repo layout
- `diag_ioc/` stays in this repo until a second diagnostic exists, then becomes its own package with modules
  found by entry point.
- `diag_ioc` imports neither `streamer` nor `interf_*`. The interferometer module is `interf_ioc.py` at the repo
  root (C5).

### D13. Agent roles and gates
- **Local agent (Windows):** the only code writer. It runs no tests unless J asks. Pushes the planned commits (C4r, C5, then C6–C8) to `refactor/linux-epics-daq`
  on top of the latest head, pulling before each commit.
- **Cloud agent (Linux):** runs the test suite, including the Linux-only tests, and synthetic scenarios (`interf_sim.synthetic`, `interf_sim --ioc-link`,
  pyepics CA checks) on the latest head, reports results and proposed patches to J, and pushes nothing. PVA
  tests are skipped where the kernel has no IPv6.
- **pyepics monitor rule (bench F2):** pyepics reports a PV connected before it subscribes, so the last monitor
  created can silently never start (8 of 35 trials). Every CA monitor client (the tests, through
  a `tests/ioc_harness.py` helper such as `monitor_all(names, callback, timeout)`) calls `epics.ca.flush_io()` until
  every PV has delivered its first value, then reports ready. Otherwise it fails and names the PVs that never
  delivered. Drop values that arrive before the first shot (the connect-time value is not a shot). The rule applies to
  CA test clients; `interf_store` uses PVA (D9).
- **Real-data gates are deferred, not waived:** plausible density, no missing ports on real `.trc` shots, the
  record length that sets `max_points`/`ca_max_array_bytes`, and TRIGGER_TIME resolution. Each stage stays
  "pending real-data check" until J connects the Linux machine with `.trc` files. No trc-dependent work before then.
- **PVA gate:** the PVA group (one update per shot, no torn reads) is untested here, because the cloud has no IPv6, so
  the cloud skips C6's PVA tests. Run them on the Linux machine, before C6 is called done.
  J re-runs the IOC bench (`ioc_bench.py run --quick`, without `--no-pva`) on an IPv6 machine.

### D14. Shot identity
- LAPD has no facility shot number yet. When one exists, it is added beside ours.
- A shot is identified by `shot_date` (YYYYMMDD) + `shot_number` (0, 1, 2, … restarting each day). Both are
  integers, carried in the envelope and HDF5, and published as `SHOT:DATE`/`SHOT:NUM`.
- `interf_main` assigns them once per capture and stores them in `RawShot`. The live link and HDF5 carry them.
  ADIOS does not (streamer is not ours, D6). The date is the D15 shot time in America/Los_Angeles local time. A run crossing midnight starts the
  new day at 0.
- The counter is persisted on disk (date + last number). A restart on the same day continues from last + 1.
- A gap in `shot_number` means a lost shot.

### D15. Shot time
- The official shot time is the LeCroy trigger time from the WAVEDESC TRIGGER_TIME fields (`tt_year` …
  `tt_second`, seconds as a double), on every channel. Use the first channel read. It is stored as float64
  seconds since the Unix epoch (UTC), like the main-branch HDF5, and used for the envelope `shot_time`, the
  EPICS record timestamps, and HDF5.
- The scope's clock is synced to the lab NTP server on a regular schedule. This is a scope setting, not code (J).
- **Conversion:** the WAVEDESC fields are wall-clock time with no zone, and the scope is always on LA local time.
  The zone is always `America/Los_Angeles` (J), a constant, not a setting. Convert with lab_scopes ≥ 0.4.1
  `wavedesc_trigger_timestamp` (`zoneinfo` in that zone; hjia94/lab_scopes#2), never with `calendar.timegm`,
  `time.mktime` or the host zone. That helper takes the first occurrence of the repeated DST fall-back hour; in
  that hour, pick whichever occurrence is closer to `host_time`.
- **Fallback:** when a shot has no trigger time (no LeCroy data, or `tt_year` = 0), `shot_time = host_time` and
  `time_source = "host"`. Otherwise `time_source = "trigger"`.
- **Check:** `STAT:TRIG_LAG` = `host_time` − `shot_time`. A shot whose lag is negative or above 5 s
  (`INTERF_TRIG_LAG_MAX_S`) is logged as a clock problem.
- `host_time` (`time.time()` when the PC detects the capture, `interf_raw.py:217`) lags the trigger by the scope's
  processing plus polling. It is kept only as the fallback and the cross-check.
- The main branch's own timezone bugs (`mktime` in the merge, `interf_read.py`'s fixed UTC-8) are fixed separately.

## 2. Open

- Config values still to set (no code change): `max_points`/`ca_max_array_bytes` (real record length, D13) and the
  systemd user/group/paths.
- Deferred by J: whether legacy HDF5 readers accept empty P40 arrays where the old writer stored zero-filled
  placeholders (D9).

## 3. Next steps (in order)

Commit labels and file-level specs are in `HANDOFF_live_epics.md`.

1. C4r, one commit revising the C1–C4 work: the C4 test fixes; this file as `docs/ARCHITECTURE.md` with the handoff;
   lab_scopes pinned to v0.4.1; the live link decoupled from streamer (D6, ADIOS output unchanged); shot identity and time (D14, D15) in acquisition,
   tested with synthetic WAVEDESCs across midnight, the DST change, a restart and a missing trigger time.
2. C5, one commit: `interf_ioc.py` per D2, D4, D5, D7, with the prefix from config, `<name>.db` written at start, the
   host's `SHOT:ACK` wait (tested by publishing back-to-back and finding no torn chain), the read-only ACF (D8) and the
   constraints file (D11).
3. Cloud agent: synthetic C5 scenario (IOC + `interf_sim --ioc-link`, CA readback of the D4 list, STALE after stop, IOC
   restart mid-run). C6 (PVA storage) is then tested on the Linux machine.
