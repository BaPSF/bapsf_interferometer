# EPICS live-analysis architecture: decisions

Repo `BaPSF/bapsf_interferometer`, branch `refactor/linux-epics-daq`. Done: C4r `4ee26b8`, the link socket lock
`ebb0b9f`, C4s `465b76a`, C5 `b8ecf40` and C6 `442c405`; C4s and C5 are tested on Linux with real recordings. `git log`
has the current head.
Status: **D1–D15 ACCEPTED by J on 2026-10-08, D16 on 2026-10-28, D17 on 2026-10-30. D18 PROPOSED (design chosen by J on 2026-11-05; open items in section 2).** Every agent follows this file, and
`HANDOFF_live_epics.md` implements it commit by commit. Anything neither covers goes to J before code is written.

**Single source:** this file is `docs/ARCHITECTURE.md` on `refactor/linux-epics-daq`, next to
`docs/HANDOFF_live_epics.md`. Every session (the local coding agent and `test-on-Bobby`) pulls the
branch and reads both before any work. Copies elsewhere (the project's shared folder, an agent's memory) are not
authoritative. Doc changes land only as commits by the local agent; other sessions send them as patch files.
The repository is public: design public, secrets private. D10, Public repository, says what never goes into a commit;
the design and its hardening, with their reasons, belong here.

## 0. What existed at 32f40cc (before C4r)

```
interf_main (acquisition, Linux)
  └─ RawShot ─┬─ RawOutput (streamer/, ADIOS BP5)        -> complete raw output
              └─ IocLink (diag_ioc/link.py, latest-wins) -> unix socket / loopback TCP
                                                              │
diag_ioc host (pythonSoftIOC, one process per config)  <──────┘
  ModuleHost: LinkListener -> ShotPipeline (mailbox + worker) -> module.analyze() -> module.publish()
  per module: <P>:STAT:*; per IOC: devIocStats; serves CA + PVA (QSRV2)
```

Both outputs currently encode with `streamer.payload.shot_variables`. D6 removes that dependency from the live
path. Each output keeps its own per-process counter (`streamer/raw_output.py:20`, `diag_ioc/link.py:80`), and
the record timestamps are `host_time`. C4r (`4ee26b8`) replaced both, per D6, D14 and D15.

## 1. Decisions

| # | Decision | Choice |
|---|---|---|
| D1 | IOC technology | pythonSoftIOC, standard record types only |
| D2 | Record definitions | Python `builder` in the module; host exports `<name>.db` at start (`<name>` = config file name) |
| D3 | Process layout | One `diag-ioc@<name>` process per diagnostic |
| D4 | PV naming | Prefix `LAPD:DIAG:INTERF` (config value), fixed sub-tree below |
| D5 | Where analysis runs | In the IOC process, from raw shots over the link |
| D6 | Live data contract | Independent of streamer; encoding beside `interf_main`, socket code in `diag_ioc` |
| D7 | Per-shot consistency | One timestamp per shot, `SHOT:NUM` set last, PVA group `<P>:SHOT` |
| D8 | Outside group | Read-only access to documented PVs; nothing built for them |
| D9 | Source of record | ADIOS raw output; IOC-fed HDF5 is best effort |
| D10 | Supervision | systemd system units, enabled at boot: acquisition as `lapd`, IOC, store and page as `diag-ioc` |
| D11 | Dependencies | Exact pins for `softioc`, `epicscorelibs`, `pvxslibs`, `p4p` |
| D12 | Repo layout | `diag_ioc` stays here until a 2nd diagnostic exists |
| D13 | Agent roles | Local agent writes code; `test-on-Bobby` tests every commit on Linux with real `.trc` data |
| D14 | Shot identity | `shot_date` (YYYYMMDD, LA local) + `shot_number` from 0 each day, persisted |
| D15 | Shot time | LeCroy WAVEDESC trigger time (scope NTP-synced), float64 epoch seconds |
| D16 | PNG run and shot | Optional CA monitor in `interf_main`, copied at capture detection, ADIOS only; readers match by `host_time` |
| D17 | Raw output never holds up acquisition | streamer's writer thread opens, retries and drains; `write()` only queues |
| D18 | Browser UI | `interf_web` behind nginx: p4p subscriber on `<P>:SHOT` + FastAPI WebSocket, read-only, per-shot density vs time first |

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
- The host writes the builder's records with `builder.WriteRecords()` to `<name>.db` (e.g. `interferometer.db` for
  `interferometer.toml`) at every start, so a current file listing every module PV always exists: 40 for the
  interferometer, its own 31 plus the host's 9 `STAT:*`/`SHOT:ACK` records. The `<P>:IOC:*` records are not in it:
  softioc's `devIocStats()` loads them from its own template, outside the builder, and they are the standard
  devIocStats set, so a copy in the `.db` would add nothing. It goes in
  `[ioc] db_dir` (default: the config file's directory). The IOC runs as non-root (D10) and cannot write
  `/etc/diag-ioc`, so the systemd unit points it at its state directory.
- Lifetime: PVs exist while the IOC runs. After a crash, systemd restarts it within about 5 s and the data PVs are
  INVALID until the next shot. When acquisition stops, PVs keep the last shot and `STAT:STALE` goes MAJOR.
  Copies that survive our restarts are a consumer's own IOC with CA links.

### D3. One IOC process per diagnostic
- Isolates crashes, restarts and the GIL. The host can also group modules; do that only for tiny ones.
- With 2+ IOCs on one host, CA search needs care (shared UDP 5064): set an address list, use a per-IOC
  `EPICS_CA_SERVER_PORT`, or add a gateway. Handle it when the second IOC arrives.
- The lab's PV gateway, `10.97.106.2`, republishes every PV on the lab subnet, ours included, and cannot be
  stopped or filtered (2026-11-09). A client that searches the subnet finds each of our PVs twice (`Duplicate PV name
  LAPD:DIAG:INTERF:SHOT ... 10.97.106.2:55075`), so the clients on the IOC's host search loopback only:
  `EPICS_PVA_ADDR_LIST=127.0.0.1` with `EPICS_PVA_AUTO_ADDR_LIST=NO` in the store's and the page's units (D9, D18).

### D4. PV list
`<P>` = `LAPD:DIAG:INTERF`. It is a config value, so code and tests never contain the literal. Later diagnostics
use `LAPD:DIAG:<NAME>`. Units and names are the stable interface; everything else may change.

| PV | Meaning |
|---|---|
| `<P>:SHOT:DATE` | shot date, YYYYMMDD, LA local (D14) |
| `<P>:SHOT:NUM` | shot number within the day, from 0 (D14); FLNK chain head, set last |
| `<P>:SHOT:TIME_SOURCE` | `trigger` or `host`: where the shot timestamp came from (D15) |
| `<P>:SHOT:HOST_TIME` | epoch s when the PC detected the capture; cross-check only |
| `<P>:SHOT:TIME_STR` | shot time as LA local text, for the screen |
| `<P>:SHOT:CRIT_PATH` | s, acquisition critical path (`critical_path_s`) |
| `<P>:SHOT:LOST` | running total of shots lost on the live link, from gaps in `SHOT:NUM`; a new day, a decrease, or the first shot after IOC start resets the baseline |
| `<P>:SHOT:MISSING` | which scope/channel is missing and why |
| `<P>:<port>:TIME_ARRAY` | ms, scope time axis (`<port>` = P20, P29, P40) |
| `<P>:<port>:PHASE` | rad |
| `<P>:<port>:NE` | m⁻³, path-averaged density |
| `<P>:<port>:NE_MEAN` | m⁻³, mean over the `CFG:NE_WINDOW_MS` window |
| `<P>:<port>:MISSING` | why this port has no data, `""` when it has; no alarm (its arrays carry INVALID) |
| `<P>:<port>:CAL` | m⁻³/rad, calibration factor; fixed from config when the records are created |
| `<P>:<port>:FREQ` | GHz, the port's interferometer frequency; fixed from config when the records are created |
| `<P>:CFG:NE_WINDOW_MS` | ms, (start, stop) averaging window, 2 elements; default (5, 10), fixed from config when the records are created; to become adjustable from the screen later (D8) |
| `<P>:STAT:CONNECTED`, `STALE`, `AGE_S`, `DROPPED`, `ANALYSIS_S`, `ERROR` | host health (exist in C4) |
| `<P>:SHOT:ACK` | internal: copies `STAT:PUBLISHED` when the chain finishes (D7); read-only, not in the group |
| `<P>:STAT:PUBLISHED` | shots published by this IOC process (monotonic, restarts at 0 with the IOC) |
| `<P>:STAT:ACK_TIMEOUTS` | publishes that went ahead after the 2 s ack timeout (D7); should stay 0 |
| `<P>:STAT:TRIG_LAG` | s, `host_time` − shot time; growth means lost NTP sync or detection lag |
| `<P>:IOC:*` | devIocStats |
| `<P>:SHOT` | PVA group of the shot's records |

`SHOT:SEQ` from the earlier handoff is dropped. `SHOT:LOST` counts shots lost on the link. `STAT:DROPPED` counts only
shots dropped by the IOC mailbox. `SHOT:LOST` does not count shots missed while the IOC was down, because the first
shot after an IOC start resets its baseline: a restart shows only as a gap in `SHOT:NUM` and in the link's outage log
(on Bobby, 2026-10-11, a SIGKILL and restart showed as 12 to 17).

### D5. Analysis runs in the IOC process
- Raw shots are not PVs, so analysis must sit on the raw-data path (the link from `interf_main`). Phoebus,
  `interf_store` and other clients only consume results. Offline, `analyze_shot()` re-runs the same analysis on
  the ADIOS raw output.
- Keeps acquisition's critical path to scope I/O only. Analysis can crash or restart without touching
  acquisition. The cost is about 12 MB per shot over a local socket. Acquisition and the IOC share a host (unix
  socket) or use an allow-listed loopback TCP link, since the link is unauthenticated.
- Rejected: a separate analysis process writing into an IOC (loses the single atomic update, needs write access).
- `analyze()` stays pure and picklable, so a process pool can be added later without touching modules.

### D6. Live link independent of streamer
- `streamer/` belongs to the streaming team; we change it only with their permission (J, 2026-10-29) and hand every
  change back; the live link never imports it.
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
  - The host increments and sets `STAT:PUBLISHED` just before each `publish()`. Before the next shot, it waits until
    the ack equals the last published count. The wait times out at 2 s, then logs, increments
    `STAT:ACK_TIMEOUTS` and proceeds.
  - The ack follows the IOC's own counter, not `SHOT:NUM`, because `SHOT:NUM` resets each day: a day ending at 0
    followed by a new day's 0 would pass the wait early.
  - Verified pattern: `repro/chain_overlap_ack.py` in the bench results, outside the repo (0 torn chains in 3000 back-to-back runs, versus
    1703 without the wait).
- **CA versus PVA delivery (bench F3).** When a CA client falls behind, the server merges its queued updates per
  channel, so the client can see a dropped, duplicated or torn shot. This is inherent to CA. CA clients match a
  shot's records by identical timestamp and expire incomplete shots. Only the PVA group is atomic under client stalls,
  so `interf_store` uses it (D9).
- Missing data is published as an empty array with INVALID alarm, the port's own reason in `<port>:MISSING` and
  every reason of the shot in `SHOT:MISSING` (cut at 1023 bytes), never as fabricated zeros.

### D8. The outside group is a read-only client
- Read access to the D4 PVs over CA/PVA, nothing else: no write access, no code or design changes for them, no
  maintenance owed. Their suggestions are input, not requirements.
- Enforced with an access-security file that makes every record read-only (`deploy/diag-ioc/readonly.acf`, the
  config's `[ioc] acf`), installed through `imports.install_pv_logging(acf_path)` before `iocInit` (softioc 4.7.2). It
  covers PVA/QSRV2 too: tests refuse the CA access right itself (DISP=1 alone leaves it granted), and
  `test_pva_puts_are_rejected` covers PVA, `IOC:HEARTBEAT` included (it runs only where IPv6 works). If they are on
  another subnet, put a CA/PVA gateway in front.
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
- **Separate process (J, 2026-10-18):** `interf_store` is its own process, fed only by `<P>:SHOT`. It runs in parallel
  with acquisition, never inside the `interf_main` loop and never in series with it, and nothing in `interf_main`,
  `interf_raw` or the IOC imports it, calls it or waits on it. The p4p callback never blocks: it only puts the value on
  a bounded queue (100). On any write trouble (a full queue, a file error, a full disk) the store drops that shot, logs
  it and goes on. A shot missing from HDF5 is acceptable, because the raw stream is the ground truth. A shot stored
  with the wrong `shot_date` or `shot_number` is not, so both come from the `date` and `num` fields of that same group
  update, and every gap is logged.
- HDF5 keys: the shot ID (`shot_date` + `shot_number`, D14; attributes of the `phase_p20` dataset, with `time_source`)
  and the trigger time (the dataset name). No PNG run or shot fields: HDF5 stores analyzed shots whether or not a PNG
  run is going.
- Missing ports in HDF5 (J, 2026-10-18): main never stored a shot with P20 or P29 missing (the error skipped the
  shot), so every legacy shot has both phases as long as `time_array`, and the readers rely on that. A missing P20 or
  P29 phase is NaN of that length, with `missing`/`missing_reason`; the shot is still stored. With both missing,
  everything stays empty. P40 stays empty, as main writes it. Main's `interf_plot.py` axis limits (`np.min`) fail on a
  NaN shot; Phoebus replaces it (C7).
- As built (C6): a missing port's reason is its own `<port>:MISSING` group field (`missing_reason` on P20 and P29,
  `rigol_missing_reason` on P40). An update with `date` or `timeStamp` 0 is the IOC's state before its first shot after
  a start, not a shot, and is skipped; a duplicate after a reconnect is skipped and counted. On SIGTERM or SIGINT it
  drains the queue and logs a summary. `interf-store.service` runs it as `diag-ioc` (D10) with
  `INTERF_DATA_DIR=/var/lib/interferometer/hdf5`; without the variable the directory is `~/data/interferometer`. Its
  PVA search is loopback only (`EPICS_PVA_ADDR_LIST=127.0.0.1`, `EPICS_PVA_AUTO_ADDR_LIST=NO`, since 2026-11-09):
  the lab's gateway republishes `<P>:SHOT` on the subnet (D3).
- The ADIOS raw output is the record. Anything analyzed can be regenerated with `analyze_shot` over
  `interf_raw_reader.iter_steps` (our reader; streamer only writes raw output). ADIOS carries no D14 fields. A raw output shot is identified by its shot time, recomputed
  from the stored WAVEDESC with the D15 conversion, which also gives `shot_date`.

### D10. Supervision: systemd
- System units (J, 2026-10-12): `deploy/systemd/diag-ioc@.service`, one IOC per `/etc/diag-ioc/<name>.toml`, and
  `deploy/systemd/interf-acquire.service`, acquisition. Install steps are in each unit's header.
- The third unit (J, 2026-10-18): `deploy/systemd/interf-store.service`, the HDF5 store (C6, D9).
- The fourth unit: `deploy/systemd/interf-web.service`, the browser page (D18), reached through Debian's nginx with the
  files in `deploy/nginx/`.
- Accounts (J, 2026-11-08; on Bobby since 2026-11-09): acquisition alone runs as `lapd`, the operators' account,
  because `lapd` holds the NERSC stream key, renewed daily. The IOC, the store and the page run as the system account
  `diag-ioc` (no login): they need neither the key nor sudo. Acquisition reaches the IOC's 0660 link socket, in the
  0750 `/run/diag-ioc/<name>` (`RuntimeDirectoryMode=0750`), through `SupplementaryGroups=diag-ioc`. Never root (as root
  EPICS locks all memory in RAM: about 316 MiB vs 62 MiB idle in the bench), and never `softioc`, the old procServ
  IOC's account. No state directory is shared across accounts, because systemd re-owns a unit's `StateDirectory`,
  `LogsDirectory` and `RuntimeDirectory` to its `User=` at every start: acquisition has `/var/lib/interf-acquire`, the
  store `/var/lib/interferometer`. The IOC, the store and the page have `IPAddressDeny=10.106.34.0/24`, the scope
  network (`enp2s0` on Bobby); acquisition has none, since it talks to the scopes.
- Paths: code in `/opt/bapsf_interferometer` (root-owned, with its venv), the operation checkout, tracking
  `refactor/linux-epics-daq`; Bobby's test clone, in the tester's home, is separate. Config in `/etc/diag-ioc/<name>.toml`
  (+ optional `<name>.env` for `EPICS_*`) and `/etc/interferometer/acquire.env`, `db_dir` `/var/lib/diag-ioc`, link
  socket `/run/diag-ioc/<name>/link.sock`, acquisition log `/var/log/interferometer`, shot counter
  `/var/lib/interf-acquire/shot_counter.json` (on Bobby since 2026-11-09; on a new machine, copied once from where
  acquisition kept it, by default `~/data/state/` of the account that ran it, so numbering continues), local raw
  output, if any, under
  `/var/lib/interf-acquire/raw/`, HDF5 files `/var/lib/interferometer/hdf5` (`INTERF_DATA_DIR`; systemd makes the
  state directory 0755 and the files come out 0644, so every local user reads them and only `diag-ioc` writes), store overrides
  `/etc/interferometer/store.env`, `interf_web` history files `/var/lib/interf-web` (`INTERF_WEB_STATE_DIR`), its
  overrides `/etc/interferometer/web.env`. Logs also go to the journal. Acquisition and the store have separate state
  directories.
- Public repository (J, 2026-11-09; replaces the rules of 2026-11-08): design public, secrets private. The repository
  is public and its history stays as it is (no rewrite). It holds the design and its hardening, with the reasons:
  account choices, limits, sandbox and `IPAddressDeny=` lines, proxy and unit configuration, and the tests that pin
  them. Lab-internal 10.x addresses and account names may appear. Security settings describe what is blocked, not how
  to get in; leaving them out would make a fresh install from the repository unsafe and let them drift untested. Only
  these stay out of commits, in untracked host config or private notes: anything that works like a password (keys,
  certificates, tokens, ntfy topic names), public IP allowlists, personal home paths, and the list of known weaknesses
  not yet fixed (each until it is fixed). Committed files carry placeholders or `.example` copies for these, as
  `deploy/systemd/acquire.env.example` and `deploy/nginx/diagnostic.nginx.example` (its listen address) do. Untracked host config: `/etc/interferometer/*.env`,
  `/etc/diag-ioc/*.env`, and the raw output stream `.conf` in the operator's home (for example
  `~lapd/nersc-stream/stream.conf`). The ntfy topics already in history are retired and replaced on the host, not
  removed from history. `.gitignore` excludes `*.local.conf`, `*.local.env` and any `local/` directory. When unsure,
  ask: would this help someone outside get in? If yes, keep it private; if not, commit it with its reason.
- Site settings (J, 2026-11-09): a host's drop-in, `/etc/systemd/system/<unit>.d/site.conf`, holds only values that
  differ per machine, such as a listen address. The four units need none on Bobby (`interf_web` keeps its default,
  127.0.0.1). nginx, a Debian package, takes its settings through a drop-in, `deploy/nginx/nginx.site.conf`, installed
  as committed, like the units; only its site file, `deploy/nginx/diagnostic.nginx.example`, has a placeholder, for the
  one per-machine value, the listen address. From 2026-11-08 to 2026-11-09 the units carried a placeholder `User=` and
  Bobby's drop-ins held the accounts and the hardening; Bobby removed those drop-ins on 2026-11-09 (Evidence). The Rigol's address is set in the host's `acquire.env` (code default: the Rigol disabled).
- Ordering: `interf-acquire` has `Wants=` and `After=` on `diag-ioc@interferometer` (and `network-online.target`), never
  `Requires=`/`BindsTo=`/`PartOf=`, so an IOC stop or crash never stops acquisition (D5). `After=` orders process starts,
  not readiness: the IOC listens about 1.3 s after it starts, and the link's retry covers the gap. `interf-store` has
  only `After=` on the IOC (no `Wants=`, never `Requires=`/`BindsTo=`/`PartOf=`), so it never starts or stops the IOC;
  p4p reconnects to the group by itself. `interf-web` has the same single `After=` and no `network-online.target`: if it
  cannot start at boot, `Restart=` retries.
- Start at boot (J, 2026-11-08): all four units are enabled, so the system recovers unattended after a power cut:
  `systemctl enable diag-ioc@interferometer interf-acquire interf-store interf-web`, and nginx. Bobby's firmware must be
  set to power on after AC loss. Raw output whose local checks fail at start (`interf_main._start_raw_output`, D17)
  stays off until acquisition restarts; acquisition itself runs. On Bobby all four and nginx are enabled (2026-11-09).
- Switching an existing host to these accounts (Bobby, 2026-11-09): stop acquisition and copy its shot counter to
  `/var/lib/interf-acquire/` first. At the store's first start as `diag-ioc`, systemd re-owned all of
  `/var/lib/interferometer` to it, old `raw/`, `raw-test/` and the stale shot counter included, and likewise
  `/var/lib/diag-ioc`. Move anything another account must still write out of those directories beforehand.
- EPICS ports: the default CA 5064 and PVA 5075. The old procServ IOC (prefix `Interferometer:Port31:`) that used them
  was removed from the lab machine by its admin on 2026-10-11. If a second IOC runs on that host again, clients there,
  Phoebus included, must search the broadcast address, not unicast `127.0.0.1`, or only one IOC answers.
- Restart: the IOC, the store and the page `always`, acquisition `on-failure`, all 5 s apart; acquisition gets 30 s to
  stop, so SIGTERM can finish the shot in flight and restore the trigger modes, the store 30 s to drain its queue (its
  writer gets 20 s, so the summary is logged before SIGKILL), and the page 30 s (open sockets get 5 s).
- The prefix `LAPD:DIAG:INTERF` appears in the store and web units' `ExecStart` (`--prefix`) only, never in code or tests (D4).
- Evidence (`test-on-Bobby`, lab machine, systemd 257, the 55-shot replay in place of acquisition): as `systemctl --user`
  units, 9 of 9 checks passed on 2026-10-11 (restart policy, ordering, clean stops, link recovery). As these system
  units, all checks passed on 2026-10-12: both run as `diag-ioc`, the socket and `/var` directories have the right
  owner and modes, `diag-ioc` can read the `.trc` data, a full replay (sent 55, dropped 0, failed 0; `SHOT:LOST`,
  `STAT:DROPPED`, `STAT:ACK_TIMEOUTS` 0), and an IOC SIGKILL mid-replay with restart and link recovery. The replay
  does not exercise acquisition's `/var/log` and `/var/lib` paths; the first live run does. Since 2026-11-09 all four
  units run on Bobby with the accounts above (acquisition as `lapd`, the rest as `diag-ioc`). At 05:34 that day Bobby
  installed the four repo units unchanged from `/opt` at `ff04e83` and removed their drop-ins: `systemctl show` gave
  the same effective settings, except the store's environment in another order and the page's `INTERF_WEB_HOST`, now
  unset, so the code's default 127.0.0.1. Bobby runs the repo units with no drop-ins; nginx's is the only one.
  `systemd-analyze verify` passes on the four units, and the full suite ran 249 tests, all OK, 11 skipped. `nginx -t`
  on the site file with the address filled in reported "syntax is ok" (run as a normal user, which then cannot bind
  port 80).

### D11. Pin the EPICS stack
- Exact versions of `softioc`, `epicscorelibs`, `pvxslibs` and (for C6) `p4p` in a constraints file (pyproject's `softioc>=4.7.2` is only a
  floor). Upgrades are deliberate and tested. The same file pins `blosc2` (the `compression` extra, as installed in
  `/opt/bapsf_interferometer/.venv`) and `interf_web`'s web stack (D18), which has run on the lab machine as its unit
  (2026-11-06).

### D12. Repo layout
- `diag_ioc/` stays in this repo until a second diagnostic exists, then becomes its own package with modules
  found by entry point.
- `diag_ioc` imports neither `streamer` nor `interf_*`. The interferometer module is `interf_ioc.py` at the repo
  root (C5).

### D13. Agent roles and gates
- **Single source (J, 2026-10-11):** see the header. A session that finds the docs and the code disagree reports it
  rather than working from its own copy.
- **Local agent (Windows):** the only code writer, `docs/` included. It runs no tests unless J asks, and says in each
  commit message what it ran and what it did not. Pushes the planned commits (C6–C8) to `refactor/linux-epics-daq`
  on top of the latest head, pulling before each commit.
- **`test-on-Bobby` (the Linux lab machine with the `.trc` recordings), the only test session (J, 2026-10-11):** runs
  the full suite, including the Linux-only tests and the PVA tests, on each pushed commit, plus its scenario on real
  `.trc` files and the real-data checks (handoff §5). It reports results and proposed patches to J, and pushes
  nothing. The cloud test agent is retired; no new synthetic data generators are needed.
- **pyepics monitor rule (bench F2):** pyepics reports a PV connected before it subscribes, so the last monitor
  created can silently never start (8 of 35 trials). Every CA monitor client (the tests, through
  a `tests/ioc_harness.py` helper such as `monitor_all(names, callback, timeout)`) calls `epics.ca.flush_io()` until
  every PV has delivered its first value, then reports ready. Otherwise it fails and names the PVs that never
  delivered. Drop values that arrive before the first shot (the connect-time value is not a shot). The rule applies to
  CA test clients; `interf_store` uses PVA (D9).
- **Real-data gates** (`test-on-Bobby`, since 2026-10-11): plausible density, no missing P20/P29 on good real `.trc`
  shots (they hold no Rigol traces, so P40 is always missing in replay), and TRIGGER_TIME resolution. Passed for C5
  on 55 recorded shots (handoff §1 has the numbers). The record length (1,000,002 samples at 0.1 µs) set
  `max_points = 2048` and `ca_max_array_bytes = 65536` in C5.
- **PVA gate:** the PVA group (one update per shot, no torn reads) needs kernel IPv6; the PVA tests skip without it.
  They passed on Bobby against the real IOC for C5 (2026-10-11), which replaces the planned PVA bench re-run. The CA
  bench verdict (D1: keep pythonSoftIOC) stands.

### D14. Shot identity
- LAPD has no facility shot number yet. When one exists, it is added beside ours.
- A shot is identified by `shot_date` (YYYYMMDD) + `shot_number` (0, 1, 2, … restarting each day). Both are
  integers, carried in the envelope and HDF5, and published as `SHOT:DATE`/`SHOT:NUM`.
- `interf_main` assigns them once per capture and stores them in `RawShot`. The live link and HDF5 carry them.
  ADIOS does not (streamer is not ours, D6). The date is the D15 shot time in America/Los_Angeles local time. A run crossing midnight starts the
  new day at 0.
- The counter is persisted on disk (date + last number), on a local disk. Numbers are assigned in memory on the
  acquisition thread. The file is written (fsync + atomic replace) by a background thread, so disk speed never holds up
  the main loop.
- At start, counting resumes from the later of the state file and the last `shot <date>-<number>` in the acquisition
  log, plus 1, so a lost last write never repeats a number (J, 2026-10-11):
  - a readable state file: the later of it and the log;
  - a missing or unreadable state file: the log's last shot (unreadable is logged as a warning);
  - neither: counting starts at 0, as on a first start. An unreadable state file with no logged shot is logged as a
    warning that numbers already used that day may repeat.
- `shot_time` stays the unique key in storage (HDF5 dataset names).
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

### D16. PNG run and shot numbers (J, 2026-10-28)
- PNG's DAQ publishes `PNG:RunNumber` and `PNG:ShotNumber` over CA, written once per shot from the same trigger as ours.
  The PVs keep their last value until overwritten.
- **Monitor:** `interf_main` subscribes to both with pyepics (`interf_png.PngMonitor`). The value callback only stores
  the value and its CA timestamp, under a lock; it makes no CA call. At start, CA is flushed until both PVs have
  delivered a first value (the subscription race of D13), for at most 5 s; after that "PNG not connected" is logged
  once and acquisition starts anyway. `interf_main` flushes CA after each handled shot, so a subscription that missed
  the start still goes out.
- **Capture stamp:** when `interf_raw._consume` detects a LeCroy capture, it copies the stored values on the line right
  after `host_time`, before the on/off check and the waveform transfer. It is a memory read with no network call, and
  separate from the transfer, so a parallel transfer later does not move it.
- **Always saved:** every shot keeps PNG's latest values and timestamps, even if unchanged since the previous shot. Only
  shots before the first value after a start have none (-1/NaN in ADIOS).
- **Matching is done by readers, not acquisition:** `abs(host_time − png_shot_time) < 0.3 s` means the same shot. The
  lag is about 0–0.2 s normally, and about 3 s (the previous shot's value) when PNG's write came after our detection.
  `host_time` is used, not the LeCroy trigger time (D15), because the scope clock steps by about 0.3 s and has been off
  by up to 1.2 s. This assumes NTP on Bobby and on PNG's server.
- **ADIOS only:** the four values travel in `RawShot.png` to raw output (schema 2: `png_run` int64, `png_run_time`
  float64, `png_shot` int64, `png_shot_time` float64; -1/NaN = no value) and to the acquisition shot line
  (`png <run>-<shot> lag <s>`). streamer writes them on every step (handoff §4, PNG); `interf_raw_reader.decode`
  reads schemas 1 and 2. HDF5 is keyed by our `shot_date` + `shot_number` and has no PNG fields (D9); the live link
  (`interf_payload`), the IOC and `interf_store` do not carry them. They do not replace our shot identity (D14).
- **Live raw output:** `INTERF_RAW_OUTPUT` (a destination) makes `interf_main` write every shot through streamer's
  `RawOutput`, beside the `IocLink`, as `interf_sim --raw-output` does; how it avoids holding up acquisition is D17.
  Unset, there is no raw output, as before. streamer is imported only then; the live link still never imports it (D6).
- **Optional and fail-safe:** off unless `INTERF_PNG_RUN_PV` and `INTERF_PNG_SHOT_PV` are both set; otherwise neither
  `interf_png` nor pyepics is imported. If PNG's IOC is down, acquisition runs normally, shots keep the last value (or
  none), and pyepics reconnects on its own. No `caget` per shot: it waits up to 5 s when the IOC is unreachable.
- Amends the rule "`interf_main` never imports EPICS" (handoff §2): allowed for this one optional CA monitor. The main
  thread's added work (the copy, the shot-line part, the flush) stays O(1), never blocks and never raises.

### D17. Raw output never holds up acquisition (J, accepted 2026-10-30)
- **Rule:** acquisition must catch every shot. Streaming must never hold up the next shot, its analysis in the IOC or
  its HDF5 store, because raw output may go over a network, and networks fail. A shot that cannot be written in time
  is dropped from raw output and counted, never waited for.
- **Wording (J, 2026-10-30):** code and docs say "raw output" (or "raw output file"); the reader is
  `interf_raw_reader`.
- **In streamer** (changed with the streaming team's permission, D6; handed back to them):
  1. `RawOutput.write(shot)` assigns `shot_index`, records the `raw_output.write` timing event and queues the shot
     object itself. `shot_variables` runs on the writer thread; `RawShot` arrays are fresh per acquisition and never
     changed afterwards. `write()` raises only after `close()`.
  2. `RawOutput(settings)` makes only fast local checks (settings values, the private key, the compression
     configuration, ADIOS2 for direct output) and starts the writer thread. Opening, connecting and launching all run
     on that thread while shots queue. The mode follows the destination, never the key (J, 2026-10-30): an existing
     directory or a path ending in `/` is direct ADIOS to local files, even with a key; a `.conf` is the SSH-launched
     consumer; any other existing file is a connection file from a consumer started by hand; a path that does not
     exist is one direct ADIOS output. The `.conf` and connection-file modes need the private key, and a missing key
     (or a file that holds no connection information) fails the start-up check. All modes stay available to
     `interf_main` and `interf_sim`.
  3. The writer never dies. On any error from opening or writing an output it records `io.error`, closes or aborts
     that output, waits `retry_delay_seconds` (3 s), opens a new one the same way (re-reading the connection file,
     relaunching for a `.conf`, a new file for direct output) and retries the same step. Queued steps keep their order;
     a full queue drops a queued step, as before. `RestartingSingleSocketIO` keeps its own reconnect. `error` holds the
     last error text while the writer fails and None once a step is written again; it is a plain reference read, with
     no lock.
  4. New settings: `socket_timeout_seconds` (30 s, for SingleSocketIO's connect and ack, as the `.conf` default),
     `retry_delay_seconds` (3 s) and `file_interval_seconds` (3600 s); `buffer_seconds` stays.
  5. Direct ADIOS never overwrites. A directory destination (`INTERF_RAW_OUTPUT=<dir>/`) gets daily subfolders,
     `<dir>/YYYYMMDD/HHMMSS.ffffff+0000.bp`, named in UTC, with a new file every `file_interval_seconds` and at UTC
     midnight (the consumer's rotating writer; daily subfolders since the streaming team's PR #7, UTC since PR #8). A
     path that exists is written as `<stem>-1<suffix>`, `-2`, ..., so `interf_sim --raw-output raw.bp` still works.
     In `.conf` mode (PR #7) the config sets only `output_directory`: the consumer writes its logs under
     `<dir>/logs/` (`consumer-<start>.{conn.json,stdout.log,pid,jsonl}`) and the data under `<dir>/data/YYYYMMDD/`. A
     config that still names `connection_file`, `stdout_log`, `pid_file` or `timing_log` keeps the old paths.
  6. `close(timeout)` stops accepting, lets the writer drain for at most `timeout` s, then aborts a socket output,
     records `io.close` and reports `unsent_shots`. The writer thread is a daemon, so a stuck destination never keeps
     the process alive.
  7. The consumer's `--keep-listening` keeps its listener and connection file after a producer disconnects and accepts
     the next one, with a new output file per connection. Off by default.
  8. TCP keepalive (idle 30 s, then 3 probes 10 s apart, where the platform has the options) on the producer's socket
     and on the consumer's producer connection, so a cut network is noticed within about a minute. No receive
     timeout: gaps of minutes between shots are normal.
- **In `interf_main`:** the settings come from `INTERF_RAW_OUTPUT` and `INTERF_RAW_OUTPUT_*`
  (`streamer.arguments.settings_from_env`), with a 60 s queue by default. A failing local check is logged and
  acquisition runs without raw output. After each shot it reads `RawOutput.error` and logs it through
  `Outage("raw output")`: once when raw output starts failing, every 5 min while it lasts, once on recovery. The
  per-retry `io.error` lines stay in the timing log. At stop, the `IocLink` closes first, then
  `RawOutput.close(timeout=15)`, and the written, dropped and unsent counts are logged.
- **Out of scope, for J:** retention of local raw output files, or moving them off Bobby (about 9.6 GB/h, 230 GB/day
  of continuous running).

### D18. Browser UI: `interf_web` (J, design chosen 2026-11-05)
- **Goal:** anyone in the lab opens the page in a browser, with nothing installed, and sees each shot's density vs time
  for every port in real time. NE_MEAN now and its history are a bonus on top. The page is read-only.
- **Why not Phoebus Web Runtime (DBWR) or pvws:** both need Java and Tomcat on Bobby, update each PV on its own (a
  plot can mix two shots), throttle arrays to one per 10 s by default, and a trend would need an EPICS archiver. A
  small Python app reuses the stack `interf_store` already proves (D9).
- **Process:** the `interf_web` package at the repo root (`python -m interf_web`), its own process like `interf_store` (D9): nothing in `interf_main`,
  `interf_raw`, the IOC or `interf_store` imports it, calls it or waits on it. It runs as `interf-web.service`, as `diag-ioc` (D10). New dependencies in the `web` extra: `fastapi`, `uvicorn` and `websockets` (uvicorn alone has
  no WebSocket support). All three and `starlette` are pinned (D11), because `/ws` relies on how they report a browser
  that left. The page is `interf_web/static/index.html` with `uPlot.iife.min.js` and `uPlot.min.css` (uPlot 1.6.30, MIT)
  vendored next to it and packaged with it, so the page loads nothing from the internet. No JavaScript framework and
  no build step.
- **Data in:** a p4p monitor on `<P>:SHOT` (`p4p.client.thread.Context("pva")`, `notify_disconnect=True`), with
  `EPICS_PVA_ADDR_LIST=127.0.0.1` and `EPICS_PVA_AUTO_ADDR_LIST=NO`: the lab's gateway republishes our PVs on the subnet
  (D3). pvRequest
  `field(timeStamp,num,date,host_time,missing,<port>.time_array,<port>.ne,<port>.ne_mean,<port>.missing)` for the
  configured ports (lower-case, as in `interf_ioc._add_group`); phase is not needed. QSRV2 honours the field selection
  (on Bobby, 62.8 kB per shot against 94.2 kB for the whole group). A second monitor on `<P>:CFG:NE_WINDOW_MS` feeds the shaded window,
  because that record is not in the group. The IOC sees one client however many browsers are open.
- **Callback rule (as D9):** the p4p callback never blocks. It converts the update into one message (date, num,
  `host_time`, per port `time_array` and `ne` as float32 and `ne_mean`, missing reasons; ~50 kB) and hands it to the
  event loop with `loop.call_soon_threadsafe`. Skip an update with `date` or `timeStamp` 0 (the IOC's state before its
  first shot) and a duplicate `(date, num)` after a reconnect, as `interf_store` does.
- **Data out:** `GET /` serves the page. `WS /ws` sends the newest shot, latest-wins per browser: a slow browser skips to
  the newest shot and never holds up the callback or the IOC. A new or reconnected browser gets the latest shot at once.
  A status text frame follows each shot and goes out at least once a second: Bobby's time, the newest shot's age, whether
  the page is waiting, the LA midnights of today and yesterday, and the NE_MEAN window. Sending it is also how a server
  that never reads notices a browser that left.
  The server never reads from the socket, has no route that writes and no p4p `put`; with D8's read-only ACF that makes
  the page read-only at every layer (POST, PUT and DELETE get 405; `/docs` and `/openapi.json` 404). No login.
- **Limits (J, 2026-11-06; constants in `interf_web/__main__.py`):** the page is for about 20 viewers and shares Bobby's
  CPU with acquisition. Before the limits, Bobby found no connection limit, an idle connection that never sent a
  request open for over an hour, and `/api/trend` unlimited.
  - At most 20 open `/ws` sockets (`MAX_VIEWERS`), and 3 from one client address; more are accepted and closed at once
    with code 1013 and a reason the page shows, and one INFO line with the address and the reason (uvicorn logs the
    socket only as "[accepted]"). uvicorn's `limit_concurrency` is 80, 4 per viewer: it counts every open connection,
    `/ws` and idle keep-alive ones included, and a viewer holds 2 (its `/ws`, and its trend connection, reused every few
    seconds on the 1 h range) plus the 2-3 its browser opens to load the page. At 40, 20 viewers alone filled it and
    every trend request got 503. An HTTP request beyond it gets 503. uvicorn does not apply that limit to a WebSocket
    upgrade, hence the `/ws` caps in the app. Behind nginx the client address is the viewer's (Address).
  - A connection whose next request has not fully arrived 30 s after it opened or after its last response is closed:
    uvicorn's keep-alive timer, which in uvicorn 0.54 starts only after a response and stops at any byte, is started
    at connect and kept running until the headers are complete (a small subclass of its h11 protocol). Proposal:
    `TCP_USER_TIMEOUT` 90 s (Linux), so the kernel drops a connection whose sent data stays unacknowledged.
  - A `/ws` send that takes over 30 s ends that socket, so a browser that stopped reading frees its slot. Incoming
    WebSocket messages are limited to 4 kB (the page sends none). Ping every 30 s with a 60 s timeout: a dead peer goes
    within 90 s, a brief network hiccup does not drop a viewer.
  - No session limit and no idle logout: a viewer whose tab is visible and still reading frames stays for days, and
    mouse or keyboard activity is never needed. A window left in front on a screen nobody watches stays live too;
    with a cap of 20 that should be rare.
  - `/api/trend` is limited to 2 requests a second per client address, in bursts of up to 5; more get 429. Measured on
    the development PC (Windows, Python 3.14, numpy 2.5, Intel x86-64 family 6 model 158) over a full two-day history
    (57,600 rows, synthetic), one request costs 1.9 ms (1 h) to 8.3 ms (48 h) for `trend()` plus about 8 ms for its
    216 kB JSON body. A page asks at most once per shot (3 s) and once per trend bucket, so 20 viewers on the 1 h range
    (one request every 2 shots) cost about 0.03 s of CPU per second; 20 asking every shot for 48 h would cost about
    0.1 s, the whole of the unit's `CPUQuota` of 10% of one core (J, 2026-11-06: Bobby may host other applications
    later). Over the quota the kernel slows the page (frames and plots arrive late, start takes longer); acquisition
    is a separate unit and unaffected. Not yet measured on Bobby with many viewers; with 1-2 browsers Bobby measured
    about 150 MB and 11 s of CPU per hour.
  - uvicorn logs every WebSocket accept and every 503 at INFO or WARNING: its log passes 30 lines a minute below ERROR,
    then one line with the count dropped.
  - The unit (sized for 80 connections; the list with its reasons is `deploy/systemd/interf-web.service`, and
    `test_web_unit_sandbox` pins it): CPU, memory, task and file limits, and a systemd sandbox. Not used:
    `MemoryDenyWriteExecute`, which can stop libraries loaded through ctypes or libffi. If the unit fails to start or
    the page shows no data, `ProcSubset` is the first to remove, then `SystemCallFilter` and `RestrictAddressFamilies`.
    With `SystemCallErrorNumber=EPERM` a blocked call fails instead of killing the process, and a compiled library may
    take that quietly, so a clean journal alone does not show the sandbox is harmless.
  - Why (Bobby, `systemd-analyze security` 9.2 "UNSAFE" before these, 1.5 "OK" with them): code run through a bug in
    the web stack runs as `diag-ioc`. `IPAddressDeny` keeps it off the scope network. `ProtectSystem=strict` makes files
    read-only, not unreadable, so `InaccessiblePaths=` hides `/etc/interferometer/acquire.env`, `/etc/diag-ioc`,
    `/var/lib/interferometer` (the HDF5 files), `/var/lib/interf-acquire` and `/var/log/interferometer`, which the page
    never needs.
- **Page connection (`static/session.js`):** one WebSocket for the life of the page, never reopened. When it closes for
  any reason (network, server restart, a cap, a timeout, or no status frame for 60 s while the tab is visible), a banner
  says "Disconnected - reload the page to reconnect", with the server's close reason if it gave one, and the last plot
  stays on screen. A tab hidden or minimized for 15 min (`HIDDEN_PAUSE_MS`, the browser's visibility event) closes its
  socket and says "Paused - reload to resume"; it is cancelled if the tab comes back first. A reload opens a fresh
  socket and refills the history. Tests: `tests/test_interf_web.py`, the page's part in node.
- **Address (2026-11-09):** behind nginx at `/diagnostic/<name>/` (`http://<lab address>/diagnostic/interferometer/`),
  `interf_web` on `127.0.0.1:8080`, the code's default (`INTERF_WEB_HOST`, `INTERF_WEB_PORT`). The proxy's site file
  is `deploy/nginx/diagnostic.nginx.example` (the listen address a placeholder; install steps in its header), and its
  systemd drop-in `deploy/nginx/nginx.site.conf`, installed as committed. `test_nginx_site` pins the headers, the
  `proxy_pass` and the listen rule below; `test_unit_accounts` pins the drop-in's `IPAddressDeny`. The proxy
  passes the viewer's address in `X-Forwarded-For`, which `interf_web` reads from 127.0.0.1 only (`server_settings`),
  so the per-address limits count each viewer, not the proxy. nginx listens on the lab network's address only, never
  `0.0.0.0` (`enp2s0` is the scope network), and `interf_web` on loopback only, so nginx is the one way in. Both are
  enabled at boot on Bobby (2026-11-09).
- **Restart (known effect, 2026-11-09):** a restart of `interf-web` takes the page down for about 32 s: nginx answers
  502, open pages say "Disconnected", and a reload after that is Live. About 25 s of it is Python start-up under
  `CPUQuota=10%` (J, 2026-11-06: Bobby may host other apps; a slow restart was accepted).
- **Page, main view (most important; mirrors main's `interf_GUI.py` / `interf_plot.py`):** one plot of density vs
  time for P20, P29 and P40 (x `Time (ms)`, y `ne (10^18 m^-3)`, title "Density assuming 40cm plasma Dia"), colours as
  main (P20 red, P29 blue, P40 green), shot ID (`shot_date-shot_number`) and time above it. Controls: Pause (freezes the
  plot; new shots keep arriving, a bar shows how many), Keep trace (the current traces stay as faded dashed lines, like
  main's "Save current trace on plot"), Clear kept, drag to zoom (x only; y follows what is in view), double-click to reset, click a legend entry to hide a
  port. The `CFG:NE_WINDOW_MS` window is shaded. A missing port draws nothing and shows its reason. P20 and P29 are
  drawn as measured, with no correction or warning.
- **Page, bonus:** this shot's NE_MEAN per port, and an NE_MEAN history over today and yesterday that follows new
  shots, with range buttons (10 min, 1 h, 8 h, today, yesterday + today), drag to zoom and pause. It draws each time
  bucket's mean as a dot with a min-to-max bar, and no line, which would run straight across a pause. Shot health, link and
  IOC statistics and PNG run/shot are not shown (PNG stays off until the fake PNG run passes).
- **Waiting state:** after 3 missed shots (no new `(date, num)` for 10 s by Bobby's clock), a grey "Waiting for shots"
  bar says how long since the last shot, and the plot keeps the last shot. The status turns back to Live on the next
  shot. The server works this out from the newest shot's `host_time`, so the IOC's last shot received at a restart does
  not read as new. (The lab paused the scope for 34 min on 2026-11-04.)
- **Times:** shot time and the history's time axis use `host_time` (Bobby's NTP clock), labelled "Bobby clock", shown in
  LA local time. Never the scope's trigger time on the page: on 2026-11-04 the scope clock wandered from 0.95 s ahead to
  0.15 s behind.
- **History store:** keyed by `(shot_date, shot_number)`, since the shot number restarts at 0 each midnight (D14).
  The two newest shot dates are kept, in memory plus one small append-only text file per date in the service's state
  directory, so a restart keeps both (about 1.6 MB/day at a shot per 3 s; a line torn by a crash costs that shot, not the
  file). A new date drops the oldest day and deletes its file. Not built, pending section 2: yesterday after a restart
  from `interf_store`'s HDF5 file for that date (read-only, retried later if locked), then cached. If it is built, never
  open today's HDF5 file: `interf_store` opens it once per shot without SWMR, and a failed open drops that shot.
  `GET /api/trend?start=&end=&points=` (points 1 to 5000) returns min, max and mean per time bucket, only for buckets
  holding a shot. The page asks for it at most once per bucket width, and not while its tab is hidden.
- **Evidence:** a hand test on Bobby, not the unit: QSRV2 honoured the field selection (Data in), latency was about
  0.2 s, and `CFG:NE_WINDOW_MS` read `[5, 10]`. The unit itself (`test-on-Bobby`, 2026-11-06, from the operation
  checkout): history loaded with 0 lines skipped, and `/ws` delivered shots about 0.2 s after each shot. Full suite in a
  venv with the web packages: 210 passed, 11 skipped (5 need node, 6 need `.trc` files); `PageSessionTests`
  (`session.js`) were not run, as Bobby has no node. The sandbox in the unit (2026-11-06): the score went from 9.2 to
  1.5, and the journal had no "Operation not permitted", `Errno 1` or `status=31/SYS`, so `ProcSubset=pid` and
  `SystemCallFilter` stay; the 3-per-address cap closed the 4th socket with 1013 and its reason, and the slot was free
  0.3 s after a close. Bobby's kernel (6.12, Debian 13) is recent enough for `TCP_USER_TIMEOUT`. Behind nginx (`test-on-Bobby`, 2026-11-09, `72af7fd` plus host
  files): the URL without its trailing slash redirects (301); `static/` and `api/trend` work; `/` returns 404; a `/ws`
  socket held 200 s, past the proxy's 90 s timeout; the 4th socket from one address was refused with 1013 and logged
  with the viewer's address, not 127.0.0.1; a made-up `X-Forwarded-For` from the client had no effect; port 8080 was
  refused from the lab address; `systemd-analyze security nginx` went from 9.4 to 6.1 with the drop-in. J checked the
  page in a browser. Still untried: the page from another lab PC.

## 2. Open

- D18, for J: yesterday's NE_MEAN from HDF5 needs a value to read. HDF5 holds phase cut to 2048 points, and the IOC
  computes NE_MEAN at full resolution before the cut, so rebuilding it would read the whole ~1.5 GB day and differ
  slightly. Proposed: `interf_store` saves each port's `ne_mean` (already in the group) as an attribute on that shot's
  phase dataset. Until J decides, the history covers only what `interf_web` saw itself.
- D18, for J: does the browser UI replace the C7 Phoebus screens, or sit beside them?
- Closed 2026-11-09: D18's final port. The page is behind nginx at `/diagnostic/interferometer/` (D18, Address).
- Closed 2026-10-18: empty P40 arrays (D9). The main-branch writer has stored empty P40 arrays with
  `rigol_missing=True` since `33ca795` (2026-05-12), so the store's empty arrays match the legacy files; only files
  from before then hold zero-filled placeholders.

## 3. Next steps (in order)

Commit labels and file-level specs are in `HANDOFF_live_epics.md`.

Done: C4r (`4ee26b8`: streamer restored, live link decoupled, shot identity and trigger time), the link socket lock
(`ebb0b9f`), C4s (`465b76a`: the shot counter's file write off the acquisition thread, start-up recovery from the
acquisition log; D14) and C5 (`b8ecf40`: `interf_ioc.py` per D2, D4, D5, D7, the host's `SHOT:ACK` wait, `<name>.db`
at start, the read-only ACF of D8 and the constraints file of D11). C5's first push, `5928659`, was reverted by
`3b3e143` before review. `test-on-Bobby` tested C4s and C5 (119 of 119 tests including PVA; the C5 scenario on 55
recorded shots with an IOC SIGKILL and restart mid-run). C6 (`442c405`: `interf_store` per D9, `<port>:MISSING`,
`interf-store.service` per D10): on Bobby its suite ran 143 of 150 passed, 6 skipped, and 1 failed from a test bug in
`test_interf_store`, fixed after `442c405`.

1. C7, then C8.
