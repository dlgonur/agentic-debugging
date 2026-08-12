#!/usr/bin/env python3
"""R6 — GPU/VRAM telemetry logger (1 s cadence) for sustained training runs.

Logs the real Windows-side GPU state to a timestamped CSV: temperature,
power, utilization, graphics/memory clocks, dedicated VRAM usage.  If the
Windows performance counters for shared GPU adapter memory are readable they
are recorded SEPARATELY (labeled shared_gpu_mib); shared-memory numbers are
never fabricated or derived from the torch allocator.

The logger is part of the R6 run evidence (STABLE + PHYSICAL-VRAM-BOUND
policy): a sustained training run must be monitored, and rising shared GPU
memory or dedicated VRAM approaching physical saturation is an abort signal.
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def _nvidia_row() -> dict[str, str]:
    query = (
        "timestamp,temperature.gpu,power.draw,utilization.gpu,"
        "clocks.gr,clocks.mem,memory.used,memory.total"
    )
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    fields = [part.strip() for part in proc.stdout.strip().split(",")]
    keys = ["wall_ts", "temp_c", "power_w", "util_pct", "clk_graphics_mhz",
            "clk_mem_mhz", "dedicated_vram_mib", "physical_vram_mib"]
    if len(fields) != len(keys):
        return {}
    return dict(zip(keys, fields))


def _shared_gpu_mib() -> str:
    """Windows 'GPU Adapter Memory' shared-usage counter, when readable."""
    try:
        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "(Get-Counter '\\GPU Adapter Memory(*)\\Shared Usage' "
                "-ErrorAction SilentlyContinue).CounterSamples | "
                "Measure-Object -Property CookedValue -Sum | "
                "Select-Object -ExpandProperty Sum",
            ],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode == 0:
            value = proc.stdout.strip()
            if value:
                return str(round(float(value) / (1024 * 1024), 1))
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ""


class _SharedGpuSampler:
    """Refresh the slow Windows shared-memory counter off the hot path."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._value = ""
        self._sampled_at = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            value = self._sample_interruptibly()
            sampled_at = time.monotonic()
            with self._lock:
                self._value = value
                self._sampled_at = sampled_at
            self._stop.wait(self._interval)

    def _sample_interruptibly(self) -> str:
        command = [
            "powershell", "-NoProfile", "-Command",
            "(Get-Counter '\\GPU Adapter Memory(*)\\Shared Usage' "
            "-ErrorAction SilentlyContinue).CounterSamples | "
            "Measure-Object -Property CookedValue -Sum | "
            "Select-Object -ExpandProperty Sum",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError:
            return ""
        deadline = time.monotonic() + 20.0
        while process.poll() is None:
            if self._stop.wait(0.25) or time.monotonic() >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                return ""
        if process.returncode != 0:
            return ""
        try:
            assert process.stdout is not None
            value = process.stdout.read().strip()
            return str(round(float(value) / (1024 * 1024), 1)) if value else ""
        except (OSError, ValueError):
            return ""

    def snapshot(self) -> tuple[str, str]:
        with self._lock:
            value = self._value
            sampled_at = self._sampled_at
        if sampled_at <= 0:
            return value, ""
        return value, str(round((time.monotonic() - sampled_at) * 1000, 1))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description="R6 GPU/VRAM telemetry logger")
    parser.add_argument("--output", type=str, required=True,
                        help="CSV output path")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--shared-interval", type=float, default=1.0)
    parser.add_argument(
        "--stop-file",
        type=str,
        default=None,
        help="exit cleanly when this path exists",
    )
    args = parser.parse_args()
    if args.interval <= 0 or args.shared_interval <= 0:
        parser.error("telemetry intervals must be positive")

    path = Path(args.output)
    stop_file = Path(args.stop_file).resolve() if args.stop_file else None
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "sample_started_local", "sample_finished_local", "sample_duration_ms",
        "wall_ts", "temp_c", "power_w", "util_pct", "clk_graphics_mhz",
        "clk_mem_mhz", "dedicated_vram_mib", "physical_vram_mib",
        "shared_gpu_mib", "shared_sample_age_ms",
    ]
    shared = _SharedGpuSampler(args.shared_interval)
    shared.start()
    try:
        with path.open("x", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=header)
            writer.writeheader()
            fh.flush()
            os.fsync(fh.fileno())
            next_sample = time.monotonic()
            while True:
                if stop_file is not None and stop_file.exists():
                    break
                started = time.monotonic()
                started_local = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
                row = _nvidia_row()
                finished_local = datetime.now().astimezone().isoformat(
                    timespec="milliseconds"
                )
                if row:
                    shared_value, shared_age = shared.snapshot()
                    row.update({
                        "sample_started_local": started_local,
                        "sample_finished_local": finished_local,
                        "sample_duration_ms": round(
                            (time.monotonic() - started) * 1000, 1
                        ),
                        "shared_gpu_mib": shared_value,
                        "shared_sample_age_ms": shared_age,
                    })
                    writer.writerow(row)
                    fh.flush()
                    os.fsync(fh.fileno())
                next_sample += args.interval
                delay = max(0.0, next_sample - time.monotonic())
                if stop_file is not None:
                    if stop_file.exists():
                        break
                    time.sleep(min(delay, 0.25))
                    while time.monotonic() < next_sample and not stop_file.exists():
                        time.sleep(
                            max(0.0, min(0.25, next_sample - time.monotonic()))
                        )
                else:
                    time.sleep(delay)
    except KeyboardInterrupt:
        return 130
    finally:
        shared.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
