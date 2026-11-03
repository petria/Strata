"""Write optional Monitor CPU identities from this worker and an SSH coordinator.

Run on the worker, then copy the output to the coordinator's serve/web directory.
This file is deployment metadata, not an engine or model configuration.
"""

import argparse
import json
from pathlib import Path
import subprocess


def cpu_name(cpuinfo):
    for line in cpuinfo.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("model name", "Hardware") and value.strip():
            return value.strip()
    raise ValueError("CPU model not found in /proc/cpuinfo")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-host", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    remote = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "--", args.coordinator_host, "cat /proc/cpuinfo"],
        check=True, capture_output=True, text=True, timeout=20,
    )
    identity = {"coordinator_cpu": cpu_name(remote.stdout),
                "worker_cpu": cpu_name(Path("/proc/cpuinfo").read_text())}
    args.output.write_text("export default " + json.dumps(identity, indent=2) + ";\n")
    print(json.dumps(identity, indent=2))


if __name__ == "__main__":
    main()
