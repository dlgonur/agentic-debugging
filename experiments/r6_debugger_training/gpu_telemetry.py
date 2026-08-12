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
import subprocess
import sys
import time
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


def main() -> int:
    parser = argparse.ArgumentParser(description="R6 GPU/VRAM telemetry logger")
    parser.add_argument("--output", type=str, required=True,
                        help="CSV output path")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["wall_ts", "temp_c", "power_w", "util_pct", "clk_graphics_mhz",
              "clk_mem_mhz", "dedicated_vram_mib", "physical_vram_mib",
              "shared_gpu_mib"]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        while True:
            row = _nvidia_row()
            if row:
                row["shared_gpu_mib"] = _shared_gpu_mib()
                writer.writerow(row)
                fh.flush()
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
