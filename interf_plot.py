# coding: utf-8
'''
Module used for showing plots on the screen
'''
from tkinter import ttk
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import os
import time

import matplotlib.pyplot as plt
import matplotlib.animation as animation

def init_plot():
    """
    This function creates a new matplotlib figure and axes, and initializes line objects for the plot.
    """
    plt.ion()  # Enable interactive mode
    fig, ax = plt.subplots(2, 1, figsize=(10, 8))

    # Initialize line objects
    line_neA, = ax[0].plot([], [], 'r-')
    line_neB, = ax[1].plot([], [], 'b-')

    # Set plot titles and labels
    ax[0].set_xlabel('Time (ms)')
    ax[0].set_ylabel('ne P20 (m^-3)')

    ax[1].set_xlabel('Time (ms)')
    ax[1].set_ylabel('ne P29 (m^-3)')

    return ax, line_neA, line_neB

def update_plot(ax, lineA, lineB, t_ms, neA, neB):
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
    # Update data for plotting
    lineA.set_data(t_ms, neA)
    lineB.set_data(t_ms, neB)

    # Adjust plot limits dynamically
    if len(t_ms) > 0:
        ax[0].set_xlim(0, max(t_ms))
        ax[1].set_xlim(0, max(t_ms))
    if len(neA) > 0:
        ax[0].set_ylim(min(neA), max(neA))
    if len(neB) > 0:
        ax[1].set_ylim(min(neB), max(neB))

    plt.draw()
    plt.pause(0.001)  # Pause to allow the plot to update

def end_plot():
    plt.ioff() # Turn off interactive mode
    plt.show() # Display the plot
