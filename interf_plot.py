# coding: utf-8

'''
Module used for showing plots on the screen

Author: Jia Han
Ver1.0 created on: 2021-06-01
'''

import tkinter as tk
from tkinter import ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.animation as animation

import os
import time
import threading
import h5py

from interf_raw import get_calibration_factor
#===============================================================================================================================================
#===============================================================================================================================================

def init_plot():
    """
    This function creates a new matplotlib figure and axes, and initializes line objects for the plot.
    """
    plt.rcParams.update({'font.size': 26})
    plt.ion()  # Enable interactive mode
    fig, ax = plt.subplots(figsize=(14, 10))

    # Initialize line objects
    line_neA, = ax.plot([], [], 'r-', label='ne P20')
    line_neB, = ax.plot([], [], 'b-', label='ne P29')

    # Set plot titles and labels
    ax.set_xlabel('Time (ms)')
    ax.set_ylabel('ne (m^-3)')
    ax.set_title('Density assuming 40cm plasma Dia')
    ax.legend()

    return fig, ax, line_neA, line_neB

def get_latest_file(dir_path):
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

def get_data(ifn):
    with h5py.File(ifn, 'r', swmr=True) as f:
        dataset_names = list(f['phase_p20'].keys())
        last_dataset_name = dataset_names[-1]
        phaseA = f['phase_p20'][last_dataset_name]
        phaseB = f['phase_p29'][last_dataset_name]
        t_ms = f['time_array'][last_dataset_name]
    return t_ms, phaseA, phaseB

def update_plot(ax, lineA, lineB, data_path):
    """
    This function updates the data for the line objects in the plot and adjusts the plot limits dynamically.

    Args:
        ax (matplotlib.axes.Axes): The axes object of the plot.
        lineA (matplotlib.lines.Line2D): The line object for neA.
        lineB (matplotlib.lines.Line2D): The line object for neB.
        t_ms (list): The time values in milliseconds.
        neA (list): The neA values.
        neB (list): The neB values.

    Returns:
        None
    """
    t_ms, phaseA, phaseB = get_data(get_latest_file(data_path))

    # Update data for plotting
    neA = phaseA * get_calibration_factor(288e9)
    neB = phaseB * get_calibration_factor(282e9)
    lineA.set_data(t_ms, neA)
    lineB.set_data(t_ms, neB)
    ax.figure.canvas.draw()

    # Adjust plot limits dynamically
    if len(t_ms) > 0:
        ax.set_xlim(min(t_ms), max(t_ms))
    if len(neA) > 0 and len(neB) > 0:
        ax.set_ylim(min(min(neA), min(neB)), max(max(neA), max(neB)))


def background_update_task(ax, line_neA, line_neB):
    while True:
        # Call update on the main thread
        root.after(100, update_plot, ax, line_neA, line_neB)
        time.sleep(1)  # Adjust the sleep time as needed

def end_plot():
    plt.ioff() # Turn off interactive mode
    plt.show() # Display the plot

#===============================================================================================================================================
#<o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o> <o>
#===============================================================================================================================================

# Main GUI setup
root = tk.Tk()
root.title("Interactive Plot")
fig, ax, line_neA, line_neB = init_plot()

canvas = FigureCanvasTkAgg(fig, master=root)
canvas.draw()
canvas.get_tk_widget().pack()

# Start the background thread
threading.Thread(target=background_update_task, args=(ax, line_neA, line_neB), daemon=True).start()

root.mainloop()
