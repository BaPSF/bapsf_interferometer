# bapsf_interferometer
Interferometer module that includes communication, analysis, saving, and plotting

Main branch contains scripts that run on raspeberry pi only (Linux version; NOT USED since Jul.16.2024)
diagnostic-pc branch contains UP TO DATE script that runs both on PC and RP.

Module works like the following:
- Interferometer raw signal goes into a LeCroy scope
- Scope runs on "save waveform" -> "Wrap", which continuously saves displayed traces on a local drive.
- The folder that contains saved scope traces is shared on DAQ NET.
- Diagnostic PC grabs data from the network folder (see interf_main.py)
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
  - Copies data and attributes to datarun files
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

**time_array/**
- Attributes:
  - `description`: "Time array for interferometer data..."
  - `unit`: "ms"
- Datasets:
  - `[timestamp1]`: Time array
  - `[timestamp2]`: Time array
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

**diagnostics/interferometer/time_array/**
- Contains time array for interferometer data
- Attributes:
  - `description`: "Time array for interferometer data in milliseconds."
  - `unit`: "ms"
- Datasets:
  - `1`: Time array for shot 1
  - `2`: Time array for shot 2
  - `3`: Time array for shot 3
  - ... (numbered by shot)

#### Dataset Naming Convention
- **Datasets within each group are named by shot number** (starting from 1)
- Shot numbers correspond to completed shots in the datarun sequence
- If a shot number doesn't exist as a dataset, it means interferometer data is missing for that shot
- The merge process matches timestamps between datarun shots and interferometer data

#### Example Access Pattern
To access interferometer data for shot number 5 in a datarun file:
- Phase data (port 20): `diagnostics/interferometer/phase_p20/5`
- Phase data (port 29): `diagnostics/interferometer/phase_p29/5`
- Time array: `diagnostics/interferometer/time_array/5`

#### Merge Process
The merge process (`interf_merge_datarun.py`):
1. Extracts timestamps from completed shots in the datarun file
2. Matches these timestamps with interferometer data (within ±1 second tolerance)
3. Copies matching interferometer data into the datarun file structure
4. Preserves all metadata and attributes from the original interferometer files
5. Only copies data when a matching timestamp is found
