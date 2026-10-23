# Offline simulation

[interf_sim/](../interf_sim/) runs `interf_main` and `interf_raw` on recorded LeCroy `.trc` files, and contacts no scope. It replaces only the two lab_scopes driver classes that `interf_raw` imports, so a change to `interf_main` or `interf_raw` takes effect in the simulation without editing it. Overview in the [README](../README.md).

```bash
python -m interf_sim --limit 20    # from the repo root; --help lists the options
python -m interf_sim --limit 20 --repeat-traces  # cycle available traces into 20 synthetic shots
python -m interf_sim --raw-output raw.bp --limit 20  # write complete raw shots to BP5
python -m interf_sim --ioc-link unix:/tmp/interf.sock --limit 20  # also send shots to a live-analysis listener
python -m interf_sim --loop --ioc-link unix:/run/diag-ioc/interferometer/link.sock  # until Ctrl-C or SIGTERM; as diag-ioc, which owns the socket
```

`python -m interf_sim.listen ADDRESS [--analyze] [--ne-window-ms START STOP]` is a development listener for `--ioc-link` and `INTERF_IOC_LINK`. It prints one line per shot received, and with `--analyze` adds each port's points and density, or the reason the port is missing. Use `tcp:127.0.0.1:PORT` where unix sockets are unavailable.

## Behavior

- The `.trc` directory is `TRC_DIR` in [interf_sim/trc_replay.py](../interf_sim/trc_replay.py) (`/home/adios/shared/Software/LAPD/data`; the lab PC's `D:/data/raw data` is commented out above it). Elsewhere, edit that line or pass `--trc-dir`.
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
- `--raw-output` enables the integrated output package. It accepts a direct ADIOS output, an encrypted socket connection file, or a remote server configuration. See [streamer/README.md](../streamer/README.md) for the payload and consumer commands.

## Synthetic shots

Without recorded shots, generate synthetic ones with a known phase (a Gaussian bump of about 6 rad), then replay them with `--trc-dir`:

```bash
python -m interf_sim.synthetic /tmp/synth --shots 20   # --help lists samples, dt, IF, period, noise
python -m interf_sim.synthetic /tmp/dead --shots 5 --flat C2   # C2 dead: P20 is reported missing ("C2 flat (no signal)")
python -m interf_sim --trc-dir /tmp/synth --period 0
```

`analyze_shot` reports a port missing, with its reason, when a channel is absent, flat (every code identical, e.g. a dead input), or too short for the 5 leading windows the phase offset needs. A disconnected input that still delivers noise is not detected yet; that needs a signal-to-noise check calibrated on real scope data.

## Using replayed shots in code

Downstream code can take the same `RawShot` objects that `acquire_shot` returns:

```python
from interf_sim.trc_replay import ReplayLeCroy, iter_shots, trc_shots

for shot in iter_shots(ReplayLeCroy(trc_shots()[:10])):
    samples, wavedesc = shot.lecroy["C1"]
```

`FakeLeCroyScope` subclasses the real `LeCroyScope`, so decoding and validation are the driver's own. It raises `SimFault`, which `except Exception` does not catch, in two cases:
- `interf_raw` calls a driver method the fake does not override and that method reaches the scope. Override the method in `FakeLeCroyScope`, keeping the lab_scopes signature.
- A capture is read twice (same-shot rule 3).
