"""Launch and reconnect a single-socket consumer through SSH."""

import configparser
import json
import math
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from .connection_security import decrypt_connection_info
from .data_operations import load_data_operation
from .raw_output_io import IO
from .ntfy_notifications import NtfyNotifier
from .single_socket_io import SingleSocketIO
from .ssh_key_monitor import read_certificate_validity


class ServerConfig:
    """Validated values from a ``server.conf`` rendezvous file."""

    SECTION_NAMES = ("server", "consumer")

    def __init__(self, path):
        self.path = Path(path)
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with self.path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, configparser.Error) as exc:
            raise ValueError(
                f"Unable to read server configuration {path}: {exc}"
            ) from exc

        section_name = next(
            (name for name in self.SECTION_NAMES if parser.has_section(name)), None
        )
        if section_name is None:
            raise ValueError(
                f"Server configuration {path} needs a [server] section"
            )
        values = parser[section_name]

        self.host = self._required(values, "host")
        self.working_directory = self._required(values, "working_directory")
        self.python = values.get("python", "python3").strip()
        self.script = values.get("script", "streamer/consumer.py").strip()
        self.connection_file = values.get("connection_file", "conn.json").strip()
        self.output_directory = values.get("output_directory", "out").strip()
        self.public_key = self._required(values, "public_key")
        self.allow_ip_ranges = tuple(
            item.strip()
            for item in values.get("allow_ip_range", "").split(",")
            if item.strip()
        )
        if not self.allow_ip_ranges:
            raise ValueError("server allow_ip_range must contain at least one CIDR")

        self.port = values.get("port", fallback=None)
        self.bind_host = values.get("bind_host", fallback=None)
        self.advertise_host = values.get("advertise_host", fallback=None)
        self.engine = values.get("engine", fallback=None)
        self.timing_log = values.get("timing_log", fallback=None)
        self.file_interval_seconds = self._positive_float(
            values, "file_interval_seconds", 3600.0
        )
        self.stdout_log = values.get(
            "stdout_log", "raw_output_consumer.stdout.log"
        ).strip()
        self.pid_file = values.get(
            "pid_file", "raw_output_consumer.pid"
        ).strip()
        self.ssh_command = shlex.split(values.get("ssh_command", "ssh"))
        if not self.ssh_command:
            raise ValueError("server ssh_command must not be empty")
        self.socket_transport = (
            values.get("socket_transport", "direct").strip().lower()
        )
        if self.socket_transport not in ("direct", "ssh"):
            raise ValueError("server socket_transport must be 'direct' or 'ssh'")
        self.startup_timeout_seconds = self._positive_float(
            values, "startup_timeout_seconds", 30.0
        )
        self.socket_timeout_seconds = self._positive_float(
            values, "socket_timeout_seconds", 30.0
        )
        self.ntfy_topic_info = values.get("ntfy_topic_info", "").strip() or None
        self.ntfy_topic_alert = (
            values.get("ntfy_topic_alert", "").strip() or None
        )
        self.ntfy_server_url = values.get(
            "ntfy_server_url", "https://ntfy.sh"
        ).strip()
        self.ntfy_token_env = values.get("ntfy_token_env", "NTFY_TOKEN").strip()
        self.ntfy_timeout_seconds = self._positive_float(
            values, "ntfy_timeout_seconds", 5.0
        )
        if (
            self.ntfy_topic_info or self.ntfy_topic_alert
        ) and not self.ntfy_server_url:
            raise ValueError("server ntfy_server_url must not be empty")
        if (
            self.ntfy_topic_info or self.ntfy_topic_alert
        ) and not self.ntfy_token_env:
            raise ValueError("server ntfy_token_env must not be empty")
        self.ssh_key_certificate = (
            values.get("ssh_key_certificate", "").strip() or None
        )
        self.ssh_key_check_interval_seconds = self._positive_float(
            values, "ssh_key_check_interval_seconds", 60.0
        )
        self.retry_delay_seconds = self._nonnegative_float(
            values, "retry_delay_seconds", 1.0
        )
        self.max_relaunch_attempts = self._nonnegative_int(
            values, "max_relaunch_attempts", 0
        )
        compression_config = values.get("compression_config", "").strip()
        self.compression_config = (
            self.path.parent / compression_config
            if compression_config and not Path(compression_config).is_absolute()
            else Path(compression_config) if compression_config else None
        )

        for name in (
            "python",
            "script",
            "connection_file",
            "output_directory",
            "stdout_log",
            "pid_file",
        ):
            if not getattr(self, name):
                raise ValueError(f"server {name} must not be empty")

    @staticmethod
    def is_server_config(path):
        path = Path(path)
        if not path.is_file():
            return False
        parser = configparser.ConfigParser(interpolation=None)
        try:
            with path.open(encoding="utf-8") as stream:
                parser.read_file(stream)
        except (OSError, UnicodeDecodeError, configparser.Error):
            return False
        return any(parser.has_section(name) for name in ServerConfig.SECTION_NAMES)

    @staticmethod
    def _required(values, name):
        value = values.get(name, "").strip()
        if not value:
            raise ValueError(f"server {name} is required")
        return value

    @staticmethod
    def _positive_float(values, name, default):
        try:
            value = values.getfloat(name, fallback=default)
        except ValueError as exc:
            raise ValueError(f"server {name} must be a number") from exc
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"server {name} must be greater than zero")
        return value

    @staticmethod
    def _nonnegative_float(values, name, default):
        try:
            value = values.getfloat(name, fallback=default)
        except ValueError as exc:
            raise ValueError(f"server {name} must be a number") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"server {name} must not be negative")
        return value

    @staticmethod
    def _nonnegative_int(values, name, default):
        try:
            value = values.getint(name, fallback=default)
        except ValueError as exc:
            raise ValueError(f"server {name} must be an integer") from exc
        if value < 0:
            raise ValueError(f"server {name} must not be negative")
        return value


class SSHConsumerLauncher:
    """Start a detached remote process and return its encrypted rendezvous."""

    def __init__(self, config, ssh_command=None):
        self.config = config
        self.ssh_command = ssh_command or config.ssh_command

    def launch(self):
        command = self._remote_command()
        try:
            completed = subprocess.run(
                [*self.ssh_command, self.config.host, command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.config.startup_timeout_seconds + 5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise OSError(
                f"Unable to launch consumer on {self.config.host}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise OSError(
                f"Consumer launch on {self.config.host} failed"
                + (f": {detail}" if detail else "")
            )
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise OSError(
                f"Consumer launch on {self.config.host} returned invalid "
                "connection information"
            ) from exc
        return envelope

    def _consumer_arguments(self):
        cfg = self.config
        arguments = [
            cfg.python,
            cfg.script,
            "--public-key",
            cfg.public_key,
            "--file-interval-seconds",
            str(cfg.file_interval_seconds),
        ]
        for network in cfg.allow_ip_ranges:
            arguments.extend(("--allow-ip-range", network))
        for option, value in (
            ("--port", cfg.port),
            ("--bind-host", cfg.bind_host),
            ("--advertise-host", cfg.advertise_host),
            ("--engine", cfg.engine),
            ("--timing-log", cfg.timing_log),
        ):
            if value:
                arguments.extend((option, value))
        arguments.extend((cfg.connection_file, cfg.output_directory))
        return arguments

    def _remote_command(self):
        cfg = self.config
        quote = shlex.quote
        workdir = quote(cfg.working_directory)
        pid_file = quote(cfg.pid_file)
        connection_file = quote(cfg.connection_file)
        stdout_log = quote(cfg.stdout_log)
        script = quote(cfg.script)
        consumer = " ".join(
            quote(item)
            for item in self._consumer_arguments()
        )
        polls = max(1, math.ceil(cfg.startup_timeout_seconds * 10))
        # All consumer descriptors are redirected before the SSH shell exits.
        # nohup makes the process independent of the SSH session's SIGHUP.
        return "\n".join(
            (
                "set -eu",
                f"cd {workdir}",
                f"if [ -f {pid_file} ]; then",
                f"  old_pid=$(cat {pid_file})",
                '  case "$old_pid" in',
                '    *[!0-9]*|"") ;;',
                "    *)",
                '      if tr \'\\000\' \'\\n\' < "/proc/$old_pid/cmdline" 2>/dev/null '
                f"| grep -Fx -- {script} >/dev/null 2>&1; then",
                '        kill "$old_pid" 2>/dev/null || true',
                "        stop_i=0",
                '        while kill -0 "$old_pid" 2>/dev/null '
                '&& [ "$stop_i" -lt 20 ]; do',
                "          sleep 0.1",
                "          stop_i=$((stop_i + 1))",
                "        done",
                '        kill -KILL "$old_pid" 2>/dev/null || true',
                "      fi",
                "      ;;",
                "  esac",
                "fi",
                f"rm -f {connection_file}",
                f"nohup {consumer} >> {stdout_log} 2>&1 < /dev/null &",
                "consumer_pid=$!",
                f"printf '%s\\n' \"$consumer_pid\" > {pid_file}",
                "i=0",
                f'while [ "$i" -lt {polls} ]; do',
                f"  if [ -s {connection_file} ]; then cat {connection_file}; exit 0; fi",
                '  if ! kill -0 "$consumer_pid" 2>/dev/null; then',
                f"    tail -n 20 {stdout_log} >&2 || true",
                "    exit 1",
                "  fi",
                "  sleep 0.1",
                "  i=$((i + 1))",
                "done",
                'kill "$consumer_pid" 2>/dev/null || true',
                'echo "consumer did not create its connection file in time" >&2',
                "exit 1",
            )
        )


class SSHStreamSocket:
    """Socket-compatible endpoint backed by an OpenSSH stdio forwarding channel."""

    family = socket.AF_UNIX

    def __init__(self, connection, process, error_file):
        self._connection = connection
        self._process = process
        self._error_file = error_file
        self._closed = False

    def sendall(self, data):
        try:
            return self._connection.sendall(data)
        except OSError as exc:
            self._raise_tunnel_error(exc)

    def recv_into(self, buffer):
        try:
            received = self._connection.recv_into(buffer)
        except OSError as exc:
            self._raise_tunnel_error(exc)
        if received == 0:
            self._raise_tunnel_error()
        return received

    def settimeout(self, timeout):
        self._connection.settimeout(timeout)

    def _raise_tunnel_error(self, cause=None):
        returncode = self._process.poll()
        if returncode is None or returncode == 0:
            if cause is not None:
                raise cause
            return
        detail = self._read_error()
        message = f"SSH socket tunnel exited with status {returncode}"
        if detail:
            message += f": {detail}"
        error = OSError(message)
        if cause is not None:
            raise error from cause
        raise error

    def _read_error(self):
        try:
            self._error_file.flush()
            self._error_file.seek(0)
            return self._error_file.read().decode("utf-8", errors="replace").strip()
        except (OSError, ValueError):
            return ""

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._connection.close()
        try:
            self._process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        finally:
            self._error_file.close()


class SSHSocketConnector:
    """Create socket-like streams through the configured SSH destination."""

    def __init__(self, config, ssh_command=None):
        self.config = config
        self.ssh_command = ssh_command or config.ssh_command

    def __call__(self, address, timeout=None):
        host, port = address
        target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        local_socket, ssh_socket = socket.socketpair()
        error_file = tempfile.TemporaryFile()
        try:
            process = subprocess.Popen(
                [
                    *self.ssh_command,
                    "-T",
                    "-o",
                    "BatchMode=yes",
                    "-W",
                    target,
                    self.config.host,
                ],
                stdin=ssh_socket,
                stdout=ssh_socket,
                stderr=error_file,
                close_fds=True,
            )
        except Exception:
            local_socket.close()
            error_file.close()
            raise
        finally:
            ssh_socket.close()
        local_socket.settimeout(timeout)
        return SSHStreamSocket(local_socket, process, error_file)


class SSHControlConnection:
    """Pin launch and forwarding channels to one SSH server connection."""

    def __init__(self, config):
        self.config = config
        self._directory = tempfile.TemporaryDirectory(prefix="interf-streamer-ssh-")
        self.control_path = str(Path(self._directory.name) / "control")
        executable, *arguments = config.ssh_command
        self.ssh_command = [
            executable,
            "-o",
            "BatchMode=yes",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "ControlMaster=auto",
            "-o",
            f"ControlPath={self.control_path}",
            "-o",
            "ControlPersist=yes",
            *arguments,
        ]
        self._closed = False

    def close(self):
        if self._closed:
            return
        self._closed = True
        executable, *arguments = self.config.ssh_command
        try:
            subprocess.run(
                [
                    executable,
                    "-S",
                    self.control_path,
                    *arguments,
                    "-O",
                    "exit",
                    self.config.host,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self._directory.cleanup()


class SSHKeyMonitorProcess:
    """Run the SSH certificate expiry monitor independently of the producer."""

    def __init__(self, config):
        self._process = None
        certificate = getattr(config, "ssh_key_certificate", None)
        topic = getattr(config, "ntfy_topic_alert", None)
        if not certificate or not topic:
            return
        monitor_script = Path(__file__).with_name("ssh_key_monitor.py")
        self._process = subprocess.Popen(
            [
                sys.executable,
                str(monitor_script),
                "--certificate",
                certificate,
                "--topic",
                topic,
                "--server-url",
                config.ntfy_server_url,
                "--token-env",
                config.ntfy_token_env,
                "--timeout-seconds",
                str(config.ntfy_timeout_seconds),
                "--interval-seconds",
                str(config.ssh_key_check_interval_seconds),
                "--parent-pid",
                str(os.getpid()),
            ],
            stdin=subprocess.DEVNULL,
            close_fds=True,
        )

    def close(self):
        if self._process is None or self._process.poll() is not None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()


class RestartingSingleSocketIO(IO):
    """Reconnect a remote consumer and retry only the interrupted output."""

    EXPIRED_KEY_RELAUNCH_ATTEMPTS = 1

    def __init__(
        self,
        settings,
        private_key,
        config=None,
        launcher=None,
        timing_log=None,
        notifier=None,
        alert_notifier=None,
    ):
        self._timing_log = timing_log
        self._private_key = private_key
        self._config = config or ServerConfig(settings.destination)
        self._operation = load_data_operation(
            getattr(self._config, "compression_config", None)
        )
        self._notifier = notifier or NtfyNotifier.from_config(
            self._config, timing_log=timing_log
        )
        self._alert_notifier = alert_notifier or NtfyNotifier.from_config(
            self._config,
            timing_log=timing_log,
            topic_attribute="ntfy_topic_alert",
        )
        self._key_monitor = None
        self._ssh_control = None
        ssh_command = getattr(self._config, "ssh_command", None)
        if launcher is None:
            self._ssh_control = SSHControlConnection(self._config)
            ssh_command = self._ssh_control.ssh_command
        self._launcher = launcher or SSHConsumerLauncher(self._config, ssh_command)
        self._connector = None
        if getattr(self._config, "socket_transport", "direct") == "ssh":
            self._connector = SSHSocketConnector(
                self._config, ssh_command=ssh_command
            )
        self._output = None
        self._closed = False
        try:
            self._key_monitor = SSHKeyMonitorProcess(self._config)
            self._connect_with_retries("launch")
        except Exception:
            if self._key_monitor is not None:
                self._key_monitor.close()
            if self._ssh_control is not None:
                self._ssh_control.close()
            if self._notifier is not None:
                self._notifier.close()
            if self._alert_notifier is not None:
                self._alert_notifier.close()
            raise

    @property
    def notifier(self):
        return self._notifier

    def _new_output(self):
        envelope = self._launcher.launch()
        connection_info = decrypt_connection_info(envelope, self._private_key)
        if connection_info.get("id") != "singlesocket":
            raise ValueError("Remote consumer must use the singlesocket protocol")
        socket_settings = SimpleNamespace(
            socket_timeout_seconds=self._config.socket_timeout_seconds
        )
        return SingleSocketIO(
            socket_settings,
            connection_info,
            self._private_key,
            connector=self._connector,
            operation=self._operation,
        )

    def _connect_with_retries(self, reason):
        attempt = 0
        expired_key_attempts = 0
        restart_failure_notified = False
        while True:
            attempt += 1
            try:
                self._output = self._new_output()
                self._record(
                    "consumer.launch",
                    reason=reason,
                    attempt=attempt,
                    host=self._config.host,
                )
                if self._notifier is not None:
                    restarted = reason == "connection_lost"
                    action = "restarted" if restarted else "started"
                    self._notifier.notify(
                        f"Raw data server {action}",
                        f"Remote consumer on {self._config.host} {action} "
                        f"(attempt {attempt}).",
                        priority=3,
                        tags=("white_check_mark",),
                    )
                return
            except (OSError, EOFError, RuntimeError) as exc:
                self._record(
                    "consumer.launch_retry",
                    reason=reason,
                    attempt=attempt,
                    host=self._config.host,
                )
                if (
                    reason == "connection_lost"
                    and not restart_failure_notified
                    and self._alert_notifier is not None
                ):
                    self._alert_notifier.notify(
                        "Raw data server restart failed",
                        f"Restart attempt {attempt} for the remote consumer on "
                        f"{self._config.host} failed: {type(exc).__name__}: {exc}",
                        priority=5,
                        tags=("rotating_light",),
                    )
                    restart_failure_notified = True
                maximum = self._config.max_relaunch_attempts
                if maximum and attempt >= maximum:
                    raise
                expired_certificate = self._expired_ssh_certificate()
                if expired_certificate is None:
                    expired_key_attempts = 0
                else:
                    expired_key_attempts += 1
                    if (
                        expired_key_attempts
                        >= self.EXPIRED_KEY_RELAUNCH_ATTEMPTS
                    ):
                        self._wait_for_ssh_key_update(expired_certificate)
                        attempt = 0
                        expired_key_attempts = 0
                        continue
                if self._config.retry_delay_seconds:
                    time.sleep(self._config.retry_delay_seconds)

    def _expired_ssh_certificate(self):
        certificate = getattr(self._config, "ssh_key_certificate", None)
        if not certificate:
            return None
        try:
            validity = read_certificate_validity(certificate)
        except ValueError:
            return None
        return validity if time.time() >= validity.valid_before else None

    def _wait_for_ssh_key_update(self, expired_certificate):
        certificate = self._config.ssh_key_certificate
        interval = getattr(
            self._config, "ssh_key_check_interval_seconds", 60.0
        )
        self._record(
            "ssh_key.wait",
            certificate=certificate,
            expired_at=expired_certificate.valid_before,
        )
        while True:
            try:
                replacement = read_certificate_validity(certificate)
            except ValueError:
                replacement = None
            now = time.time()
            if (
                replacement is not None
                and replacement != expired_certificate
                and replacement.valid_after <= now < replacement.valid_before
            ):
                self._record(
                    "ssh_key.updated",
                    certificate=certificate,
                    valid_before=replacement.valid_before,
                )
                return
            time.sleep(interval)

    def write_data(self, data):
        if self._closed:
            raise RuntimeError("Cannot write to closed output")
        try:
            self._output.write_data(data)
        except (OSError, EOFError, RuntimeError) as exc:
            self._relaunch_and_retry(data, exc)

    def _relaunch_and_retry(self, data, failure):
        while True:
            self._record(
                "consumer.connection_lost",
                error_type=type(failure).__name__,
            )
            if self._notifier is not None:
                self._notifier.notify(
                    "Raw data server disconnected",
                    f"Connection to {self._config.host} was lost: "
                    f"{type(failure).__name__}: {failure}",
                    priority=5,
                    tags=("rotating_light",),
                )
            self._discard_output(graceful=False)
            self._connect_with_retries("connection_lost")
            try:
                self._output.write_data(data)
                self._record("io.retry")
                return
            except (OSError, EOFError, RuntimeError) as exc:
                failure = exc

    def _record(self, event, **fields):
        if self._timing_log is not None:
            self._timing_log.record(event, **fields)

    def _discard_output(self, graceful=True):
        if self._output is not None:
            if graceful:
                self._output.close()
            else:
                self._output.abort()
            self._output = None

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._discard_output()
        finally:
            try:
                if self._key_monitor is not None:
                    self._key_monitor.close()
            finally:
                try:
                    if self._ssh_control is not None:
                        self._ssh_control.close()
                finally:
                    try:
                        if self._notifier is not None:
                            self._notifier.close()
                    finally:
                        if self._alert_notifier is not None:
                            self._alert_notifier.close()
