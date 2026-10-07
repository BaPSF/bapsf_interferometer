"""Diagnostic IOC host: shot link, analysis pipeline and EPICS records (pythonSoftIOC).

Must not import softioc or any EPICS library: interf_main imports diag_ioc.link on the acquisition
host, whose main thread may not load them.
"""
