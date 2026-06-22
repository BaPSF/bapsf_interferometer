# Bench-test checklist — `use-lab-scopes-lecroy`

This branch sources **both** scope drivers from the `lab-scopes` package:
- LeCroy `.trc` readers: `lab_scopes.io.lecroy_files`
- Rigol DHO800 driver: `lab_scopes.rigol` (12-bit/WORD, pinned to `v0.4.0a1`)

The local `read_scope_data.py` / `LeCroy_Scope_Header.py` copies were removed,
so the program **will not start** unless `lab_scopes` is importable.

## 1. Get the branch on the bench PC

```bash
cd <path>/bapsf_interferometer
git fetch origin
git checkout use-lab-scopes-lecroy
```

## 2. Make sure lab_scopes provides v0.4.0a1

Two valid setups — pick one:

- **Editable clone (current PC setup):** the `lab_scopes` clone must be checked
  out at the pre-release tag.
  ```bash
  cd <path>/lab_scopes
  git fetch origin
  git checkout v0.4.0a1        # detached HEAD at the tagged pre-release
  ```
  The existing `pip install -e` keeps serving code from this clone, so checking
  out the tag is enough — no reinstall needed.

- **Fresh pinned install (matches pyproject):**
  ```bash
  cd <path>/bapsf_interferometer
  pip install --pre .          # pulls lab-scopes @ v0.4.0a1 from GitHub
  ```
  Note: this replaces an editable lab_scopes install with a fixed checkout.

## 3. Pre-flight import check (no hardware needed)

```bash
python -c "from lab_scopes.io.lecroy_files import read_trc_data_simplified, read_trc_data_no_header; from lab_scopes.rigol import RigolDHO800; import inspect; print('imports OK;', inspect.signature(RigolDHO800.read_channel))"
```
Expect: `imports OK; (self, channel, fmt='WORD', verify_window=False)`
(`fmt='WORD'` confirms the 12-bit acquisition path.)

## 4. Verify paths in interf_main.py before running

These are PC-specific and not part of this change — confirm they match the bench rig:
- `scope_path = r"I:\\"`            — LeCroy SMB share
- `hdf5_path  = r"C:\data\interferometer"`
- `log_path   = r"C:\data\log"`
- `RIGOL_IP   = "192.168.7.63"`, channels `C1`=ref / `C2`=plasma
- Rigol memory depth **<= 1M** (deeper records exceed the 2.5 s `RIGOL_OPERATION_TIMEOUT` and get dropped per shot)

## 5. Run

```bash
python interf_main.py
```

Watch for, per shot:
- `Shot <n>` lines advancing
- `Saved interferometer shot at ...`
- If the Rigol is unreachable: `Rigol unavailable` / `Rigol error (...)` and
  `phase_p40` written as a zero-length placeholder with `rigol_missing=True`.
  The LeCroy pipeline (p20/p29) must keep writing regardless.

## 6. Spot-check the output HDF5

`C:\data\interferometer\interferometer_data_YYYY-MM-DD.hdf5` should contain
`phase_p20`, `phase_p29`, `phase_p40`, `time_array`, `time_array_p40`.
On a healthy Rigol shot, `phase_p40[<ts>].attrs['rigol_missing'] == False` and
`time_array_p40` is non-empty (its own length, independent of `time_array`).
