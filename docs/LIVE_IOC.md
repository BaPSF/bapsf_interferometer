# Live analysis IOC and HDF5 store

The IOC analyzes every shot from acquisition's link and serves the results as EPICS PVs; `interf_store` writes them to the daily HDF5 file. Overview in the [README](../README.md); the design decisions (D1–D15) and the PV list (D4) are in [ARCHITECTURE.md](ARCHITECTURE.md).

## IOC

`diag_ioc` runs [interf_ioc.py](../interf_ioc.py), which analyzes every shot from the link with `analyze_shot` and serves the results as EPICS PVs over CA and PVA, under the prefix `LAPD:DIAG:INTERF`.

```bash
pip install -e '.[ioc,clients]' -c constraints.txt   # constraints.txt pins the tested EPICS stack
python -m diag_ioc --config my_interferometer.toml   # a copy of deploy/diag-ioc/interferometer.toml
INTERF_IOC_LINK=unix:/run/diag-ioc/interferometer/link.sock LECROY_IP=<address> python interf_main.py
```

- [deploy/diag-ioc/interferometer.toml](../deploy/diag-ioc/interferometer.toml) is the production config, run by `diag-ioc@interferometer` from `/etc/diag-ioc/` with `readonly.acf` beside it. For a trial run, copy it and change the prefix, `listen` and `db_dir`.
- Every PV of a shot carries the shot time as its timestamp. `SHOT:NUM` is set last, and the PVA group `<prefix>:SHOT` updates once per shot with every value of it. A CA client that falls behind can miss or mix values of a shot; only the group is atomic.
- A port without data (P40 when the Rigol is missing, which is every shot replayed from LeCroy-only `.trc` files) gets empty arrays with INVALID severity. `<port>:MISSING` gives that port's own reason (`""` when it has data, no alarm), and `SHOT:MISSING` lists every reason of the shot.
- Module options: `max_points` (2048; a 1,000,002-sample record gives 1953 points per port, so none are dropped), `ne_window_ms` (default 5 to 10 ms), `plasma_length_m` (0.4) and `ports` (default P20, P29, P40). `<port>:CAL`, `<port>:FREQ` and `CFG:NE_WINDOW_MS` show them; a change needs an IOC restart.
- At every start the IOC writes `<config name>.db`, listing every record, into `[ioc] db_dir`. `[ioc] acf` installs an access file; `readonly.acf` makes every PV read-only over CA and PVA.
- Each publish waits, at most 2 s, until the previous shot's records have all processed (`SHOT:ACK` reaches `STAT:PUBLISHED`), so no update mixes two shots. `STAT:ACK_TIMEOUTS` counts the waits that timed out and should stay 0.
- Each published shot logs one INFO line to the IOC log (stdout; the journal under systemd): `module interferometer timing <YYYYMMDD>-<number> (ms) | decode <ms> | P20 <ms> | P29 <ms> | P40 <ms> | ack wait <ms> | publish <ms>`. `decode` turns the link message into arrays, each port is its own analysis, `ack wait` is the wait above, and `publish` sets the records. A shot whose analysis or publish failed logs none. `STAT:ANALYSIS_S` is unchanged: the whole of analysis, ack wait and publish.

### The `diag_ioc` package

[diag_ioc/](../diag_ioc/) is the diagnostic IOC host (pythonSoftIOC; Linux). `python -m diag_ioc --config FILE.toml` serves each configured module's records over CA and PVA, plus `STAT:*` link and analysis status, the publish ack (`SHOT:ACK`, `STAT:PUBLISHED`, `STAT:ACK_TIMEOUTS`) and the devIocStats health records.
- Modules subclass `module.DiagnosticModule` (`create_records`, which returns its chain's last record, `analyze`, `publish`) and build records with `records.py`.
- `link.py` is the latest-wins shot link from acquisition (`IocLink`) to the IOC (`LinkListener`, which refuses a message announcing more than 1 GiB, about 90× today's 12 MB shot), with its own framing (`framing.py`) and allow-list (`network.py`), so the package never imports `streamer/`.
- `outage.py` logs a persisting failure when it starts, every 5 min, and on recovery.
- Importing the package never loads softioc, so acquisition can use the link. `pip install -e '.[ioc]' -c constraints.txt` installs the IOC side.

## HDF5 store

```bash
python -m interf_store --prefix LAPD:DIAG:INTERF   # or interf-store; --data-dir, default $INTERF_DATA_DIR or ~/data/interferometer
```

[interf_store.py](../interf_store.py) monitors the PVA group `<prefix>:SHOT` (p4p, from the `clients` extra) and writes each shot to `interferometer_data_<YYYY-MM-DD>.hdf5` (the shot's Los Angeles date), in the layout of [HDF5_FORMAT.md](HDF5_FORMAT.md).
- It is its own process, fed only by the group. Acquisition and the IOC never import it, call it or wait on it, and its monitor callback only queues the shot (at most 100).
- It is best effort: a shot published while the store is stopped or disconnected, or dropped on a full queue or a failed write (a file error, a full disk), is not stored and is logged. Gaps in the shot number are logged. The raw archive is meant to be the complete record (D9); live acquisition writes it only with `INTERF_RAW_OUTPUT`.
- A shot's date, number and data all come from the same group update. The dataset name is the shot time: the LeCroy trigger time, or the host time for a shot without one.
- The IOC sends its latest shot to every new connection, so after a restart or reconnect that shot is found already stored and skipped. Before the IOC's first shot after a start, the group holds no shot (date or timestamp 0), and the store skips it.
- SIGTERM or Ctrl-C stores what is queued and logs a summary: shots stored, already stored, not received (gaps), dropped, failed writes and unreadable updates.
- [deploy/systemd/interf-store.service](../deploy/systemd/interf-store.service) runs it as `diag-ioc`, with `INTERF_DATA_DIR=/var/lib/interferometer/hdf5`. systemd creates `/var/lib/interferometer` as 0755, and the files come out 0644, so every local user can read them and only `diag-ioc` can write. Install steps are in its header.
