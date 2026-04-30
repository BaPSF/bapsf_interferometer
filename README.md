# bapsf_interferometer
Interferometer module that includes communication, analysis, saving, and plotting

Main branch contains scripts that run on raspeberry pi only (Linux version; NOT USED since Jul.16.2024)
diagnostic-pc branch contains UP TO DATE script that runs both on PC and RP.

Module works like the following:
- Interferometer raw signal goes into a LeCroy scope (ports 20 and 29) and a Rigol DHO scope (port 40)
- LeCroy scope runs on "save waveform" -> "Wrap", which continuously saves displayed traces on a local drive.
- The folder that contains saved scope traces is shared on DAQ NET.
- Rigol DHO scope (192.168.7.63) runs in AUTO trigger mode; traces are read in-memory over telnet on each shot
- Diagnostic PC grabs data from the network folder and from the Rigol scope (see interf_main.py)
- Analyzes the raw signal to find phase shift (see interf_raw.py)
- Stores phase data and time array locally on hdf5 (see interf_file.py)
- Deletes saved traces on the scope so disk don't fill up (see interf_cleanup.py)
- hdf5 data folder is shared on DAQ NET.
- Raspberry Pi sitting inside main lab runs a plotting GUI that displays the traces by reading the hdf5 file (see interf_GUI.py)
- Folder is mounted as a network drive on DAQ PC.
- When data run is over, interferometer data can be merged into datarun hdf5 files (see intef_merge_datarun.py)

## File Structure and Descriptions

### Core Files

- **interf_main.py**
  - Main script that runs on the diagnostic PC
  - Monitors LeCroy scope for new scope traces
  - Reads and analyzes raw interferometer data
  - Saves processed data to HDF5 files

- **interf_raw.py**
  - Contains core analysis functions for interferometer signals
  - Calculates phase shifts from raw interferometer data
  - Two methods for phase calculation:
    - cross-correlation (in use)
    - Hilbert transform (not in use; provides same result but slower)
  - Provides calibration factors for density estimation


- **interf_file.py**
  - Handles all HDF5 file operations
  - Creates and initializes HDF5 file structure
  - Manages data writing and organization
  - Maintains consistent file naming and metadata

- **interf_read.py**
  - Provides functions for reading interferometer data
  - Allows retrieval of data by specific date and time
  - Returns phase data and time arrays from HDF5 files
  - Includes example usage and plotting capabilities

### Support Files

- **interf_GUI.py**
  - Graphical interface for real-time data display
  - Runs on Raspberry Pi in the main lab
  - Continuously updates plots of interferometer data
  - Shows density measurements from both ports

- **interf_plot.py**
  - Contains plotting utilities
  - Manages dynamic plot updates
  - Handles data visualization formatting
  - Provides consistent plotting styles

- **interf_cleanup.py**
  - Manages disk space on the scope
  - Removes processed raw data files
  - Prevents storage overflow
  - Maintains logging of cleanup operations

- **interf_merge_datarun.py**
  - Merges interferometer data into datarun HDF5 files
  - Matches timestamps between datasets
  - Copies data and attributes to datarun files (per-channel attrs onto each subgroup, per-shot attrs onto each dataset)
  - Auto-detects which interferometer channels are present in the source file (works with both legacy two-channel files and new three-channel files that include port 40)
  - Defaults to merging only the first and last shot of a run; pass `all_shots=True` to merge every shot
  - Maintains data organization for experiments

### Utility Files

- **read_scope_data.py**
  - Functions for reading LeCroy scope data files
  - Handles both binary (.trc) and ASCII (.txt) formats
  - Decodes scope headers and data formats
  - Optimized for speed in data acquisition

- **LeCroy_Scope_Header.py**
  - Defines LeCroy scope header structure
  - Decodes binary header information
  - Manages time array generation
  - Handles scope-specific data formats

- **cpu_temp.py**
  - Monitors Raspberry Pi CPU temperature
  - Provides temperature logging
  - Helps prevent overheating issues
  - Simple diagnostic tool

- **read_hdf5.py**
  - Utility functions for reading LAPD datarun HDF5 files via bapsflib
  - Reads probe motion data from the 6K Compumotor control
  - Reads digitizer signal data by board/channel
  - Unpacks the datarun sequence (messages, status, timestamps)

## Data Structure

### Standalone Interferometer HDF5 Files

The standalone interferometer HDF5 files are named `interferometer_data_YYYY-MM-DD.hdf5` and have the following structure:

#### Root Attributes
- `created`: Time structure when file was created
- `description`: "Interferometer data. Datasets in each group are named by timestamp..."

#### Groups and Datasets

**phase_p20/**
- Attributes:
  - `description`: "Phase data for interferometer at port 20..."
  - `unit`: "rad" 
  - `Microwave frequency (Hz)`: 288e9
  - `calibration factor (m^-3/rad)`: [value]
- Datasets:
  - `[timestamp1]`: Phase data array
  - `[timestamp2]`: Phase data array
  - ...

**phase_p29/**
- Attributes:
  - `description`: "Phase data for interferometer at port 29..."
  - `unit`: "rad"
  - `Microwave frequency (Hz)`: 282e9
  - `calibration factor (m^-3/rad)`: [value]
- Datasets:
  - `[timestamp1]`: Phase data array
  - `[timestamp2]`: Phase data array
  - ...

**phase_p40/**
- Attributes:
  - `description`: "Phase data for interferometer at port 40 (Rigol DHO scope)..."
  - `unit`: "rad"
  - `Microwave frequency (Hz)`: 288e9
  - `calibration factor (m^-3/rad)`: [value]
- Datasets:
  - `[timestamp1]`: Phase data array (resampled onto LeCroy time grid; same length as phase_p20)
  - Per-dataset attribute `rigol_missing` (bool): True when the Rigol was unreachable for this shot and the array is a zero-filled placeholder
  - Per-dataset attribute `rigol_missing_reason` (str): human-readable reason string when `rigol_missing` is True (e.g., timeout message, interpolation error)
  - ...

**time_array/**
- Attributes:
  - `description`: "Time array for interferometer data..."
  - `unit`: "ms"
- Datasets:
  - `[timestamp1]`: Time array
  - `[timestamp2]`: Time array
  - ...

**time_array_p40/**
- Attributes:
  - `description`: "Time array for phase_p40 (Rigol). Independent of time_array which is LeCroy."
  - `unit`: "ms"
- Datasets:
  - `[timestamp1]`: Time array (currently identical to time_array since phase_p40 is resampled onto the LeCroy grid)
  - ...

Timestamp is the time when the scope trace was saved on the scope, in particular the last channel (C4) was saved.
Might have some delay between when shot was received and when the scope trace was saved.

### Datarun HDF5 Files After Merge

After interferometer data is merged into datarun HDF5 files using `interf_merge_datarun.py`, the data is organized under the following structure:

#### Main Path Structure
```
diagnostics/interferometer/
```

#### Specific Data Groups

**diagnostics/interferometer/phase_p20/**
- Contains phase data from interferometer at port 20
- Attributes:
  - `description`: "Phase data for interferometer at port 20. Attribute calibration factor assumes 40cm plasma length."
  - `unit`: "rad"
  - `Microwave frequency (Hz)`: 288e9
  - `calibration factor (m^-3/rad)`: [calculated value]
- Datasets:
  - `1`: Phase data array for shot 1
  - `2`: Phase data array for shot 2
  - `3`: Phase data array for shot 3
  - ... (numbered by shot)

**diagnostics/interferometer/phase_p29/**
- Contains phase data from interferometer at port 29
- Attributes:
  - `description`: "Phase data for interferometer at port 29. Attribute calibration factor assumes 40cm plasma length."
  - `unit`: "rad"
  - `Microwave frequency (Hz)`: 282e9
  - `calibration factor (m^-3/rad)`: [calculated value]
- Datasets:
  - `1`: Phase data array for shot 1
  - `2`: Phase data array for shot 2
  - `3`: Phase data array for shot 3
  - ... (numbered by shot)

**diagnostics/interferometer/phase_p40/** *(present only when merging from new-format interferometer files)*
- Contains phase data from interferometer at port 40 (Rigol DHO scope)
- Attributes:
  - `description`: "Phase data for interferometer at port 40 (Rigol DHO scope). Attribute calibration factor assumes 40cm plasma length."
  - `unit`: "rad"
  - `Microwave frequency (Hz)`: 288e9
  - `calibration factor (m^-3/rad)`: [calculated value]
- Datasets:
  - `1`, `2`, ... (numbered by shot)
  - Per-dataset attribute `rigol_missing` (bool): True when the Rigol was unreachable for this shot and the array is a zero-filled placeholder
  - Per-dataset attribute `rigol_missing_reason` (str): human-readable reason string when `rigol_missing` is True

**diagnostics/interferometer/time_array/**
- Contains time array for the LeCroy-acquired channels (phase_p20, phase_p29)
- Attributes:
  - `description`: "Time array for interferometer data in milliseconds."
  - `unit`: "ms"
- Datasets:
  - `1`: Time array for shot 1
  - `2`: Time array for shot 2
  - `3`: Time array for shot 3
  - ... (numbered by shot)

**diagnostics/interferometer/time_array_p40/** *(present only when merging from new-format interferometer files)*
- Time array for `phase_p40` (Rigol). Currently identical to `time_array` because phase_p40 is resampled onto the LeCroy grid before saving, but kept as a separate group so the LeCroy and Rigol time bases can diverge in the future without breaking older datarun files.
- Attributes:
  - `description`: "Time array for phase_p40 (Rigol). Independent of time_array which is LeCroy."
  - `unit`: "ms"

#### Backward compatibility
Old interferometer source files (pre–port 40 era) only contain `phase_p20`, `phase_p29`, and `time_array`. `interf_merge_datarun.py` detects which subgroups exist in the source file and only creates/populates those, so merging an old file produces the same three-group layout as before — no `phase_p40` / `time_array_p40` groups are added.

#### Dataset Naming Convention
- **Datasets within each group are named by shot number** (starting from 1)
- Shot numbers correspond to completed shots in the datarun sequence
- If a shot number doesn't exist as a dataset, it means interferometer data is missing for that shot
- The merge process matches timestamps between datarun shots and interferometer data
- For new-format files, an individual shot may have `phase_p20`, `phase_p29`, and `time_array` written but no `phase_p40` / `time_array_p40` entry — this happens when the Rigol was completely unavailable for that shot

#### Example Access Pattern
To access interferometer data for shot number 5 in a datarun file:
- Phase data (port 20): `diagnostics/interferometer/phase_p20/5`
- Phase data (port 29): `diagnostics/interferometer/phase_p29/5`
- Phase data (port 40, if present): `diagnostics/interferometer/phase_p40/5`
- Time array (LeCroy): `diagnostics/interferometer/time_array/5`
- Time array (Rigol, if present): `diagnostics/interferometer/time_array_p40/5`

#### Merge Process
The merge process (`interf_merge_datarun.py`):
1. Extracts timestamps from completed shots in the datarun file
2. Matches these timestamps with interferometer data (within ±1 second tolerance)
3. Copies matching interferometer data into the datarun file structure
4. Preserves all metadata and attributes from the original interferometer files (per-channel group attrs are written onto each `diagnostics/interferometer/<name>` subgroup; per-shot dataset attrs such as `rigol_missing` are preserved on each shot dataset)
5. Only copies data when a matching timestamp is found
6. **Default: merges only the first and last completed shot.** This is a fast sanity check that keeps the merged datarun file small. To merge every shot, call `merge_interferometer_data(datarun_path, interf_path, all_shots=True)`.
7. The datarun HDF5 file is opened and closed once per shot so that, if the merge is interrupted, all already-merged shots remain safely flushed to disk.

## Update 04/21/2026

- Added a third interferometer at **port 40** (288 GHz, 40 cm plasma path) acquired with a Rigol DHO scope at `192.168.7.63` (CH1 = ref, CH2 = plasma).
- Per shot, `interf_main.py` now: detects the LeCroy `.trc` file → sends `:STOP` to the Rigol → reads both Rigol channels in-memory over telnet → sends `:RUN` → reads all four LeCroy channels via the multiprocessing pool → runs all three `phase_from_raw` analyses in parallel via the same pool.
- HDF5 schema gained two groups: `phase_p40` and `time_array_p40`. The Rigol phase is resampled onto the LeCroy time grid via `np.interp` after bounds/sanity validation, so all three phase traces and both time arrays share the same length per shot.
- If the Rigol is unreachable or errors mid-run, the LeCroy pipeline keeps writing; `phase_p40` is saved as a zero-filled placeholder with per-dataset attributes `rigol_missing = True` and `rigol_missing_reason` (string). Reconnect is retried every 100 shots.
- `interf_GUI.py` and `interf_plot.py` now display a third trace (P40) alongside P20 and P29; older HDF5 files without the `phase_p40` group still load correctly.
- Added `_worker_init()` as the multiprocessing pool initializer to suppress `SIGINT` in worker processes. On Windows, Ctrl-C is broadcast to all console processes; without this, workers raised `KeyboardInterrupt` mid-task and stalled `pool.join()`. The main process retains full Ctrl-C handling.
