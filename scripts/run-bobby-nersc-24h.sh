#!/usr/bin/env bash
set -euo pipefail

mkdir -p data/logs

run_name=bobby-nersc-24h-20261001
pid_file="data/logs/${run_name}.producer.pid"
stdout_log="data/logs/${run_name}.producer.stdout.log"
timing_log="data/logs/${run_name}.producer.jsonl"

if [[ -s "$pid_file" ]]; then
    old_pid=$(<"$pid_file")
    if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
        echo "Producer process $old_pid is already running; refusing to start another." >&2
        exit 1
    fi
fi

if [[ -e "$timing_log" || -e "$stdout_log" ]]; then
    echo "Run logs for ${run_name} already exist; refusing to append to them." >&2
    exit 1
fi

# Step 0 is immediate; steps 1 through 28799 follow at a three-second cadence.
# The final snapshot is therefore scheduled at 23:59:57.
nohup python3 -m interf_sim \
    --trc-dir /home/norbert/Software/LAPD/data \
    --period 3 \
    --limit 28800 \
    --repeat-traces \
    --raw-output streamer/conf/bobby_to_nersc_24h_20261001.conf \
    --raw-output-private-key keys/testkey \
    --raw-output-timing-log ${timing_log} \
    --raw-output-buffer-seconds 600 \
    >"$stdout_log" 2>&1 < /dev/null &
producer_pid=$!
printf '%s\n' "$producer_pid" > "$pid_file"

echo "Started ${run_name} as PID ${producer_pid}."
echo "Follow progress with: tail -f ${stdout_log}"
echo "Inspect timing events with: tail -f ${timing_log}"
