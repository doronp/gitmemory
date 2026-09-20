#!/usr/bin/env python3
"""E4: Hook shim latency benchmark tool.

Runs the POSIX sh hook shim N times (default 1000) against a realistic
Claude Code PreCompact payload, reports percentiles (p50, p95, p99, max),
and prints the host system and OS details.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import platform
import shutil
import subprocess
import sys
import tempfile
import time

# The cheapest real program there is: the control for process-spawn overhead.
NOTHING = shutil.which("true") or "/usr/bin/true"


def get_machine_info() -> str:
    """Detect and return host OS, architecture, and CPU details."""
    os_name = platform.system()
    os_release = platform.release()
    arch = platform.machine()
    cpu = ""
    if os_name == "Darwin":
        with contextlib.suppress(Exception):
            cpu = (
                subprocess.check_output(
                    ["sysctl", "-n", "machdep.cpu.brand_string"],
                    stderr=subprocess.DEVNULL,
                )
                .decode()
                .strip()
            )
    if not cpu:
        cpu = platform.processor() or "Unknown CPU"
    return f"{os_name} {os_release} ({arch}, {cpu})"


def generate_payload() -> bytes:
    """Generate a realistic ~30KB PreCompact JSON transcript payload."""
    turns = []
    for i in range(15):
        turns.append(
            {
                "type": "user",
                "uuid": f"user-uuid-{i}",
                "sessionId": "session-12345",
                "message": {
                    "role": "user",
                    "content": (
                        f"This is user turn {i}. We are testing the latency of the "
                        "gitmemory-hook.sh shim. It needs to handle realistic payloads, "
                        "which means we should feed it sufficient bytes to simulate a real "
                        "conversation history. "
                    )
                    * 10,
                },
            }
        )
        turns.append(
            {
                "type": "assistant",
                "uuid": f"assistant-uuid-{i}",
                "sessionId": "session-12345",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"This is assistant response {i}. The gitmemory tool is "
                                "capturing this entire transcript to keep a persistent "
                                "record of the conversation. Let's ensure performance is fast! "
                            )
                            * 10,
                        }
                    ],
                },
            }
        )
    return json.dumps(turns).encode("utf-8")


def main() -> None:
    # 1. Parse command line arguments
    count = 1000
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
            if count <= 0:
                raise ValueError
        except ValueError:
            print(f"Error: count must be a positive integer. Got {sys.argv[1]!r}", file=sys.stderr)
            sys.exit(1)

    # 2. Paths
    script_dir = pathlib.Path(__file__).parent.resolve()
    shim_path = (script_dir / "../hook/gitmemory-hook.sh").resolve()
    if not shim_path.exists():
        print(f"Error: shim script not found at {shim_path}", file=sys.stderr)
        sys.exit(1)

    # Use dash if available to match the test environment, otherwise execute directly
    run_cmd = ["dash", str(shim_path)] if shutil.which("dash") else [str(shim_path)]

    # 3. Prepare payload and temporary environment
    payload = generate_payload()
    payload_size_kb = len(payload) / 1024.0

    print(f"Benchmarking {shim_path.name} with {count} iterations...")
    print(f"Payload size: {payload_size_kb:.2f} KB")
    print(f"Machine: {get_machine_info()}")
    print("-" * 50)

    # We use a temporary directory for the benchmark runs
    with tempfile.TemporaryDirectory(prefix="gitmemory_bench_") as tmp_dir:
        env = os.environ.copy()
        env["GITMEMORY_HOME"] = tmp_dir

        # Warm up run to ensure any filesystem/OS caches are populated
        warm = subprocess.run(
            run_cmd + ["PreCompact"],
            env=env,
            input=payload,
            capture_output=True,
        )
        if warm.returncode != 0:
            # A broken shim exits fast, and a fast broken shim benchmarks
            # beautifully. Refusing to report is the only honest option. [E4]
            print(f"Error: the shim exited {warm.returncode} on the warm-up run", file=sys.stderr)
            print(warm.stderr.decode(errors="replace"), file=sys.stderr)
            sys.exit(1)

        # 4. Execute runs and collect latencies
        latencies = []
        baseline = []
        failures = 0
        start_bench = time.perf_counter()

        for _ in range(count):
            t0 = time.perf_counter()
            res = subprocess.run(
                run_cmd + ["PreCompact"],
                env=env,
                input=payload,
                capture_output=True,
            )
            t1 = time.perf_counter()
            failures += res.returncode != 0
            latencies.append((t1 - t0) * 1000.0)

            # The same measurement around a program that does nothing. What we
            # time is Python building an argv, forking, exec'ing, and draining
            # two pipes — and only then the shim. Without this control the
            # number reads as the shim's cost when most of it is the harness's.
            t0 = time.perf_counter()
            subprocess.run([NOTHING], capture_output=True)
            baseline.append((time.perf_counter() - t0) * 1000.0)

        total_bench_time = time.perf_counter() - start_bench

    if failures:
        print(f"Error: {failures}/{count} runs exited non-zero", file=sys.stderr)
        sys.exit(1)

    # 5. Compute statistics
    latencies.sort()
    baseline.sort()

    def pct(values: list[float], q: float) -> float:
        return values[min(int(len(values) * q), len(values) - 1)]

    p50 = pct(latencies, 0.50)
    p95 = pct(latencies, 0.95)
    p99 = pct(latencies, 0.99)
    max_lat = latencies[-1]
    avg_lat = sum(latencies) / len(latencies)
    b50 = pct(baseline, 0.50)

    # 6. Print formatted Markdown table and results
    print("\n### Latency Benchmark Results\n")
    print("| Metric | Shim (ms) | Spawning `true` (ms) |")
    print("| :--- | :--- | :--- |")
    print(f"| p50 | {p50:.2f} | {b50:.2f} |")
    print(f"| p95 | {p95:.2f} | {pct(baseline, 0.95):.2f} |")
    print(f"| p99 | {p99:.2f} | {pct(baseline, 0.99):.2f} |")
    print(f"| Max | {max_lat:.2f} | {baseline[-1]:.2f} |")
    print(f"| Average | {avg_lat:.2f} | {sum(baseline) / len(baseline):.2f} |")
    print(
        f"\nThe shim's own share of p50 is {p50 - b50:.2f} ms; the remaining "
        f"{b50:.2f} ms is what it costs this machine to start any process at all."
    )
    print("\n### Environment\n")
    print(f"- **OS/Platform**: {get_machine_info()}")
    print(f"- **Shell Execution**: {' '.join(run_cmd)}")
    print(f"- **Benchmark Size**: {count} runs")
    print(f"- **Total Time**: {total_bench_time:.2f} seconds")
    print(f"- **Payload**: {payload_size_kb:.2f} KB PreCompact")


if __name__ == "__main__":
    main()
