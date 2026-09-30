#!/usr/bin/env python3
"""Monitor an OpenSSH certificate and send expiry alerts through ntfy."""

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "streamer"

from .ntfy_notifications import NtfyNotifier


EXPIRY_WARNING_HOURS = (6, 3, 2, 1)
VALIDITY_PATTERN = re.compile(
    r"^\s*Valid:\s+from\s+(\S+)\s+to\s+(\S+)\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class CertificateValidity:
    valid_after: float
    valid_before: float


def _local_timestamp(value):
    """Parse ssh-keygen's local-time ISO timestamp."""
    parsed = datetime.fromisoformat(value)
    return parsed.timestamp()


def read_certificate_validity(path, runner=subprocess.run):
    """Return validity timestamps from an OpenSSH user certificate."""
    path = Path(path).expanduser()
    try:
        completed = runner(
            ["ssh-keygen", "-L", "-f", str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"unable to inspect {path}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(
            f"unable to inspect {path}" + (f": {detail}" if detail else "")
        )
    match = VALIDITY_PATTERN.search(completed.stdout)
    if match is None or match.group(2).lower() == "forever":
        raise ValueError(f"{path} does not contain a finite certificate validity")
    try:
        validity = CertificateValidity(
            valid_after=_local_timestamp(match.group(1)),
            valid_before=_local_timestamp(match.group(2)),
        )
    except ValueError as exc:
        raise ValueError(f"unable to parse certificate validity in {path}") from exc
    if validity.valid_before <= validity.valid_after:
        raise ValueError(f"certificate validity in {path} is invalid")
    return validity


class SSHKeyExpiryChecker:
    """Perform one check at a time while retaining per-certificate alert state."""

    def __init__(self, certificate, notifier, reader=read_certificate_validity):
        self.certificate = str(Path(certificate).expanduser())
        self.notifier = notifier
        self.reader = reader
        self._validity = None
        self._warned_hours = set()
        self._read_error = None
        self._not_yet_valid = False
        self._expired = False

    def check(self, now=None):
        now = time.time() if now is None else now
        try:
            validity = self.reader(self.certificate)
        except ValueError as exc:
            reason = str(exc)
            if reason != self._read_error:
                self.notifier.notify(
                    "NERSC SSH key cannot be validated",
                    reason,
                    priority=5,
                    tags=("rotating_light", "key"),
                )
                self._read_error = reason
            return

        self._read_error = None
        if validity != self._validity:
            self._validity = validity
            self._warned_hours.clear()
            self._not_yet_valid = False
            self._expired = False

        if now < validity.valid_after:
            if not self._not_yet_valid:
                self.notifier.notify(
                    "NERSC SSH key is not valid yet",
                    f"{self.certificate} is not valid yet; validity starts at "
                    f"{_format_local_time(validity.valid_after)}.",
                    priority=5,
                    tags=("rotating_light", "key"),
                )
                self._not_yet_valid = True
            return

        self._not_yet_valid = False

        if now >= validity.valid_before:
            if not self._expired:
                self.notifier.notify(
                    "NERSC SSH key has expired",
                    f"Replace {self.certificate}; it expired at "
                    f"{_format_local_time(validity.valid_before)}.",
                    priority=5,
                    tags=("rotating_light", "key"),
                )
                self._expired = True
            return

        remaining = validity.valid_before - now
        for hours in EXPIRY_WARNING_HOURS:
            if remaining <= hours * 3600 and hours not in self._warned_hours:
                self.notifier.notify(
                    f"NERSC SSH key expires in {hours} "
                    f"hour{'s' if hours != 1 else ''}",
                    f"Replace {self.certificate} before "
                    f"{_format_local_time(validity.valid_before)}.",
                    priority=5 if hours == 1 else 4,
                    tags=("warning", "key"),
                )
                self._warned_hours.add(hours)


def _format_local_time(timestamp):
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def monitor(
    certificate,
    notifier,
    interval_seconds=60.0,
    stop_event=None,
    parent_pid=None,
):
    stop_event = stop_event or threading.Event()
    checker = SSHKeyExpiryChecker(certificate, notifier)
    while not stop_event.is_set() and (
        parent_pid is None or os.getppid() == parent_pid
    ):
        checker.check()
        stop_event.wait(interval_seconds)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--server-url", default="https://ntfy.sh")
    parser.add_argument("--token-env", default="NTFY_TOKEN")
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be greater than zero")
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be greater than zero")
    return args


def main(argv=None):
    args = parse_args(argv)
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    notifier = NtfyNotifier(
        args.topic,
        server_url=args.server_url,
        token_env=args.token_env,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        monitor(
            args.certificate,
            notifier,
            interval_seconds=args.interval_seconds,
            stop_event=stop_event,
            parent_pid=args.parent_pid,
        )
    finally:
        notifier.close()


if __name__ == "__main__":
    main()
