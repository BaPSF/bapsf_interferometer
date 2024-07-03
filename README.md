# bapsf_interferometer

Includes scripts that run on raspeberry pi connected to DAQ Net right now.
On a shot by shot basis, the script reads interferometer raw data, computes the phase, plots it on the screen, and saves computed phase data into an hdf5 file.

Also includes script that copies interferometer data over into datarun hdf5 files via matching timestamps
