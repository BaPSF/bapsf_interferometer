# Interferometer raw-output streamer

`streamer` adapts the producer/consumer runtime from `../mockapp` to the raw
BaPSF interferometer acquisition. It replaces the heat-transfer generator and
its fixed `d1` through `d6` variables with complete `RawShot` payloads produced
by `interf_main`.

Raw output is opt-in. Without `--raw-output`, `python -m interf_sim` and the
live `python interf_main.py` entry point retain their previous behavior. The
live entry point currently passes no outputs; a future live-output CLI can
pass the same `RawOutput` used by the simulator in `main(outputs=[...])`. A
failing output is logged and skipped without affecting other outputs or the
shot log line.

## Output destinations

`--raw-output DESTINATION` accepts three kinds of destination:

1. An ADIOS output path, written directly by the producer.
2. An encrypted connection-information file created by a manually started
   consumer.
3. A `.conf` file with a `[server]` section, used to launch and recover a
   consumer through SSH.

Write ten replayed shots directly to BP5:

```bash
python -m interf_sim --limit 10 --raw-output raw.bp
```

BP5 is the default engine. Select another engine supported by the installed
ADIOS2 build with `--raw-output-engine ENGINE`.

## Raw-shot payload

Every output step contains the acquired sample arrays and the metadata needed
to interpret them:

| Variable | Contents |
|---|---|
| `schema_version` | Scalar payload schema version, currently `1` |
| `shot_index` | Scalar zero-based output sequence number |
| `host_time` | Scalar Unix time when the LeCroy capture was detected |
| `critical_path_s` | Scalar capture-to-rearm duration in seconds |
| `missing_json` | UTF-8 JSON byte array describing unavailable scope data |
| `lecroy_<channel>_samples` | Raw `int16` LeCroy samples |
| `lecroy_<channel>_wavedesc` | Raw 346-byte LeCroy WAVEDESC |
| `rigol_<channel>_samples` | Raw `uint16` Rigol sample codes |
| `rigol_<channel>_metadata_json` | UTF-8 JSON byte array with Rigol calibration metadata |

Unavailable channels are omitted rather than replaced by fabricated arrays.
They remain described by `missing_json`, and a variable can first appear in a
later step when a scope recovers. `streamer.payload.decode_json()` decodes the
JSON byte-array variables, and `streamer.payload.shot_from_variables()` rebuilds
a whole step as a `DecodedShot` that `interf_analysis.analyze_shot()` accepts.

Every non-scalar ADIOS variable is a single-process global array. Its global
shape and count equal the payload array shape, and its start is zero in every
dimension. Variable-length JSON metadata updates its global shape and
selection on every step. Scalar values remain ADIOS scalars.

## Buffering and timing logs

Output is queued and written by a dedicated background thread so a slow
destination does not normally pause acquisition. The queue holds 600 seconds
of output by default; change it with:

```bash
--raw-output-buffer-seconds SECONDS
```

Capacity in steps is the configured duration divided by the simulator shot
period (three seconds when `--period 0`). When the queue is full, one queued
snapshot is discarded to admit the newest snapshot. The selection advances
through the queue so the same position is not always discarded. Remaining
snapshots are flushed during an orderly producer shutdown.

Producer timing records are line-delimited JSON. The default simulator log is
`interf_sim/log/raw_output.jsonl`; choose another path with
`--raw-output-timing-log FILE`. It records each `raw_output.write` enqueue and
each `io.buffer_drop`, including the dropped and incoming shot indexes.

The consumer timing log records socket receives and output writes. Remote
launches, reconnects, retries, SSH-certificate waits, and notification failures
are also recorded when those features are enabled.

## Keys and connection security

Generate a Curve25519 keypair once and keep the private key only with the
producer:

```bash
python -m streamer.keygen generate keys/interf.pub keys/interf.key
```

The consumer encrypts its connection information with the public key. The
producer decrypts it with the private key and then proves possession of that
key by answering a fresh encrypted challenge on every socket. Encrypted
rendezvous files use this format identifier:

```text
lapd-curve25519-sealed-box-v1
```

The producer rejects plaintext socket connection information and envelopes
with another format identifier. Socket output therefore requires
`--raw-output-private-key FILE`; direct ADIOS output does not.

Consumers require at least one producer IPv4 range in CIDR notation. Repeat
`--allow-ip-range` to permit more than one range. Connections outside these
ranges are closed before key authentication or protocol parsing. For an
Internet- or site-facing listener, enforce the same restriction with a host
firewall or infrastructure security group so unwanted TCP handshakes are
dropped before reaching the application.

By default, a consumer listens on any available ephemeral port. Use
`--port 5000` to prefer port 5000 and increment through available higher ports,
`--port 5000-5000` to require exactly port 5000, or `--port 5000-5009` to stay
within a firewall-approved range. The listener enables address reuse, and the
operating system releases its descriptors if the process exits.

## Manually launched single-socket consumer

Start the consumer before the producer so it can create the encrypted
connection file:

```bash
python -m streamer.consumer connection.json received \
    --public-key keys/interf.pub \
    --allow-ip-range 127.0.0.1/32 \
    --port 5000-5009 \
    --file-interval-seconds 3600 \
    --timing-log consumer-timing.jsonl
```

Then run the simulator producer:

```bash
python -m interf_sim --limit 10 \
    --raw-output connection.json \
    --raw-output-private-key keys/interf.key \
    --raw-output-timing-log producer-timing.jsonl
```

The connection ID is `singlesocket`. All scalar, sample, and metadata
variables travel through one TCP connection. The consumer acknowledges a step
only after its output write completes, allowing the producer to distinguish a
completed step from a stalled consumer or a connection that failed during
transfer.

The connection information advertises protocol version 4. Its array framing
supports arbitrary named NumPy arrays, encoded-payload lengths, shapes, dtypes,
and optional operation metadata. The initial hello lists the first step's
variables; later data messages are self-describing so channels may disappear
or recover. Producer and consumer must use compatible protocol versions.

The consumer stores received steps in timestamped outputs under `received/`.
A new output is opened every 3600 seconds by default; set
`--file-interval-seconds` to another positive interval. Rotation occurs before
the first step received at or after the deadline.

For a consumer reached from another host, specify both the listening interface
and the address the producer can reach:

```bash
python -m streamer.consumer connection.json received \
    --public-key keys/interf.pub \
    --allow-ip-range PRODUCER_NETWORK/24 \
    --port 5000-5009 \
    --bind-host 0.0.0.0 \
    --advertise-host RECEIVER_HOST
```

## Payload operations and compression

Socket transformations implement the generic `DataOperation` interface in
`data_operations.py`. An operation encodes a contiguous NumPy array and emits
a self-describing operation record. The consumer selects the registered
decoder from that record and reconstructs the original dtype and shape. This
boundary can support future lossy compressors or scientific-data refactoring
operations as well as the included lossless compressor.

The included implementation uses Blosc2 with Zstandard. Install the optional
dependency in both producer and consumer environments:

```bash
python -m pip install 'blosc2>=4,<5'
```

The recommended `streamer/conf/blosc2-zstd.conf` contains:

```ini
[compression]
implementation = blosc2
codec = zstd
compression_level = 1
filter = shuffle
threads = 1
```

Enable it on the producer with:

```bash
--raw-output-compression-config streamer/conf/blosc2-zstd.conf
```

Compression runs in the output worker. `shuffle` uses each array's NumPy item
size, so it handles `int16` samples, `uint16` codes, byte metadata, and scalar
values. If compression would enlarge a particular variable, that variable is
sent uncompressed. Omitting the option sends every variable uncompressed.

## Launching and recovering a consumer through SSH

Pass a configuration containing a `[server]` section to `--raw-output`. The
checked-in files under `streamer/conf/` are deployment-specific examples and
must be reviewed for current hosts, paths, credentials, and notification
topics. A minimal configuration is:

```ini
[server]
host = consumer-host
working_directory = /path/to/bapsf_interferometer
python = /path/to/venv/bin/python3
script = streamer/consumer.py
compression_config = streamer/conf/blosc2-zstd.conf

connection_file = data/logs/interf.conn.json
output_directory = data/raw-output
file_interval_seconds = 3600
stdout_log = data/logs/raw-output-consumer.stdout.log
pid_file = data/logs/raw-output-consumer.pid
timing_log = data/logs/raw-output-consumer.jsonl

public_key = keys/interf.pub
allow_ip_range = 192.168.1.0/24
port = 8501-8599
bind_host = 0.0.0.0
advertise_host = consumer-host

socket_timeout_seconds = 30
startup_timeout_seconds = 30
retry_delay_seconds = 1
max_relaunch_attempts = 0
```

Start the producer with the configuration as its raw-output destination:

```bash
python -m interf_sim --limit 10 \
    --raw-output streamer/conf/server.conf \
    --raw-output-private-key keys/interf.key \
    --raw-output-buffer-seconds 600 \
    --raw-output-timing-log producer-timing.jsonl
```

`compression_config` is resolved relative to the server configuration on the
producer. The remote consumer does not open that file: each encoded variable
carries its operation description and the consumer selects the corresponding
decoder. Omit the setting to disable transformations.

The SSH client must already authenticate noninteractively. Paths other than
`working_directory` are interpreted after changing to the remote working
directory. `allow_ip_range` accepts a comma-separated list. `ssh_command` can
specify OpenSSH options or another executable.

The launcher establishes one private OpenSSH multiplexed control connection
and reuses it for launches and, when selected, data forwarding. It is
noninteractive and public-key-only, so an expired SSH certificate fails rather
than falling through to password or keyboard-interactive authentication.

### Direct and SSH-forwarded data sockets

The data connection is direct by default. When the producer can SSH to the
consumer host but cannot reach its listening port, route the stream through
the same SSH destination:

```ini
socket_transport = ssh
bind_host = 127.0.0.1
advertise_host = 127.0.0.1
allow_ip_range = 127.0.0.1/32
```

This uses an OpenSSH stdio forward equivalent to `ssh -W HOST:PORT`. It honors
the host, keys, `ProxyJump`, and other settings in the normal SSH
configuration, and it allocates no local listening port. Forwarding channels
share the persistent control connection so a load-balanced SSH destination
cannot route a launch and its data channel to different servers. Use the
default `socket_transport = direct` whenever the producer can reach the
advertised socket directly.

### Remote lifecycle and recovery

The remote command removes stale rendezvous information, stops the process
recorded by `pid_file`, and starts the consumer with `nohup`. Standard input is
`/dev/null`; stdout and stderr append to `stdout_log`. Once encrypted connection
information is returned, the launch channel exits. The consumer remains
independent of that channel while the shared SSH control connection stays
available to the producer.

`socket_timeout_seconds` bounds socket operations, including the per-step
acknowledgement. If a socket breaks or the consumer stalls past the timeout,
the producer closes the failed channel, launches a replacement consumer, and
retries only the interrupted in-flight snapshot. Already acknowledged
snapshots are not resent. The buffered queue then drains in FIFO order before
newer shots are sent; acquisitions are never recomputed.

Each replacement consumer creates a new timestamped output and never appends
to a potentially damaged file. The failed snapshot is retried into the new
output while earlier outputs remain unchanged. `max_relaunch_attempts = 0`
retries indefinitely; a positive value limits each launch series.

Remote recovery records `consumer.connection_lost`, `consumer.launch`,
`consumer.launch_retry`, `io.retry`, `ssh_key.wait`, and `ssh_key.updated`
events in the producer timing log.

## ntfy notifications and SSH-certificate monitoring

Remote configurations can enable best-effort ntfy notifications:

```ini
ntfy_topic_info = my-info-topic
ntfy_topic_alert = my-alert-topic
ssh_key_certificate = ~/.ssh/nersc-cert.pub

ntfy_server_url = https://ntfy.sh
ntfy_token_env = NTFY_TOKEN
ntfy_timeout_seconds = 5
ssh_key_check_interval_seconds = 60
```

Set access tokens in the environment instead of the configuration file, for
example `export NTFY_TOKEN=tk_...` before starting the producer.

The info topic receives asynchronous consumer start, restart, disconnect, and
buffer-pressure notifications. Buffer thresholds at 20%, 40%, and 60% notify
once per producer run. At 80% or more, notification is immediate and then
limited to once per hour; later messages include snapshots discarded since
the previous high-buffer notification.

When `ssh_key_certificate` and `ntfy_topic_alert` are set, a separate local
monitor reads the OpenSSH certificate with `ssh-keygen -L`. It warns 6, 3, 2,
and 1 hour before expiration and reports expired, missing, invalid, or not-yet-
valid certificates. It re-reads the file on every check, so replacing a
certificate resets the warning schedule without restarting acquisition. The
first failed remote restart in a reconnect episode also goes to the alert
topic.

When an expired configured certificate prevents recovery, the producer pauses
launch attempts after the first failure and watches for a different, currently
valid certificate. It resumes recovery after that replacement appears.

Notification failures never interrupt output. Main-process failures are
recorded as `notification.failed` timing events. The certificate monitor stops
with the producer and does not itself open an SSH connection.

## Running a consumer without ADIOS2

The socket consumer can run without the `adios2` Python module. In that case,
each timestamped output uses `.pkl` instead of `.bp`. Each pickle stream
contains one raw-shot dictionary per received step with the same variable
names described above:

```python
import pickle

steps = []
with open("received/20260925T120000.000000-0400.pkl", "rb") as stream:
    while True:
        try:
            steps.append(pickle.load(stream))
        except EOFError:
            break
```

Direct producer output still requires ADIOS2. The pickle fallback applies only
to the receiving side.

## Tests

Run the checked-in streamer tests with:

```bash
python -m unittest discover -s tests -v
```

They cover payload construction and metadata, write-before-log ordering,
encrypted rendezvous information, arbitrary named socket variables, and
dynamic global ADIOS variables. The end-to-end simulator path can be exercised
with one recorded shot using:

```bash
python -m interf_sim --period 0 --limit 1 --raw-output /tmp/interf-raw.bp
```
