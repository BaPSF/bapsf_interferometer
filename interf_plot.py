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
    plt.rcParams.update({'font.size': 14})
    plt.ion()  # Enable interactive mode
    fig, ax = plt.subplots(figsize=(8, 5))

    # Initialize line objects
    line_neA, = ax.plot([], [], 'r-', label='ne P20')
    line_neB, = ax.plot([], [], 'b-', label='ne P29')

    # Set plot titles and labels
    ax.set_xlabel('Time (ms)')
    ax.set_ylabel('ne (m^-3)')
    ax.set_title('Density assuming 40cm plasma length')
    ax.legend()

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
        ax.set_xlim(min(t_ms), max(t_ms))
    if len(neA) > 0 and len(neB) > 0:
        ax.set_ylim( min(min(neA),min(neB)), max(max(neA),max(neB)) )

    plt.draw()
    plt.pause(0.001)  # Pause to allow the plot to update

def end_plot():
    plt.ioff() # Turn off interactive mode
    plt.show() # Display the plot
