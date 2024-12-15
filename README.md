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

The HDF5 files are named `interferometer_data_YYYY-MM-DD.hdf5` and have the following structure:

### Root Attributes
- `created`: Time structure when file was created
- `description`: "Interferometer data. Datasets in each group are named by timestamp..."

### Groups and Datasets

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
