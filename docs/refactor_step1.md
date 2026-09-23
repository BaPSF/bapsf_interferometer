# Refactor step 1: direct raw acquisition from both scopes

Branch: `refactor/linux-epics-daq`. Overall goal (later steps): run on Linux, publish to EPICS,
grab LeCroy data over Ethernet instead of `.trc` files, hand raw digitizer data to a downstream
workflow.

## Scope

Per shot, acquire raw samples + headers from the LeCroy (ports 20, 29) and the Rigol DHO804
(port 40), guaranteed to be the same shot. **The step ends when `interf_raw.acquire_shot()`
returns them.**

Out of scope for this step: writing raw data to disk, analysis in the loop, EPICS, and any
parallelism (no threads, no process pool). Parallelism is added only after this step passes a
bench test, because Linux and Windows differ in multiprocessing behavior. Threads are not used in
the Linux version at all; the next concurrency step is multiprocessing. Linux-only from here on:
the Rigol deadline uses `signal.setitimer`, which Windows lacks.

## Operating conditions

- LAPD fires about one shot every 3 s. Experiments pause for long, unannounced periods with no
  trigger.
- LeCroy trigger-out is hard-wired to the Rigol trigger input, so the Rigol triggers only when
  the LeCroy does. Software assumes this wiring and does not verify it. Per LeCroy trigger mode:
  SINGLE emits exactly one trigger-out per arm, STOP emits none, NORM/AUTO emit one every shot.
- Requirements: both scopes' data belong to the same shot; a shot missed for any reason is
  skipped, never back-filled; a scope that yields no data is logged missing and the program
  continues; a pause is waited out, retrying every `TRIGGER_TIMEOUT`.

## Same-shot rules

The Rigol keeps the method from `main`: it free-runs in AUTO sweep, is never armed by software,
and triggers on every LeCroy trigger-out through the hard-wired input. Per shot it is stopped,
read, and resumed. The LeCroy has a sweep counter that separates a fresh capture from a stale
STOP. The guarantee rests on three rules:

1. **The Rigol `:STOP` precedes the LeCroy re-arm.** A captured SINGLE LeCroy emits no further
   trigger-out until it is re-armed, so the Rigol receives no newer triggered shot before it is
   stopped.
2. **A free-running LeCroy (NORM/AUTO) is switched to SINGLE**, because free-running it emits a
   trigger-out every shot, which would break rule 1.
3. **A LeCroy capture is read at most once.** `lecroy_armed` is cleared when a capture is
   consumed and set only by a successful arm, so a failed re-arm leads to re-arming, never to
   re-reading the same capture.

**Caveat (pending bench test):** in AUTO sweep the Rigol forces an untriggered acquisition when no
trigger arrives within its auto interval. One landing between the LeCroy edge and our `:STOP`
would replace the shot, and software cannot detect that. `main` had the same exposure with a
longer delay (seconds, waiting for the `.trc` file). AUTO is kept until the bench test decides.

## File changes

| File | Action |
|---|---|
| `interf_analysis.py` | **New.** Receives the entire current `interf_raw.py` verbatim (`get_calibration_factor`, `phase_from_raw`, `phase_from_hilbert`, helpers, smoke test). Justification: `interf_raw` becomes the scope-acquisition module; GUI, `interf_read`, and merge scripts need calibration/phase code without importing scope drivers. |
| `interf_raw.py` | **Rewritten** as raw acquisition (design below). |
| `interf_main.py` | **Rewritten** as a minimal loop: call `acquire_shot()` forever, log one line per shot (below), discard the data. Ctrl-C exits cleanly. |
| `interf_file.py`, `interf_GUI.py`, `interf_read.py`, `interf_merge_datarun.py` | Import line only: `from interf_raw import get_calibration_factor` → `from interf_analysis import ...`. |
| `interf_plot.py`, `interf_cleanup.py`, `interf_save.py` | **Deleted.** Plotting will be replaced by EPICS-side plotting; cleanup only deleted `.trc` files on `I:\`; `interf_save.py` is an unused `.trc` script. |
| `README.md` | Rewrite entirely based on new refactored code. |

Deferred: raw writer (LAPD_DAQ spool layout, but per-scope dtype preserved -- `spool_format`
casts to int16, which would corrupt Rigol uint16 codes > 32767), removal of the dead file-share
functions in `interf_file.py`, parallelism, EPICS, analysis in the loop.

## `interf_raw.py` design

Adapted from LAPD_DAQ `acquisition/scope_runner.py` (`MultiScopeAcquisition` arm/wait/read),
sequential only. Requires `lab_scopes` v0.4.0 (already pinned; contains `arm_master_single` and
`wait_for_stop_then_complete`). Real-time (single-segment) LeCroy records only, as today.

**Constants** (env-var overridable). Kept from the old `interf_main.py`: `RIGOL_IP`
(`192.168.7.63`), `RIGOL_REF_CH`/`RIGOL_PLA_CH` (`C1`/`C2`), `RIGOL_RETRY_INTERVAL` (shots),
`RIGOL_CONNECT_TIMEOUT`, `RIGOL_OPERATION_TIMEOUT` (now enforced by the
signal deadline below instead of a thread). New: `LECROY_IP` (placeholder `10.10.10.10` until
the real address is known), `LECROY_CHANNELS = ("C1","C2","C3","C4")`, `LECROY_TIMEOUT`,
`TRIGGER_TIMEOUT` (~10 s, > 3 shot periods), `LECROY_RETRY_INTERVAL`. Removed: `CATCHUP_*`;
`RIGOL_SOCKET_TIMEOUT` (the Rigol transport selects
before every recv, so the socket timeout only ever bounded the TCP connect, which
`RIGOL_CONNECT_TIMEOUT` now sets). Initial values are tuned at the bench.

**Return value:** `RawShot` when a LeCroy capture was consumed, `None` otherwise (no trigger,
LeCroy error, or stop request). `RawShot` holds host time, LeCroy
`{ch: (int16 samples, 346-byte WAVEDESC)}`, Rigol `{ch: (uint16 WORD codes, calibration
metadata dict)}` (the Rigol has no header block), and `missing: {scope: reason}`.

**State kept between iterations:** `lecroy_armed` and the Rigol backoff counter. The flag starts
False, so a restart behaves like the rows below.

### Per-iteration flow (`acquire_shot()`)

LeCroy: one connection per iteration, closed in `finally`. Rigol: each Rigol operation
(`rigol_read`, `rigol_run`) is a self-contained top-level function that connects, acts, and
closes under one deadline (constraints below); it is skipped while the Rigol is in backoff, and
any failure starts the backoff. After any error on a scope, that scope is not used again in the
iteration; closing its socket also discards a late reply to a timed-out query.

1. **Connect** the LeCroy with `discover_traces=LECROY_CHANNELS`; on failure, log, sleep
   `LECROY_RETRY_INTERVAL`, return `None` (no master, no shot).
2. **Prepare** the LeCroy, by `TRIG_MODE?` (the Rigol is left free-running):

   | LeCroy mode | `lecroy_armed` | Action |
   |---|---|---|
   | SINGLE (waiting) | any | Leave it; set `lecroy_armed`. |
   | STOP, sweep counter ≥ 1 | True | An unconsumed capture is waiting (possibly landed after the previous wait ended): go to step 3, which returns at once. |
   | STOP, otherwise | any | Arm (`arm_master_single`). Startup, failed re-arms, and a manual STOP land here; any held capture is discarded (rule 3). |
   | NORM / AUTO | any | Arm (rule 2). |

3. **Wait** with `wait_for_stop_then_complete(LECROY_CHANNELS[0], ...)` in 0.5 s slices up to
   `TRIGGER_TIMEOUT`, checking `stop_requested()` between slices (see Shutdown). On timeout: log
   (throttled) and return `None` with no state change; the next iteration's step 2 re-classifies
   the LeCroy, so a capture landing after the last slice is consumed then and a manual STOP or
   NORM is re-armed. The caller loops at once: this is the retry during pauses.
4. **Consume**, as soon as the capture is detected and before any read: clear `lecroy_armed`, so
   an exception anywhere later can never lead to a re-read (rule 3).
5. **Read LeCroy** channels one after another. A channel that fails after a complete reply (no
   data, e.g. trace switched off; unknown trace; bad header) is skipped and the rest are still
   read, so one port's channel cannot cost the other port. A link error (connection, timeout,
   protocol, `OSError`) stops the reads: the stream may hold a late reply. Failed channels are
   listed in `missing["lecroy"]` alongside the channels that were read.
6. **Rigol** (`rigol_read`, the sequence of `main`'s `read_stopped_rigol`): `:STOP`, read
   `RIGOL_REF_CH`, `RIGOL_PLA_CH` one after another (the driver waits for STOP first), `:RUN` in
   `finally` so it free-runs again even after a failed read. A failed `:RUN` after a good read is
   logged and the data kept.
7. **Re-arm the LeCroy** (`arm_master_single(LECROY_CHANNELS[0])` → `lecroy_armed`), after
   step 6 (rule 1), unless step 5 hit a link error. A failure leaves the flag clear, and the next
   iteration's prepare step handles it.
8. **Close** the LeCroy (`finally`); return `RawShot`.

Steps 4-7 are the critical path from capture to re-arm. Steps 1-3 run while the LeCroy is armed,
so reconnecting costs no shots as long as it finishes before the next trigger. `critical_path_s`
starts at detection, so it excludes a capture's wait through a reconnect (~10 round trips) and
understates trigger-to-re-arm for such shots.

### Failure handling

| Event | Result | Logged |
|---|---|---|
| No trigger within `TRIGGER_TIMEOUT` | Nothing recorded; retry | First timeout, then every 5 min while paused; "resumed" at the next capture |
| LeCroy unreachable, or errors in steps 2-3 | No shot (master); connection closed, retry after `LECROY_RETRY_INTERVAL` | Throttled like above |
| Next shot arrives during steps 4-7 | LeCroy is STOP and emits no trigger-out, so both scopes skip it | Shows as Δt ≈ 6 s between LeCroy trigger times |
| LeCroy channel fails | Other channels kept (all later channels lost after a link error); Rigol still read | Per shot, failed channels with reasons |
| Bug or (later) write failure while handling a shot | Logged with traceback; loop continues, so exit still restores the scopes | Per event |
| Rigol unreachable, deadline, or read fails | Rigol missing; LeCroy kept; the Rigol is skipped for `RIGOL_RETRY_INTERVAL` shots | Per shot, with reason |
| Re-arm fails | Next iteration re-arms; the held capture is never re-read | Per event |

### Logging (`interf_main.py`)

One line per `RawShot`: host time, LeCroy trigger time (from WAVEDESC) and Δt since the previous
capture, points per channel, missing scopes with reasons, critical-path time. Python `logging` to
stdout and `LOG_DIR/interf_acquire.log`.

### Shutdown (Ctrl-C / SIGTERM)

Goal: a shot whose capture was already detected is read completely and handed to the caller
before exit; connections are always closed; neither scope is left stopped.

- `interf_main.py` installs a SIGINT/SIGTERM handler (SIGTERM is what systemd sends) that only
  sets a stop flag and restores Python's default SIGINT handler, so a second Ctrl-C raises
  `KeyboardInterrupt` (forced exit). A handler that does not raise never interrupts a socket call
  (PEP 475 retries it), so an in-progress transfer is not cut. `acquire_shot()` receives the flag
  as a `stop_requested()` callable.
- Step 3 waits in 0.5 s slices of `wait_for_stop_then_complete` up to `TRIGGER_TIMEOUT`, checking
  the flag between slices, so a stop during a pause takes effect within 0.5 s. The
  `LECROY_RETRY_INTERVAL` sleep after a LeCroy error is sliced the same way.
- Stop requested before a capture is detected: return `None`.
- Stop requested after a capture is detected: steps 4-6 run to completion (bounded by
  `LECROY_TIMEOUT` and the Rigol deadlines); step 7 is skipped; the `RawShot` is returned and the
  loop handles it exactly like every other shot before exiting. In step 1 that handling is the
  log line; in the step that adds the raw writer, the write completes before exit by the same
  rule.
- Restore: after the loop exits, `interf_main.py` calls `release_scopes()`, which opens its own
  LeCroy connection, sets `TRIG_MODE NORM`, closes it, then calls `rigol_run()` (connect, `:RUN`,
  close, under `RIGOL_OPERATION_TIMEOUT`). The Rigol is normally already running; this covers a
  failed read. It runs outside `acquire_shot()` so a stop requested after step 7 has already
  re-armed the LeCroy is still restored. Next startup lands in the NORM row.
- `finally` closes every connection on every path, including a forced second Ctrl-C. A forced
  exit skips the restore and logs that the trigger modes were not restored.
- LeCroy Auto-Save is not used in operation, so NORM on exit writes no files.

### Rigol deadline (`SIGALRM`, no thread)

Problem: the `lab_scopes` Rigol driver waits up to 15 s per text query (including `*IDN?` on
connect) and ≥ 15 s per waveform chunk, and these are not configurable. A Rigol that stops
answering would hold the whole loop, losing LeCroy shots.

Each Rigol block runs under a hard wall-clock deadline from `signal.setitimer(ITIMER_REAL, t)`.
The `SIGALRM` handler raises `RigolDeadline`, which interrupts the blocking `select`/`recv` in the
main thread (PEP 475: a raising handler stops the syscall retry). The timer is cleared in
`finally`, so it can never fire later in LeCroy code.

| Rigol function | Covers | Budget |
|---|---|---|
| `rigol_read` (step 6) | connect, `:STOP`, both channel reads, `:RUN`, close | `RIGOL_OPERATION_TIMEOUT` (2.5 s, sized for two 1M WORD reads, as on `main`) |
| `rigol_run` (exit) | connect, `:RUN`, close | `RIGOL_OPERATION_TIMEOUT` |

`RIGOL_CONNECT_TIMEOUT` remains the TCP connect timeout inside each function. Reconnecting per
function adds a TCP connect plus `*IDN?` (milliseconds on the local network) to the critical
path; measure it with the rest.

- `RigolDeadline` subclasses `BaseException`, not `Exception`: `rigol_functions.command` has an
  `except Exception:` retry loop (line 89) that would swallow an `Exception` subclass and keep
  waiting.
- On expiry: close the Rigol connection (the telnet stream is mid-reply and unusable), log the
  Rigol missing with "deadline", start the backoff. The `:RUN` in `finally` is attempted on the
  broken stream and may not take effect, so the Rigol can stay stopped until its next successful
  read (as on `main`); `release_scopes()` retries `:RUN` on exit.
- The same bound works unchanged inside a worker process (itimers and handlers are per-process;
  Linux does not inherit itimers across fork).

### Multiprocessing-ready constraints (binding for step 1 code)

The next step moves the Rigol into a dedicated worker process (not a `Pool`: a pool cannot kill
one stuck task); the parent bounds it with `Connection.poll(timeout)` and terminates it on
overrun, with the in-worker deadline as the first line. Step 1 code must make that a relocation,
not a rewrite:

1. **One deadline helper.** A single context manager in `interf_raw.py` owns `setitimer`,
   `SIGALRM`, and `RigolDeadline`, and wraps each Rigol function (`rigol_read`, `rigol_run`). The only other signal code is the SIGINT/SIGTERM stop-flag handler in
   `interf_main.py` (main process only).
2. **Picklable, top-level Rigol functions.** Rigol connect/stop/read/run logic lives in
   module-level functions taking plain arguments (IP, channel names, timeouts) and returning
   picklable values (numpy arrays, dicts, str, bool). No closures, lambdas, bound methods, or
   scope objects crossing the function boundary. Required because Python 3.14 defaults Linux to
   the `forkserver` start method, which pickles the target and its arguments.
3. **Sockets opened where they are used.** `rigol_read` and `rigol_run` each open and close their
   own Rigol connection, so each is a self-contained unit a worker can run. The LeCroy
   connection is opened and closed within one `acquire_shot()` call. No connection object is
   stored module-wide or passed to a Rigol function.
4. **Scope state is plain data.** `lecroy_armed` and the backoff counter live in a
   small dataclass of bools/ints owned by the caller, so the parent can keep them while a worker
   does the I/O.

Ctrl-C handling stays in the main process only; future workers ignore `SIGINT` (the old
`_worker_init` pattern).

## Timing

The critical path must fit in ~3 s. Rigol at 1M WORD is ~0.77 s per channel, ~1.5 s for two;
the LeCroy's four channels depend on its record length (measure). If the path exceeds 3 s, every
other shot is skipped (safe, still same-shot); parallel reads are the planned fix.

## Verification

Static checks only here (import/syntax, `grep` that nothing imports analysis from `interf_raw`).
Bench test by the user; expected:
- steady state: both scopes in every shot, Δt ≈ 3 s;
- pause: throttled waiting lines, capture resumes on its own;
- Rigol unplugged/replugged: Rigol logged missing with a reason, LeCroy continues, Rigol returns
  after the backoff;
- startup with the LeCroy in SINGLE, STOP, and NORM all reach steady state;
- LeCroy never observed sitting in STOP between shots; Rigol free-running between shots.

## Bench checks

- Reconnecting (LeCroy connect-time queries, Rigol `*IDN?`) does not disturb an armed scope.
- Rigol AUTO sweep holds the shot: port-40 phase shows the plasma on the same shots as ports 20
  and 29 (see the caveat under Same-shot rules). Decides whether AUTO stays.
- LeCroy Auto-Save is off (not used in operation).
- Ctrl-C while waiting exits within ~0.5 s; Ctrl-C during a read still logs that shot, then
  exits; after either, LeCroy reads NORM and Rigol is running; a second Ctrl-C exits at once.
- Measure the critical path per shot.
- Set `LECROY_IP`; `10.10.10.10` is a placeholder.
- Rigol deadline: pull the Rigol network cable mid-read; the loop must log "deadline" within
  `RIGOL_OPERATION_TIMEOUT` and the LeCroy keeps capturing.
