from PyQt5.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QLabel, QPushButton, QWidget
from PyQt5.QtCore import QThread, pyqtSignal, QObject

import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar

import numpy as np

from interf_plot import init_plot, get_latest_file, get_data
from interf_raw import get_calibration_factor

class Worker(QObject):
    data_updated = pyqtSignal(np.ndarray, np.ndarray, np.ndarray)  # Signal to emit the data

    def __init__(self, ifn):
        super().__init__()
        self.dir_path = r"C:\data\LAPD\interferometer_samples"

    def run(self):
        # This method will run in a separate thread
        # Replace this with your actual data update logic
        while True:
            ifn = get_latest_file(self.dir_path)
            t_ms, phaseA, phaseB = get_data(ifn)
            neA = phaseA * get_calibration_factor(288e9)
            neB = phaseB * get_calibration_factor(282e9)
            self.data_updated.emit(t_ms, neA, neB)
            QThread.sleep(1)  # Simulate some delay

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        layout = QVBoxLayout(central_widget)
        layout.addWidget(QLabel("Interferometer Plot"))
        button = QPushButton("Start Plot")
        layout.addWidget(button)
        button.clicked.connect(self.start_plot)

        self.autoscale_enabled = True  # Flag to enable/disable autoscale
        toggle_button = QPushButton("Toggle Autoscale")
        layout.addWidget(toggle_button)
        toggle_button.clicked.connect(self.toggle_autoscale)

        self.fig = Figure()
        self.ax = self.fig.add_subplot(111)
        self.canvas = FigureCanvas(self.fig)  # Create a canvas for the figure
        layout.addWidget(self.canvas)  # Add the canvas to the layout

        # Add the navigation toolbar
        self.toolbar = NavigationToolbar(self.canvas, self)
        layout.addWidget(self.toolbar)

        self.ax.set_xlabel('Time (ms)')
        self.ax.set_ylabel('ne (m^-3)')
        self.ax.set_title('Density assuming 40cm plasma Dia')
        self.ax.legend()

        self.line_neA, = self.ax.plot([], [], 'r-')
        self.line_neB, = self.ax.plot([], [], 'g-')

        self.ifn = r"C:\data\LAPD\interferometer_samples"
        self.thread = QThread()  # Thread for running the worker
        self.worker = Worker(self.ifn)  # Worker object
        self.worker.moveToThread(self.thread)  # Move worker to the thread
        self.worker.data_updated.connect(self.update_plot)  # Connect signal
        self.thread.started.connect(self.worker.run)  # Start worker.run when the thread starts

        self.update_count = 0  # Counter for testing

    def toggle_autoscale(self):
        # Toggle the autoscale state
        self.autoscale_enabled = not self.autoscale_enabled


    def start_plot(self):
        self.thread.start()  # Start the thread, which starts worker.run

    def update_plot(self, x, y_neA, y_neB):
        # Update the plot with new data
        self.line_neA.set_data(x, y_neA)
        self.line_neB.set_data(x, y_neB)
        self.ax.relim()

        if self.autoscale_enabled:
            self.ax.autoscale_view(True, True, True)
        else:
            # If autoscale is disabled, you might want to handle it differently
            pass

        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

        self.update_count += 1  # Increment the update counter
        print(f"Plot updated: {self.update_count}")  # Print the update count


if __name__ == "__main__":
    app = QApplication([])
    window = MainWindow()
    window.show()
    app.exec_()