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
