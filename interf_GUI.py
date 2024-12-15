# coding: utf-8

'''
This module contains functions for plotting interferometer data in real-time.
The plotted data reads hdf5 files saved using interf_main.py

Author: Jia Han
Ver1.0 created on: 2021-07-13
'''

from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QLabel, QPushButton, QWidget
from PyQt5.QtCore import QThread, pyqtSignal, QObject
from PyQt5.QtGui import QFont

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

import numpy as np
import h5py
import os
import time

from interf_raw import get_calibration_factor


#===============================================================================================================================================
#===============================================================================================================================================

def get_latest_file(dir_path="/mnt/diagnostic-data/interferometer"):
    """
    This function returns the latest file in a directory.

    Args:
        dir_path (str): The path to the directory.

    Returns:
        str: The path to the latest file.
    """
    file_list = os.listdir(dir_path)
    full_path_file_list = [os.path.join(dir_path, file) for file in file_list]
    return max(full_path_file_list, key=os.path.getctime)

def get_data(ifn, i):
    '''
    read the data from the hdf5 file
    '''
    with h5py.File(ifn, 'r', swmr=True) as f:
        dataset_names = list(f['phase_p20'].keys())
        dataset_name = dataset_names[i]
        phaseA = np.array(f['phase_p20'][dataset_name])
        phaseB = np.array(f['phase_p29'][dataset_name])
        t_ms = np.array(f['time_array'][dataset_name])

    return t_ms, phaseA, phaseB, time.ctime(float(dataset_name))

#===============================================================================================================================================
#===============================================================================================================================================

class Worker(QObject):
    '''
    Worker function that emits the data to the plotting GUI
    Runs in a separate thread to avoid blocking the GUI
    '''
    data_updated = pyqtSignal(np.ndarray, np.ndarray, np.ndarray, str)  # Signal to emit the data

    def __init__(self):
        super().__init__()
        self.i = 0 # Counter for testing

    def run(self):
        '''
        Find the latest file and read the last indexed data from it
        '''
        while True:
            try:
                ifn = get_latest_file()
                t_ms, phaseA, phaseB, tstamp = get_data(ifn, -1)
                neA = phaseA * get_calibration_factor(288e9)
                neB = phaseB * get_calibration_factor(282e9)
                self.data_updated.emit(t_ms, neA, neB, tstamp)
                self.i += 1
                QThread.sleep(1)  # Sleep for 1 second
            except OSError:
                print("Unable to open hdf5 file. Retry...")
                QThread.sleep(1)


class MainWindow(QMainWindow):

    def __init__(self):
        super().__init__() # Call the parent class constructor

        #======================== GUI setup ========================
        central_widget = QWidget() # Create a central widget
        self.setCentralWidget(central_widget) # Set the central widget
        self.setGeometry(100,100,1500,1000)

        # Create a layout for the central widget
        layout = QVBoxLayout(central_widget)
        layout.addWidget(QLabel("Interferometer Plot"))
        # Create a button to start the plot
        button = QPushButton("Start Plot")
        button.setFont(QFont("Arial", 24)) 
        layout.addWidget(button)
        button.clicked.connect(self.start_plot)
        # Create a button to save the current trace
        self.keep_trace_button = QPushButton("Save current trace on plot")
        self.keep_trace_button.setFont(QFont("Arial", 24))  
        layout.addWidget(self.keep_trace_button)
        self.keep_trace_button.clicked.connect(self.keep_trace)
        # Create a button to remove the saved trace from keep_trace
        self.remove_trace_button = QPushButton("Remove saved trace from plot")
        self.remove_trace_button.setFont(QFont("Arial", 24))
        layout.addWidget(self.remove_trace_button)
        self.remove_trace_button.clicked.connect(self.remove_trace)

        # Create a figure and a canvas for the figure
        self.fig = Figure(figsize=(15,15))
        plt.rcParams['font.size'] = 24
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)  # Create a canvas for the figure
        # Add the navigation toolbar for interacting with plot
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas)  # Add the canvas to the layout
        # Plot label and title
        self.ax.set_xlabel('Time (ms)')
        self.ax.set_ylabel('ne (m^-3)')
        self.ax.set_title('Density assuming 40cm plasma Dia')
        self.ax.grid(True)
        # Create the plot lines
        self.line_neA, = self.ax.plot([], [], 'r-')
        self.line_neB, = self.ax.plot([], [], 'g-')
        #======================== END GUI setup ========================
    
        # Updating the plot by reading data from hdf5; use thread to avoid blocking the GUI
        self.thread = QThread()  # Thread for running the worker
        self.worker = Worker()  # Worker object
        self.worker.moveToThread(self.thread)  # Move worker to the thread
        self.worker.data_updated.connect(self.update_plot)  # Connect signal
        self.thread.started.connect(self.worker.run)  # Start worker.run when the thread starts

        self.update_count = 0  # Counter for testing

    #======================== GUI functions ========================
    def start_plot(self):
        self.thread.start()  # Start the thread, which starts worker.run

    def update_plot(self, x, y_neA, y_neB, label):
        # Update the plot with new data
        self.line_neA.set_data(x, y_neA)
        self.line_neB.set_data(x, y_neB)
        self.line_neA.set_label('P20  ' + label)
        self.line_neB.set_label('P29  ' + label)
        self.ax.legend(loc='upper right', fontsize=18)
        self.ax.relim()
        self.ax.autoscale_view(True, True, True)

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        self.update_count += 1  # Increment the update counter
        print(f"Plot updated: {self.update_count}")  # Print the update count

    def keep_trace(self):
        # Save the current trace
        x = self.line_neA.get_xdata()
        y_neA = self.line_neA.get_ydata()
        y_neB = self.line_neB.get_ydata()
        label = self.line_neA.get_label()
        self.ax.plot(x, y_neA, 'r--', alpha=0.5, label=label)
        self.ax.plot(x, y_neB, 'g--', alpha=0.5, label=label)
        self.ax.legend()
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    def remove_trace(self):
        # Remove the saved trace from the plot
        for line in self.ax.lines:
            if line.get_linestyle() == '--':
                line.remove()
        self.ax.legend()
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()
    #======================== END GUI functions ========================

if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()