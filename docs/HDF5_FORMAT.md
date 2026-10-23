# Daily HDF5 file format

The daily interferometer file, written by the previous acquisition (on Windows, from LeCroy `.trc` files) and now by `interf_store` ([LIVE_IOC.md](LIVE_IOC.md)). The schema and writers are in [interf_file.py](../interf_file.py). Overview in the [README](../README.md).

## Layout (previous acquisition)

Each daily file is named `interferometer_data_YYYY-MM-DD.hdf5`. Its groups each contain one dataset per shot, named by its timestamp in seconds since the Unix epoch:

| Group | Contents | Unit |
|---|---|---|
| `phase_p20/`, `phase_p29/`, `phase_p40/` | Phase per port. The group attributes give the microwave frequency and the calibration factor, which assumes a 40 cm plasma path | rad |
| `time_array/` | LeCroy time base | ms |
| `time_array_p40/` | Rigol time base, separate from `time_array` | ms |

- `phase_p40` datasets have per-shot attributes `rigol_missing` and `rigol_missing_reason`. When the Rigol was down, the previous acquisition wrote empty arrays since `33ca795` (2026-05-12), and a zero-filled placeholder before.
- Files from before port 40 was added have only `phase_p20`, `phase_p29`, and `time_array`.
- Timestamps mark when the LeCroy saved its C4 file, which can be slightly later than the shot.

## Files written by `interf_store`

- Dataset names are the LeCroy trigger time, or the host time for a shot without one.
- A missing P20 or P29 is stored as NaN, as long as `time_array`, since legacy readers plot both phases against that one axis. Its dataset carries `missing` (true) and `missing_reason`. With both missing, all three are empty. A missing P40 has empty `phase_p40` and `time_array_p40` and carries `rigol_missing` and `rigol_missing_reason`, as before. No missing port is ever written as zeros, and each reason is that port's own `<port>:MISSING`. `interf_plot.py` from `main` fails on a NaN shot (its `np.min` axis limits); the Phoebus screens (C7) replace it.
- `time_array` holds port 20's time axis, or port 29's when port 20 has none.
- Each `phase_p20` dataset carries `shot_date`, `shot_number` and `time_source`.
